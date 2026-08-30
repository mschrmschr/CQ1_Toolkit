# Build script for the CQ1 Toolkit launcher -> standalone Windows exe
# Run this from the project folder: X:\Marie\Skripte\PythonScripts\CQ1_Toolkit_Launcher
# Builds on local disk (fast) instead of the X:\ network share (slow), then
# copies the finished dist\ folder back to the project directory.

$ErrorActionPreference = "Stop"

$src        = $PSScriptRoot
$build      = "C:\build\CQ1_Toolkit_Launcher"
$toolName   = "CQ1_Toolkit_Launcher"
# Same CQ1 conda env used to build the two tools -- no PyPI access on this
# machine, and PyInstaller is already installed there.
$condaPython = "C:\Users\schreiber57\AppData\Local\anaconda3\envs\CQ1\python.exe"

if (-not (Test-Path $condaPython)) {
    throw "CQ1 conda env python not found at $condaPython"
}

# Calling python.exe directly (no 'conda activate') leaves envs\CQ1\Library\bin
# off PATH, which is where conda's Tcl/Tk DLLs (tcl86t.dll/tk86t.dll) live.
# Without it, PyInstaller can't resolve _tkinter's dependencies and silently
# ships a launcher that fails at runtime with no console to show why.
$condaEnvRoot = Split-Path $condaPython -Parent
$env:PATH = "$condaEnvRoot;$condaEnvRoot\Library\bin;$condaEnvRoot\Scripts;$env:PATH"

& $condaPython -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller is not installed in the CQ1 conda env."
}

# 1. Fresh local build folder
if (Test-Path $build) { Remove-Item -Recurse -Force $build }
New-Item -ItemType Directory -Path $build | Out-Null

Copy-Item "$src\launcher.py" $build

Set-Location $build

# 2. Build --onedir --windowed (no console window). Pure stdlib (tkinter,
#    subprocess, os, sys) -- no --collect-all needed.
& $condaPython -m PyInstaller --onedir --windowed --noconfirm `
    --name $toolName `
    launcher.py
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build failed (exit $LASTEXITCODE) -- see output above. Not touching the existing dist\ folder."
}

# 3. Copy the finished dist folder back to the project folder
$destDist = Join-Path $src "dist"
if (Test-Path $destDist) { Remove-Item -Recurse -Force $destDist }
Copy-Item -Recurse "$build\dist" $destDist

Write-Host "`nDone. Built exe is at: $destDist\$toolName\$toolName.exe"
Write-Host "Local build files (PyInstaller intermediates) left at: $build"
