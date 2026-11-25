"""Bounded scheduling and a metadata-only GeoTIFF inventory."""
import argparse
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import csv
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import perf_counter

from scene_core import compare, describe, find_tiffs, inspect


class Cancelled(RuntimeError):
    pass


def bounded_map(function, items, workers, stopped=lambda: False):
    if not 1 <= workers <= 32:
        raise ValueError("并行数须在 1 到 32 之间。")
    iterator = iter(items)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending = {}
        exhausted = False
        while pending or not exhausted:
            if stopped():
                for future in pending:
                    future.cancel()
                return
            while len(pending) < workers * 2 and not exhausted:
                try:
                    item = next(iterator)
                except StopIteration:
                    exhausted = True
                    break
                pending[pool.submit(function, item)] = item
            if not pending:
                break
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                item = pending.pop(future)
                yield item, future


def paths_in(folder, recursive=True, exclude=None):
    paths = (path.resolve() for path in find_tiffs(Path(folder), recursive))
    return list(dict.fromkeys(path for path in paths if not exclude or not path.is_relative_to(exclude)))


def inventory(folder, reference, output, workers=4, recursive=True, stopped=lambda: False,
              progress=None, exclude=None):
    started = perf_counter()
    reference = inspect(Path(reference).resolve())
    if reference.error or reference.georef != "仿射地理定位":
        raise ValueError("参考影像缺少可用的仿射定位。")
    paths = paths_in(folder, recursive, exclude=exclude)
    if not paths:
        raise ValueError("目录中没有 GeoTIFF。")
    output = Path(output).resolve()
    if output.suffix.lower() != ".csv" or not output.parent.is_dir():
        raise ValueError("请选择已有目录下的新 CSV 路径。")
    if output.exists():
        raise FileExistsError(output)

    def check(path):
        scene = inspect(path)
        compare(scene, reference)
        return scene

    counts = Counter()
    with NamedTemporaryFile(mode="w", encoding="utf-8-sig", newline="", dir=output.parent,
                            suffix=".tmp", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            columns = ["文件", *describe(reference)]
            writer = csv.DictWriter(stream, fieldnames=columns)
            writer.writeheader()
            for index, (path, future) in enumerate(bounded_map(check, paths, workers, stopped), 1):
                scene = future.result()
                counts[scene.grid] += 1
                writer.writerow({"文件": str(path), **describe(scene)})
                if progress:
                    progress(index, len(paths), path.name)
            if stopped():
                raise Cancelled("盘点已停止。")
            stream.close()
            if os.name == "nt":
                temporary.rename(output)
            else:
                os.link(temporary, output)
        finally:
            stream.close()
            temporary.unlink(missing_ok=True)
    return dict(files=len(paths), workers=workers, seconds=perf_counter()-started,
                counts=dict(counts), output=str(output))


if __name__ == "__main__":
    import json
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder")
    parser.add_argument("reference")
    parser.add_argument("output")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    print(json.dumps(inventory(args.folder, args.reference, args.output, args.workers), ensure_ascii=False))
