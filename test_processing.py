"""Regression cases for raster masks, cache invalidation and temporal reduction."""
import csv
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import numpy as np
import rasterio
from rasterio.transform import Affine, from_origin

from batch_processing import execute_batch
from grid_processing import Grid, align
from inventory import inventory
from temporal import monthly


class ProcessingTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.folder = self.root / "input"
        self.folder.mkdir()
        self.transform = from_origin(510000, 3860000, 10, 10)

    def scene(self, name, values, shift=0, scale=1, offset=0, mask=None):
        values = np.asarray(values, dtype="uint16")
        path = self.folder / name
        with rasterio.open(path, "w", driver="GTiff", width=values.shape[2], height=values.shape[1],
                           count=len(values), dtype="uint16", nodata=0 if mask is None else None,
                           crs="EPSG:32648", transform=self.transform*Affine.translation(shift, 0)) as dst:
            dst.write(values)
            dst.scales = (scale,) * len(values)
            dst.offsets = (offset,) * len(values)
            if mask is not None:
                dst.write_mask(mask)
        return path

    def test_warp_retains_independent_nodata_and_crop_retains_values(self):
        a = np.full((2, 12, 12), 10)
        a[0, 2, 2] = 0
        reference = self.scene("20240101.tif", a)
        source = self.scene("20240102.tif", a, -1)
        out = self.root / "warp.tif"
        self.assertEqual(align(source, out, Grid.from_file(reference))["route"], "warp")
        with rasterio.open(out) as ds:
            values = ds.read()
            self.assertTrue(np.isnan(values[0, 2, 1]))
            self.assertEqual(values[1, 2, 1], 10)
            self.assertTrue(np.isnan(values[:, :, -1]).all())
        mask = np.full(a.shape[1:], 255, dtype=np.uint8)
        mask[3,3] = 0
        masked = self.scene("20240103.tif", a, -1, mask=mask)
        out = self.root / "mask.tif"
        align(masked, out, Grid.from_file(reference))
        with rasterio.open(out) as ds:
            self.assertTrue(np.isnan(ds.read()[:,3,2]).all())
        out = self.root / "crop.tif"
        self.assertEqual(align(reference, out, Grid.from_file(reference, (3, 3, 6, 6)))["route"], "direct")
        with rasterio.open(out) as ds:
            np.testing.assert_array_equal(ds.read(), a[:,3:9,3:9])

    def test_inventory_parallel_resume_and_recompute_missing_output(self):
        a = np.ones((1, 12, 12))
        reference = self.scene("20240101.tif", a)
        self.scene("20240102.tif", a, .5)
        csv_path = self.root / "report.csv"
        report = inventory(self.folder, reference, csv_path, workers=2)
        self.assertEqual(report["counts"], {"格线对齐":1, "格线错位":1})
        with csv_path.open(encoding="utf-8-sig") as stream:
            self.assertEqual(len(list(csv.DictReader(stream))), 2)
        dest = self.root / "aligned"
        initial = execute_batch(self.folder, reference, dest, workers=8, memory_mib=256)
        self.assertEqual(initial["workers_used"], 2)
        self.assertEqual([row["state"] for row in initial["jobs"]], ["completed"]*2)
        again = execute_batch(self.folder, reference, dest)
        self.assertEqual([row["state"] for row in again["jobs"]], ["reused"]*2)
        Path(again["jobs"][0]["output"]).unlink()
        retried = execute_batch(self.folder, reference, dest)
        self.assertEqual([row["state"] for row in retried["jobs"]], ["completed","reused"])

    def test_monthly_mean_and_observation_count_with_scaled_values(self):
        reference = None
        for n in range(1, 19):
            data = np.full((1, 10, 10),n)
            data[0,0,0] = 0
            if n%2:
                data[0,1,1] = 0
            source = self.scene(f"202401{n:02d}.tif",data,scale=2,offset=1)
            reference = reference or source
        report = monthly(self.folder, reference, self.root / "monthly")
        self.assertEqual(report["jobs"][0]["state"], "completed", report)
        with rasterio.open(report["jobs"][0]["output"]) as ds:
            array = ds.read()
        self.assertTrue(np.isnan(array[0,0,0]))
        self.assertEqual(array[1,0,0], 0)
        self.assertEqual(tuple(array[:,1,1]), (21,9))
        self.assertEqual(tuple(array[:,2,2]), (20,18))


if __name__ == "__main__":
    unittest.main()
