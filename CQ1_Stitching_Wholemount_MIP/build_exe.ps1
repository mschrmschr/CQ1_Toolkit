# Build script for CQ1_Stitching_Wholemount_MIP -> standalone Windows exe
# Run this from the project folder: X:\Marie\Skripte\PythonScripts\CQ1_Stitching_Wholemount_MIP
# Builds on local disk (fast) instead of the X:\ network share (slow), then
# copies the finished dist\ folder back to the project directory.

$ErrorActionPreference = "Stop"

$src        = $PSScriptRoot
$build      = "C:\build\CQ1_Stitching_Wholemount_MIP"
$toolName   = "CQ1_Stitching_Wholemount_MIP"
# No PyPI access from this machine (pip.ini's proxy line is disabled/renamed
# to pip.ini.txt) -- build using the existing "CQ1" conda env instead of a
# fresh venv. It already has numpy/tifffile/pandas/tqdm; PyInstaller must be
# installed into it separately (e.g. via Anaconda Navigator or a working
# proxy) before running this script.
$condaPython = "C:\Users\schreiber57\AppData\Local\anaconda3\envs\CQ1\python.exe"

if (-not (Test-Path $condaPython)) {
    throw "CQ1 conda env python not found at $condaPython"
}

# Calling python.exe directly (no 'conda activate') leaves envs\CQ1\Library\bin
# off PATH, which is where conda's native DLLs (tcl86t.dll/tk86t.dll for
# tkinter, libcrypto/liblzma/libbz2 for stdlib hashlib/lzma/bz2) live.
# Without it, PyInstaller can't cleanly resolve dependencies for the
# extension modules that need them and may silently pick up an incompatible
# same-named DLL from elsewhere on this machine's PATH instead. Restrict PATH
# to just the conda env + minimal Windows system dirs so resolution only ever
# finds genuinely matching files.
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

# 2. Build --onedir --windowed (gui.py is the entry point, no console window).
#    numpy/pandas/tifffile/tqdm have no compiled extensions PyInstaller's
#    static analysis tends to miss (unlike lxml in the FieldToStacks tool).
#    matplotlib is now a real dependency (mip_plotter.py writes a colorized
#    MIP panel PNG) and calls matplotlib.use("Agg") at module level, so
#    PyInstaller's backend auto-detection picks the lightweight headless
#    backend on its own -- same as FieldToStacks -- without needing to
#    exclude/force anything here. (Previously this build excluded
#    matplotlib/PySide6 outright because nothing used matplotlib and
#    pandas' optional-plotting hook was pulling in a full unused Qt
#    toolkit; that's no longer true now that matplotlib is genuinely used.)
#    imagecodecs (added for zstd TIFF compression, see README's compression
#    section) ships ~65 separate compiled per-codec extension submodules
#    (_zstd, _lzw, _jpeg, ...) that tifffile imports from lazily at the
#    point a given codec is actually used -- the same "PyInstaller's static
#    scanner can't see this" shape as the lxml/etree DLL issue this project
#    already hit once (see plan doc 6a). --collect-all bundles all of them
#    regardless of what static analysis would have found on its own.
& $condaPython -m PyInstaller --onedir --windowed --noconfirm `
    --name $toolName `
    --collect-all imagecodecs `
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
