"""Read-only GeoTIFF checks, common-window previews and translation estimates."""
import csv
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
import re

import numpy as np
from PIL import Image
import rasterio
from rasterio.enums import ColorInterp, Resampling
from rasterio.transform import Affine
from rasterio.vrt import WarpedVRT
from rasterio.warp import transform_bounds
from rasterio.windows import Window


PIXEL_TOLERANCE = 1e-5
LINEAR_TOLERANCE = 1e-8
BACKGROUND = (25, 36, 43)


@dataclass
class Scene:
    path: Path
    date: str = ""
    date_source: str = ""
    crs: object = None
    affine: object = None
    width: int = 0
    height: int = 0
    bounds: tuple = ()
    bands: int = 0
    dtypes: tuple = ()
    nodata: tuple = ()
    scales: tuple = ()
    offsets: tuple = ()
    resolution: tuple = ()
    georef: str = ""
    error: str = ""
    findings: list = field(default_factory=list)
    grid: str = ""
    footprint: str = ""
    coverage: float | None = None
    grid_offset: tuple | None = None
    residual: tuple | None = None
    registration: str = "未检查"
    valid_preview: float | None = None


def find_tiffs(folder, recursive=False):
    candidates = folder.rglob("*") if recursive else folder.iterdir()
    return sorted((p for p in candidates if p.suffix.lower() in (".tif", ".tiff")
                   and p.is_file()), key=lambda p: str(p).casefold())


def capture_date(path, tags):
    for source, value in [
        ("影像标签", tags.get("ACQUISITION_DATE", "")),
        ("影像标签", tags.get("DATE_ACQUIRED", "")),
        ("影像标签", tags.get("SENSING_TIME", "")),
        ("文件名", path.stem),
    ]:
        match = re.search(r"(?<!\d)(19\d{2}|20\d{2})[-_:]?(\d{2})[-_:]?(\d{2})(?!\d)", value)
        if match:
            try:
                return date(*map(int, match.groups())).isoformat(), source
            except ValueError:
                continue
    return "", ""


def inspect(path):
    scene = Scene(path)
    try:
        with rasterio.open(path) as src:
            scene.date, scene.date_source = capture_date(path, src.tags())
            scene.crs, scene.affine = src.crs, src.transform
            scene.width, scene.height, scene.bands = src.width, src.height, src.count
            scene.bounds = tuple(src.bounds)
            scene.dtypes, scene.nodata = src.dtypes, src.nodatavals
            scene.scales, scene.offsets = src.scales, src.offsets
            scene.resolution = (float(np.hypot(src.transform.a, src.transform.d)),
                                float(np.hypot(src.transform.b, src.transform.e)))
            if not src.count:
                scene.error = "没有可读取的栅格波段"
            elif src.crs and src.transform != Affine.identity() and src.transform.determinant != 0:
                scene.georef = "仿射地理定位"
            elif src.gcps[0] or src.rpcs or src.tags(ns="GEOLOCATION"):
                scene.georef = "非仿射定位"
                scene.findings.append("使用 GCP、RPC 或定位数组，当前版本无法核对格网")
            else:
                scene.georef = "定位信息不足"
                scene.findings.append("缺少可用的坐标系或地理变换")
            if not scene.date:
                scene.findings.append("未识别影像日期")
    except (rasterio.errors.RasterioError, OSError, ValueError) as exc:
        scene.error = str(exc)
    return scene


def bounds_coverage(scene, reference):
    bounds = scene.bounds if scene.crs == reference.crs else transform_bounds(
        scene.crs, reference.crs, *scene.bounds, densify_pts=21)
    left, bottom, right, top = reference.bounds
    x1, y1, x2, y2 = bounds
    width = max(0, min(right, x2) - max(left, x1))
    height = max(0, min(top, y2) - max(bottom, y1))
    area = (right - left) * (top - bottom)
    if area <= 0 or not np.isfinite(bounds).all():
        return None
    return min(1.0, width * height / area)


def compare(scene, reference):
    if scene.error:
        scene.grid = "读取失败"
        return
    if scene.georef != "仿射地理定位" or reference.georef != "仿射地理定位":
        scene.grid = "无法核对"
        return
    if (scene.width, scene.height) != (reference.width, reference.height):
        scene.findings.append("行列数与参考不同")
    if scene.bands != reference.bands:
        scene.findings.append("波段数与参考不同")
    if scene.dtypes != reference.dtypes:
        scene.findings.append("像元数据类型与参考不同")
    if tuple(map(str, scene.nodata)) != tuple(map(str, reference.nodata)):
        scene.findings.append("NoData 声明与参考不同")
    if (scene.scales, scene.offsets) != (reference.scales, reference.offsets):
        scene.findings.append("比例因子或数值偏移与参考不同")
    if scene.crs != reference.crs:
        scene.grid = "坐标系不同"
    else:
        relation = ~reference.affine * scene.affine
        offset = (relation.c, relation.f)
        scene.grid_offset = offset
        matrix = (relation.a, relation.b, relation.d, relation.e)
        basis_error = np.abs(np.asarray(matrix) - [1, 0, 0, 1])
        drift = basis_error[[0, 2]] * scene.width + basis_error[[1, 3]] * scene.height
        same_basis = np.all(basis_error <= LINEAR_TOLERANCE) and np.all(drift <= PIXEL_TOLERANCE)
        if not same_basis:
            different_res = not np.allclose(scene.resolution, reference.resolution,
                                             rtol=LINEAR_TOLERANCE, atol=0)
            scene.grid = "分辨率不同" if different_res else "像元方向或变换不同"
        elif any(abs(value - round(value)) > PIXEL_TOLERANCE for value in offset):
            scene.grid = "格线错位"
        else:
            scene.grid = "格线对齐"
            x, y = map(round, offset)
            if x == y == 0 and (scene.width, scene.height) == (reference.width, reference.height):
                scene.footprint = "范围一致"
            else:
                overlap_x = max(0, min(x + scene.width, reference.width) - max(0, x))
                overlap_y = max(0, min(y + scene.height, reference.height) - max(0, y))
                scene.coverage = overlap_x * overlap_y / (reference.width * reference.height)
                scene.footprint = ("无重叠" if scene.coverage == 0 else
                                   "覆盖参考范围" if scene.coverage == 1 else "部分覆盖")
    if scene.footprint == "范围一致":
        scene.coverage = 1.0
    elif scene.coverage is None:
        try:
            scene.coverage = bounds_coverage(scene, reference)
        except (ValueError, rasterio.errors.RasterioError):
            scene.findings.append("无法比较覆盖范围")
        if scene.coverage is not None:
            scene.footprint = ("包络无重叠" if scene.coverage == 0 else
                               "包络覆盖参考范围" if scene.coverage == 1 else "包络部分覆盖")


def band_selection(src):
    colors = src.colorinterp
    if all(color in colors for color in (ColorInterp.red, ColorInterp.green, ColorInterp.blue)):
        return [colors.index(color) + 1 for color in (ColorInterp.red, ColorInterp.green, ColorInterp.blue)]
    return [next((i for i, color in enumerate(colors, 1) if color != ColorInterp.alpha), 1)]


def display_shape(width, height, max_side):
    factor = max_side / max(width, height)
    return max(1, round(height * factor)), max(1, round(width * factor))


def make_view(src, reference, height, width):
    affine = reference.affine * Affine.scale(reference.width / width, reference.height / height)
    return WarpedVRT(src, crs=reference.crs, transform=affine,
                     height=height, width=width, warp_mem_limit=24,
                     resampling=Resampling.nearest,
                     add_alpha=ColorInterp.alpha not in src.colorinterp)


def preview(scene, reference, max_side=260):
    if scene.error:
        return None
    with rasterio.open(scene.path) as src:
        indices = band_selection(src)
        true_color_byte = src.dtypes[indices[0] - 1] == "uint8" and len(indices) == 3
        common = scene.georef == reference.georef == "仿射地理定位"
        if common:
            height, width = display_shape(reference.width, reference.height, max_side)
            with make_view(src, reference, height, width) as view:
                samples = view.read(indices, masked=True).astype(np.float32).filled(np.nan)
                valid = view.dataset_mask() > 0
        else:
            height, width = display_shape(src.width, src.height, max_side)
            samples = src.read(indices, out_shape=(len(indices), height, width),
                               masked=True).astype(np.float32).filled(np.nan)
            valid = src.dataset_mask(out_shape=(height, width)) > 0
    valid &= np.isfinite(samples).all(axis=0)
    scene.valid_preview = float(np.mean(valid))
    rgb = np.full((height, width, 3), BACKGROUND, dtype=np.uint8)
    if valid.any():
        for i in range(3):
            values = samples[min(i, len(samples) - 1)][valid]
            if true_color_byte:
                rgb[..., i][valid] = np.clip(values, 0, 255).astype(np.uint8)
            else:
                lo, hi = np.percentile(values, (2, 98))
                normalized = np.clip((values - lo) / (hi - lo), 0, 1) if hi > lo else .5
                rgb[..., i][valid] = np.round(255 * np.asarray(normalized)).astype(np.uint8)
    return Image.fromarray(rgb)


def sample_windows(reference):
    side = min(144, reference.width, reference.height)
    if side < 40:
        return []
    windows = []
    for x, y in ((.5, .5), (.27, .27), (.73, .73), (.27, .73), (.73, .27)):
        col = min(reference.width - side, max(0, round(reference.width * x - side / 2)))
        row = min(reference.height - side, max(0, round(reference.height * y - side / 2)))
        window = Window(col, row, side, side)
        if window not in windows:
            windows.append(window)
    return windows


def read_patches(scene, reference, windows):
    with rasterio.open(scene.path) as src:
        bands = band_selection(src)
        band = bands[1] if len(bands) == 3 else bands[0]
        with make_view(src, reference, reference.height, reference.width) as view:
            for window in windows:
                patch = view.read(band, window=window, masked=True).astype("float32").filled(np.nan)
                patch[view.dataset_mask(window=window) == 0] = np.nan
                yield patch


def standardize(patch, valid):
    values = patch[valid]
    lo, hi = np.percentile(values, (2, 98))
    if not hi > lo:
        return None
    clipped = np.clip(patch, lo, hi)
    centered = np.where(valid, clipped - float(clipped[valid].mean()), 0)
    scale = np.std(centered[valid])
    return centered / scale if scale > 0 else None


def shift_patch(reference, scene):
    valid = np.isfinite(reference) & np.isfinite(scene)
    if np.mean(valid) < .75:
        return None
    a, b = standardize(reference, valid), standardize(scene, valid)
    if a is None or b is None:
        return None
    taper = np.outer(np.hanning(a.shape[0]), np.hanning(a.shape[1]))
    cross = np.fft.fft2(a * taper) * np.conjugate(np.fft.fft2(b * taper))
    peak_map = np.fft.fftshift(np.fft.ifft2(
        cross / np.maximum(np.abs(cross), 1e-10)).real)
    cy, cx = (size // 2 for size in a.shape)
    radius = min(18, cy // 2, cx // 2)
    region = peak_map[cy-radius:cy+radius+1, cx-radius:cx+radius+1]
    py, px = np.unravel_index(np.argmax(region), region.shape)
    if min(py, px) == 0 or max(py, px) == 2 * radius:
        return None
    dy, dx = int(py - radius), int(px - radius)
    if region[py, px] < .08:
        return None
    ay = slice(max(dy, 0), a.shape[0] + min(dy, 0))
    ax = slice(max(dx, 0), a.shape[1] + min(dx, 0))
    by = slice(max(-dy, 0), a.shape[0] + min(-dy, 0))
    bx = slice(max(-dx, 0), a.shape[1] + min(-dx, 0))
    matched = valid[ay, ax] & valid[by, bx]
    if np.mean(matched) < .65:
        return None
    matched_a, matched_b = a[ay, ax][matched], b[by, bx][matched]
    if min(np.std(matched_a), np.std(matched_b)) <= 0:
        return None
    score = np.corrcoef(matched_a, matched_b)[0, 1]
    return (dx, dy) if np.isfinite(score) and score >= .7 else None


def estimate_registration(scene, reference, windows, patches):
    if scene.path == reference.path:
        scene.registration = "参考影像"
    elif scene.grid != "格线对齐":
        scene.registration = "格网不同，无法确认"
    elif not windows or scene.coverage == 0:
        scene.registration = "无法确认"
    else:
        votes = [shift_patch(a, b) for a, b in zip(patches, read_patches(scene, reference, windows))]
        shifts = [vote for vote in votes if vote is not None]
        if len(shifts) >= 2:
            center = np.median(shifts, axis=0)
            agreed = np.sum(np.max(np.abs(np.asarray(shifts) - center), axis=1) <= 1)
            if agreed >= max(2, (len(shifts) + 1) // 2) and (np.max(abs(center)) > 0 or
                    np.count_nonzero(np.any(np.asarray(shifts) != 0, axis=1)) == 0):
                scene.residual = tuple(float(x) for x in center)
                scene.registration = "疑似内容平移" if max(abs(x) for x in center) else "抽样未检出平移"
                return
        scene.registration = "无法确认"


def describe(scene):
    def pair(values, suffix=""):
        return " × ".join(f"{x:.6g}" for x in values) + suffix if values else "—"

    def percent(value):
        return f"{value:.1%}" if value is not None else "—"

    return {
        "日期": scene.date or "未识别",
        "日期来源": scene.date_source or "—",
        "坐标系": str(scene.crs or "—"),
        "地理定位": scene.georef or "—",
        "像元列 × 行": f"{scene.width} × {scene.height}" if scene.width else "—",
        "像元总数": f"{scene.width * scene.height:,}" if scene.width else "—",
        "像元大小": pair(scene.resolution, "（坐标系单位）"),
        "波段": str(scene.bands),
        "数据类型": ", ".join(scene.dtypes),
        "NoData": ", ".join(str(x) if x is not None else "未声明" for x in scene.nodata),
        "格网": scene.grid,
        "参考像元起点偏移（列、行）": pair(scene.grid_offset),
        "范围": scene.footprint or "—",
        "参考范围覆盖": percent(scene.coverage),
        "有效预览比例（抽样）": percent(scene.valid_preview),
        "配准抽查": scene.registration,
        "估计校正偏移（参考像元列、行）": pair(scene.residual),
        "补充说明": "；".join(scene.findings) or "—",
        "读取错误": scene.error or "—",
    }


def export_csv(filename, scenes, reference, selected):
    first = describe(scenes[0])
    fields = ["文件", "参考影像", "已选片", *first]
    with open(filename, "w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for scene in scenes:
            writer.writerow({"文件": str(scene.path), "参考影像": str(reference.path),
                             "已选片": "是" if scene.path in selected else "否", **describe(scene)})
