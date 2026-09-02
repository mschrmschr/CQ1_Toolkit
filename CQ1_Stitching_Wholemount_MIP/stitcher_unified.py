# stitcher_unified.py
# Three interchangeable stitch backends, sharing tile-placement/blend helpers
# and a common FIJI-safe OME-TIFF writer:
#  - stitch_grid_classic      (fast, no blending — straight tile paste)
#  - stitch_grid_seamless     (feather blending; robust to typical overlaps)
#  - stitch_grid_tinyoverlap  (special handling for ~0-1% visual overlap)
#
# All three write via tifffile.imwrite(..., ome=True, metadata={"axes": "ZCYX", ...})
# and never pass a hand-built `description=` — that combination is what lets
# tifffile generate real OME-XML from the array layout, which is what Bio-Formats/
# FIJI needs to open the result as a proper multi-channel Z-stack hyperstack. Passing
# a manual `description=` instead would force tifffile's OME writer off and produce a
# generic "shaped" TIFF that Bio-Formats cannot reassemble into a hyperstack.

from __future__ import annotations
import os
import time
from typing import Optional, List, Union, Tuple

import numpy as np
import pandas as pd
from tifffile import imread, imwrite

try:
    from tqdm import tqdm
except Exception:  # pragma: no cover
    def tqdm(iterable=None, total=None, desc=None, unit=None):
        return iterable if iterable is not None else range(total or 0)


# ---------------------------- shared helpers ---------------------------------

def _resolve_tile_path(tile_file, image_root: str) -> str:
    tile_file = str(tile_file).replace("\\", "/")
    if os.path.isabs(tile_file) and os.path.exists(tile_file):
        return tile_file
    tile_file = tile_file.lstrip("/").lstrip("./")
    if image_root.replace("\\", "/").endswith("Image") and tile_file.startswith("Image/"):
        tile_file = tile_file[len("Image/"):]
    return os.path.join(image_root, tile_file)


def _median_step(vals: np.ndarray) -> float:
    u = np.sort(np.unique(vals.astype(float)))
    if u.size < 2:
        return np.nan
    d = np.diff(u)
    d = d[d > 0]
    return float(np.median(d)) if d.size else np.nan


def _raised_cosine(n: int) -> np.ndarray:
    if n <= 0:
        return np.ones((1,), dtype=np.float32)
    x = np.linspace(0, np.pi, n, dtype=np.float32)
    return (1.0 - np.cos(x)) * 0.5


def _feather_mask(h: int, w: int, tx: int, ty: int) -> np.ndarray:
    mask = np.ones((h, w), dtype=np.float32)
    if tx > 0:
        m = min(w, int(tx))
        rx = _raised_cosine(m)
        mask[:, :m] *= rx
        mask[:, -m:] *= rx[::-1]
    if ty > 0:
        m = min(h, int(ty))
        ry = _raised_cosine(m)
        mask[:m, :] *= ry[:, None]
        mask[-m:, :] *= ry[::-1, None]
    return mask


def _blend_into(dest: np.ndarray, src: np.ndarray, x: int, y: int, tx: int, ty: int) -> None:
    H, W = dest.shape
    h, w = src.shape
    x0 = max(int(x), 0)
    y0 = max(int(y), 0)
    x1 = min(x0 + w, W)
    y1 = min(y0 + h, H)
    if x1 <= x0 or y1 <= y0:
        return
    tw, th = (x1 - x0), (y1 - y0)
    src = src[:th, :tw].astype(np.float32)
    mask = _feather_mask(th, tw, tx, ty)
    region = dest[y0:y1, x0:x1].astype(np.float32)
    region = region * (1.0 - mask) + src * mask
    if np.issubdtype(dest.dtype, np.integer):
        region = np.clip(region, 0, np.iinfo(dest.dtype).max)
        dest[y0:y1, x0:x1] = region.astype(dest.dtype)
    else:
        dest[y0:y1, x0:x1] = region


def _channel_matches(cval: Union[int, str], ch_name: Optional[str], allow: Optional[List[Union[int, str]]]) -> bool:
    if allow is None:
        return True
    allow = {str(a) for a in allow}
    return (str(cval) in allow) or (ch_name is not None and str(ch_name) in allow)


def _lattice_snap(vals: np.ndarray, step: float, snap_px: int) -> np.ndarray:
    if not np.isfinite(step) or step <= 0 or snap_px <= 0:
        return vals
    k = np.round(vals / step)
    tgt = k * step
    need = np.abs(tgt - vals) <= snap_px
    out = vals.copy()
    out[need] = tgt[need]
    return out


def _best_shift_1d(a: np.ndarray, b: np.ndarray, max_shift: int) -> int:
    if max_shift <= 0 or a.size < 8 or b.size != a.size:
        return 0
    a = a.astype(np.float32)
    b = b.astype(np.float32)
    a -= a.mean()
    b -= b.mean()
    denom = a.std() * b.std()
    if denom == 0:
        return 0
    a /= denom
    b /= denom
    L = a.size
    best_s, best_r = 0, -1e9
    for s in range(-max_shift, max_shift + 1):
        if s < 0:
            r = np.dot(a[-s:], b[:L + s])
        elif s > 0:
            r = np.dot(a[:L - s], b[s:])
        else:
            r = np.dot(a, b)
        if r > best_r:
            best_r = r
            best_s = s
    return int(best_s)


def _refine_from_overlap(mosaic_region: np.ndarray, tile_region: np.ndarray,
                          ovx: int, ovy: int, max_shift: int,
                          axis: str = "both") -> Tuple[int, int]:
    dx = dy = 0
    H, W = tile_region.shape
    if axis in ("both", "x") and ovx > 0 and max_shift > 0 and W > ovx and mosaic_region.shape[1] > 0:
        left_m = mosaic_region[:, :min(ovx + max_shift, mosaic_region.shape[1])]
        left_t = tile_region[:, :min(ovx + max_shift, tile_region.shape[1])]
        if left_m.size >= 256 and left_t.shape == left_m.shape:
            a = left_m.mean(axis=0)
            b = left_t.mean(axis=0)
            dx = _best_shift_1d(a, b, max_shift)
    if axis in ("both", "y") and ovy > 0 and max_shift > 0 and H > ovy and mosaic_region.shape[0] > 0:
        top_m = mosaic_region[:min(ovy + max_shift, mosaic_region.shape[0]), :]
        top_t = tile_region[:min(ovy + max_shift, tile_region.shape[0]), :]
        if top_m.size >= 256 and top_t.shape == top_m.shape:
            a = top_m.mean(axis=1)
            b = top_t.mean(axis=1)
            dy = _best_shift_1d(a, b, max_shift)
    return dx, dy


def _safe_phys(v, default: float = 1.0) -> float:
    try:
        v = float(v)
        if not np.isfinite(v) or v <= 0:
            return default
        return v
    except Exception:
        return default


def _order_df(df_plane: pd.DataFrame) -> pd.DataFrame:
    if {"RowIndex", "ColIndex"}.issubset(df_plane.columns):
        return df_plane.sort_values(["RowIndex", "ColIndex"])
    if {"StageY_um", "StageX_um"}.issubset(df_plane.columns):
        return df_plane.sort_values(["StageY_um", "StageX_um"], ascending=[False, True])
    return df_plane


def _writer_kwargs(compression: Optional[str], compression_level: int, predictor: bool,
                    tile: Optional[Tuple[int, int]], bigtiff: bool, pyramids: bool) -> dict:
    """Build the shared tifffile.imwrite() IO kwargs used by every backend.

    `pyramids` is accepted for config forward-compatibility but not wired up:
    tifffile's `imwrite()` convenience wrapper (unlike its lower-level
    `TiffWriter`) has no `subifds` parameter in the installed tifffile version,
    and writing real per-level downsampled pyramids needs that lower-level API
    plus explicit downsampling logic - out of scope here. Passing
    `pyramids: true` today is a no-op, same as before this port.
    """
    comp = compression
    if isinstance(comp, str) and comp.lower() in {"none", "null"}:
        comp = None
    compressionargs = {"level": int(compression_level)} if comp is not None else None
    predictor_value = 1 if (predictor and isinstance(comp, str) and comp.lower() in {"zlib", "zstd"}) else False

    kwargs = dict(
        ome=True,
        photometric="minisblack",
        bigtiff=bool(bigtiff),
        compression=comp,
        compressionargs=compressionargs,
        predictor=predictor_value,
    )
    if tile:
        kwargs["tile"] = tuple(tile)
    return kwargs


def _tile_chunks(planes, tile: Optional[Tuple[int, int]], height: int, width: int):
    """Re-chunk a per-plane iterator into the per-tile iterator tifffile's
    `imwrite()`/`TiffWriter.write()` actually require when both `data` is an
    iterator *and* `tile=` is set.

    This is easy to miss: the docstring says "If `tile` is specified,
    iterator items must match the tile shape" -- passing whole (H,W) planes
    instead (as this module briefly did) works fine for small test mosaics
    that happen to be smaller than one tile, but silently breaks on any real
    mosaic spanning multiple tiles: `TiffWriter.write()` computes the total
    chunk count as `tiles_per_plane * num_planes` and pulls exactly that many
    items from the iterator via `next()`, one per *tile*, not one per
    *plane* -- so a plane-per-item generator runs out early and tifffile's
    internal encoder raises `RuntimeError: generator raised StopIteration`
    once every plane has been consumed but more tile-sized chunks are still
    expected. Reproduced directly against installed tifffile (2025.5.10) at
    production scale (3789x3789 mosaic, 512x512 tile, 205 planes) before
    landing this fix.

    Chunks are yielded page-major (matching plane order), then tile-row-major
    within each page -- the same traversal tifffile's own `iter_tiles()` uses
    for a materialized array, verified against it directly. Partial edge
    tiles are yielded un-padded (smaller than tile shape); tifffile's own
    `encode_chunks()` zero-pads them the same way it pads a materialized
    array's edge tiles, so there's no need to pad here too.
    """
    if not tile:
        for plane in planes:
            yield plane
        return
    th, tw = tile
    n_ty = (height + th - 1) // th
    n_tx = (width + tw - 1) // tw
    for plane in planes:
        for ty in range(n_ty):
            y0 = ty * th
            y1 = min(y0 + th, height)
            for tx in range(n_tx):
                x0 = tx * tw
                x1 = min(x0 + tw, width)
                yield plane[y0:y1, x0:x1]


def _mosaic_extent(g: pd.DataFrame, tw: int, th: int, placement: str) -> Tuple[str, str, str, int, int, int, int]:
    """Return (xcol, ycol, mode, x_min, y_min, max_x, max_y) for tile placement."""
    use_grid = (placement == "grid") and {"PixelX_grid", "PixelY_grid"}.issubset(g.columns)
    if use_grid:
        xcol, ycol = "PixelX_grid", "PixelY_grid"
        x_min = y_min = 0
        max_x = int(np.max(g[xcol].values)) + tw
        max_y = int(np.max(g[ycol].values)) + th
        return xcol, ycol, "grid", x_min, y_min, max_x, max_y

    if not {"PixelX_stage", "PixelY_stage"}.issubset(g.columns):
        raise KeyError("stage placement requested but PixelX_stage/PixelY_stage missing.")
    xcol, ycol = "PixelX_stage", "PixelY_stage"
    x_min = int(np.floor(g[xcol].min()))
    y_min = int(np.floor(g[ycol].min()))
    max_x = int(np.ceil((g[xcol] - x_min).max())) + tw
    max_y = int(np.ceil((g[ycol] - y_min).max())) + th
    return xcol, ycol, "stage", x_min, y_min, max_x, max_y


# ---------------------------- classic -----------------------------------------

def stitch_grid_classic(
    df: pd.DataFrame, image_root: str, output_path: str, well_id: str, grid_index: int, *,
    placement: str = "stage", verbose: bool = True,
    compression: Optional[str] = "zlib", compression_level: int = 6, predictor: bool = True,
    tile: Optional[Tuple[int, int]] = (512, 512), bigtiff: bool = True, pyramids: bool = False,
) -> None:
    t0 = time.perf_counter()
    g = df[(df["WellID"] == well_id) & (df["GridIndex"] == grid_index)].copy()
    if g.empty:
        print(f"No data for Well {well_id}, Grid {grid_index}. Skipping.")
        return

    p0 = _resolve_tile_path(g["TiffFile"].iloc[0], image_root)
    img0 = imread(p0)
    th, tw = img0.shape
    dtype = img0.dtype

    psx = _safe_phys(g["PxSizeX_um"].iloc[0])
    psy = _safe_phys(g["PxSizeY_um"].iloc[0])
    psz = _safe_phys(g.get("PxSizeZ_um", pd.Series([1.0])).iloc[0])

    xcol, ycol, mode, x_min, y_min, max_x, max_y = _mosaic_extent(g, tw, th, placement)

    if verbose:
        print(f"[{well_id} G{grid_index}] placement={mode}")
        print(f"[{well_id} G{grid_index}] mosaic {max_x}x{max_y}; tile {tw}x{th}")

    z_slices = sorted(g["Z"].unique())
    channels = sorted(g["C"].unique())
    out_shape = (len(z_slices), len(channels), max_y, max_x)

    total = len(z_slices) * len(channels)
    progress = tqdm(total=total, desc=f"Placing tiles {well_id} G{grid_index}", unit="plane")

    # Stream one (Z,C) plane at a time instead of materializing the whole
    # mosaic in RAM: a full Z*C*H*W array easily exceeds available memory on
    # a large wholemount grid (e.g. 5.09 GiB for a 5x4x8371x16336 uint16
    # mosaic crashed with MemoryError), while each plane only needs to exist
    # long enough to be written out.
    def _planes():
        for z in z_slices:
            for c in channels:
                zc = _order_df(g[(g["Z"] == z) & (g["C"] == c)].copy())
                plane = np.zeros((max_y, max_x), dtype=dtype)
                for _, row in zc.iterrows():
                    pth = _resolve_tile_path(row["TiffFile"], image_root)
                    if not os.path.exists(pth):
                        print(f"[warn] missing: {pth}")
                        continue
                    img = imread(pth)
                    px = int(np.round(float(row[xcol]) - (0 if mode == "grid" else x_min)))
                    py = int(np.round(float(row[ycol]) - (0 if mode == "grid" else y_min)))
                    H, W = plane.shape
                    x0 = max(px, 0); y0 = max(py, 0); x1 = min(px + tw, W); y1 = min(py + th, H)
                    if x1 > x0 and y1 > y0:
                        tx0 = x0 - px; ty0 = y0 - py; tx1 = tx0 + (x1 - x0); ty1 = ty0 + (y1 - y0)
                        plane[y0:y1, x0:x1] = img[ty0:ty1, tx0:tx1]
                progress.update(1)
                yield plane

    try:
        ch_names = list(g.sort_values("C")["ChannelName"].unique())
    except Exception:
        ch_names = [f"C{c}" for c in channels]

    metadata = {"axes": "ZCYX", "PhysicalSizeX": psx, "PhysicalSizeY": psy, "PhysicalSizeZ": psz,
                "Channel": {"Name": ch_names}}
    try:
        imwrite(output_path, _tile_chunks(_planes(), tile, max_y, max_x), shape=out_shape, dtype=dtype, metadata=metadata,
                **_writer_kwargs(compression, compression_level, predictor, tile, bigtiff, pyramids))
    finally:
        progress.close()
    print(f" Saved: {output_path}  Shape: {out_shape} (Z,C,Y,X)  total={time.perf_counter() - t0:.2f}s")


# ---------------------------- seamless -----------------------------------------

def stitch_grid_seamless(
    df: pd.DataFrame,
    image_root: str,
    output_path: str,
    well_id: str,
    grid_index: int,
    *,
    placement: str = "stage",
    verbose: bool = True,
    # writer
    compression: Optional[str] = "zlib",
    compression_level: int = 6,
    predictor: bool = True,
    bigtiff: bool = True,
    tile: Optional[Tuple[int, int]] = (512, 512),
    pyramids: bool = False,
    # overlap
    default_overlap_fraction: float = 0.01,
    min_feather_px: int = 6,
    max_feather_px: int = 32,
    # lattice/refine
    lattice_snap_px: int = 0,
    max_snap_px: int = 0,  # alias accepted; used when lattice_snap_px==0
    snap_axis: str = "both",
    refine_alignment: bool = False,
    refine_max_shift_px: int = 6,
    refine_axis: str = "both",
    # gain matching
    overlap_gain_match: bool = False,
    gain_clip_low: float = 0.92,
    gain_clip_high: float = 1.08,
    gain_match_channels: Optional[List[Union[int, str]]] = None,
) -> None:
    t0 = time.perf_counter()
    g = df[(df["WellID"] == well_id) & (df["GridIndex"] == grid_index)].copy()
    if g.empty:
        print(f"No data for Well {well_id}, Grid {grid_index}. Skipping.")
        return

    p0 = _resolve_tile_path(g["TiffFile"].iloc[0], image_root)
    img0 = imread(p0)
    th, tw = img0.shape
    dtype = img0.dtype

    psx = _safe_phys(g["PxSizeX_um"].iloc[0])
    psy = _safe_phys(g["PxSizeY_um"].iloc[0])
    psz = _safe_phys(g.get("PxSizeZ_um", pd.Series([1.0])).iloc[0])

    xcol, ycol, mode, x_min, y_min, max_x, max_y = _mosaic_extent(g, tw, th, placement)
    use_grid = mode == "grid"

    if {"StepX_px", "StepY_px"}.issubset(g.columns):
        step_x = float(np.nanmedian(g["StepX_px"].values))
        step_y = float(np.nanmedian(g["StepY_px"].values))
    elif use_grid:
        step_x = _median_step(g[xcol].values)
        step_y = _median_step(g[ycol].values)
    else:
        step_x = tw * (1.0 - default_overlap_fraction)
        step_y = th * (1.0 - default_overlap_fraction)

    ovx = max(int(round(tw - step_x)), 0) if np.isfinite(step_x) else int(round(tw * default_overlap_fraction))
    ovy = max(int(round(th - step_y)), 0) if np.isfinite(step_y) else int(round(th * default_overlap_fraction))

    tx = int(np.clip(max(ovx // 4, min_feather_px), min_feather_px, max_feather_px))
    ty = int(np.clip(max(ovy // 4, min_feather_px), min_feather_px, max_feather_px))

    if lattice_snap_px == 0 and max_snap_px:
        lattice_snap_px = int(max_snap_px)  # accept alias from configs
    if lattice_snap_px > 0 and np.isfinite(step_x) and np.isfinite(step_y) and step_x > 0 and step_y > 0:
        if snap_axis in ("both", "x"):
            g[xcol] = _lattice_snap(g[xcol].astype(float).values, step_x, lattice_snap_px)
        if snap_axis in ("both", "y"):
            g[ycol] = _lattice_snap(g[ycol].astype(float).values, step_y, lattice_snap_px)

    if verbose:
        print(f"[{well_id} G{grid_index}] placement={mode}")
        print(f"[{well_id} G{grid_index}] mosaic size: {max_x}x{max_y} px; tile {tw}x{th}")
        print(f"[{well_id} G{grid_index}] step~=({step_x:.2f},{step_y:.2f}) -> overlap~=({ovx},{ovy}); taper=({tx},{ty})")

    z_slices = sorted(g["Z"].unique())
    channels = sorted(g["C"].unique())
    out_shape = (len(z_slices), len(channels), max_y, max_x)

    total = len(z_slices) * len(channels)
    progress = tqdm(total=total, desc=f"Blending planes {well_id} G{grid_index}", unit="plane")

    # Stream one (Z,C) plane at a time instead of materializing the whole
    # mosaic in RAM: a full Z*C*H*W array easily exceeds available memory on
    # a large wholemount grid (e.g. 5.09 GiB for a 5x4x8371x16336 uint16
    # mosaic crashed with MemoryError), while each plane only needs to exist
    # long enough to be written out. Blending only ever reads/writes within
    # the current plane, so this is a pure memory optimization.
    def _planes():
        for z in z_slices:
            for c in channels:
                zc = _order_df(g[(g["Z"] == z) & (g["C"] == c)].copy())
                plane = np.zeros((max_y, max_x), dtype=dtype)
                try:
                    ch_name = zc["ChannelName"].iloc[0]
                except Exception:
                    ch_name = None

                for _, row in zc.iterrows():
                    pth = _resolve_tile_path(row["TiffFile"], image_root)
                    if not os.path.exists(pth):
                        print(f"[warn] missing: {pth}")
                        continue
                    img = imread(pth)

                    px = int(np.round(float(row[xcol]) - (0 if mode == "grid" else x_min)))
                    py = int(np.round(float(row[ycol]) - (0 if mode == "grid" else y_min)))

                    dx = dy = 0
                    if refine_alignment and refine_max_shift_px > 0:
                        H, W = img.shape
                        mos_view = plane[max(py - ovy, 0):py + min(ovy, H), max(px - ovx, 0):px + min(ovx, W)]
                        tile_view = img[:mos_view.shape[0], :mos_view.shape[1]] if mos_view.size else None
                        if tile_view is not None and mos_view.shape == tile_view.shape:
                            dx, dy = _refine_from_overlap(mos_view, tile_view, ovx, ovy, refine_max_shift_px, axis=refine_axis)

                    if overlap_gain_match and _channel_matches(c, ch_name, gain_match_channels):
                        def _q95(a):
                            a = a.astype(np.float32)
                            return float(np.percentile(a, 95.0)) if a.size > 0 else 0.0
                        scales = []
                        if ovx > 0 and px > 0:
                            lx0 = max(px - ovx, 0); lw = min(ovx, img.shape[1], plane.shape[1] - lx0)
                            if lw > 4:
                                A = plane[py:py + img.shape[0], lx0:lx0 + lw]
                                B = img[:, :lw]
                                qA, qB = _q95(A), _q95(B)
                                if qA > 0 and qB > 0:
                                    scales.append(qA / qB)
                        if ovy > 0 and py > 0:
                            ty0 = max(py - ovy, 0); oh = min(ovy, img.shape[0], plane.shape[0] - ty0)
                            if oh > 4:
                                A = plane[ty0:ty0 + oh, px:px + img.shape[1]]
                                B = img[:oh, :]
                                qA, qB = _q95(A), _q95(B)
                                if qA > 0 and qB > 0:
                                    scales.append(qA / qB)
                        if scales:
                            s = float(np.clip(float(np.median(scales)), gain_clip_low, gain_clip_high))
                            img = (img.astype(np.float32) * s).astype(img.dtype)

                    _blend_into(plane, img, px + dx, py + dy, tx, ty)

                progress.update(1)
                yield plane

    try:
        ch_names = list(g.sort_values("C")["ChannelName"].unique())
    except Exception:
        ch_names = [f"C{c}" for c in channels]

    metadata = {"axes": "ZCYX", "PhysicalSizeX": psx, "PhysicalSizeY": psy, "PhysicalSizeZ": psz,
                "Channel": {"Name": ch_names}}
    try:
        imwrite(output_path, _tile_chunks(_planes(), tile, max_y, max_x), shape=out_shape, dtype=dtype, metadata=metadata,
                **_writer_kwargs(compression, compression_level, predictor, tile, bigtiff, pyramids))
    finally:
        progress.close()
    print(f" Saved blended OME-TIFF: {output_path}  Shape: {out_shape} (Z,C,Y,X)  total={time.perf_counter() - t0:.2f}s")


# ---------------------------- tiny overlap --------------------------------------

def stitch_grid_tinyoverlap(
    df: pd.DataFrame, image_root: str, output_path: str,
    well_id: str, grid_index: int, *,
    placement: str = "stage", verbose: bool = True,
    compression: Optional[str] = "zlib", compression_level: int = 6, predictor: bool = True,
    tile: Optional[Tuple[int, int]] = (512, 512), bigtiff: bool = True, pyramids: bool = False,
    default_overlap_fraction: float = 0.01, min_feather_px: int = 6, max_feather_px: int = 24,
    max_snap_px: int = 6,
) -> None:
    t0 = time.perf_counter()
    g = df[(df["WellID"] == well_id) & (df["GridIndex"] == grid_index)].copy()
    if g.empty:
        print(f"No data for Well {well_id}, Grid {grid_index}. Skipping.")
        return

    p0 = _resolve_tile_path(g["TiffFile"].iloc[0], image_root)
    img0 = imread(p0)
    th, tw = img0.shape
    dtype = img0.dtype

    psx = _safe_phys(g["PxSizeX_um"].iloc[0])
    psy = _safe_phys(g["PxSizeY_um"].iloc[0])
    psz = _safe_phys(g.get("PxSizeZ_um", pd.Series([1.0])).iloc[0])

    xcol, ycol, mode, x_min, y_min, max_x, max_y = _mosaic_extent(g, tw, th, placement)
    use_grid = mode == "grid"

    if {"StepX_px", "StepY_px"}.issubset(g.columns):
        step_x = float(np.nanmedian(g["StepX_px"].values))
        step_y = float(np.nanmedian(g["StepY_px"].values))
    elif use_grid:
        step_x = _median_step(g[xcol].values)
        step_y = _median_step(g[ycol].values)
    else:
        step_x = tw * (1.0 - default_overlap_fraction)
        step_y = th * (1.0 - default_overlap_fraction)

    ovx = max(int(round(tw - step_x)), 0) if np.isfinite(step_x) else int(round(tw * default_overlap_fraction))
    ovy = max(int(round(th - step_y)), 0) if np.isfinite(step_y) else int(round(th * default_overlap_fraction))
    tx = int(np.clip(ovx // 2, min_feather_px, max_feather_px))
    ty = int(np.clip(ovy // 2, min_feather_px, max_feather_px))

    if verbose:
        print(f"[{well_id} G{grid_index}] placement={mode}")
        print(f"[{well_id} G{grid_index}] mosaic: {max_x}x{max_y} px; tile {tw}x{th}")
        print(f"[{well_id} G{grid_index}] step~=({step_x:.2f},{step_y:.2f}) px -> overlap~=({ovx},{ovy}) px; feather=({tx},{ty})")

    if mode == "stage":
        g[xcol] = _lattice_snap(g[xcol].astype(float).values, step_x, max_snap_px)
        g[ycol] = _lattice_snap(g[ycol].astype(float).values, step_y, max_snap_px)

    z_slices = sorted(g["Z"].unique())
    channels = sorted(g["C"].unique())
    out_shape = (len(z_slices), len(channels), max_y, max_x)

    total = len(z_slices) * len(channels)
    progress = tqdm(total=total, desc=f"Blending planes {well_id} G{grid_index}", unit="plane")

    # Stream one (Z,C) plane at a time instead of materializing the whole
    # mosaic in RAM: a full Z*C*H*W array easily exceeds available memory on
    # a large wholemount grid (e.g. 5.09 GiB for a 5x4x8371x16336 uint16
    # mosaic crashed with MemoryError), while each plane only needs to exist
    # long enough to be written out.
    def _planes():
        for z in z_slices:
            for c in channels:
                zc = _order_df(g[(g["Z"] == z) & (g["C"] == c)].copy())
                plane = np.zeros((max_y, max_x), dtype=dtype)

                meds = []; paths = []; xs = []; ys = []
                for _, row in zc.iterrows():
                    pth = _resolve_tile_path(row["TiffFile"], image_root)
                    arr = imread(pth)
                    meds.append(float(np.median(arr)))
                    paths.append(pth)
                    px = int(np.round(float(row[xcol]) - (0 if mode == "grid" else x_min)))
                    py = int(np.round(float(row[ycol]) - (0 if mode == "grid" else y_min)))
                    xs.append(px); ys.append(py)

                target = float(np.median(meds)) if meds else 0.0
                for med, pth, px, py in zip(meds, paths, xs, ys):
                    img = imread(pth)
                    gain = 1.0 if med == 0 else (target / float(med))
                    img_corr = (img.astype(np.float32) * float(gain)).astype(img.dtype)
                    _blend_into(plane, img_corr, px, py, tx, ty)

                progress.update(1)
                yield plane

    try:
        ch_names = list(g.sort_values("C")["ChannelName"].unique())
    except Exception:
        ch_names = [f"C{c}" for c in channels]

    metadata = {"axes": "ZCYX", "PhysicalSizeX": psx, "PhysicalSizeY": psy, "PhysicalSizeZ": psz,
                "Channel": {"Name": ch_names}}
    try:
        imwrite(output_path, _tile_chunks(_planes(), tile, max_y, max_x), shape=out_shape, dtype=dtype, metadata=metadata,
                **_writer_kwargs(compression, compression_level, predictor, tile, bigtiff, pyramids))
    finally:
        progress.close()
    print(f" Saved: {output_path}  Shape: {out_shape} (Z,C,Y,X)  total={time.perf_counter() - t0:.2f}s")
