#!/usr/bin/env python3
# main.py - routes to unified stitcher backends and can generate the metadata
# CSV from a CQ1 OME-XML on demand.

from __future__ import annotations
import argparse
import inspect
import os
from time import perf_counter
from typing import Callable, Dict, List, Optional, Tuple

import pandas as pd

from stitcher_unified import (
    stitch_grid_classic,
    stitch_grid_seamless,
    stitch_grid_tinyoverlap,
)

try:
    from ome_metadata import generate_metadata_csv
except Exception:
    generate_metadata_csv = None  # type: ignore

_BACKENDS: Dict[str, Callable] = {
    "classic": stitch_grid_classic,
    "seamless": stitch_grid_seamless,
    "tiny": stitch_grid_tinyoverlap,
}


def _pairs(df: pd.DataFrame) -> List[Tuple[str, int]]:
    return list(
        df[["WellID", "GridIndex"]]
        .drop_duplicates()
        .sort_values(["WellID", "GridIndex"])
        .itertuples(index=False, name=None)
    )


def _dispatch(backend: str, **kw) -> None:
    """Call the selected backend, forwarding only the keyword arguments it
    actually declares. Backends differ in which writer/blend knobs they accept
    (e.g. `classic` takes far fewer than `seamless`) - filtering here means a
    single shared `blend_args` dict can drive any of the three without one of
    them crashing on an unexpected keyword argument."""
    fn = _BACKENDS.get(backend.lower())
    if fn is None:
        raise ValueError(f"Unknown stitch_backend={backend!r} (choices: {list(_BACKENDS)})")
    accepted = set(inspect.signature(fn).parameters)
    filtered = {k: v for k, v in kw.items() if k in accepted}
    fn(**filtered)


def _apply_channel_names(df: pd.DataFrame, channel_names: Optional[List[str]], verbose: bool = True) -> pd.DataFrame:
    """Override the CSV's 'ChannelName' column (as parsed from the CQ1 OME-XML)
    with user-supplied names, mapped onto the sorted unique 'C' plane indices."""
    if not channel_names:
        return df
    unique_c = sorted(df["C"].unique())
    if len(channel_names) != len(unique_c):
        print(f"[warn] --channel_names has {len(channel_names)} entries but "
              f"{len(unique_c)} channel(s) found in CSV (C={unique_c}); ignoring override.")
        return df
    mapping = dict(zip(unique_c, channel_names))
    df = df.copy()
    df["ChannelName"] = df["C"].map(mapping)
    if verbose:
        print(f"[meta] Overriding channel names: {mapping}")
    return df


def main(meta_csv: str, image_root: str, output_dir: str, *, blend_args: Optional[Dict] = None,
         only_well: Optional[str] = None, only_wells: Optional[List[str]] = None,
         only_grid: Optional[float] = None, dry_run: bool = False,
         stitch_backend: str = "seamless", only_z: Optional[float] = None,
         channel_names: Optional[List[str]] = None, verbose: bool = True) -> List[str]:
    if blend_args is None:
        blend_args = {}
    df = pd.read_csv(meta_csv)
    if verbose:
        print("CSV Columns:", list(df.columns))
    df = _apply_channel_names(df, channel_names, verbose=verbose)

    if only_z is not None and "Z" in df.columns:
        df = df[df["Z"].astype(float) == float(only_z)]
        if df.empty:
            print(f"No rows found for Z == {only_z}. Nothing to stitch.")
            return []

    pairs = _pairs(df)
    if only_wells:
        wanted = {str(w) for w in only_wells}
        pairs = [p for p in pairs if str(p[0]) in wanted]
    elif only_well is not None:
        pairs = [p for p in pairs if str(p[0]) == str(only_well)]
    if only_grid is not None:
        pairs = [p for p in pairs if float(p[1]) == float(only_grid)]

    os.makedirs(output_dir, exist_ok=True)
    if verbose:
        print(f"Will stitch {len(pairs)} grid(s) to {output_dir}")

    produced: List[str] = []
    for well_id, grid_index in pairs:
        safe_well = str(well_id).replace("/", "_")
        suffix = f"_Z{int(float(only_z))}" if only_z is not None else ""
        out_path = os.path.join(output_dir, f"{safe_well}_A{int(float(grid_index))}{suffix}_stitched.ome.tif")

        if dry_run:
            print(f"Would process: WellID={well_id}, Grid={grid_index} -> {out_path}")
            continue

        print(f"\n=== Stitching WellID={well_id}, Grid={grid_index} to {out_path} ===")
        t0 = perf_counter()
        _dispatch(
            stitch_backend,
            df=df, image_root=image_root, output_path=out_path,
            well_id=well_id, grid_index=int(float(grid_index)),
            **blend_args, verbose=verbose,
        )
        print(f"[{well_id} G{grid_index}] Total elapsed (incl. write): {perf_counter() - t0:.2f}s")
        produced.append(out_path)
    return produced


def run_main(meta_csv: str, image_root: str, output_dir: str, *, blend_args: Optional[Dict] = None,
             only_well: Optional[str] = None, only_wells: Optional[List[str]] = None,
             only_grid: Optional[float] = None, dry_run: bool = False,
             stitch_backend: str = "seamless", only_z: Optional[float] = None, xml_file: Optional[str] = None,
             overlap_fraction: float = 0.01, channel_names: Optional[List[str]] = None,
             verbose: bool = True) -> List[str]:
    if not os.path.exists(meta_csv):
        if not xml_file:
            raise FileNotFoundError(f"Metadata CSV not found at '{meta_csv}', and no xml_file provided.")
        if generate_metadata_csv is None:
            raise RuntimeError("ome_metadata.generate_metadata_csv not importable; cannot build metadata CSV.")
        if verbose:
            print(f"[meta] CSV missing -> generating from OME-XML: {xml_file} (overlap_fraction={overlap_fraction})")
        generate_metadata_csv(xml_file, meta_csv, overlap_fraction=overlap_fraction)

    return main(meta_csv, image_root, output_dir, blend_args=blend_args, only_well=only_well,
                only_wells=only_wells, only_grid=only_grid, dry_run=dry_run, stitch_backend=stitch_backend,
                only_z=only_z, channel_names=channel_names, verbose=verbose)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Batch stitch CQ1 wholemount mosaics into FIJI-compatible OME-TIFFs.")
    p.add_argument("--meta_csv", required=True)
    p.add_argument("--image_root", required=True)
    p.add_argument("--output_dir", required=True)
    p.add_argument("--xml_file", default=None)
    p.add_argument("--overlap_fraction", type=float, default=0.01)
    p.add_argument("--stitch_backend", choices=list(_BACKENDS), default="seamless")
    p.add_argument("--placement", choices=["stage", "grid"], default="stage")

    # writer/blend/gain knobs (unified; unused ones are filtered per-backend by _dispatch)
    p.add_argument("--compression", default="zlib")
    p.add_argument("--compression_level", type=int, default=6)
    p.add_argument("--predictor", action="store_true", default=True)
    p.add_argument("--no_predictor", dest="predictor", action="store_false")
    p.add_argument("--tile", nargs=2, type=int, default=(512, 512), metavar=("H", "W"))
    p.add_argument("--bigtiff", action="store_true", default=True)
    p.add_argument("--no_bigtiff", dest="bigtiff", action="store_false")
    p.add_argument("--pyramids", action="store_true", default=False)

    p.add_argument("--default_overlap_fraction", type=float, default=0.01)
    p.add_argument("--min_feather_px", type=int, default=6)
    p.add_argument("--max_feather_px", type=int, default=32)
    p.add_argument("--max_snap_px", type=int, default=6)

    p.add_argument("--lattice_snap_px", type=int, default=0)
    p.add_argument("--snap_axis", choices=["both", "x", "y"], default="both")
    p.add_argument("--refine_alignment", action="store_true", default=False)
    p.add_argument("--refine_max_shift_px", type=int, default=6)
    p.add_argument("--refine_axis", choices=["both", "x", "y"], default="both")

    p.add_argument("--overlap_gain_match", action="store_true", default=False)
    p.add_argument("--gain_clip_low", type=float, default=0.92)
    p.add_argument("--gain_clip_high", type=float, default=1.08)
    p.add_argument("--gain_match_channels", nargs="*", default=None)

    p.add_argument("--channel_names", nargs="*", default=None,
                   help="Override channel names, in C-index order (e.g. --channel_names DAPI GFP RFP).")
    p.add_argument("--only_well", default=None)
    p.add_argument("--only_wells", nargs="*", default=None)
    p.add_argument("--only_grid", type=float, default=None)
    p.add_argument("--only_z", type=float, default=None)
    p.add_argument("--dry_run", action="store_true")
    p.add_argument("--quiet", action="store_true")
    return p.parse_args()


def _cli():
    a = _parse_args()
    blend_args = {
        "placement": a.placement,
        "compression": a.compression,
        "compression_level": a.compression_level,
        "predictor": bool(a.predictor),
        "tile": tuple(a.tile),
        "bigtiff": bool(a.bigtiff),
        "pyramids": bool(a.pyramids),
        "default_overlap_fraction": float(a.default_overlap_fraction),
        "min_feather_px": int(a.min_feather_px),
        "max_feather_px": int(a.max_feather_px),
        "max_snap_px": int(a.max_snap_px),
        "lattice_snap_px": int(a.lattice_snap_px),
        "snap_axis": a.snap_axis,
        "refine_alignment": bool(a.refine_alignment),
        "refine_max_shift_px": int(a.refine_max_shift_px),
        "refine_axis": a.refine_axis,
        "overlap_gain_match": bool(a.overlap_gain_match),
        "gain_clip_low": float(a.gain_clip_low),
        "gain_clip_high": float(a.gain_clip_high),
        "gain_match_channels": a.gain_match_channels,
    }
    run_main(a.meta_csv, a.image_root, a.output_dir, blend_args=blend_args,
              only_well=a.only_well, only_wells=a.only_wells, only_grid=a.only_grid, dry_run=a.dry_run,
              stitch_backend=a.stitch_backend, only_z=a.only_z, channel_names=a.channel_names,
              xml_file=a.xml_file, overlap_fraction=a.overlap_fraction, verbose=not a.quiet)


if __name__ == "__main__":
    _cli()
