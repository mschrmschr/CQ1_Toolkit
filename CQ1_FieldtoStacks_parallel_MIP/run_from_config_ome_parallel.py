# file: run_from_config_ome_parallel.py
"""CLI entry point: run a batch of OME-TIFF assembly jobs from a JSON config, then auto-generate MIPs.

Reads a ``jobs_ome.json``-style config containing a top-level ``jobs`` array
(each entry either providing a ``base_path`` that `_inject_paths` expands
into ``ome_xml_path``/``base_dir``/``save_dir``, or supplying those directly)
plus optional ``mip_defaults``. For each job it:

  1. Validates the job dict into a `JobConfig` (`_validate_job`) and calls
     `ome_xml_assembler.assemble_one_job` to write the OME-TIFF stacks.
  2. Unless `--no-mip` is passed (or the job sets ``"generate_mip": false``),
     builds a MIP job dict (`_build_mip_job`, merging computed defaults <
     ``mip_defaults`` < the job's own ``"mip"`` overrides) and runs
     `mip_plotter.run_mip_job` against the freshly written stacks.

Failures in one job (assembly or MIP generation) are caught, logged, and
skipped so the remaining jobs in the batch still run.

Resuming a crashed run: each field/well stack is written atomically and
skipped on a later run if its output file already exists, so simply
re-running the same command picks up where a crashed/killed run left off
(see `JobConfig.overwrite`). Pass ``--overwrite`` (or set ``"overwrite":
true`` on a job) to force a full rewrite instead.

Usage: ``python run_from_config_ome_parallel.py [--config path/to/jobs.json] [--no-mip] [--overwrite]``
(defaults to ``jobs_ome.json`` next to this script).
"""

from __future__ import annotations
import argparse, json, sys
from pathlib import Path
from typing import Any, Dict, Optional
from ome_xml_assembler import JobConfig, assemble_one_job  # uses the patched assembler
from mip_plotter import run_mip_job

def _lower_process_priority() -> None:
    """Drop this process to below-normal OS priority so the batch stays CPU-hungry
    without starving the foreground apps you're actively using.

    Windows only gives child processes (e.g. the ProcessPoolExecutor workers spawned
    for MIP generation) this same below-normal class automatically, since CreateProcess
    inherits IDLE/BELOW_NORMAL priority classes from the parent when none is specified.
    No-op on non-Windows platforms.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
        handle = ctypes.windll.kernel32.GetCurrentProcess()
        ctypes.windll.kernel32.SetPriorityClass(handle, BELOW_NORMAL_PRIORITY_CLASS)
    except Exception:
        pass

def _load_config(path: Path) -> Dict[str, Any]:
    """Read and JSON-parse `path`, raising if it's missing or not a top-level object."""
    if not path.exists():
        raise FileNotFoundError(f"Config not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Top-level config must be a JSON object.")
    return data

def _inject_paths(d: Dict[str, Any]) -> Dict[str, Any]:
    """If `d` has a truthy ``base_path``, derive default ome_xml_path/base_dir/save_dir under it.

    Derived paths only fill in keys not already present in `d` (``setdefault``),
    so an explicit ``ome_xml_path``/``base_dir``/``save_dir`` in the job always
    wins. Returns `d` unchanged if no ``base_path`` is set.
    """
    if "base_path" in d and d["base_path"]:
        base = Path(str(d["base_path"]))
        out = dict(d)
        out.setdefault("ome_xml_path", str(base / "MeasurementResult.ome.xml"))
        # Keep "Image" to match dataset naming; flat mode works even if it contains files directly.
        out.setdefault("base_dir",     str(base / "Image"))
        out.setdefault("save_dir",     str(base / "Stacks"))
        return out
    return d

def _check_no_placeholders(value: str, key: str) -> None:
    """Raise ValueError if `value` (the resolved config field `key`) still contains an unexpanded ``{...}`` placeholder."""
    if isinstance(value, str) and ("{" in value or "}" in value):
        raise ValueError(
            f"Unexpanded placeholder detected in '{key}': {value}\n"
            "Add 'base_path' and let the script derive paths."
        )

def _validate_job(d: Dict[str, Any]) -> JobConfig:
    """Validate one raw job dict (after `_inject_paths`) and convert it into a `JobConfig`.

    Checks required keys are present, paths have no leftover placeholders and
    exist on disk, and `channels`/`channel_names` are equal length. Raises
    ValueError/FileNotFoundError on any violation.
    """
    required = ["ome_xml_path", "base_dir", "save_dir", "channels", "channel_names"]
    for k in required:
        if k not in d:
            raise ValueError(f"Missing required key: {k}")

    ome_xml_path = str(d["ome_xml_path"])
    base_dir     = str(d["base_dir"])
    save_dir     = str(d["save_dir"])
    _check_no_placeholders(ome_xml_path, "ome_xml_path")
    _check_no_placeholders(base_dir,     "base_dir")
    _check_no_placeholders(save_dir,     "save_dir")

    if not Path(ome_xml_path).exists():
        raise FileNotFoundError(f"ome_xml_path not found: {ome_xml_path}")
    if not Path(base_dir).exists():
        raise FileNotFoundError(f"base_dir not found: {base_dir}")

    channels      = list(d["channels"])
    channel_names = list(d["channel_names"])
    if len(channels) != len(channel_names):
        raise ValueError("channels and channel_names must have the same length.")

    return JobConfig(
        ome_xml_path=ome_xml_path,
        base_dir=base_dir,
        save_dir=save_dir,
        channels=channels,
        channel_names=channel_names,
        field_start=int(d.get("field_start", 1)),
        field_end=int(d["field_end"]) if d.get("field_end") is not None else None,
        fields=[int(f) for f in d["fields"]] if d.get("fields") is not None else None,
        field_strategy=str(d.get("field_strategy", "filesystem")),
        enforce_equal_z=bool(d.get("enforce_equal_z", True)),
        dtype=str(d["dtype"]) if d.get("dtype") is not None else None,
        split_by_w_within_field=bool(d.get("split_by_w_within_field", False)),
        only_well=int(d["only_well"]) if d.get("only_well") is not None else None,
        overwrite=bool(d.get("overwrite", False)),
    )

def _default_mip_output_dir(raw_job: Dict[str, Any], job_cfg: JobConfig) -> str:
    """Return the default MIP output directory: ``base_path/MIP`` if set, else a sibling of `job_cfg.save_dir` named "MIP"."""
    if raw_job.get("base_path"):
        return str(Path(str(raw_job["base_path"])) / "MIP")
    return str(Path(job_cfg.save_dir).parent / "MIP")


def _build_mip_job(raw_job: Dict[str, Any], job_cfg: JobConfig, mip_defaults: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Merge (lowest to highest priority): computed defaults < mip_defaults < per-job 'mip'.

    "parallel" (bool, default True) gates the "workers" count: set it to False
    in mip_defaults or a job's "mip" block to force single-process MIP generation
    (workers=1) regardless of the configured "workers" value, e.g. to keep CPU
    usage down.
    """
    mip_job: Dict[str, Any] = dict(mip_defaults or {})
    mip_job.update(raw_job.get("mip") or {})
    mip_job.setdefault("stacks_dir", job_cfg.save_dir)
    mip_job.setdefault("output_dir", _default_mip_output_dir(raw_job, job_cfg))
    mip_job.setdefault("channel_names", job_cfg.channel_names)
    mip_job.setdefault("workers", 1)
    mip_job.setdefault("parallel", True)
    # run_mip_job() scans the *entire* stacks_dir, not just what this job just
    # assembled -- so on a repeat run scoped to a different well (only_well set,
    # e.g. to give each well its own channel_names), an unfiltered MIP step would
    # re-process every well's stacks ever assembled into this folder and relabel
    # them all with *this* run's channel_names. Default include_keywords to the
    # well token so a scoped run only touches its own well's files, unless the
    # job already set its own include_keywords explicitly.
    if mip_job.get("include_keywords") is None and job_cfg.only_well is not None:
        mip_job["include_keywords"] = [f"W{job_cfg.only_well:04d}"]
    return mip_job


def _run_mip_for_job(raw_job: Dict[str, Any], job_cfg: JobConfig, mip_defaults: Optional[Dict[str, Any]]) -> None:
    """Build the merged MIP job for `raw_job`/`job_cfg` and run it via `mip_plotter.run_mip_job`, printing a summary."""
    mip_job = _build_mip_job(raw_job, job_cfg, mip_defaults)
    # Always single-process: this build ships as a frozen PyInstaller exe, where
    # ProcessPoolExecutor workers are the most common source of relaunch-loop /
    # extra-spawned-exe headaches on Windows. "workers"/"parallel" config keys
    # are ignored here on purpose.
    mip_job.pop("workers", None)
    mip_job.pop("parallel", None)
    workers = 1
    print(f"\n🔬 Generating MIPs: {mip_job['stacks_dir']} -> {mip_job['output_dir']}")
    summary = run_mip_job(mip_job, workers=workers)
    print(f"🖼️ MIP: wrote {summary['written']} file(s), {len(summary['warnings'])} warning(s).")
    for w in summary["warnings"]:
        print("   ⚠️", w)


def main() -> None:
    """Parse CLI args, load the job config, and run each job's stack assembly followed by MIP generation."""
    default_config = Path(__file__).parent / "jobs_ome.json"
    parser = argparse.ArgumentParser(description="Assemble OME-TIFF stacks from OME-XML using a JSON config.")
    parser.add_argument("--config", type=Path, default=default_config)
    parser.add_argument("--no-mip", action="store_true", help="Skip automatic MIP generation for all jobs.")
    parser.add_argument("--overwrite", action="store_true",
                         help="Rewrite every field/well stack even if its output file already exists "
                              "(default: skip existing outputs and resume from where a crashed/killed run left off).")
    args = parser.parse_args()

    _lower_process_priority()

    cfg = _load_config(args.config)
    jobs = cfg.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        raise ValueError("Config must contain a non-empty 'jobs' array.")
    mip_defaults = cfg.get("mip_defaults")

    for idx, j in enumerate(jobs, start=1):
        print(f"\n=== Job {idx}/{len(jobs)} ===")
        j = _inject_paths(j)
        job_cfg = _validate_job(j)
        if args.overwrite:
            job_cfg.overwrite = True
        try:
            outputs = assemble_one_job(job_cfg)
            print(f"🧾 Wrote {len(outputs)} file(s).")
        except Exception as e:
            print(f"❌ Job {idx} failed: {e}")
            continue

        if outputs and not args.no_mip and bool(j.get("generate_mip", True)):
            try:
                _run_mip_for_job(j, job_cfg, mip_defaults)
            except Exception as e:
                print(f"❌ MIP generation failed for job {idx}: {e}")
    print("\n🎉 All jobs complete.")

if __name__ == "__main__":
    main()
