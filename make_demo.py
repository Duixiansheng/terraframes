"""Generate the small synthetic GeoTIFF series bundled with TerraFrames."""
from pathlib import Path
import sys

import numpy as np
import rasterio
from rasterio.enums import ColorInterp
from rasterio.transform import Affine, from_origin
from rasterio.warp import calculate_default_transform, reproject


def write(folder, day, name, data, transform, crs="EPSG:32648"):
    path = folder / f"{day}_{name}.tif"
    with rasterio.open(path, "w", driver="GTiff", width=data.shape[2],
                       height=data.shape[1], count=3, dtype="uint8", crs=crs,
                       transform=transform, nodata=0, compress="deflate") as dst:
        dst.write(data)
        dst.colorinterp = (ColorInterp.red, ColorInterp.green, ColorInterp.blue)


def generate(folder):
    folder.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(42)
    y, x = np.mgrid[:360, :480]
    river = np.abs(y - (170 + 45*np.sin(x/78) + x*.11)) < 13
    terrain = np.sin(x/17 + y/29) * 12 + np.cos(y/13 - x/31) * 10
    crops = np.where((x//35 + 2*(y//25)) % 5 < 2, 25, -12)
    noise = rng.normal(0, 6, x.shape)
    pixels = np.clip(np.stack([
        np.where(river, 42, 86 + crops + terrain + noise),
        np.where(river, 101, 123 + crops + terrain + noise),
        np.where(river, 133, 66 + terrain + noise),
    ]), 1, 255).astype("uint8")
    grid = from_origin(510000, 3860000, 10, 10)
    write(folder, "20240101", "reference", pixels, grid)
    write(folder, "20240201", "half_pixel_grid", pixels, grid * Affine.translation(.5, 0))
    write(folder, "20240301", "cropped_aligned", pixels[:, :, 60:], grid * Affine.translation(60, 0))
    shifted = np.zeros_like(pixels)
    shifted[:, 3:, 4:] = pixels[:, :-3, :-4]
    write(folder, "20240401", "shifted_content", shifted, grid)
    masked = pixels.copy()
    masked[:, 110:240, 45:215] = 0
    write(folder, "20240501", "nodata_gap", masked, grid)
    write(folder, "20240601", "coarser_grid", pixels[:, ::2, ::2], grid * Affine.scale(2))
    bounds = rasterio.transform.array_bounds(360, 480, grid)
    transform, width, height = calculate_default_transform(
        "EPSG:32648", "EPSG:4326", 480, 360, *bounds)
    reprojected = np.zeros((3, height, width), dtype="uint8")
    reproject(pixels, reprojected, src_transform=grid, src_crs="EPSG:32648",
              dst_transform=transform, dst_crs="EPSG:4326", src_nodata=0, dst_nodata=0)
    write(folder, "20240701", "different_crs", reprojected, transform, "EPSG:4326")


if __name__ == "__main__":
    generate(Path(sys.argv[1]))
