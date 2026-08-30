# Plan: Packaging as a standalone .exe for non-technical colleagues

Status: not started — captured for later.

## Goal

Let colleagues without Python installed run the pipeline on their work PCs,
without admin rights, the way Fiji is used (unzip a folder, double-click an
exe).

## Decisions made

- **No parallel computing in the exe.** Always call `run_mip_job(job,
  workers=1)`. This avoids `ProcessPoolExecutor` under PyInstaller, which is
  the most common source of frozen-multiprocessing headaches on Windows
  (relaunch loops, extra spawned exe copies). Trade-off: slower MIP
  generation, but far more reliable packaging.
- **PyInstaller, `--onedir` build (not `--onefile`, not an installer).**
  - `--onefile` unpacks itself to `%TEMP%` on every launch — slower, and
    more likely to be flagged by corporate antivirus (looks like a dropper).
  - `--onedir` produces a plain folder that can be zipped, copied to
    Desktop/Documents/a network share, and run directly — no installation
    step, so no admin rights or UAC prompt needed.
  - Runtime writes (e.g. matplotlib font cache) land in the user's own
    profile (`%LOCALAPPDATA%`/`%USERPROFILE%`), which is writable without
    admin.

## Dependencies

Third-party (confirmed via imports across all three scripts):

- `numpy`
- `tifffile`
- `matplotlib`
- `lxml`

Everything else used is Python stdlib (`os`, `pathlib`, `argparse`, `json`,
`sys`, `concurrent.futures`, `dataclasses`, `typing`, `collections`).

Build machine needs: Python 3.x + `pip install numpy tifffile matplotlib
lxml pyinstaller`. Colleagues need nothing — no Python, no pip packages.

## Steps

1. Hardcode/force `workers=1` in the call path (remove reliance on the
   `ProcessPoolExecutor` branch in `mip_plotter.py`).
   - Concretely: wherever the entry-point script builds the MIP job dict and
     calls `run_mip_job(...)`, drop/ignore any `"workers"`/`"parallel"` keys
     coming from config and hardcode `workers=1` in the call itself. This
     way a colleague editing the JSON config can't accidentally re-enable
     `ProcessPoolExecutor` in the frozen exe.

2. Add a `requirements.txt` (third-party deps only; stdlib doesn't need
   pinning) and install build deps.

   **Important:** if the script lives on a network share/mapped drive
   (e.g. `X:\...` backed by a UNC path), do the venv + PyInstaller build on
   **local disk** instead (e.g. `C:\build\<project>`), then copy the
   finished `dist\` folder back to the share. Network I/O makes `pip
   install` and the PyInstaller build dramatically slower, and PyInstaller
   occasionally trips over file locking/junction weirdness on network
   drives.

   ```powershell
   # requirements.txt — list only the third-party imports the script(s) use
   numpy
   tifffile
   matplotlib
   lxml

   # From a local working copy (not the network share):
   python -m venv .buildvenv
   .\.buildvenv\Scripts\python.exe -m pip install --upgrade pip
   .\.buildvenv\Scripts\python.exe -m pip install -r requirements.txt pyinstaller
   ```

3. Build with PyInstaller, `--onedir`, targeting the CLI entry point
   (whichever script has `if __name__ == "__main__":`) — other local
   modules it imports (e.g. `ome_xml_assembler.py`, `mip_plotter.py`) are
   picked up automatically via static import analysis:

   ```powershell
   .\.buildvenv\Scripts\python.exe -m PyInstaller --onedir --noconfirm `
       --name <tool_name> `
       --collect-all lxml `
       run_from_config_ome_parallel.py
   ```

   `--collect-all lxml` is worth keeping by default: `lxml` ships compiled
   `libxml2`/`libxslt` extensions that PyInstaller's static analysis can
   miss, and `--collect-all` forces it to bundle all of lxml's binary/data
   files rather than guessing. Add similar `--collect-all <pkg>` flags for
   any other dependency with compiled C extensions if the exe errors with
   `ImportError`/`DLL load failed` at runtime.

   Output lands in `dist\<tool_name>\` (the `.exe` plus all bundled
   DLLs/libs) and `build\<tool_name>\` (intermediate PyInstaller artifacts —
   safe to delete, not part of what you ship).

4. Test the built folder on a clean Windows machine/VM with no Python
   installed, to catch missing-DLL or path issues before handing it off —
   pay particular attention to `lxml`'s compiled C extensions
   (libxml2/libxslt). At minimum, smoke-test locally first:

   ```powershell
   cd dist\<tool_name>
   .\<tool_name>.exe --config path\to\some_config.json
   ```

5. Zip the output folder for distribution; colleagues unzip and run the
   `.exe` inside, config JSON alongside it (config stays external/editable
   so paths can be changed without rebuilding).

   ```powershell
   Compress-Archive -Path dist\<tool_name> -DestinationPath <tool_name>_win.zip
   ```

6. Optional, if hand-editing JSON is too much for colleagues: add a small
   tkinter folder-picker wrapper (~30–50 lines) that writes/overrides the
   config before invoking the pipeline. Adds roughly 1–2 hours.

## Open risk: AppLocker / SmartScreen / corporate AV

Not an admin-rights issue, but unsigned exes from an unknown publisher can
still be silently blocked or quarantined on locked-down corporate machines
via application allow-listing policies. Worth checking with IT ahead of
time whether unsigned portable exes are permitted to run at all. If not,
code-signing becomes a required (and costed) extra step.

## Effort estimate

- Core build (steps 1–5): **2–4 hours**
- With optional folder-picker GUI (step 6): **+1–2 hours**
