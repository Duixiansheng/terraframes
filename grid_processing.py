"""Write block-tiled GeoTIFFs on a reference pixel grid."""
import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import perf_counter

import numpy as np
import rasterio
from rasterio.enums import ColorInterp, Resampling
from rasterio.vrt import WarpedVRT
from rasterio.windows import Window

from inventory import Cancelled


@dataclass(frozen=True)
class Grid:
    crs: object
    transform: object
    width: int
    height: int

    @classmethod
    def from_file(cls, reference, window=None):
        with rasterio.open(reference) as src:
            require_affine(src)
            grid = cls(src.crs, src.transform, src.width, src.height)
            if window is None:
                return grid
            col, row, width, height = window
            if min(col, row) < 0 or min(width, height) <= 0 or col + width > src.width or row + height > src.height:
                raise ValueError("裁切窗口须在参考图范围内，使用列、行、宽、高。")
            return cls(src.crs, src.window_transform(Window(col, row, width, height)), width, height)

    def signature(self):
        return dict(crs=self.crs.to_wkt(), transform=list(self.transform)[:6],
                    width=self.width, height=self.height)


def require_affine(src):
    if not src.crs or src.transform.is_identity or src.transform.determinant == 0:
        raise ValueError(f"影像缺少可用的仿射定位：{Path(src.name).name}")


def data_bands(src):
    return [index for index, color in enumerate(src.colorinterp, 1) if color != ColorInterp.alpha]


def float_dtype(src, bands):
    types = [np.dtype(src.dtypes[index-1]) for index in bands]
    if any(dtype.kind not in "uif" or (dtype.kind in "ui" and dtype.itemsize > 4) for dtype in types):
        raise ValueError("支持 32 位以内整数或浮点波段。")
    return "float64" if any(dtype.itemsize > 4 or dtype.kind in "ui" and dtype.itemsize == 4
                            for dtype in types) else "float32"


def source_window(src, grid):
    if src.crs != grid.crs:
        return None
    relation = ~src.transform * grid.transform
    matrix = np.abs(np.array([relation.a, relation.b, relation.d, relation.e]) - [1, 0, 0, 1])
    offset = (relation.c, relation.f)
    drift = matrix[[0, 2]] * grid.width + matrix[[1, 3]] * grid.height
    if np.max(drift) > 1e-6 or any(abs(value-round(value)) > 1e-6 for value in offset):
        return None
    col, row = map(round, offset)
    if min(col, row) < 0 or col + grid.width > src.width or row + grid.height > src.height:
        return None
    return Window(col, row, grid.width, grid.height)


@contextmanager
def read_on_grid(src, grid, bands, resampling, dtype, warp_memory):
    aligned = source_window(src, grid)
    if aligned is not None:
        def read(window):
            translated = Window(window.col_off + aligned.col_off, window.row_off + aligned.row_off,
                                window.width, window.height)
            return src.read(bands, window=translated, masked=True, out_dtype=dtype).filled(np.nan)
        yield read, "direct"
        return
    kwargs = dict(crs=grid.crs, transform=grid.transform, width=grid.width, height=grid.height,
                  dtype=dtype, nodata=np.nan, resampling=Resampling[resampling],
                  warp_mem_limit=warp_memory, NUM_THREADS="1", UNIFIED_SRC_NODATA="PARTIAL")
    with WarpedVRT(src, **kwargs) as view:
        alpha = next((i for i, c in enumerate(view.colorinterp, 1) if c == ColorInterp.alpha), None)
        def read(window):
            values = view.read(bands, window=window, masked=True).filled(np.nan)
            if alpha:
                mask = view.read(alpha, window=window)
                values[:, ~np.isfinite(mask) | (mask <= 0)] = np.nan
            return values
        yield read, "warp"


@contextmanager
def raster_output(output, grid, count, dtype, compress, tile=256, replace=False):
    output = Path(output).resolve()
    if output.suffix.lower() not in (".tif", ".tiff"):
        raise ValueError("输出文件须使用 .tif 或 .tiff 扩展名。")
    if output.exists() and not replace:
        raise FileExistsError(output)
    with NamedTemporaryFile(dir=output.parent, prefix=".frames-", suffix=".tif", delete=False) as file:
        temporary = Path(file.name)
    try:
        with rasterio.open(temporary, "w", driver="GTiff", crs=grid.crs,
                           transform=grid.transform, width=grid.width, height=grid.height,
                           count=count, dtype=dtype, nodata=np.nan, tiled=True, blockxsize=tile,
                           blockysize=tile, compress=compress, BIGTIFF="IF_SAFER",
                           NUM_THREADS="1", **({"predictor": 3} if compress != "none" else {})) as dst:
            yield dst
        if replace:
            temporary.replace(output)
        elif os.name == "nt":
            temporary.rename(output)
        else:
            os.link(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)


def align(source, output, grid, resampling="nearest", compress="deflate", warp_memory=32,
          stopped=lambda: False, replace=False, verify=lambda: None):
    source, output = Path(source).resolve(), Path(output).resolve()
    if source == output or output.exists() and source.samefile(output):
        raise ValueError("输出不能覆盖源影像。")
    if resampling not in ("nearest", "bilinear", "average") or compress not in ("none", "deflate"):
        raise ValueError("不支持的重采样或压缩选项。")
    started = perf_counter()
    with rasterio.open(source) as src:
        require_affine(src)
        bands = data_bands(src)
        if not bands:
            raise ValueError("影像没有数据波段。")
        dtype = float_dtype(src, bands)
        tile = 256
        while tile * tile * len(bands) * np.dtype(dtype).itemsize * 4 > 32 * 1024**2:
            tile //= 2
            if tile < 16:
                raise ValueError("波段数过多，超出单任务块内存预算。")
        with read_on_grid(src, grid, bands, resampling, dtype, warp_memory) as (read, route):
            with raster_output(output, grid, len(bands), dtype, compress, tile=tile, replace=replace) as dst:
                dst.scales = tuple(src.scales[i-1] for i in bands)
                dst.offsets = tuple(src.offsets[i-1] for i in bands)
                for index, band in enumerate(bands, 1):
                    dst.set_band_description(index, src.descriptions[band-1] or f"band_{band}")
                    if src.units[band-1]:
                        dst.set_band_unit(index, src.units[band-1])
                dst.colorinterp = tuple(ColorInterp.undefined if src.colorinterp[i-1] == ColorInterp.palette
                                        else src.colorinterp[i-1] for i in bands)
                for _, window in dst.block_windows(1):
                    if stopped():
                        raise Cancelled("对齐已停止。")
                    values = read(window)
                    values[~np.isfinite(values)] = np.nan
                    dst.write(values, window=window)
                verify()
    return dict(source=str(source), output=str(output), route=route, bands=len(bands),
                seconds=perf_counter()-started)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source")
    parser.add_argument("reference")
    parser.add_argument("output")
    parser.add_argument("--window", nargs=4, type=int)
    parser.add_argument("--resampling", choices=["nearest", "bilinear", "average"], default="nearest")
    parser.add_argument("--compress", choices=["none", "deflate"], default="deflate")
    cfg = parser.parse_args()
    print(json.dumps(align(cfg.source, cfg.output, Grid.from_file(cfg.reference, cfg.window),
                           cfg.resampling, cfg.compress), ensure_ascii=False))
