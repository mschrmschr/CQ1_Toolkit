# Build script for CQ1_FieldtoStacks_parallel_MIP -> standalone Windows exe
# Run this from the project folder: X:\Marie\Skripte\PythonScripts\CQ1_FieldtoStacks_parallel_MIP
# Builds on local disk (fast) instead of the X:\ network share (slow), then
# copies the finished dist\ folder back to the project directory.

$ErrorActionPreference = "Stop"

$src        = $PSScriptRoot
$build      = "C:\build\CQ1_FieldtoStacks_parallel_MIP"
$toolName   = "CQ1_FieldtoStacks_parallel_MIP"
# No PyPI access from this machine (pip.ini's proxy line is disabled/renamed
# to pip.ini.txt) -- build using the existing "CQ1" conda env instead of a
# fresh venv. It already has numpy/tifffile/matplotlib/lxml/pandas/tqdm;
# PyInstaller must be installed into it separately (e.g. via Anaconda
# Navigator or a working proxy) before running this script.
$condaPython = "C:\Users\schreiber57\AppData\Local\anaconda3\envs\CQ1\python.exe"

if (-not (Test-Path $condaPython)) {
    throw "CQ1 conda env python not found at $condaPython"
}

# Calling python.exe directly (no 'conda activate') leaves envs\CQ1\Library\bin
# off PATH, which is where conda's native DLLs (tcl86t.dll/tk86t.dll for
# tkinter, libcrypto/liblzma/libbz2 for stdlib hashlib/lzma/bz2) live.
# Without it, PyInstaller can't cleanly resolve dependencies for the
# extension modules that need them and may silently pick up an incompatible
# same-named DLL from elsewhere on this machine's PATH (e.g. other installed
# apps) instead -- which is what caused the "DLL load failed while importing
# etree" crash from the first build of this tool. Restrict PATH to just the
# conda env + minimal Windows system dirs so resolution only ever finds
# genuinely matching files (or cleanly skips ones nothing on this machine has,
# which is fine for anything not actually needed at runtime).
$condaEnvRoot = Split-Path $condaPython -Parent
$env:PATH = "$condaEnvRoot;$condaEnvRoot\Library\bin;$condaEnvRoot\Scripts;C:\Windows\System32;C:\Windows"

& $condaPython -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller is not installed in the CQ1 conda env. Install it there first (e.g. '& `"$condaPython`" -m pip install pyinstaller' with the proxy configured, or via Anaconda Navigator)."
}

# 1. Fresh local build folder, copy only source files (not any dist/build leftovers)
if (Test-Path $build) { Remove-Item -Recurse -Force $build }
New-Item -ItemType Directory -Path $build | Out-Null

Copy-Item "$src\*.py"   $build
Copy-Item "$src\*.json" $build
Copy-Item "$src\requirements.txt" $build

Set-Location $build

# 2. Build --onedir --windowed (gui.py is the entry point, no console window),
#    force-collecting lxml's compiled extensions.
#    mip_plotter.py optionally uses ome_types (from ome_xml) to recover real
#    channel names for MIP panel labels. ome_types' underlying xsdata parser
#    discovers its pydantic support via a setuptools entry point that
#    ome_types itself registers (group 'xsdata.plugins.class_types', pointing
#    at xsdata_pydantic_basemodel.hooks.class_type -- confirmed via
#    `importlib.metadata.entry_points(group=...)[0].dist.metadata['Name']`
#    == 'ome_types'; xsdata_pydantic_basemodel has no dist-info of its own).
#    PyInstaller's static import scanner can't see this (nothing does a plain
#    `import xsdata_pydantic_basemodel` anywhere) -- so without these two
#    flags the frozen build silently can't find the plugin at runtime
#    (ModuleNotFoundError caught by mip_plotter's broad except, falls back to
#    generic "Channel0/1/2" panel labels instead of real channel names).
#    --hidden-import bundles the plugin module itself; --copy-metadata ome_types
#    bundles ome_types' dist-info/entry_points.txt (where the registration
#    actually lives) so importlib.metadata can discover it at runtime inside
#    the frozen environment.
& $condaPython -m PyInstaller --onedir --windowed --noconfirm `
    --name $toolName `
    --collect-all lxml `
    --hidden-import xsdata_pydantic_basemodel.hooks `
    --copy-metadata ome_types `
    gui.py
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build failed (exit $LASTEXITCODE) -- see output above. Not touching the existing dist\ folder."
}

# 3. Copy the finished dist folder back to the project folder
$destDist = Join-Path $src "dist"
if (Test-Path $destDist) { Remove-Item -Recurse -Force $destDist }
Copy-Item -Recurse "$build\dist" $destDist

Write-Host "`nDone. Built exe is at: $destDist\$toolName\$toolName.exe"
Write-Host "Local build files (PyInstaller intermediates) left at: $build"
