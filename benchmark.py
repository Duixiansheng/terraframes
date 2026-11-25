"""Measure serial and parallel alignment and verify equal pixel output."""
import argparse
import json
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory

import numpy as np
import rasterio

from batch_processing import atomic_report, execute_batch


def verify_equal(left, right):
    a = {row["source"]: row["output"] for row in left["jobs"]}
    b = {row["source"]: row["output"] for row in right["jobs"]}
    if a.keys() != b.keys():
        raise ValueError("两轮的产物集合不同。")
    for source in a:
        with rasterio.open(a[source]) as x, rasterio.open(b[source]) as y:
            if (x.crs, x.transform, x.shape, x.dtypes) != (y.crs, y.transform, y.shape, y.dtypes):
                raise ValueError(f"产物元数据不一致：{source}")
            for _, window in x.block_windows(1):
                if not np.array_equal(x.read(window=window), y.read(window=window), equal_nan=True):
                    raise ValueError(f"产物像元不一致：{source}")


def benchmark(folder, reference, scratch, workers=4, rounds=2, resampling="bilinear"):
    if workers < 2 or rounds < 1:
        raise ValueError("并行数至少为 2，轮数至少为 1。")
    scratch = Path(scratch).resolve()
    scratch.mkdir(parents=True, exist_ok=True)
    records = []
    for iteration in range(rounds):
        with TemporaryDirectory(prefix="frames-benchmark-", dir=scratch) as directory:
            outputs = {}
            for n in ((1, workers) if iteration % 2 == 0 else (workers, 1)):
                out = Path(directory) / str(n)
                run = execute_batch(folder, reference, out, workers=n, resampling=resampling, memory_mib=max(512, n*128))
                if any(job["state"] != "completed" for job in run["jobs"]):
                    raise RuntimeError(str(run["jobs"]))
                outputs[n] = run
            verify_equal(outputs[1], outputs[workers])
            resume = execute_batch(folder, reference, Path(directory) / str(workers),
                    workers=workers, resampling=resampling, memory_mib=max(512, workers*128))
            cached = sum(job["state"] == "reused" for job in resume["jobs"])
            if cached != resume["files"]:
                raise RuntimeError("续跑未复用所有结果。")
            records.append(dict(files=outputs[1]["files"], serial_seconds=outputs[1]["seconds"],
                                parallel_seconds=outputs[workers]["seconds"], resume_seconds=resume["seconds"],
                                workers_used=outputs[workers]["workers_used"], reused=cached))
    serial = median(row["serial_seconds"] for row in records)
    parallel = median(row["parallel_seconds"] for row in records)
    return dict(serial_median_seconds=serial, parallel_median_seconds=parallel,
                speed_ratio=serial/parallel, pixel_comparison="exact_with_nan",
                rounds=records)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder")
    parser.add_argument("reference")
    parser.add_argument("scratch")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    out = benchmark(args.folder, args.reference, args.scratch, args.workers, args.rounds)
    if args.output:
        atomic_report(args.output, out)
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
