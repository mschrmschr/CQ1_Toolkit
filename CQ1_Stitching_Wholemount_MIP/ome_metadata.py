#!/usr/bin/env python3
"""Parse a CQ1 MeasurementResult.ome.xml into a per-tile stitching metadata CSV.

Extracts, per raw tile TIFF: WellID/GridIndex/FieldIndex (from the CQ1 ImageName),
Z/C plane indices, stage position (um), physical pixel size, and both lattice
("_grid") and measured-stage ("_stage") pixel coordinates for placing tiles
during stitching.
"""
from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET

import numpy as np
import pandas as pd

# Shared with generate_metadata_csv's own parse_image_name() below and with
# list_wells() -- factored out so the two can never drift on what counts as
# a valid CQ1 ImageName.
_IMAGE_NAME_RE = re.compile(r"W(\d+)\(R(\d+)C(\d+)\),A(\d+),F(\d+)")


def list_wells(xml_path: str) -> "list[str]":
    """Cheap well enumeration for GUI checklists: a single streaming XML pass
    over Image/@Name only -- no stage-position merge, no pandas, no pixel I/O.
    `generate_metadata_csv` (below) stays the source of truth for the real
    stage/grid geometry the actual stitch needs; this is only for populating
    a "which wells exist" checklist quickly right after Browse."""
    ns = {"ome": "http://www.openmicroscopy.org/Schemas/OME/2013-06"}
    wells: set = set()
    for _, elem in ET.iterparse(xml_path, events=("end",)):
        if elem.tag == f"{{{ns['ome']}}}Image":
            m = _IMAGE_NAME_RE.match(elem.attrib.get("Name", ""))
            if m:
                wells.add(f"W{m.group(1)}")
            elem.clear()
    return sorted(wells)


def _median_step(unique_positions: np.ndarray) -> float:
    """Median positive step among unique sorted positions; NaN if not enough points."""
    vals = np.sort(np.unique(unique_positions.astype(float)))
    if vals.size < 2:
        return np.nan
    diffs = np.diff(vals)
    diffs = diffs[diffs > 0]
    return float(np.median(diffs)) if diffs.size else np.nan


def generate_metadata_csv(xml_path: str, output_csv: str, overlap_fraction: float = 0.20) -> None:
    """Parse CQ1 OME-XML into a stitching CSV with *measured* per-grid pixel steps.

    overlap_fraction is only used as a fallback when the step cannot be measured
    from the stage positions themselves.
    """
    ns = {"ome": "http://www.openmicroscopy.org/Schemas/OME/2013-06"}
    tree = ET.parse(xml_path)
    root = tree.getroot()

    # ---- 1) Frame manifest: file, Z/C, sizes, physical size, channel names ----
    manifest = []
    for img in root.findall("ome:Image", ns):
        image_id = img.attrib["ID"]
        image_name = img.attrib.get("Name", "")
        pixels = img.find("ome:Pixels", ns)
        if pixels is None:
            continue
        size_x = int(pixels.attrib.get("SizeX", 0))
        size_y = int(pixels.attrib.get("SizeY", 0))
        psx = float(pixels.attrib.get("PhysicalSizeX", 0) or 0)
        psy = float(pixels.attrib.get("PhysicalSizeY", 0) or 0)
        psz = pixels.attrib.get("PhysicalSizeZ")
        psz = float(psz) if psz not in (None, "", "NaN") else np.nan

        channels = pixels.findall("ome:Channel", ns)
        ch_names = [ch.attrib.get("Name", f"Channel_{i + 1}") for i, ch in enumerate(channels)]

        for tiffdata in pixels.findall("ome:TiffData", ns):
            tz = int(tiffdata.attrib.get("FirstZ", 0))
            tc = int(tiffdata.attrib.get("FirstC", 0))
            uuid = tiffdata.find("ome:UUID", ns)
            filename = uuid.attrib.get("FileName") if uuid is not None else None
            manifest.append({
                "ImageID": image_id,
                "ImageName": image_name,
                "Z": tz,
                "C": tc,
                "ChannelName": ch_names[tc] if tc < len(ch_names) else f"Channel_{tc + 1}",
                "TiffFile": filename,
                "SizeX": size_x,
                "SizeY": size_y,
                "PxSizeX_um": psx,
                "PxSizeY_um": psy,
                "PxSizeZ_um": psz,
            })

    manifest_df = pd.DataFrame(manifest)

    # ---- 2) Per-plane stage positions (um) ----
    planes_md = []
    for img in root.findall("ome:Image", ns):
        img_id = img.attrib["ID"]
        for pix in img.findall("ome:Pixels", ns):
            for plane in pix.findall("ome:Plane", ns):
                z = int(plane.attrib.get("TheZ", -1))
                c = int(plane.attrib.get("TheC", -1))
                t = int(plane.attrib.get("TheT", 0))
                posx = plane.attrib.get("PositionX")
                posy = plane.attrib.get("PositionY")
                posz = plane.attrib.get("PositionZ")
                planes_md.append({
                    "ImageID": img_id, "Z": z, "C": c, "T": t,
                    "StageX_um": (float(posx) if posx not in (None, "") else np.nan),
                    "StageY_um": (float(posy) if posy not in (None, "") else np.nan),
                    "StageZ_um": (float(posz) if posz not in (None, "") else np.nan),
                })
    stage_df = pd.DataFrame(planes_md)

    manifest_df["Z"] = manifest_df["Z"].astype(int)
    manifest_df["C"] = manifest_df["C"].astype(int)
    merged = pd.merge(manifest_df, stage_df, on=["ImageID", "Z", "C"], how="left")

    # ---- 3) Parse WellID, GridIndex, FieldIndex from ImageName ----
    def parse_image_name(name):
        m = _IMAGE_NAME_RE.match(str(name))
        if m:
            return f"W{m.group(1)}", int(m.group(4)), int(m.group(5))
        return None, None, None

    merged[["WellID", "GridIndex", "FieldIndex"]] = merged["ImageName"].apply(
        lambda x: pd.Series(parse_image_name(x))
    )
    merged = merged[merged["WellID"].notnull()].copy()

    # ---- 4) Row/col indices from rounded stage positions (stable indexing) ----
    def assign_rowcol(df):
        df = df.copy()
        df["StageX_r"] = np.round(df["StageX_um"], 1)
        df["StageY_r"] = np.round(df["StageY_um"], 1)
        uniq_x = np.sort(df["StageX_r"].dropna().unique())
        uniq_y = np.sort(df["StageY_r"].dropna().unique())
        df["ColIndex"] = df["StageX_r"].map({x: i for i, x in enumerate(uniq_x)})
        df["RowIndex"] = df["StageY_r"].map({y: i for i, y in enumerate(uniq_y)})
        df["GridRows"] = len(uniq_y)
        df["GridCols"] = len(uniq_x)
        return df

    merged = merged.groupby(["WellID", "GridIndex"], group_keys=False).apply(assign_rowcol)

    # ---- 5) Measured pixel steps & lattice pixel coords per (Well, Grid) ----
    all_groups = []
    for (w, g), gdf in merged.groupby(["WellID", "GridIndex"]):
        gdf = gdf.copy()
        tile_w = int(gdf["SizeX"].iloc[0])
        tile_h = int(gdf["SizeY"].iloc[0])

        psx = float(gdf["PxSizeX_um"].iloc[0] or 1.0)
        psy = float(gdf["PxSizeY_um"].iloc[0] or 1.0)

        step_x_um = _median_step(gdf["StageX_um"].values)
        step_y_um = _median_step(gdf["StageY_um"].values)
        step_x_px = step_x_um / psx if np.isfinite(step_x_um) and psx > 0 else np.nan
        step_y_px = step_y_um / psy if np.isfinite(step_y_um) and psy > 0 else np.nan

        if not np.isfinite(step_x_px):
            print(f"[WARNING] StepX could not be measured for Well {w} Grid {g}; "
                  f"falling back to overlap_fraction={overlap_fraction}")
            step_x_px = tile_w * (1.0 - float(overlap_fraction))
        if not np.isfinite(step_y_px):
            print(f"[WARNING] StepY could not be measured for Well {w} Grid {g}; "
                  f"falling back to overlap_fraction={overlap_fraction}")
            step_y_px = tile_h * (1.0 - float(overlap_fraction))

        step_x_i = max(int(round(step_x_px)), 1)
        step_y_i = max(int(round(step_y_px)), 1)

        ovx = np.clip((tile_w - step_x_px) / tile_w, 0.0, 1.0)
        ovy = np.clip((tile_h - step_y_px) / tile_h, 0.0, 1.0)
        gdf["StepX_px"] = step_x_px
        gdf["StepY_px"] = step_y_px
        gdf["OverlapX_frac"] = ovx
        gdf["OverlapY_frac"] = ovy

        gdf["ColIndex"] = gdf["ColIndex"].astype(int)
        gdf["RowIndex"] = gdf["RowIndex"].astype(int)

        # Lattice pixel coordinates (placement="grid"): X left->right, Y inverted (top = small PixelY)
        c0 = int(gdf["ColIndex"].min())
        rmax = int(gdf["RowIndex"].max())
        gdf["PixelX_grid"] = (gdf["ColIndex"] - c0) * step_x_i
        gdf["PixelY_grid"] = (rmax - gdf["RowIndex"]) * step_y_i

        print(f"[{w} G{g}] step_x={step_x_i}px, step_y={step_y_i}px; overlap~=({ovx:.3f},{ovy:.3f})")

        all_groups.append(gdf)

    merged2 = pd.concat(all_groups, ignore_index=True)

    # ---- 6) Normalized stage->pixel positions (origin at 0,0) ----
    merged2["PixelX_stage"] = (
        (merged2["StageX_um"] - merged2.groupby(["WellID", "GridIndex"])["StageX_um"].transform("min"))
        / merged2["PxSizeX_um"]
    ).astype(int)
    merged2["PixelY_stage"] = (
        (merged2.groupby(["WellID", "GridIndex"])["StageY_um"].transform("max") - merged2["StageY_um"])
        / merged2["PxSizeY_um"]
    ).astype(int)

    out_path = os.path.abspath(output_csv)
    merged2.to_csv(out_path, index=False)
    print(f"Generated metadata CSV (measured steps): {out_path}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Generate CQ1 metadata CSV from OME-XML (measured steps)")
    parser.add_argument("xml_path", help="Path to OME-XML file")
    parser.add_argument("output_csv", help="Path to output CSV")
    parser.add_argument("--overlap", type=float, default=0.20, help="Fallback tile overlap fraction")
    args = parser.parse_args()
    generate_metadata_csv(args.xml_path, args.output_csv, overlap_fraction=args.overlap)
