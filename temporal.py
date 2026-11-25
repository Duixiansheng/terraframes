"""Compute tiled monthly means and valid-observation counts without stacking scenes."""
import argparse
from collections import defaultdict
from contextlib import ExitStack
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

import numpy as np
import rasterio

from batch_processing import atomic_report, budget
from grid_processing import Grid, data_bands, float_dtype, raster_output, read_on_grid, require_affine
from inventory import Cancelled, bounded_map, paths_in
from scene_core import capture_date

FAN_IN = 16


def partition(items, limit=FAN_IN):
    for start in range(0, len(items), limit):
        yield items[start:start+limit]


def composite_group(paths, output, grid, band=1, resampling="nearest",
                    stopped=lambda: False):
    if band < 1 or resampling not in ("nearest", "bilinear", "average"):
        raise ValueError("波段编号从 1 开始，重采样须为 nearest、bilinear 或 average。")
    if not paths:
        raise ValueError("合成至少需要一张影像。")
    if output.exists():
        raise FileExistsError(f"月度产物已存在：{output.name}。请换一个输出目录。")
    unit = None
    with TemporaryDirectory(prefix=".frames-reduce-", dir=output.parent) as folder:
        temporary = Path(folder)
        levels = [[]]

        def accumulate(path, index):
            level = 0
            while True:
                if level == len(levels):
                    levels.append([])
                bucket = levels[level]
                bucket.append(path)
                if len(bucket) < FAN_IN:
                    return
                path = temporary / f"carry-{level}-{index}.tif"
                merge_partials(bucket, path, grid, False, unit, stopped)
                for consumed in bucket:
                    consumed.unlink()
                bucket.clear()
                level += 1

        for index, subset in enumerate(partition(paths)):
            if stopped():
                raise Cancelled("合成已停止。")
            final = len(paths) <= FAN_IN
            partial = output if final else temporary / f"inputs-{index}.tif"
            with ExitStack() as stack:
                readers = []
                for source in subset:
                    src = stack.enter_context(rasterio.open(source))
                    require_affine(src)
                    if band not in data_bands(src):
                        raise ValueError(f"{source.name} 的波段 {band} 不可用于合成。")
                    float_dtype(src, [band])
                    band_unit = src.units[band-1] or ""
                    if unit is not None and unit != band_unit:
                        raise ValueError(f"{source.name} 的波段单位与同组其他影像不同。")
                    unit = band_unit
                    read, _ = stack.enter_context(read_on_grid(src, grid, [band], resampling, "float64", 24))
                    readers.append((read, src.scales[band-1], src.offsets[band-1]))
                with raster_output(partial, grid, 2, "float64", "deflate" if final else "none") as dst:
                    if final:
                        describe_product(dst, unit)
                    for _, window in dst.block_windows(1):
                        if stopped():
                            raise Cancelled("合成已停止。")
                        shape = (int(window.height), int(window.width))
                        total, count = np.zeros(shape), np.zeros(shape)
                        for read, scale, offset in readers:
                            data = read(window)[0] * scale + offset
                            valid = np.isfinite(data)
                            np.add(total, data, out=total, where=valid)
                            count += valid
                        if final:
                            total = mean_valid(total, count)
                        dst.write(total, 1, window=window)
                        dst.write(count, 2, window=window)
            if final:
                return
            accumulate(partial, index)

        partials = [path for bucket in levels for path in bucket]
        level = 0
        while len(partials) > FAN_IN:
            merged = []
            for index, subset in enumerate(partition(partials)):
                path = temporary / f"level-{level}-{index}.tif"
                merge_partials(subset, path, grid, False, unit, stopped)
                merged.append(path)
                for consumed in subset:
                    consumed.unlink()
            partials = merged
            level += 1
        merge_partials(partials, output, grid, True, unit, stopped)


def describe_product(dst, unit):
    dst.set_band_description(1, "mean")
    dst.set_band_description(2, "valid_observations")
    if unit:
        dst.set_band_unit(1, unit)


def mean_valid(total, count):
    return np.divide(total, count, out=np.full_like(total, np.nan), where=count > 0)


def merge_partials(paths, output, grid, final, unit, stopped):
    with ExitStack() as stack:
        sources = [stack.enter_context(rasterio.open(path)) for path in paths]
        with raster_output(output, grid, 2, "float64", "deflate" if final else "none") as dst:
            if final:
                describe_product(dst, unit)
            for _, window in dst.block_windows(1):
                if stopped():
                    raise Cancelled("合成已停止。")
                result = np.zeros((2, int(window.height), int(window.width)))
                for src in sources:
                    result += src.read(window=window)
                if final:
                    result[0] = mean_valid(result[0], result[1])
                dst.write(result, window=window)


def monthly(folder, reference, destination, *, band=1, workers=2, memory_mib=512,
            resampling="nearest", window=None, recursive=True, stopped=lambda: False, progress=None):
    started = perf_counter()
    actual, cache = budget(workers, memory_mib)
    folder, destination = Path(folder).resolve(), Path(destination).resolve()
    if folder == destination:
        raise ValueError("请选择与影像输入目录不同的输出目录。")
    grid = Grid.from_file(reference, window)
    paths = paths_in(folder, recursive, exclude=destination)
    groups, omitted = defaultdict(list), []
    for source in paths:
        if stopped():
            break
        try:
            with rasterio.open(source) as src:
                date, _ = capture_date(source, src.tags())
            if date:
                groups[date[:7]].append(source)
            else:
                omitted.append(dict(source=str(source), error="未识别影像日期"))
        except (OSError, ValueError) as error:
            omitted.append(dict(source=str(source), error=str(error)))
    if not groups and not stopped():
        raise ValueError("未发现带可识别日期的 GeoTIFF。")
    destination.mkdir(parents=True, exist_ok=True)

    def process(month):
        count = len(groups[month])
        output = destination / f"{month}-band-{band}-mean-count.tif"
        composite_group(groups[month], output, grid, band, resampling, stopped)
        return dict(month=month, scenes=count, output=str(output), state="completed")

    rows = []
    with rasterio.Env(GDAL_CACHEMAX=cache * 1024**2):
        for month, future in bounded_map(process, sorted(groups), actual, stopped):
            try:
                rows.append(future.result())
            except Cancelled:
                rows.append(dict(month=month, state="cancelled"))
            except Exception as error:
                rows.append(dict(month=month, state="failed", error=str(error)))
            if progress:
                progress(len(rows), len(groups), month)
    summary = dict(operation="monthly", workers_used=actual, seconds=perf_counter()-started,
                   grid=grid.signature(), source_band=band, omitted=omitted, files=len(paths),
                   cancelled=stopped(), jobs=sorted(rows, key=lambda item: item["month"]))
    atomic_report(destination / "monthly-report.json", summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder")
    parser.add_argument("reference")
    parser.add_argument("output")
    parser.add_argument("--band", type=int, default=1)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--memory-mib", type=int, default=512)
    parser.add_argument("--window", nargs=4, type=int)
    parser.add_argument("--resampling", choices=["nearest", "bilinear", "average"], default="nearest")
    args = vars(parser.parse_args())
    result = monthly(args.pop("folder"), args.pop("reference"), args.pop("output"), **args)
    print(json.dumps({key:value for key,value in result.items() if key not in ("jobs","omitted")},
                     ensure_ascii=False))
    return int(any(job["state"] == "failed" for job in result["jobs"]))


if __name__ == "__main__":
    raise SystemExit(main())
