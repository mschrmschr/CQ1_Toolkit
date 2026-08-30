#!/usr/bin/env python3
"""Batch runner for multiple CQ1 wholemount datasets via jobs.json:
stitch each (Well, Grid) mosaic into a FIJI-compatible OME-TIFF, then write a
colorized per-channel MIP panel PNG alongside it."""
from __future__ import annotations

import json
import os
import re
import sys
from typing import Any, Dict, List, Optional

from main import run_main            # returns list[str] of written OME-TIFFs
from mip_plotter import run_mip_job  # writes the colorized MIP panel PNGs


def _resolve(base: str, maybe_rel: str) -> str:
    if not maybe_rel:
        return ""
    return maybe_rel if os.path.isabs(maybe_rel) else os.path.join(base, maybe_rel)


def _normalize_mip_out(mip_out_value: Optional[str], stitched_dir: str) -> str:
    """Resolve the MIP output directory so it works whether the user passes
    'mip' (preferred), a nested relative path, or an absolute path."""
    stitched_dir_norm = os.path.normpath(stitched_dir)
    if not mip_out_value:
        return os.path.join(stitched_dir_norm, "mip")
    if os.path.isabs(mip_out_value):
        return os.path.normpath(mip_out_value)
    return os.path.join(stitched_dir_norm, os.path.normpath(mip_out_value))


def run_job(job: Dict[str, Any]) -> None:
    root = job.get("root_dir", "")
    if not root:
        raise ValueError("Job is missing 'root_dir'.")

    xml_file = _resolve(root, job.get("xml_file", "MeasurementResult.ome.xml"))
    image_dir = _resolve(root, job.get("image_dir", "Image"))
    meta_csv = _resolve(root, job.get("meta_csv", "image_metadata_with_stage_and_grid.csv"))
    output_dir = _resolve(root, job.get("output_dir", "stitched_output"))
    os.makedirs(output_dir, exist_ok=True)

    stitch_backend = str(job.get("stitch_backend", "seamless")).lower()
    placement = str(job.get("placement", "stage")).lower()

    only_well = job.get("only_well")
    only_grid = job.get("only_grid")
    only_z = job.get("only_z")
    dry_run = bool(job.get("dry_run", False))
    channel_names = job.get("channel_names")

    overlap_fraction = float(job.get("overlap_fraction", 0.01))
    compression = job.get("compression", "zlib")
    if isinstance(compression, str) and compression.lower() in ("none", "null"):
        compression = None
    compression_level = int(job.get("compression_level", 6))
    predictor = bool(job.get("predictor", True))
    tile = tuple(job.get("tile", [512, 512]))
    bigtiff = bool(job.get("bigtiff", True))
    pyramids = bool(job.get("pyramids", False))

    min_feather_px = int(job.get("min_feather_px", 4))
    max_feather_px = int(job.get("max_feather_px", 16))
    max_snap_px = int(job.get("max_snap_px", 6))
    lattice_snap_px = int(job.get("lattice_snap_px", 0))
    snap_axis = str(job.get("snap_axis", "both"))

    refine_alignment = bool(job.get("refine_alignment", False))
    refine_max_shift_px = int(job.get("refine_max_shift_px", 6))
    refine_axis = str(job.get("refine_axis", "both"))

    overlap_gain_match = bool(job.get("overlap_gain_match", False))
    gain_clip_low = float(job.get("gain_clip_low", 0.92))
    gain_clip_high = float(job.get("gain_clip_high", 1.08))
    gain_match_channels = job.get("gain_match_channels")

    print("\n=== Running job ===")
    print(f"ROOT_DIR:       {root}")
    print(f"XML_FILE:       {xml_file}")
    print(f"IMAGE_DIR:      {image_dir}")
    print(f"META_CSV:       {meta_csv}")
    print(f"OUTPUT_DIR:     {output_dir}")
    print(f"STITCH_BACKEND: {stitch_backend}")
    print(f"PLACEMENT:      {placement}")

    blend_args = {
        "placement": placement,
        "compression": compression,
        "compression_level": compression_level,
        "predictor": predictor,
        "tile": tile,
        "bigtiff": bigtiff,
        "pyramids": pyramids,
        "default_overlap_fraction": overlap_fraction,
        "min_feather_px": min_feather_px,
        "max_feather_px": max_feather_px,
        "max_snap_px": max_snap_px,
        "lattice_snap_px": lattice_snap_px,
        "snap_axis": snap_axis,
        "refine_alignment": refine_alignment,
        "refine_max_shift_px": refine_max_shift_px,
        "refine_axis": refine_axis,
        "overlap_gain_match": overlap_gain_match,
        "gain_clip_low": gain_clip_low,
        "gain_clip_high": gain_clip_high,
        "gain_match_channels": gain_match_channels,
    }

    # ---- Stitch ----
    produced: List[str] = run_main(
        meta_csv=meta_csv,
        image_root=image_dir,
        output_dir=output_dir,
        blend_args=blend_args,
        only_well=only_well,
        only_grid=only_grid,
        only_z=only_z,
        dry_run=dry_run,
        stitch_backend=stitch_backend,
        xml_file=xml_file,
        overlap_fraction=overlap_fraction,
        channel_names=channel_names,
        verbose=True,
    )

    # ---- MIP panel PNG (always, if something was stitched and not a dry run) ----
    if produced and not dry_run:
        mip_cfg = dict(job.get("mip", {}))  # optional override block in jobs.json

        mip_out = _normalize_mip_out(mip_cfg.get("output_dir"), output_dir)
        os.makedirs(mip_out, exist_ok=True)

        # run_mip_job() scans the *entire* stacks_dir, not just what this job just
        # produced -- so on a repeat run scoped to a different well (only_well set,
        # e.g. to give each well its own channel_names), an unfiltered MIP step would
        # re-process every well's files ever stitched into this folder and relabel
        # them all with *this* run's channel_names. Default include_keywords to the
        # well token so a scoped run only touches its own well's files, unless the
        # job already set its own include_keywords explicitly.
        include_keywords = mip_cfg.get("include_keywords")
        if include_keywords is None and only_well:
            include_keywords = [str(only_well).replace("/", "_")]

        mip_job = {
            "stacks_dir": output_dir,
            "output_dir": mip_out,
            "channel_names": mip_cfg.get("channel_names", channel_names),
            "channel_colors": mip_cfg.get("channel_colors"),
            "projection": mip_cfg.get("projection", "max"),
            "z_range": mip_cfg.get("z_range"),
            "preprocess": mip_cfg.get("preprocess", {"gain_match": False}),
            "norm": mip_cfg.get("norm", {"p_low": 2.0, "p_high": 99.8, "clip": True}),
            "include_keywords": include_keywords,
            "exclude_keywords": mip_cfg.get("exclude_keywords"),
        }

        print(f"\n=== Running MIP panel PNG on {len(produced)} stitched file(s) -> {mip_out} ===")
        summary = run_mip_job(mip_job)
        print(f"MIP wrote outputs for {summary.get('written', 0)} file(s)")
        for w in summary.get("warnings", []):
            print("MIP warning:", w)


def _discover_manifest() -> str:
    here = os.path.dirname(__file__)
    candidates = [
        os.path.join(here, "jobs.json"),
        os.path.join(here, "jobs.jsonc"),
        os.path.join(here, "jobs", "jobs.json"),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    for name in os.listdir(here):
        if name.lower().endswith(".json"):
            path = os.path.join(here, name)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    first = f.read(200).lstrip()
                if first.startswith("["):
                    return path
            except Exception:
                pass
    raise FileNotFoundError("No jobs.json found next to run_jobs.py")


def main():
    jobs_path = sys.argv[1] if len(sys.argv) >= 2 else _discover_manifest()
    with open(jobs_path, "r", encoding="utf-8") as f:
        txt = f.read()
    if "//" in txt:
        txt = re.sub(r"//.*", "", txt)
    jobs = json.loads(txt)
    if not isinstance(jobs, list):
        raise ValueError("Manifest must be a JSON array of job objects.")

    for i, job in enumerate(jobs, 1):
        print(f"\n##### JOB {i}/{len(jobs)} #####")
        run_job(job)
    print("\nAll jobs complete.")


if __name__ == "__main__":
    main()
