# mip_plotter.py
"""Read stitched CQ1 OME-TIFF wholemounts and write a colorized
per-channel max-intensity-projection panel PNG per file, for a
quick-look overview.

The panel PNG tints each channel with a color (either from the job's
"channel_colors" or a built-in default palette -- there's no GUI field
for this, colors just always apply, same as FieldToStacks' equivalent).
"""

from __future__ import annotations
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import tifffile
import matplotlib
matplotlib.use("Agg")  # headless: no interactive backend, keeps the frozen
                        # exe from pulling in a full Qt/PySide6 toolkit
import matplotlib.pyplot as plt


# --------------------------
# Reader helpers
# --------------------------

def _reorder_to_ZCYX(arr: np.ndarray, axes: str) -> np.ndarray:
    """Reorder array according to its tifffile axes string into Z,C,Y,X order.
    Handles a singleton/elided T or Z gracefully - tifffile drops any axis of
    length 1 from the axes string on write (e.g. a stack stitched with
    only_z restricted to one plane comes back as "CYX", not "ZCYX")."""
    axes = axes.upper()
    dims = list(axes)
    shape = list(arr.shape)

    if "T" in dims:
        if len(shape) == len(dims):
            t_idx = dims.index("T")
            if shape[t_idx] != 1:
                raise ValueError(f"T dimension >1 not supported (axes={axes}, shape={shape})")
            arr = np.take(arr, indices=0, axis=t_idx)
            dims.pop(t_idx)
            shape.pop(t_idx)
        else:
            dims.remove("T")

    if "Z" not in dims:
        arr = arr[np.newaxis, ...]
        dims = ["Z"] + dims
        shape = [1] + shape

    for r in ("Z", "C", "Y", "X"):
        if r not in dims:
            raise ValueError(f"Missing '{r}' in axes={axes}, shape={shape}")

    perm = [dims.index("Z"), dims.index("C"), dims.index("Y"), dims.index("X")]
    return np.transpose(arr, axes=perm)


def _read_stack_to_ZCYX(path: str) -> Tuple[np.ndarray, List[str]]:
    """Read an OME-TIFF/TIFF and return an array with axes (Z, C, Y, X) plus
    channel names (falling back to 'Channel{i}')."""
    with tifffile.TiffFile(path) as tif:
        series = tif.series[0]
        arr = series.asarray()
        axes_tf = series.axes
        print(f"Using tifffile axes: {axes_tf}, shape={arr.shape}")

        ch_names = None
        ome_xml = getattr(tif, "ome_metadata", None)
        if ome_xml:
            try:
                from ome_types import from_xml  # optional dependency
                ome = from_xml(ome_xml)
                im = ome.images[0]
                ch_names = [ch.name or f"Channel{i}" for i, ch in enumerate(im.pixels.channels)]
            except Exception as e:
                print(f"[warn] Failed parsing OME metadata: {e}")

    zcyx = _reorder_to_ZCYX(arr, axes_tf)

    if ch_names is None or len(ch_names) != zcyx.shape[1]:
        ch_names = [f"Channel{i}" for i in range(zcyx.shape[1])]

    return zcyx, ch_names


# --------------------------
# Processing
# --------------------------

def _project_stack_streaming(
    path: str, z_range: Optional[Sequence[int]], projection: str,
) -> Optional[Tuple[np.ndarray, List[str]]]:
    """Compute the same result as
    `_projection(_apply_z_range(_read_stack_to_ZCYX(path)[0], z_range), projection)`
    but page-by-page, without ever materializing the full (Z,C,Y,X) array.

    Reading a whole stitched wholemount into RAM just to max-project it back
    down to (C,Y,X) is what OOM'd here at the ~5 GiB scale (same class of bug
    as the stitcher's own full-mosaic allocation) -- this only ever holds one
    (C,Y,X)-sized accumulator plus one (Y,X) page at a time.

    Returns None (caller should fall back to the whole-array path) for any
    layout this fast path doesn't confidently handle -- RGB/multi-sample
    pages, a page count that doesn't match the declared non-Y/X shape, or a
    'T' axis with size >1 (same restriction `_reorder_to_ZCYX` enforces).
    """
    projection = (projection or "max").lower()
    if projection not in ("max", "mean"):
        raise ValueError("projection must be 'max' or 'mean'")

    with tifffile.TiffFile(path) as tif:
        series = tif.series[0]
        pages = series.pages
        axes = list(series.axes.upper())
        shape = list(series.shape)

        if series.keyframe.samplesperpixel != 1:
            return None  # RGB/multi-sample - let the whole-array path handle it
        if "Y" not in axes or "X" not in axes or "C" not in axes:
            return None
        flat_axes = [a for a in axes if a not in ("Y", "X")]
        flat_shape = [shape[axes.index(a)] for a in flat_axes]
        if int(np.prod(flat_shape, dtype=np.int64)) != len(pages):
            return None  # pyramidal/nested series etc. - not the simple layout we assume
        if "T" in flat_axes and flat_shape[flat_axes.index("T")] != 1:
            return None  # matches _reorder_to_ZCYX's own restriction

        z_pos = flat_axes.index("Z") if "Z" in flat_axes else None
        c_pos = flat_axes.index("C")
        nz = flat_shape[z_pos] if z_pos is not None else 1
        nc = flat_shape[c_pos]
        h, w = shape[axes.index("Y")], shape[axes.index("X")]

        z0, z1 = (None, None) if not z_range else (z_range[0], z_range[1])
        z0 = 0 if z0 is None else int(z0)
        z1 = nz if (z1 is None or z1 <= 0) else int(z1)
        z0 = max(0, min(z0, nz))
        z1 = max(z0 + 1, min(z1, nz))

        dtype = series.dtype
        if projection == "max":
            acc = np.zeros((nc, h, w), dtype=dtype)
        else:
            acc = np.zeros((nc, h, w), dtype=np.float64)

        for p_idx, page in enumerate(pages):
            idx = np.unravel_index(p_idx, flat_shape) if len(flat_shape) > 1 else (p_idx,)
            z = idx[z_pos] if z_pos is not None else 0
            if z < z0 or z >= z1:
                continue
            c = idx[c_pos]
            data = page.asarray()
            if projection == "max":
                np.maximum(acc[c], data, out=acc[c])
            else:
                acc[c] += data

        if projection == "mean":
            acc = (acc / max(z1 - z0, 1)).astype(np.float64)

        ch_names = None
        ome_xml = getattr(tif, "ome_metadata", None)
        if ome_xml:
            try:
                from ome_types import from_xml  # optional dependency
                ome = from_xml(ome_xml)
                im = ome.images[0]
                ch_names = [ch.name or f"Channel{i}" for i, ch in enumerate(im.pixels.channels)]
            except Exception as e:
                print(f"[warn] Failed parsing OME metadata: {e}")

    if ch_names is None or len(ch_names) != nc:
        ch_names = [f"Channel{i}" for i in range(nc)]

    return acc, ch_names


def _apply_z_range(zcyx: np.ndarray, z_range: Optional[Sequence[int]]) -> np.ndarray:
    if not z_range:
        return zcyx
    z0, z1 = z_range
    z0 = 0 if z0 is None else int(z0)
    z1 = zcyx.shape[0] if (z1 is None or z1 <= 0) else int(z1)
    z0 = max(0, min(z0, zcyx.shape[0]))
    z1 = max(z0 + 1, min(z1, zcyx.shape[0]))
    return zcyx[z0:z1]


def _projection(zcyx: np.ndarray, mode: str = "max") -> np.ndarray:
    mode = (mode or "max").lower()
    if mode == "max":
        return zcyx.max(axis=0)  # C, Y, X
    elif mode == "mean":
        return zcyx.mean(axis=0)
    else:
        raise ValueError("projection must be 'max' or 'mean'")


def _percentile_normalize(cyx: np.ndarray, p_low: float, p_high: float, clip: bool = True) -> np.ndarray:
    out = np.empty_like(cyx, dtype=np.float32)
    for c in range(cyx.shape[0]):
        ch = cyx[c].astype(np.float32)
        lo = np.percentile(ch, p_low) if p_low is not None else ch.min()
        hi = np.percentile(ch, p_high) if p_high is not None else ch.max()
        if hi <= lo:
            hi = lo + 1.0
        ch = (ch - lo) / (hi - lo)
        if clip:
            ch = np.clip(ch, 0, 1)
        out[c] = ch
    return out


def _gain_match(cyx: np.ndarray) -> np.ndarray:
    target = np.median([np.percentile(cyx[c], 95) for c in range(cyx.shape[0])])
    out = cyx.copy()
    for c in range(cyx.shape[0]):
        q = np.percentile(out[c], 95)
        scale = 1.0 if q == 0 else target / q
        out[c] = np.clip(out[c] * scale, 0, 1)
    return out


# --------------------------
# Saving
# --------------------------

# --------------------------
# Colorized panel PNG
# --------------------------

_DEFAULT_PALETTE = ["#00FF00", "#FF00FF", "#00FFFF", "#FFFF00",
                    "#FF0000", "#0000FF", "#FFFFFF", "#FFA500"]


def _default_palette(n: int) -> List[str]:
    if n <= len(_DEFAULT_PALETTE):
        return _DEFAULT_PALETTE[:n]
    return [_DEFAULT_PALETTE[i % len(_DEFAULT_PALETTE)] for i in range(n)]


def _normalize_colors(colors: Optional[Sequence[str]], C: int) -> List[str]:
    """Return exactly `C` hex color strings, filling in/padding from the default palette."""
    if not colors:
        return _default_palette(C)
    out = list(colors[:C])
    if len(out) < C:
        palette = _default_palette(C)
        i = 0
        while len(out) < C:
            out.append(palette[i % len(palette)])
            i += 1
    return out


def _tint_single_channel(ch: np.ndarray, color_hex: str) -> np.ndarray:
    """Colorize a single normalized (Y, X) channel into an (Y, X, 3) RGB image using `color_hex`."""
    h = color_hex.lstrip('#')
    r, g, b = (int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    rgb = np.stack([r * ch, g * ch, b * ch], axis=-1)
    return np.clip(rgb, 0, 1).astype(np.float32)


def save_mip_panel(cyx: np.ndarray, ch_names: Sequence[str], out_path: str,
                    colors: Optional[Sequence[str]] = None,
                    dpi: int = 300, panel_cols: int = 4,
                    panel_title: Optional[str] = None) -> None:
    """Write a colorized per-channel panel grid PNG for the (already normalized) MIP."""
    C = cyx.shape[0]
    norm_colors = _normalize_colors(colors, C)

    rows = int(np.ceil(C / panel_cols))
    fig = plt.figure(figsize=(2.5 * panel_cols, 2.5 * rows))
    for i in range(C):
        ax = fig.add_subplot(rows, panel_cols, i + 1)
        ax.imshow(_tint_single_channel(cyx[i], norm_colors[i]))
        ax.set_title(str(ch_names[i]), fontsize=8)
        ax.axis('off')
    if panel_title:
        fig.suptitle(panel_title, fontsize=10)
    fig.savefig(out_path, dpi=dpi, bbox_inches="tight", pad_inches=0.1)
    plt.close(fig)
    print(f"Wrote {out_path}")


# --------------------------
# Job runner
# --------------------------

def _get(obj: Any, key: str, default=None):
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def run_mip_job(job: Any) -> Dict[str, Any]:
    """Run a single MIP job. `job` may be a dict or an object with attributes.

    Expected keys/attrs:
      - stacks_dir: str
      - output_dir: str
      - channel_names: Optional[List[str]] (override names recovered from the stack)
      - preprocess: Optional[dict] with key gain_match (bool)
      - projection: "max" or "mean"
      - z_range: Optional [start, end]
      - norm: dict with p_low, p_high, clip
      - include_keywords / exclude_keywords: Optional[List[str]]
      - channel_colors: Optional[List[str]] (hex, one per channel, for the
        panel PNG; defaults to a built-in palette when not given)
    """
    stacks_dir = Path(_get(job, "stacks_dir"))
    output_dir = Path(_get(job, "output_dir"))
    output_dir.mkdir(parents=True, exist_ok=True)

    channel_names = _get(job, "channel_names")
    channel_colors = _get(job, "channel_colors")
    preprocess = _get(job, "preprocess") or {}
    projection = (_get(job, "projection") or "max").lower()
    z_range = _get(job, "z_range")
    norm = _get(job, "norm") or {"p_low": 2.0, "p_high": 99.8, "clip": True}

    include_keywords = _get(job, "include_keywords")
    exclude_keywords = _get(job, "exclude_keywords")

    files = sorted({p.resolve() for p in stacks_dir.glob("*.tif")} |
                    {p.resolve() for p in stacks_dir.glob("*.tiff")})
    files = [Path(p) for p in files]
    print(f"Found {len(files)} files in {stacks_dir}")

    def keep(name: str) -> bool:
        s = name.lower()
        if include_keywords and not any(k.lower() in s for k in include_keywords):
            return False
        if exclude_keywords and any(k.lower() in s for k in exclude_keywords):
            return False
        return True

    files = [f for f in files if keep(f.name)]
    print(f"Filtered to {len(files)} files after include/exclude")

    written = []
    warnings = []

    for f in files:
        base = f.stem.replace(".ome", "")
        print(f"-> {base}")
        try:
            streamed = _project_stack_streaming(str(f), z_range, projection)
            if streamed is not None:
                cyx, ch_names = streamed
            else:
                zcyx, ch_names = _read_stack_to_ZCYX(str(f))
                zcyx = _apply_z_range(zcyx, z_range)
                cyx = _projection(zcyx, projection)

            if channel_names and len(channel_names) == cyx.shape[0]:
                ch_names = list(channel_names)

            cyx = _percentile_normalize(cyx, norm.get("p_low", 2.0), norm.get("p_high", 99.8), norm.get("clip", True))

            if preprocess.get("gain_match"):
                cyx = _gain_match(cyx)

            save_mip_panel(cyx, ch_names, str(output_dir / f"{base}_MIP_panel.png"),
                            colors=channel_colors, panel_title=base)
            written.append(base)
        except Exception as e:
            warnings.append(f"{f.name}: {e}")
            print(f"[error] Failed {f.name}: {e}")

    return {"written": len(written), "files": [f.name for f in files], "warnings": warnings}
