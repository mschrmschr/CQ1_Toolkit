# CQ1 Toolkit

Two standalone Windows tools for processing Yokogawa CQ1 microscope
acquisitions, packaged behind one "Fiji-style" launcher: unzip, double-click,
pick a tool, no Python or admin rights needed on the machine running it.

- **Field → Stacks + MIP** (`CQ1_FieldtoStacks_parallel_MIP/`) — assembles
  raw per-field CQ1 tile images into per-well/per-field multi-channel
  OME-TIFF stacks, then writes a max-intensity-projection (MIP) overview.
- **Stitching + MIP** (`CQ1_Stitching_Wholemount_MIP/`) — stitches tiled
  CQ1 wholemount acquisitions into one image per well/grid, then writes the
  same kind of MIP overview.
- **`CQ1_Toolkit_Launcher/`** — a tiny Tkinter launcher with no import
  dependency on either tool; it just starts each tool's own built exe as a
  subprocess. Exists so both tools can ship as one unzip-and-run folder even
  though they can't be merged into a single PyInstaller build (both projects
  define same-named modules like `mip_plotter.py` with different contents).

For end-user instructions (which tool to pick, why "Channel names" is
required, handling a plate with mixed staining, where output lands,
troubleshooting), see [`USAGE.txt`](USAGE.txt).

## Download

No Python or build step needed to run the toolkit — grab the ready-to-run
build from [Releases](https://github.com/mschrmschr/CQ1_Toolkit/releases):

1. Download `CQ1_Toolkit_v1.0.0_win.zip` from the latest release
2. Unzip anywhere (Desktop, Documents, a network share)
3. Double-click `CQ1_Toolkit_Launcher.exe` and pick a tool

This repository itself holds source only (see below) — the release zip is
where the actual Windows executables live.

## Repository layout

This repo holds **source only**. Each tool also has its own `build_exe.ps1`
(`build_launcher_exe.ps1` for the launcher) that produces a PyInstaller
`--onedir` build in a local `dist/` folder — intentionally not committed
here (thousands of numpy/matplotlib/pandas dependency files, fully
rebuildable from source, not something a git repo should hold).

```
CQ1_Toolkit_Launcher/
  launcher.py                 tiny Tkinter launcher, no pipeline imports
  build_launcher_exe.ps1

CQ1_FieldtoStacks_parallel_MIP/
  gui.py                      single-dataset GUI front end
  ome_xml_assembler.py        raw CQ1 tiles -> per-field/well OME-TIFF stacks
  mip_plotter.py               MIP + colorized panel PNG generation
  run_from_config_ome_parallel.py   batch/CLI entry point (jobs_ome.json)
  jobs_ome.example.json       copy to jobs_ome.json and edit for your data
  jobs_ome.schema.json
  build_exe.ps1

CQ1_Stitching_Wholemount_MIP/
  gui.py                      single-dataset GUI front end
  stitcher_unified.py         tile stitching backends (classic/seamless/tiny)
  ome_metadata.py             CQ1 OME-XML -> per-tile metadata CSV
  mip_plotter.py               MIP panel PNG generation
  run_jobs.py                 batch/CLI entry point (jobs.json)
  jobs.example.json           copy to jobs.json and edit for your data
  build_exe.ps1
  README.md                   developer notes: FIJI-compatibility details,
                               stitch backend tradeoffs, bug history
```

## Building from source

Each tool builds independently with PyInstaller, from a conda/venv with that
project's `requirements.txt` installed:

```powershell
cd CQ1_FieldtoStacks_parallel_MIP
.\build_exe.ps1
cd ..\CQ1_Stitching_Wholemount_MIP
.\build_exe.ps1
cd ..\CQ1_Toolkit_Launcher
.\build_launcher_exe.ps1
```

Then assemble the shippable folder: copy `CQ1_Toolkit_Launcher\dist\
CQ1_Toolkit_Launcher\*` to a `CQ1_Toolkit\` folder, copy
`CQ1_FieldtoStacks_parallel_MIP\dist\CQ1_FieldtoStacks_parallel_MIP` to
`CQ1_Toolkit\tools\FieldToStacks`, copy `CQ1_Stitching_Wholemount_MIP\dist\
CQ1_Stitching_Wholemount_MIP` to `CQ1_Toolkit\tools\Stitching`, and add
`USAGE.txt` as `README.txt` alongside the launcher exe.

Real job configs (`jobs.json` / `jobs_ome.json`) are gitignored since they
can contain real dataset paths — copy the `.example.json` file in each
project and edit it, or use each tool's GUI instead.
