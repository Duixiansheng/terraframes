"""Resume and parallelize independent GeoTIFF jobs under a configurable budget."""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from tempfile import NamedTemporaryFile
from time import perf_counter

import rasterio

from grid_processing import Grid, align
from inventory import Cancelled, bounded_map, paths_in

REVISION = "3.0.0"


def atomic_report(path, result):
    path = Path(path)
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                           suffix=".tmp", delete=False) as stream:
        temporary = Path(stream.name)
    try:
        temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def fingerprint(files):
    records = []
    for name in sorted(set(map(str, files))):
        path = Path(name).resolve()
        stat = path.stat()
        records.append((str(path), stat.st_size, stat.st_mtime_ns))
    return records


def key_for(data):
    canonical = json.dumps(data, ensure_ascii=False, sort_keys=True)
    return sha256(canonical.encode("utf-8")).hexdigest()


def budget(workers, memory_mib):
    if not 1 <= workers <= 32 or not 128 <= memory_mib <= 16384:
        raise ValueError("并行数须为 1–32，工作内存预算须为 128–16384 MiB。")
    cache = min(256, memory_mib // 4)
    actual = min(workers, max(1, (memory_mib - cache) // 96))
    return actual, cache


def execute_batch(folder, reference, destination, *, workers=4, memory_mib=512,
                  resampling="nearest", compress="deflate", window=None, recursive=True,
                  stopped=lambda: False, progress=None):
    actual, cache = budget(workers, memory_mib)
    policy = dict(resampling=resampling, compress=compress)
    started = perf_counter()
    folder, destination = Path(folder).resolve(), Path(destination).resolve()
    if folder == destination:
        raise ValueError("请选择与影像输入目录不同的输出目录。")
    grid = Grid.from_file(reference, window)
    inputs = paths_in(folder, recursive, exclude=destination)
    if not inputs:
        raise ValueError("没有待处理的 GeoTIFF。")
    destination.mkdir(parents=True, exist_ok=True)
    products = destination / "aligned"
    products.mkdir(exist_ok=True)
    connection = sqlite3.connect(destination / "alignment.sqlite3", timeout=30)
    try:
        connection.execute("CREATE TABLE IF NOT EXISTS receipts "
                           "(key TEXT PRIMARY KEY, size INTEGER, mtime INTEGER)")
        connection.commit()
        rows = []

        def plan():
            for source in inputs:
                if stopped():
                    return
                try:
                    with rasterio.open(source) as src:
                        members = src.files
                    snapshot = fingerprint(members)
                    key = key_for(dict(revision=REVISION, fingerprint=snapshot,
                                       grid=grid.signature(), policy=policy))
                    safe_stem = "".join(c if c.isalnum() or c in "-_" else "_" for c in source.stem)[:52]
                    target = products / f"{safe_stem}-{key[:24]}.tif"
                    if target.is_symlink():
                        raise ValueError("输出路径是符号链接。请选择其他输出目录。")
                    receipt = connection.execute("SELECT size, mtime FROM receipts WHERE key=?", (key,)).fetchone()
                    current = target.stat() if target.is_file() else None
                    if current and receipt == (current.st_size, current.st_mtime_ns):
                        rows.append(dict(source=str(source), output=str(target), state="reused"))
                        if progress:
                            progress(len(rows), len(inputs), source.name)
                    else:
                        yield source, snapshot, key, target
                except (OSError, ValueError, rasterio.errors.RasterioError) as error:
                    rows.append(dict(source=str(source), state="failed", error=str(error)))
                    if progress:
                        progress(len(rows), len(inputs), source.name)

        def process(item):
            source, snapshot, _, target = item
            def verify():
                if fingerprint([record[0] for record in snapshot]) != snapshot:
                    raise ValueError("处理过程中源文件或关联文件发生变化。")
            return align(source, target, grid, **policy, warp_memory=32, stopped=stopped,
                         replace=True, verify=verify)

        with rasterio.Env(GDAL_CACHEMAX=cache * 1024**2):
            for item, future in bounded_map(process, plan(), actual, stopped):
                source, _, key, target = item
                try:
                    result = future.result()
                    stat = target.stat()
                    connection.execute("INSERT OR REPLACE INTO receipts VALUES (?, ?, ?)",
                                       (key, stat.st_size, stat.st_mtime_ns))
                    connection.commit()
                    rows.append({**result, "state":"completed"})
                except Cancelled:
                    rows.append(dict(source=str(source), state="cancelled"))
                except Exception as error:
                    rows.append(dict(source=str(source), state="failed", error=str(error)))
                if progress:
                    progress(len(rows), len(inputs), source.name)
    finally:
        connection.close()

    summary = dict(operation="align", workers_requested=workers, workers_used=actual,
                   working_memory_mib=memory_mib, gdal_cache_mib=cache,
                   seconds=perf_counter()-started, files=len(inputs), cancelled=stopped(),
                   jobs=sorted(rows, key=lambda row: row["source"]),
                   reference=str(Path(reference).resolve()), policy=policy, grid=grid.signature())
    atomic_report(destination / "alignment-report.json", summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder")
    parser.add_argument("reference")
    parser.add_argument("output")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--memory-mib", type=int, default=512)
    parser.add_argument("--window", nargs=4, type=int)
    parser.add_argument("--resampling", choices=["nearest", "bilinear", "average"], default="nearest")
    parser.add_argument("--compress", choices=["deflate", "none"], default="deflate")
    args = vars(parser.parse_args())
    result = execute_batch(args.pop("folder"), args.pop("reference"), args.pop("output"), **args)
    print(json.dumps({key:value for key,value in result.items() if key != "jobs"}, ensure_ascii=False))
    return int(any(job["state"] == "failed" for job in result["jobs"]))


if __name__ == "__main__":
    raise SystemExit(main())
