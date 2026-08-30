"""Core MIP (max-intensity-projection) pipeline for CQ1 TIFF/OME-TIFF stacks.

Reads Z-stack TIFF/OME-TIFF files, reorders their axes to (Z, C, Y, X),
applies an optional Z-range crop, projects along Z (max or mean), normalizes
and optionally gain-matches channels, then saves the result either as a
multi-channel OME-TIFF or as PNGs into configurable subfolders: a per-channel
panel grid (panel/), and a Z-thirds facet grid (zstack_panel/) showing the
complete-stack MIP alongside MIPs of the Z axis split into thirds.
`run_mip_job` drives a whole job over a directory of files, optionally in
parallel via a process pool.

Copied from CQ1_Stacks_to_MIP_parallel so CQ1_FieldtoStacks_parallel can run
MIP generation automatically right after assembling stacks, without a
cross-folder import dependency.
"""

from __future__ import annotations
from pathlib import Path
from typing import Tuple, List, Sequence, Optional, Dict, Any

import os
# Cap BLAS/OMP thread pools to 1 per process: each ProcessPoolExecutor worker
# otherwise spawns its own multi-threaded numpy backend, so N workers can end
# up fighting over N x cpu_count() threads instead of just N.
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("NUMEXPR_NUM_THREADS", "1")
import numpy as np
import tifffile
import matplotlib
matplotlib.use("Agg")  # headless-safe; avoids GUI backends in workers
import matplotlib.pyplot as plt
from concurrent.futures import ProcessPoolExecutor, as_completed


# --------------------------
# Reader helpers
# --------------------------

def _read_stack_to_ZCYX(path: str) -> Tuple[np.ndarray, List[str]]:
    """
    Read an OME-TIFF/TIFF and return:
      - array with axes (Z, C, Y, X)
      - channel names list (fallback to 'Channel{i}')
    """
    with tifffile.TiffFile(path) as tif:
        series = tif.series[0]
        arr = series.asarray()
        axes_tf = series.axes  # e.g., "CZYX" or "TCZYX"
        print(f"[{os.getpid()}] axes: {axes_tf}, shape={arr.shape}, file={Path(path).name}")

        ch_names = None
        ome_xml = getattr(tif, "ome_metadata", None)
        if ome_xml:
            try:
                from ome_types import from_xml  # optional dependency
                ome = from_xml(ome_xml)
                im = ome.images[0]
                ch_names = [ch.name or f"Channel{i}" for i, ch in enumerate(im.pixels.channels)]
            except Exception as e:
                print(f"⚠️ Failed parsing OME metadata: {e}")

    zcyx = _reorder_to_ZCYX(arr, axes_tf)

    if ch_names is None or len(ch_names) != zcyx.shape[1]:
        ch_names = [f"Channel{i}" for i in range(zcyx.shape[1])]

    return zcyx, ch_names


def _reorder_to_ZCYX(arr: np.ndarray, axes: str) -> np.ndarray:
    """Reorder array according to axes string into Z,C,Y,X order. Singleton T allowed."""
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

    for r in ("Z", "C", "Y", "X"):
        if r not in dims:
            raise ValueError(f"Missing '{r}' in axes={axes}, shape={shape}")

    perm = [dims.index("Z"), dims.index("C"), dims.index("Y"), dims.index("X")]
    return np.transpose(arr, axes=perm)


# --------------------------
# Processing
# --------------------------

def _apply_z_range(zcyx: np.ndarray, z_range: Optional[Sequence[int]]) -> np.ndarray:
    """Crop a (Z, C, Y, X) array to the [z0, z1) slice given by `z_range`.

    `z_range` is a 2-element (z0, z1) sequence; either side may be None
    (z0 defaults to 0, z1 defaults to the full depth). Falsy `z_range`
    (None or empty) returns `zcyx` unchanged. Bounds are clamped to the
    array's Z extent and z1 is kept at least z0 + 1.
    """
    if not z_range:
        return zcyx
    z0, z1 = z_range
    z0 = 0 if z0 is None else int(z0)
    z1 = zcyx.shape[0] if (z1 is None or z1 <= 0) else int(z1)
    z0 = max(0, min(z0, zcyx.shape[0]))
    z1 = max(z0 + 1, min(z1, zcyx.shape[0]))
    return zcyx[z0:z1]


def _projection(zcyx: np.ndarray, mode: str = "max") -> np.ndarray:
    """Collapse the Z axis of a (Z, C, Y, X) array into a (C, Y, X) projection.

    `mode` is 'max' (max-intensity projection) or 'mean'; raises ValueError
    for anything else.
    """
    mode = (mode or "max").lower()
    if mode == "max":
        return zcyx.max(axis=0)  # C, Y, X
    elif mode == "mean":
        return zcyx.mean(axis=0)
    else:
        raise ValueError("projection must be 'max' or 'mean'")


def _percentile_normalize(cyx: np.ndarray, p_low: float, p_high: float, clip: bool = True) -> np.ndarray:
    """Rescale each channel of a (C, Y, X) array to [0, 1] independently.

    For each channel, `p_low`/`p_high` percentile intensities (0-100) are
    mapped to 0/1; pass None for either to use the channel's raw min/max
    instead. If `clip` is True, values outside [0, 1] are clipped after
    scaling. Degenerate channels (hi <= lo) fall back to a unit range
    starting at lo, so the output stays finite.
    """
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
    """Rescale each channel's 95th-percentile intensity to the cross-channel median.

    Expects `cyx` already normalized to [0, 1] (e.g. via `_percentile_normalize`).
    Channels are individually scaled toward a shared brightness target and
    re-clipped to [0, 1], so no single channel dominates a merged RGB image.
    """
    target = np.median([np.percentile(cyx[c], 95) for c in range(cyx.shape[0])])
    out = cyx.copy()
    for c in range(cyx.shape[0]):
        q = np.percentile(out[c], 95)
        # ensure float arithmetic to avoid static type warnings in linters
        scale = 1.0 if float(q) == 0.0 else float(target) / float(q)
        out[c] = np.clip(out[c] * scale, 0, 1)
    return out


# --------------------------
# Colors & saving / plotting
# --------------------------

def _default_palette(n: int) -> List[str]:
    """Return `n` hex color strings from a fixed 8-color palette, cycling if `n` exceeds it."""
    base = ["#00FF00","#FF00FF","#00FFFF","#FFFF00","#FF0000","#0000FF","#FFFFFF","#FFA500"]
    if n <= len(base):
        return base[:n]
    return [base[i % len(base)] for i in range(n)]


def _normalize_colors(colors: Optional[Sequence[str]], C: int) -> List[str]:
    """Return exactly `C` hex color strings, filling in/padding from the default palette.

    `colors` may be None, shorter than `C` (padded with palette colors), or
    longer than `C` (truncated).
    """
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


def save_mip_tiff(cyx: np.ndarray, out_path: str, ch_names: Sequence[str]) -> None:
    """Write a (C, Y, X) projection as a float32 multi-channel OME-TIFF with channel names embedded."""
    cyx = np.asarray(cyx, dtype=np.float32)
    metadata = {"axes": "CYX", "Channel": {"Name": list(map(str, ch_names))}}
    tifffile.imwrite(out_path, cyx, dtype=np.float32, photometric="minisblack",
                     metadata=metadata, ome=True)
    print(f"💾 Wrote {out_path}")


def _tint_single_channel(ch: np.ndarray, color_hex: str) -> np.ndarray:
    """Colorize a single normalized (Y, X) channel into an (Y, X, 3) RGB image using `color_hex`."""
    h = color_hex.lstrip('#')
    r, g, b = (int(h[i:i+2], 16) / 255.0 for i in (0, 2, 4))
    rgb = np.stack([r * ch, g * ch, b * ch], axis=-1)
    return np.clip(rgb, 0, 1).astype(np.float32)


def _resolve_dir(out_dir: Path, sub: Optional[str], default_name: str) -> Path:
    """Resolve a subfolder under `out_dir` (or use `sub` as-is if absolute)."""
    if sub:
        p = Path(sub)
        return p if p.is_absolute() else out_dir / p
    return out_dir / default_name


def plot_mip(cyx: np.ndarray,
             ch_names: Sequence[str],
             out_dir: str,
             basename: str,
             colors: Optional[Sequence[str]] = None,
             panel: bool = True,
             dpi: int = 300,
             panel_cols: int = 4,
             panel_title: Optional[str] = None,
             per_channel_colored: bool = False,
             panel_subdir: Optional[str] = None) -> None:
    """
    Write the per-channel grayscale (or tinted) panel grid as a PNG.
    If `panel_subdir` is absolute it is used as-is, otherwise it is created under out_dir.
    """
    if not panel:
        return

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    panel_dir = _resolve_dir(out_dir, panel_subdir, "panel")
    panel_dir.mkdir(parents=True, exist_ok=True)

    C = cyx.shape[0]
    norm_colors = _normalize_colors(colors, C)

    rows = int(np.ceil(C / panel_cols))
    fig = plt.figure(figsize=(2.5 * panel_cols, 2.5 * rows))
    for i in range(C):
        ax = fig.add_subplot(rows, panel_cols, i + 1)
        if per_channel_colored:
            ax.imshow(_tint_single_channel(cyx[i], norm_colors[i]))
        else:
            ax.imshow(cyx[i], vmin=0, vmax=1, cmap="gray")
        ax.set_title(str(ch_names[i]), fontsize=8)
        ax.axis('off')
    if panel_title:
        fig.suptitle(panel_title, fontsize=10)
    panel_name = panel_dir / f"{basename}_MIP_panel.png"
    fig.savefig(panel_name, dpi=dpi, bbox_inches="tight", pad_inches=0.1)
    plt.close(fig)
    print(f"🖼️ Wrote {panel_name}")


def plot_zstack_panel(zcyx: np.ndarray,
                       ch_names: Sequence[str],
                       out_dir: str,
                       basename: str,
                       colors: Optional[Sequence[str]] = None,
                       norm: Optional[Dict[str, Any]] = None,
                       gain_match: bool = False,
                       dpi: int = 300,
                       panel_title: Optional[str] = None,
                       per_channel_colored: bool = False,
                       subdir: Optional[str] = None) -> None:
    """
    Facet grid: row 0 is the MIP of the complete (Z, C, Y, X) `zcyx` stack;
    rows 1-3 are MIPs of the stack's Z axis split into thirds (as evenly as
    possible via `np.array_split`). Columns are channels. Each row is
    percentile-normalized (and optionally gain-matched) independently, using
    the same `norm`/`gain_match` settings as the main panel.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    zstack_dir = _resolve_dir(out_dir, subdir, "zstack_panel")
    zstack_dir.mkdir(parents=True, exist_ok=True)

    norm = norm or {"p_low": 2.0, "p_high": 99.8, "clip": True}
    Z, C = zcyx.shape[0], zcyx.shape[1]
    thirds = np.array_split(np.arange(Z), 3)
    row_specs = [("Complete Z", np.arange(Z))] + [
        (f"Z third {i + 1}/3", idx) for i, idx in enumerate(thirds)
    ]

    norm_colors = _normalize_colors(colors, C)

    n_rows = len(row_specs)
    fig = plt.figure(figsize=(2.5 * C, 2.5 * n_rows))
    for r, (row_label, idx) in enumerate(row_specs):
        if len(idx) == 0:
            continue
        row_cyx = zcyx[idx].max(axis=0)  # C, Y, X
        row_cyx = _percentile_normalize(row_cyx, norm.get("p_low", 2.0),
                                        norm.get("p_high", 99.8), norm.get("clip", True))
        if gain_match:
            row_cyx = _gain_match(row_cyx)
        for c in range(C):
            ax = fig.add_subplot(n_rows, C, r * C + c + 1)
            if per_channel_colored:
                ax.imshow(_tint_single_channel(row_cyx[c], norm_colors[c]))
            else:
                ax.imshow(row_cyx[c], vmin=0, vmax=1, cmap="gray")
            ax.axis('off')
            if r == 0:
                ax.set_title(str(ch_names[c]), fontsize=8)
            if c == 0:
                ax.text(-0.1, 0.5, row_label, transform=ax.transAxes, fontsize=8,
                        ha='right', va='center', rotation=90)
    if panel_title:
        fig.suptitle(panel_title, fontsize=10)
    out_name = zstack_dir / f"{basename}_MIP_zstack_panel.png"
    fig.savefig(out_name, dpi=dpi, bbox_inches="tight", pad_inches=0.1)
    plt.close(fig)
    print(f"🖼️ Wrote {out_name}")


# --------------------------
# Main job runner (+ parallel)
# --------------------------

def _get(obj: Any, key: str, default=None):
    """Fetch `key` from `obj`, whether `obj` is a dict or an attribute-bearing object."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _discover_files(stacks_dir: Path,
                    include_keywords: Optional[Sequence[str]],
                    exclude_keywords: Optional[Sequence[str]]) -> List[Path]:
    """List *.tif/*.tiff files directly under `stacks_dir`, filtered by filename keywords.

    A filename is kept only if it contains at least one `include_keywords`
    entry (when given) and none of the `exclude_keywords` entries. Matching
    is case-insensitive substring matching.
    """
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
    return files


def _process_one_file(f_path: str, job: Dict[str, Any]) -> Dict[str, Any]:
    """
    Worker entry. Returns dict with keys: base, warning (optional).
    Why process-level: avoids GIL, isolates matplotlib state, better CPU utilization.
    """
    try:
        f = Path(f_path)
        base = f.stem.replace(".ome", "")

        zcyx, ch_names = _read_stack_to_ZCYX(str(f))

        channel_names = job.get("channel_names")
        if channel_names and len(channel_names) == zcyx.shape[1]:
            ch_names = list(channel_names)

        z_range = job.get("z_range")
        projection = (job.get("projection") or "max").lower()
        norm = job.get("norm") or {"p_low": 2.0, "p_high": 99.8, "clip": True}
        preprocess = job.get("preprocess") or {}
        save = job.get("save") or {"fmt": "png", "dpi": 300, "panel": True, "zstack_panel": True}
        out_dir = Path(job["output_dir"])
        out_dir.mkdir(parents=True, exist_ok=True)
        channel_colors = job.get("channel_colors")

        zcyx = _apply_z_range(zcyx, z_range)
        cyx = _projection(zcyx, projection)
        cyx = _percentile_normalize(cyx, norm.get("p_low", 2.0),
                                    norm.get("p_high", 99.8),
                                    norm.get("clip", True))
        if preprocess.get("gain_match"):
            cyx = _gain_match(cyx)

        fmt = str(save.get("fmt", "png")).lower()
        if fmt in ("tif", "tiff"):
            out_name = f"{base}_MIP.tif"
            save_mip_tiff(cyx, str(out_dir / out_name), ch_names)
        else:
            title = save.get("panel_title")
            if isinstance(title, str):
                title = title.replace("{basename}", base)
            # Default to tinting panels with `channel_colors` whenever colors
            # were actually given -- there's no other consumer of that field,
            # so "I set colors" should be enough to get colored output
            # without also needing a separate "use colors" toggle. Only
            # falls back to grayscale by default when no colors are given at
            # all; an explicit "per_channel_colored" in `save` still wins
            # either way, for the JSON/CLI power-user path.
            default_colored = channel_colors is not None
            per_channel_colored = bool(save.get("per_channel_colored", default_colored))
            plot_mip(
                cyx, ch_names, str(out_dir), base,
                colors=channel_colors,
                panel=bool(save.get("panel", True)),
                dpi=int(save.get("dpi", 300)),
                panel_cols=int(save.get("panel_cols", 4)),
                panel_title=title,
                per_channel_colored=per_channel_colored,
                panel_subdir=save.get("panel_subdir"),
            )
            if bool(save.get("zstack_panel", True)):
                plot_zstack_panel(
                    zcyx, ch_names, str(out_dir), base,
                    colors=channel_colors,
                    norm=norm,
                    gain_match=bool(preprocess.get("gain_match")),
                    dpi=int(save.get("dpi", 300)),
                    panel_title=title,
                    per_channel_colored=per_channel_colored,
                    subdir=save.get("zstack_panel_subdir"),
                )
        return {"base": base}
    except Exception as e:
        return {"base": None, "warning": f"{Path(f_path).name}: {e}"}


def run_mip_job(job: Any, workers: int = 1) -> Dict[str, Any]:
    """
    Run a single MIP job: discover stack files, project each in parallel or
    serially, and save the results. `job` may be a dict (e.g. parsed from
    JSON) or any object with matching attributes; recognized keys are:

      stacks_dir (str, required)      - folder to scan for *.tif/*.tiff
      output_dir (str, required)      - folder to write outputs into
      include_keywords (list[str])    - keep only filenames containing one of these
      exclude_keywords (list[str])    - drop filenames containing any of these
      channel_names (list[str])       - override names read from OME metadata
      channel_colors (list[str])      - hex colors, one per channel, for RGB/PNG output
      z_range ([z0, z1])              - crop before projecting; either side may be None
      projection ("max" | "mean")     - Z-projection mode (default "max")
      norm {p_low, p_high, clip}      - percentile normalization (defaults 2.0, 99.8, True)
      preprocess {gain_match}         - if True, equalize channel brightness after normalizing
      save {fmt, dpi, panel, panel_cols, panel_subdir, panel_title,
            per_channel_colored, zstack_panel, zstack_panel_subdir}
                                       - fmt "tif"/"tiff" writes an OME-TIFF;
                                         anything else (default "png") writes:
                                           panel/         - one grid PNG, channels side by side
                                           zstack_panel/  - facet grid: row 0 is the MIP of the
                                                            complete (cropped) Z stack, rows 1-3
                                                            are MIPs of the Z axis split into thirds
                                         (zstack_panel defaults to on; set False to skip it)

    Parallel when `workers` > 1 (one process per file, via ProcessPoolExecutor);
    serial otherwise. Per-file exceptions are caught and reported as warnings
    rather than aborting the whole job.

    Returns {"written": count, "files": [names found], "warnings": [messages]}.
    """
    stacks_dir = Path(_get(job, "stacks_dir"))
    output_dir = Path(_get(job, "output_dir"))
    output_dir.mkdir(parents=True, exist_ok=True)

    include_keywords = _get(job, "include_keywords")
    exclude_keywords = _get(job, "exclude_keywords")

    files = _discover_files(stacks_dir, include_keywords, exclude_keywords)
    written: List[str] = []
    warnings: List[str] = []

    if workers and workers > 1:
        # Chunk size heuristic: balance I/O and CPU
        chunk = max(1, len(files) // (workers * 4) or 1)
        print(f"🧵 Parallel mode: workers={workers}, chunk_size={chunk}, n_files={len(files)}")
        with ProcessPoolExecutor(max_workers=workers) as ex:
            futures = [ex.submit(_process_one_file, str(f), dict(job)) for f in files]
            for fut in as_completed(futures):
                res = fut.result()
                if res.get("base"):
                    written.append(res["base"])
                if res.get("warning"):
                    warnings.append(res["warning"])
                    print("❌", res["warning"])
    else:
        print(f"🧵 Serial mode: n_files={len(files)}")
        for f in files:
            res = _process_one_file(str(f), dict(job))
            if res.get("base"):
                written.append(res["base"])
            if res.get("warning"):
                warnings.append(res["warning"])
                print("❌", res["warning"])

    return {"written": len(written), "files": [f.name for f in files], "warnings": warnings}
