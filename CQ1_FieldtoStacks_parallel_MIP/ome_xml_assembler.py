# file: ome_xml_assembler.py
"""Assemble per-field OME-TIFF Z-stacks from CQ1 output using an OME-XML sidecar for metadata.

A CQ1 acquisition writes one 2D TIFF per (Well, Field, Timepoint, Z, Channel)
combination, either as loose files in a single flat directory (encoding
W/F/T/Z/C in the filename, e.g. ``W0001F0001T0001Z001C1.tif``) or grouped into
``FieldXXXX`` subdirectories. This module discovers those files, groups them
by field (and optionally by well), reads each channel's Z-planes into a numpy
array, stacks channels into a single (Z, C, Y, X) volume, and writes one
multi-channel OME-TIFF per field (or per field/well) via `tifffile`.

Physical voxel size and per-field image names are pulled from the
accompanying ``MeasurementResult.ome.xml`` (`extract_voxel_size`,
`extract_image_names`) and embedded in the output OME-TIFF metadata.

Entry point: `assemble_one_job(cfg)`, driven by a `JobConfig` describing the
source XML/directory, output directory, channel selection, and field
selection strategy. Typically invoked by ``run_from_config_ome_parallel.py``
for each job in a JSON config.
"""
from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from typing import List, Dict, Optional, Tuple, Any, Set
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed

import numpy as np
from lxml import etree
from tifffile import imread, imwrite


# ----------------------------
# Configuration
# ----------------------------

@dataclass
class JobConfig:
    """Describes one assembly job: where the source files/XML are, what to write, and how.

    Attributes:
        ome_xml_path: Path to the ``MeasurementResult.ome.xml`` sidecar used
            for voxel size and per-field image names.
        base_dir: Directory to scan for source TIFFs — either a flat
            directory of W/F/T/Z/C-encoded files, or a directory containing
            ``FieldXXXX`` subdirectories.
        save_dir: Directory to write the assembled OME-TIFF stacks into
            (created if missing).
        channels: Channel tokens to collect, e.g. ``["C1", "C02"]``; accepts
            C1/C01/etc. and is normalized internally via `_norm_channel_token`.
        channel_names: Display names for `channels`, same length and order,
            embedded in the output OME-TIFF metadata.
        field_start: Lowest field number to include (1-based).
        field_end: Highest field number to include, or None for no upper bound.
        fields: Explicit list of field numbers to include; overrides
            `field_start`/`field_end`/`field_strategy` field discovery when set.
        field_strategy: How to discover fields when `fields` is not given:
            'filesystem' (existing FieldXXXX dirs), 'xml' (OME-XML image
            count), 'intersection', or 'union' of the two.
        enforce_equal_z: If True, raise if channels within a group have
            differing Z-plane counts instead of silently allowing it.
        dtype: Force output array dtype (e.g. "uint16"); if None, inferred
            from the first image read.
        split_by_w_within_field: If True, write one OME-TIFF per Well found
            within each field (``F####_W####.ome.tif``) instead of one per
            field.
        only_well: If set, restricts processing to this one Well number
            (matches the ``W####`` filename token, e.g. ``1`` for ``W0001``)
            -- only meaningful when wells are actually distinguished, i.e.
            flat mode or folder mode with `split_by_w_within_field=True`.
            Useful for datasets where different wells were stained
            differently: run once per well (group), with `channel_names`
            set to match, into the same `save_dir` -- output filenames
            already include the well number, so repeated runs don't
            collide.
        only_wells: Same idea as `only_well` but for a set of wells at once
            (e.g. from a GUI checklist); takes priority over `only_well`
            when both are set. Still meant for one `channel_names` group at
            a time -- wells with different staining still need separate
            runs.
        overwrite: If False (default), a field/well whose output OME-TIFF
            already exists in `save_dir` is skipped (its source Z-planes are
            never re-read) instead of being rewritten -- lets a crashed/killed
            run be resumed by simply re-launching the same job. Outputs are
            written atomically (temp file + rename) so a file only exists
            once it's complete; a crash mid-write can't leave a corrupt file
            that gets mistaken for "already done". Set True to force a full
            rewrite of every field/well.
    """

    ome_xml_path: str
    base_dir: str
    save_dir: str
    channels: List[str]              # accepts C1/C01/etc., normalized internally
    channel_names: List[str]

    field_start: int = 1
    field_end: Optional[int] = None
    fields: Optional[List[int]] = None
    field_strategy: str = "filesystem"  # 'filesystem'|'xml'|'intersection'|'union'

    enforce_equal_z: bool = True
    dtype: Optional[str] = None

    # In flat mode: write per Well inside each Field (and also in FieldXXXX mode)
    split_by_w_within_field: bool = False
    only_well: Optional[int] = None
    only_wells: Optional[List[int]] = None
    overwrite: bool = False

    # Parallel field/well assembly: each field (or field+well) is written
    # independently, so this can run across a process pool. `parallel=False`
    # forces serial assembly even when unfrozen (e.g. to keep CPU usage down
    # on a shared machine). `workers=None` auto-picks min(cpu_count, 4) --
    # kept separate from `parallel` so "not set" is distinguishable from an
    # explicit "workers": 1. Ignored (forced to 1 worker) when running as the
    # frozen gui.exe, to avoid ProcessPoolExecutor's known PyInstaller
    # relaunch-loop risk -- see _resolve_assembly_workers.
    parallel: bool = True
    workers: Optional[int] = None


# ----------------------------
# XML helpers
# ----------------------------

def extract_voxel_size(ome_xml_path: str) -> Dict[str, float]:
    """Return the physical voxel size ``{"x", "y", "z"}`` from the first ``Pixels`` element with all three set.

    Raises ValueError if no ``Pixels`` element declares PhysicalSizeX/Y/Z.
    """
    with open(ome_xml_path, "rb") as f:
        tree = etree.parse(f)
    ns = {"ome": "http://www.openmicroscopy.org/Schemas/OME/2013-06"}
    pixels = tree.xpath("//ome:Pixels", namespaces=ns)
    for px in pixels:
        a = px.attrib
        if all(k in a for k in ("PhysicalSizeX", "PhysicalSizeY", "PhysicalSizeZ")):
            return {
                "x": float(a["PhysicalSizeX"]),
                "y": float(a["PhysicalSizeY"]),
                "z": float(a["PhysicalSizeZ"]),
            }
    raise ValueError("No PhysicalSizeX/Y/Z found in OME-XML Pixels.")

def extract_image_names(ome_xml_path: str) -> List[str]:
    """Return the ``Name`` of each ``Image`` element in the OME-XML, in document order.

    The first ``Image`` (position 1) is skipped since CQ1 uses it for a
    whole-well overview rather than a field; the returned list is index-0 ==
    field 1, index-1 == field 2, etc. Falls back to ``Image_{i}`` for any
    element missing a ``Name`` attribute.
    """
    with open(ome_xml_path, "rb") as f:
        tree = etree.parse(f)
    ns = {"ome": "http://www.openmicroscopy.org/Schemas/OME/2013-06"}
    images = tree.xpath("//ome:Image[position()>1]", namespaces=ns)  # skip overview at position 1
    return [img.attrib.get("Name", f"Image_{i}") for i, img in enumerate(images, start=1)]


# ----------------------------
# Channel normalization & FS helpers
# ----------------------------

_C_TOKEN = re.compile(r"(?i)\bC\s*0*(\d+)\b")

def _norm_channel_token(token: str) -> str:
    """Normalize a channel token like "C1", "C01", or "c 001" to canonical form "C1"."""
    m = _C_TOKEN.search(token)
    if not m:
        raise ValueError(f"Invalid channel token: {token}")
    return f"C{int(m.group(1))}"

def _iter_existing_fields(base_dir: str) -> List[int]:
    """Return sorted field numbers for which a ``FieldXXXX`` subdirectory exists under `base_dir`."""
    out: List[int] = []
    if not os.path.isdir(base_dir):
        return out
    for name in os.listdir(base_dir):
        if name.startswith("Field") and len(name) == 9 and os.path.isdir(os.path.join(base_dir, name)):
            try:
                out.append(int(name[-4:]))
            except ValueError:
                pass
    return sorted(out)

def _collect_any_tifs(dir_path: str) -> List[str]:
    """Return sorted full paths of all entries in `dir_path` (unused helper; kept permissive, no real filtering)."""
    if not os.path.isdir(dir_path):
        return []
    files = [f for f in os.listdir(dir_path) if f.lower().endswith((".tif", ".tiff")) or re.search(r"(?i)\.tif{1,2}$", f) or True]  # allow no-ext too
    files.sort()
    # Keep all files; extension may be missing. We'll filter by patterns elsewhere.
    return [os.path.join(dir_path, f) for f in files]

def _collect_tifs_for_channel(dir_path: str, ch_norm: str) -> List[str]:
    """
    Flat non-W mode collector: match suffix ...C#, ...C0# with optional .tif/.tiff, allow no extension.
    """
    if not os.path.isdir(dir_path):
        return []
    m = _C_TOKEN.search(ch_norm)
    ch_num = int(m.group(1)) if m else None
    patt = re.compile(rf"(?i).*C0*{ch_num}(?:\.tif{1,2})?$")
    files = [f for f in os.listdir(dir_path) if patt.match(f)]
    files.sort()
    return [os.path.join(dir_path, f) for f in files]

def _infer_dtype_from_first_path(path: str) -> str:
    """Return the numpy dtype name of the image at `path`, read to sniff its dtype."""
    return str(imread(path).dtype)


# ----------------------------
# Flat scanner: W/F/T/Z/C with optional extension
# ----------------------------

# Example: W0001F0001T0001Z001C1   or   W0001F0001T0001Z001C01.tif
_FLAT_TOKEN = re.compile(
    r"(?i)^.*W(?P<w>\d{4})F(?P<f>\d{4})T(?P<t>\d{4})Z(?P<z>\d{3,4})C0*(?P<c>\d+)(?:\.tif{1,2})?$"
)

def _scan_flat_wfztc(dir_path: str) -> Dict[int, Dict[int, Dict[str, List[Tuple[int, str]]]]]:
    """
    Returns: { F:int -> { W:int -> { C<norm>: [(Z:int, path), ...] } } }
    """
    index: Dict[int, Dict[int, Dict[str, List[Tuple[int, str]]]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(list))
    )
    if not os.path.isdir(dir_path):
        return {}
    for fn in os.listdir(dir_path):
        m = _FLAT_TOKEN.match(fn)
        if not m:
            continue
        f = int(m.group("f"))
        w = int(m.group("w"))
        z = int(m.group("z"))
        c_norm = f"C{int(m.group('c'))}"
        index[f][w][c_norm].append((z, os.path.join(dir_path, fn)))
    # sort by Z
    for f in list(index.keys()):
        for w in list(index[f].keys()):
            for c in list(index[f][w].keys()):
                index[f][w][c].sort(key=lambda t: t[0])
    return index


def list_wells(base_dir: str) -> List[int]:
    """Cheap well enumeration for GUI checklists: os.listdir + regex only, no
    TIFF reads. Mirrors assemble_one_job's own layout detection (flat WFZC
    filenames vs. FieldXXXX subfolders each scanned for WFZC filenames)."""
    if _iter_existing_fields(base_dir):
        wells: Set[int] = set()
        for field in _iter_existing_fields(base_dir):
            field_dir = os.path.join(base_dir, f"Field{field:04d}")
            sub_index = _scan_flat_wfztc(field_dir)
            wells |= {w for wdict in sub_index.values() for w in wdict.keys()}
        return sorted(wells)
    flat_index = _scan_flat_wfztc(base_dir)
    return sorted({w for wdict in flat_index.values() for w in wdict.keys()})


# ----------------------------
# Field selection
# ----------------------------

def _fields_from_xml(image_names: List[str], start: int = 1) -> List[int]:
    """Return `len(image_names)` consecutive field numbers starting at `start`."""
    n = len(image_names)
    return list(range(start, start + n))

def _decide_fields(cfg: JobConfig, image_names: List[str]) -> List[int]:
    """Resolve the final sorted list of field numbers to process for `cfg`.

    Priority: an explicit `cfg.fields` list (filtered by `field_start`/
    `field_end`) wins outright; otherwise an explicit `cfg.field_end` yields a
    plain range from `field_start`; otherwise fields are discovered from the
    filesystem (existing ``FieldXXXX`` dirs) and/or the OME-XML image count,
    combined per `cfg.field_strategy` ('filesystem', 'xml', 'intersection', or
    'union').
    """
    if cfg.fields:
        sel = sorted(int(f) for f in cfg.fields if int(f) >= cfg.field_start)
        return [f for f in sel if cfg.field_end is None or f <= cfg.field_end]

    if cfg.field_end is not None:
        return list(range(cfg.field_start, cfg.field_end + 1))

    fs_fields: Set[int] = set(_iter_existing_fields(cfg.base_dir))
    fs_fields = {f for f in fs_fields if f >= cfg.field_start}
    xml_fields: Set[int] = set(_fields_from_xml(image_names, start=cfg.field_start))

    strat = (cfg.field_strategy or "filesystem").lower()
    if strat == "filesystem":
        chosen = fs_fields
    elif strat == "xml":
        chosen = xml_fields
    elif strat == "intersection":
        chosen = fs_fields & xml_fields
    elif strat == "union":
        chosen = fs_fields | xml_fields
    else:
        raise ValueError(f"Unknown field_strategy '{cfg.field_strategy}'")
    return sorted(chosen)


# ----------------------------
# Assembly
# ----------------------------

def assemble_one_job(cfg: JobConfig) -> List[str]:
    """Assemble OME-TIFF stack(s) for one job described by `cfg` and return the written file paths.

    Reads voxel size and image names from `cfg.ome_xml_path`, then detects the
    source layout under `cfg.base_dir`:

      - Flat mode (no ``FieldXXXX`` subdirs): if filenames carry W/F/T/Z/C
        tokens, delegates to `_assemble_flat_wfztc`, writing one OME-TIFF per
        field (or per field+well if `cfg.split_by_w_within_field`). Otherwise
        falls back to a simple per-channel filename collector and writes a
        single stack for `cfg.field_start`.
      - Folder mode (``FieldXXXX`` subdirs present): iterates the fields
        chosen by `_decide_fields`, collecting per-channel files within each
        field directory (optionally split per well via a nested WFZC scan)
        and writing one OME-TIFF per field (or per field+well).

    Fields/wells missing one or more requested channels are skipped with a
    warning rather than aborting the whole job.
    """
    os.makedirs(cfg.save_dir, exist_ok=True)
    if len(cfg.channels) != len(cfg.channel_names):
        raise ValueError("channels and channel_names must have identical length.")

    cfg_channels_norm = [_norm_channel_token(ch) for ch in cfg.channels]

    voxel = extract_voxel_size(cfg.ome_xml_path)
    image_names = extract_image_names(cfg.ome_xml_path)
    print(f"✅ Voxel size: {voxel}")
    print(f"✅ {len(image_names)} image names from OME-XML (overview skipped)")

    # Detect layout
    has_field_dirs = len(_iter_existing_fields(cfg.base_dir)) > 0
    # Flat if there are files matching our flat token OR any tif-like entries
    flat_index = _scan_flat_wfztc(cfg.base_dir)
    has_flat_pattern = bool(flat_index)
    has_any_files = len(os.listdir(cfg.base_dir)) > 0
    flat_mode = (not has_field_dirs) and (has_flat_pattern or has_any_files)

    if flat_mode:
        label = f"Flat@{os.path.basename(cfg.base_dir) or cfg.base_dir}"
        print(f"\n📦 Processing FLAT directory mode: {label}")

        # Prefer WFZC pattern if present
        if has_flat_pattern:
            return _assemble_flat_wfztc(cfg, cfg_channels_norm, voxel, image_names, flat_index)

        # Fallback: legacy simple per-channel collector (no W/F/T/Z tokens)
        print("ℹ️ No WFZC tokens detected; using simple per-channel collector.")
        collected_paths_per_channel: List[List[str]] = []
        for ch_norm in cfg_channels_norm:
            paths = _collect_tifs_for_channel(cfg.base_dir, ch_norm)
            if not paths:
                print(f"⚠️ Missing files for {ch_norm} in flat directory.")
                print("🧾 Wrote 0 file(s).")
                return []
            collected_paths_per_channel.append(paths)

        field = int(cfg.field_start)
        out_path = os.path.join(cfg.save_dir, f"F{field:04d}.ome.tif")
        workers = _resolve_assembly_workers(cfg)
        return _write_groups(
            [(collected_paths_per_channel, _safe_image_name(image_names, field), out_path, f"F{field:04d}")],
            cfg, voxel, workers,
        )

    # Folder (FieldXXXX) mode — original behavior
    fields = _decide_fields(cfg, image_names)
    if not fields:
        print("ℹ️ No fields selected.")
        print("🧾 Wrote 0 file(s).")
        return []

    groups: List[Tuple[List[List[str]], str, str, str]] = []
    for field in fields:
        field_dir = os.path.join(cfg.base_dir, f"Field{field:04d}")
        label = f"Field{field:04d}"
        print(f"\n📦 Processing {label}")

        if not cfg.split_by_w_within_field:
            collected_paths_per_channel: List[List[str]] = []
            for ch_norm in cfg_channels_norm:
                paths = _collect_tifs_for_channel(field_dir, ch_norm)
                if not paths:
                    print(f"⚠️ Missing files for {ch_norm} in {label}, skipping this field.")
                    break
                collected_paths_per_channel.append(paths)
            else:
                groups.append((
                    collected_paths_per_channel,
                    _safe_image_name(image_names, field),
                    os.path.join(cfg.save_dir, f"F{field:04d}.ome.tif"),
                    label,
                ))
            continue

        # Split by W#### within field directories: reuse flat WFZC scanner on the subfolder
        sub_index = _scan_flat_wfztc(field_dir)
        if not sub_index:
            print(f"ℹ️ No WFZC groups detected in {label}; skipping.")
            continue

        wells = sorted({w for wdict in sub_index.values() for w in wdict.keys()})
        if cfg.only_wells is not None:
            wanted = set(cfg.only_wells)
            wells = [w for w in wells if w in wanted]
        elif cfg.only_well is not None:
            wells = [w for w in wells if w == cfg.only_well]
        print(f"🧪 Wells in {label}: {wells if len(wells)<=20 else str(wells[:10])+' ...'}")

        for w in wells:
            per_channel_paths: List[List[str]] = []
            for ch_norm in cfg_channels_norm:
                # gather all Z from the single field 'field' and well 'w'
                z_paths = sub_index.get(field, {}).get(w, {}).get(ch_norm, [])
                paths = [p for _z, p in z_paths]
                if not paths:
                    print(f"    ⚠️ Missing {ch_norm} for {label} W{w:04d}, skipping this well.")
                    break
                per_channel_paths.append(paths)
            else:
                out = os.path.join(cfg.save_dir, f"F{field:04d}_W{w:04d}.ome.tif")
                name = f"{_safe_image_name(image_names, field)}_W{w:04d}"
                groups.append((per_channel_paths, name, out, f"F{field:04d}_W{w:04d}"))

    workers = _resolve_assembly_workers(cfg)
    return _write_groups(groups, cfg, voxel, workers)


def _assemble_flat_wfztc(
    cfg: JobConfig,
    cfg_channels_norm: List[str],
    voxel: Dict[str, float],
    image_names: List[str],
    index: Dict[int, Dict[int, Dict[str, List[Tuple[int, str]]]]],
) -> List[str]:
    """
    Assemble from flat WFZC pattern: {F -> W -> C -> [(Z, path)]}
    """
    groups: List[Tuple[List[List[str]], str, str, str]] = []
    fields = sorted(index.keys())
    print(f"🗂  Fields detected in flat dir: {fields if len(fields)<=20 else str(fields[:10])+' ...'}")

    for fi, field in enumerate(fields, start=1):
        wells = sorted(index[field].keys())
        if cfg.only_wells is not None:
            wanted = set(cfg.only_wells)
            wells = [w for w in wells if w in wanted]
        elif cfg.only_well is not None:
            wells = [w for w in wells if w == cfg.only_well]
        if not wells:
            print(f"ℹ️ Field {field:04d}: no wells found, skipping.")
            continue

        # If not splitting by W and multiple wells exist: pick smallest and warn (proceed anyway)
        if not cfg.split_by_w_within_field and len(wells) > 1:
            print(f"⚠️ Field {field:04d} has multiple wells {wells}, but split_by_w_within_field=False. "
                  f"Proceeding with W{wells[0]:04d} only.")

        w_iter = wells if cfg.split_by_w_within_field else [wells[0]]

        for wi, w in enumerate(w_iter, start=1):
            print(f"  → Field F{field:04d} Well W{w:04d} [{wi}/{len(w_iter)}]")
            per_channel_paths: List[List[str]] = []
            for ch_norm in cfg_channels_norm:
                z_paths = index[field][w].get(ch_norm, [])
                paths = [p for _z, p in z_paths]
                if not paths:
                    print(f"    ⚠️ Missing {ch_norm} for F{field:04d} W{w:04d}, skipping.")
                    break
                per_channel_paths.append(paths)
            else:
                out = (os.path.join(cfg.save_dir, f"F{field:04d}_W{w:04d}.ome.tif")
                       if cfg.split_by_w_within_field
                       else os.path.join(cfg.save_dir, f"F{field:04d}.ome.tif"))
                name = (f"{_safe_image_name(image_names, field)}_W{w:04d}"
                        if cfg.split_by_w_within_field
                        else _safe_image_name(image_names, field))
                groups.append((per_channel_paths, name, out, f"F{field:04d}_W{w:04d}"))

    workers = _resolve_assembly_workers(cfg)
    written = _write_groups(groups, cfg, voxel, workers)

    if not written:
        print("ℹ️ No outputs written (likely channel mismatch). "
              "Check 'channels' in config match the C# present in filenames.")
    else:
        print(f"🧾 Wrote {len(written)} file(s).")
    return written


# ----------------------------
# Assembly helper
# ----------------------------

def _safe_image_name(image_names: List[str], field: int) -> str:
    """Return the OME-XML image name for 1-based `field`, or ``"Field{field:04d}"`` if out of range."""
    return image_names[field - 1] if 0 <= (field - 1) < len(image_names) else f"Field{field:04d}"


def _resolve_assembly_workers(cfg: "JobConfig") -> int:
    """Return how many worker processes `_write_groups` should use for `cfg`.

    Always 1 when running as the frozen gui.exe (`sys.frozen`), regardless of
    `cfg.parallel`/`cfg.workers` -- avoids ProcessPoolExecutor under
    PyInstaller (see EXE_PACKAGING_PLAN.md). Otherwise 1 if `cfg.parallel` is
    False, `cfg.workers` if explicitly set, else min(cpu_count, 4).
    """
    if getattr(sys, "frozen", False):
        return 1
    if not cfg.parallel:
        return 1
    if cfg.workers:
        return max(1, int(cfg.workers))
    return max(1, min(os.cpu_count() or 1, 4))


def _write_group_job(
    collected_paths_per_channel: List[List[str]],
    cfg: "JobConfig",
    voxel: Dict[str, float],
    image_name: str,
    out_path: str,
    label: str,
) -> Dict[str, Any]:
    """Worker-process entry point: run `_write_stack_for_group`, converting any exception into a result dict instead of raising across the pool boundary."""
    try:
        written = _write_stack_for_group(collected_paths_per_channel, cfg, voxel, image_name, out_path)
        return {"label": label, "written": written, "error": None}
    except Exception as e:
        return {"label": label, "written": [], "error": str(e)}


def _write_groups(
    groups: List[Tuple[List[List[str]], str, str, str]],
    cfg: "JobConfig",
    voxel: Dict[str, float],
    workers: int,
) -> List[str]:
    """Write a batch of independent field/well groups, in parallel when `workers` > 1.

    Each group is `(collected_paths_per_channel, image_name, out_path, label)`.
    Groups whose `out_path` already exists (and `cfg.overwrite` is False) are
    filtered out up front -- printed and skipped -- before any process pool is
    created, so a resumed/mostly-done run pays no pool overhead. Falls back to
    the plain serial call for `workers <= 1` or a single remaining group, so
    the frozen/serial path stays byte-for-byte identical to calling
    `_write_stack_for_group` directly.
    """
    written: List[str] = []
    to_write: List[Tuple[List[List[str]], str, str, str]] = []
    for paths, name, out_path, label in groups:
        if os.path.exists(out_path) and not cfg.overwrite:
            print(f"⏭️  Skipping existing: {out_path}")
            written.append(out_path)
        else:
            to_write.append((paths, name, out_path, label))

    if not to_write:
        return written

    if workers <= 1 or len(to_write) == 1:
        for paths, name, out_path, label in to_write:
            written.extend(_write_stack_for_group(paths, cfg, voxel, name, out_path))
        return written

    n_workers = min(workers, len(to_write))
    print(f"🧵 Parallel assembly: workers={n_workers}, groups={len(to_write)}")
    with ProcessPoolExecutor(max_workers=n_workers) as ex:
        futures = {
            ex.submit(_write_group_job, paths, cfg, voxel, name, out_path, label): label
            for paths, name, out_path, label in to_write
        }
        for fut in as_completed(futures):
            res = fut.result()
            if res["error"]:
                print(f"❌ {res['label']} failed: {res['error']}")
            else:
                written.extend(res["written"])
    return written


def _write_stack_for_group(
    collected_paths_per_channel: List[List[str]],
    cfg: JobConfig,
    voxel: Dict[str, float],
    image_name: str,
    out_path: str,
) -> List[str]:
    """Writes one OME-TIFF for the provided [paths per channel].

    Skips the (expensive) read/write entirely if `out_path` already exists
    and `cfg.overwrite` is False -- see `JobConfig.overwrite` for the resume
    behavior this enables.
    """
    if os.path.exists(out_path) and not cfg.overwrite:
        print(f"⏭️  Skipping existing: {out_path}")
        return [out_path]

    channel_stacks: List[np.ndarray] = []
    z_count: Optional[int] = None

    forced_dtype = np.dtype(cfg.dtype) if cfg.dtype is not None else None
    if forced_dtype is None:
        forced_dtype = np.dtype(_infer_dtype_from_first_path(collected_paths_per_channel[0][0]))

    for i, paths in enumerate(collected_paths_per_channel):
        z_planes = [imread(p) for p in paths]
        stack = np.stack(z_planes, axis=0)

        if z_count is None:
            z_count = stack.shape[0]
        elif cfg.enforce_equal_z and stack.shape[0] != z_count:
            raise ValueError(
                f"Inconsistent Z: expected {z_count}, got {stack.shape[0]} for channel index {i}"
            )

        if stack.dtype != forced_dtype:
            stack = stack.astype(forced_dtype, copy=False)
        channel_stacks.append(stack)

    if not channel_stacks:
        return []

    combined = np.stack(channel_stacks, axis=1)  # [Z, C, Y, X]
    z, c, y, x = combined.shape

    channel_names = [
        cfg.channel_names[i] if i < len(cfg.channel_names) else f"Channel{i}"
        for i in range(c)
    ]

    # Let tifffile generate the OME-XML itself (from the real array layout) instead of
    # passing a hand-built `description`: tifffile forces ome=False whenever a `description`
    # is given, so any manually assembled OME-XML would be silently discarded and replaced
    # with a generic "shaped" description that Bio-Formats/Fiji cannot build a hyperstack from.
    # Write to a temp file first and rename into place once complete, so a
    # crash mid-write never leaves a partial file at `out_path` that a later
    # resume run would mistake for "already done".
    tmp_path = out_path + ".part"
    imwrite(
        tmp_path,
        combined,
        photometric="minisblack",
        ome=True,  # tifffile normally infers this from the ".ome.tif" filename suffix,
                   # which the ".part" temp name doesn't have -- force it so Bio-Formats/Fiji
                   # gets real OME-XML instead of tifffile's fallback "shaped" JSON metadata.
        metadata={
            "axes": "ZCYX",
            "Name": image_name,
            "PhysicalSizeX": voxel["x"],
            "PhysicalSizeY": voxel["y"],
            "PhysicalSizeZ": voxel["z"],
            "Channel": {"Name": channel_names},
        },
    )
    os.replace(tmp_path, out_path)
    print(f"✅ Saved: {out_path}")
    return [out_path]
