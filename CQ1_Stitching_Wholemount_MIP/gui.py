# file: gui.py
"""Single-dataset GUI front-end for the wholemount stitching + MIP pipeline.

Lets a colleague without Python/JSON experience pick one dataset folder,
fill in a form (with an optional Advanced section for the tuning knobs
normally set in jobs.json), and run the same pipeline the CLI (run_jobs.py)
uses -- via the same run_job() function, not a subprocess. Batch/JSON-array
runs are unaffected; this is an additive single-job front end for the
PyInstaller --windowed build.
"""
from __future__ import annotations

import os
import queue
import sys
import threading
import traceback
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

from run_jobs import run_job, _resolve
from checklist_widget import WellChecklist
from ome_metadata import list_wells


def _parse_csv(text: str) -> list[str]:
    return [p.strip() for p in text.split(",") if p.strip()]


def _parse_int(text: str, default: int | None = None) -> int | None:
    text = text.strip()
    if not text:
        return default
    return int(text)


def _parse_float(text: str, default: float) -> float:
    text = text.strip()
    return float(text) if text else default


class ScrollableFrame(ttk.Frame):
    """A vertically-scrollable container -- standard tkinter canvas+frame recipe."""

    def __init__(self, parent):
        super().__init__(parent)
        canvas = tk.Canvas(self, highlightthickness=0)
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=canvas.yview)
        self.body = ttk.Frame(canvas)

        self.body.bind(
            "<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        canvas.create_window((0, 0), window=self.body, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        def _on_wheel(event):
            canvas.yview_scroll(int(-event.delta / 120), "units")

        canvas.bind_all("<MouseWheel>", _on_wheel)


class QueueWriter:
    """A file-like object that pushes writes onto a queue, read by the GUI's log pane."""

    def __init__(self, q: "queue.Queue[str]"):
        self.q = q

    def write(self, text: str) -> None:
        if text:
            self.q.put(text)

    def flush(self) -> None:
        pass


class App(ttk.Frame):
    def __init__(self, root: tk.Tk):
        super().__init__(root)
        root.title("CQ1 Wholemount Stitching + MIP")
        root.geometry("720x760")
        self.pack(fill="both", expand=True)

        self.log_queue: "queue.Queue[str]" = queue.Queue()
        self.well_scan_queue: "queue.Queue[tuple]" = queue.Queue()
        self.output_dir: str | None = None
        self.running = False
        self.advanced_widgets: list[tk.Widget] = []

        self._build_form()
        self._build_actions()
        self.after(100, self._poll_log)
        self.after(100, self._poll_well_scan)

    # ---- form ----

    def _build_form(self) -> None:
        scroller = ScrollableFrame(self)
        scroller.pack(fill="both", expand=True, padx=8, pady=8)
        form = scroller.body
        form.columnconfigure(1, weight=1)

        row = 0

        def add_label(text: str, bold: bool = False, advanced: bool = False) -> None:
            nonlocal row
            font = ("Segoe UI", 10, "bold") if bold else ("Segoe UI", 10)
            w = ttk.Label(form, text=text, font=font)
            w.grid(row=row, column=0, columnspan=3, sticky="w", pady=(10, 2))
            row += 1
            if advanced:
                self.advanced_widgets.append(w)

        def add_field(label: str, widget_factory, default: str = "", advanced: bool = False) -> tk.Variable:
            nonlocal row
            var = tk.StringVar(value=default)
            lbl = ttk.Label(form, text=label)
            lbl.grid(row=row, column=0, sticky="w", padx=(0, 8))
            widget = widget_factory(form, var)
            widget.grid(row=row, column=1, columnspan=2, sticky="we")
            row += 1
            if advanced:
                self.advanced_widgets.append(lbl)
                self.advanced_widgets.append(widget)
            return var

        def entry_factory(parent, var):
            return ttk.Entry(parent, textvariable=var, width=48)

        def combo_factory(values):
            def factory(parent, var):
                return ttk.Combobox(parent, textvariable=var, values=values, state="readonly", width=45)
            return factory

        def check_factory(parent, var):
            return ttk.Checkbutton(parent, variable=var, onvalue="1", offvalue="0")

        # -- Basic --
        add_label("Dataset", bold=True)

        self.root_dir_var = tk.StringVar()
        ttk.Label(form, text="Dataset folder").grid(row=row, column=0, sticky="w", padx=(0, 8))
        path_frame = ttk.Frame(form)
        path_frame.grid(row=row, column=1, columnspan=2, sticky="we")
        ttk.Entry(path_frame, textvariable=self.root_dir_var).pack(side="left", fill="x", expand=True)
        ttk.Button(path_frame, text="Browse...", command=self._browse_root_dir).pack(side="left", padx=(6, 0))
        row += 1
        ttk.Label(
            form,
            text="The folder for one CQ1 acquisition -- should contain "
                 "MeasurementResult.ome.xml and an Image\\ subfolder.",
            foreground="#666666",
        ).grid(row=row, column=0, columnspan=3, sticky="w")
        row += 1

        add_label("Wells")
        self.well_checklist = WellChecklist(form)
        self.well_checklist.grid(row=row, column=0, columnspan=3, sticky="w")
        self.well_checklist.set_status("Pick a dataset folder above to see its wells here.")
        row += 1

        self.channel_names_var = add_field(
            "Channel names (comma-separated, one per channel, in acquisition order)",
            entry_factory, "DAPI,GFP,RFP,FarRed",
        )
        ttk.Label(
            form,
            text="1st name = channel 1, 2nd name = channel 2, and so on, in the order "
                 "channels were acquired. Applies to every checked well -- wells "
                 "stained differently? Check just one staining group above, run, "
                 "then check the next group and run again.",
            foreground="#666666", wraplength=640,
        ).grid(row=row, column=0, columnspan=3, sticky="w")
        row += 1

        # -- Advanced toggle --
        self.show_advanced_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            form,
            text="Show advanced options",
            variable=self.show_advanced_var,
            command=self._toggle_advanced,
        ).grid(row=row, column=0, columnspan=3, sticky="w", pady=(14, 0))
        row += 1

        add_label("Paths", bold=True, advanced=True)
        self.xml_file_var = add_field(
            "OME-XML filename", entry_factory, "MeasurementResult.ome.xml", advanced=True
        )
        self.image_dir_var = add_field("Image subfolder", entry_factory, "Image", advanced=True)
        self.output_dir_var = add_field(
            "Output folder (relative to dataset, or absolute)", entry_factory, "stitched_output", advanced=True
        )

        add_label("Filters / dry run", bold=True, advanced=True)
        self.only_well_var = add_field(
            "Only well (override -- leave blank to use the Wells checklist above; "
            "only needed if the automatic scan failed)",
            entry_factory, "", advanced=True,
        )
        self.only_grid_var = add_field(
            "Only grid index (for wells with more than one tiled region; blank = all)",
            entry_factory, "", advanced=True,
        )
        self.only_z_var = add_field("Only Z plane (process a single slice; blank = all)", entry_factory, "", advanced=True)
        self.dry_run_var = add_field(
            "Dry run (list what would be processed, write nothing)", check_factory, "0", advanced=True
        )

        add_label("Output file options", bold=True, advanced=True)
        self.overlap_fraction_var = add_field(
            "Fallback overlap fraction (only used if stage positions can't measure the real tile overlap)",
            entry_factory, "0.01", advanced=True,
        )
        self.compression_var = add_field("TIFF compression", combo_factory(["zlib", "zstd", "none"]), "zlib", advanced=True)
        self.compression_level_var = add_field(
            "Compression level (zlib: 0-9, zstd: 0-22 -- higher = smaller file, slower write)",
            entry_factory, "6", advanced=True,
        )
        self.predictor_var = add_field(
            "Predictor (this checkbox has no effect on the output either way -- "
            "benchmarking showed it makes this data larger, not smaller, so it's "
            "intentionally kept off internally regardless of this setting; see README)",
            check_factory, "1", advanced=True,
        )
        self.tile_var = add_field("Tile size (width,height)", entry_factory, "512,512", advanced=True)
        self.bigtiff_var = add_field("BigTIFF", check_factory, "1", advanced=True)
        self.pyramids_var = add_field("Pyramids (not yet implemented, no-op)", check_factory, "0", advanced=True)

        add_label("Seamless blending", bold=True, advanced=True)
        self.min_feather_px_var = add_field("Min feather px", entry_factory, "4", advanced=True)
        self.max_feather_px_var = add_field("Max feather px", entry_factory, "16", advanced=True)
        self.max_snap_px_var = add_field("Max snap px", entry_factory, "6", advanced=True)
        self.lattice_snap_px_var = add_field("Lattice snap px (0 = off)", entry_factory, "0", advanced=True)
        self.snap_axis_var = add_field("Snap axis", combo_factory(["both", "x", "y"]), "both", advanced=True)
        self.refine_alignment_var = add_field(
            "Refine alignment (local cross-correlation nudge)", check_factory, "0", advanced=True
        )
        self.refine_max_shift_px_var = add_field("Refine max shift px", entry_factory, "6", advanced=True)
        self.refine_axis_var = add_field("Refine axis", combo_factory(["both", "x", "y"]), "both", advanced=True)
        self.overlap_gain_match_var = add_field("Overlap gain match", check_factory, "0", advanced=True)
        self.gain_clip_low_var = add_field("Gain clip low", entry_factory, "0.92", advanced=True)
        self.gain_clip_high_var = add_field("Gain clip high", entry_factory, "1.08", advanced=True)
        self.gain_match_channels_var = add_field(
            "Gain-match channels (blank = all, comma-sep)", entry_factory, "", advanced=True
        )

        add_label("MIP generation", bold=True, advanced=True)
        self.generate_mip_var = add_field("Generate MIP after stitching", check_factory, "1", advanced=True)
        self.mip_output_dir_var = add_field(
            "MIP output folder (blank = <output>\\mip)", entry_factory, "", advanced=True
        )
        self.mip_projection_var = add_field("Projection", combo_factory(["max", "mean"]), "max", advanced=True)
        self.mip_z_range_var = add_field(
            "Z range (blank = full, e.g. 0,10)", entry_factory, "", advanced=True
        )
        self.mip_gain_match_var = add_field("Gain match channels", check_factory, "0", advanced=True)
        self.mip_p_low_var = add_field("Normalize: low percentile", entry_factory, "2.0", advanced=True)
        self.mip_p_high_var = add_field("Normalize: high percentile", entry_factory, "99.8", advanced=True)
        self.mip_clip_var = add_field("Normalize: clip", check_factory, "1", advanced=True)
        self.mip_include_var = add_field(
            "Include filenames containing (comma-sep, blank = all)", entry_factory, "", advanced=True
        )
        self.mip_exclude_var = add_field(
            "Exclude filenames containing (comma-sep, blank = none)", entry_factory, "", advanced=True
        )

        self._toggle_advanced()

    def _toggle_advanced(self) -> None:
        show = self.show_advanced_var.get()
        for widget in self.advanced_widgets:
            if show:
                widget.grid()
            else:
                widget.grid_remove()

    def _browse_root_dir(self) -> None:
        path = filedialog.askdirectory(title="Select dataset folder")
        if path:
            self.root_dir_var.set(path)
            self.well_checklist.clear()
            self._scan_wells(path)

    def _scan_wells(self, root_dir: str) -> None:
        xml_name = self.xml_file_var.get().strip() or "MeasurementResult.ome.xml"
        xml_path = os.path.join(root_dir, xml_name)
        if not os.path.isfile(xml_path):
            self.well_checklist.set_status(f"No {xml_name} found in this folder.", error=True)
            return
        self.well_checklist.set_status("Scanning wells…")
        threading.Thread(target=self._scan_wells_worker, args=(xml_path,), daemon=True).start()

    def _scan_wells_worker(self, xml_path: str) -> None:
        try:
            wells = list_wells(xml_path)
            self.well_scan_queue.put(("ok", wells))
        except Exception as e:
            self.well_scan_queue.put(("err", str(e)))

    def _poll_well_scan(self) -> None:
        try:
            while True:
                status, payload = self.well_scan_queue.get_nowait()
                if status == "ok":
                    if payload:
                        self.well_checklist.set_wells(payload)
                    else:
                        self.well_checklist.set_status(
                            "No wells detected in this dataset's XML.", error=True
                        )
                else:
                    self.well_checklist.set_status(f"Could not read wells: {payload}", error=True)
        except queue.Empty:
            pass
        self.after(100, self._poll_well_scan)

    # ---- actions / log ----

    def _build_actions(self) -> None:
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=8, pady=(0, 4))
        self.run_button = ttk.Button(bar, text="Run", command=self._on_run)
        self.run_button.pack(side="left")
        self.open_folder_button = ttk.Button(
            bar, text="Open output folder", command=self._open_output_folder, state="disabled"
        )
        self.open_folder_button.pack(side="left", padx=(6, 0))

        self.log_widget = scrolledtext.ScrolledText(self, height=14, state="disabled")
        self.log_widget.pack(fill="both", expand=False, padx=8, pady=(0, 8))

    def _log(self, text: str) -> None:
        self.log_widget.configure(state="normal")
        self.log_widget.insert("end", text)
        self.log_widget.see("end")
        self.log_widget.configure(state="disabled")

    def _open_output_folder(self) -> None:
        if self.output_dir and os.path.isdir(self.output_dir):
            os.startfile(self.output_dir)  # noqa: S606 -- Windows-only GUI tool

    # ---- run ----

    def _parse_tile(self) -> list[int]:
        parts = _parse_csv(self.tile_var.get())
        if len(parts) != 2:
            raise ValueError("Tile size must be 'width,height', e.g. 512,512.")
        return [int(parts[0]), int(parts[1])]

    def _parse_z_range(self):
        parts = _parse_csv(self.mip_z_range_var.get())
        if not parts:
            return None
        if len(parts) != 2:
            raise ValueError("Z range must be 'start,end', e.g. 0,10.")
        return [int(parts[0]), int(parts[1])]

    def _build_job(self) -> dict:
        root_dir = self.root_dir_var.get().strip()
        if not root_dir:
            raise ValueError("Please choose a dataset folder.")

        # Required, not optional: CQ1 wholemount acquisitions don't carry
        # channel names in their OME-XML the way field acquisitions do, so
        # there's no XML fallback to silently use here (unlike the old
        # "blank = use CQ1 XML names" label used to imply).
        channel_names = _parse_csv(self.channel_names_var.get())
        if not channel_names:
            raise ValueError("Channel names are required.")

        compression = self.compression_var.get()
        job: dict = {
            "root_dir": root_dir,
            "xml_file": self.xml_file_var.get().strip() or "MeasurementResult.ome.xml",
            "image_dir": self.image_dir_var.get().strip() or "Image",
            "output_dir": self.output_dir_var.get().strip() or "stitched_output",
            # Fixed rather than exposed as choices: "seamless" (feathered/
            # blended overlaps) and "stage" (stage-coordinate tile
            # placement) are what every job in this project's own
            # jobs.json already uses.
            "stitch_backend": "seamless",
            "placement": "stage",
            "dry_run": self.dry_run_var.get() == "1",
            "generate_mip": self.generate_mip_var.get() == "1",
            "overlap_fraction": _parse_float(self.overlap_fraction_var.get(), 0.01),
            "compression": None if compression == "none" else compression,
            "compression_level": _parse_int(self.compression_level_var.get(), 6),
            "predictor": self.predictor_var.get() == "1",
            "tile": self._parse_tile(),
            "bigtiff": self.bigtiff_var.get() == "1",
            "pyramids": self.pyramids_var.get() == "1",
            "min_feather_px": _parse_int(self.min_feather_px_var.get(), 4),
            "max_feather_px": _parse_int(self.max_feather_px_var.get(), 16),
            "max_snap_px": _parse_int(self.max_snap_px_var.get(), 6),
            "lattice_snap_px": _parse_int(self.lattice_snap_px_var.get(), 0),
            "snap_axis": self.snap_axis_var.get(),
            "refine_alignment": self.refine_alignment_var.get() == "1",
            "refine_max_shift_px": _parse_int(self.refine_max_shift_px_var.get(), 6),
            "refine_axis": self.refine_axis_var.get(),
            "overlap_gain_match": self.overlap_gain_match_var.get() == "1",
            "gain_clip_low": _parse_float(self.gain_clip_low_var.get(), 0.92),
            "gain_clip_high": _parse_float(self.gain_clip_high_var.get(), 1.08),
        }

        only_well = self.only_well_var.get().strip()
        if only_well:
            # Advanced override always wins -- guarantees a run is still
            # possible even if the automatic well scan failed or is stale.
            job["only_well"] = only_well
        elif not self.well_checklist.all_checked():
            checked = self.well_checklist.get_checked()
            if checked:
                job["only_wells"] = checked
        only_grid = self.only_grid_var.get().strip()
        if only_grid:
            job["only_grid"] = int(only_grid)
        only_z = self.only_z_var.get().strip()
        if only_z:
            job["only_z"] = int(only_z)

        job["channel_names"] = channel_names

        gain_match_channels = _parse_csv(self.gain_match_channels_var.get())
        if gain_match_channels:
            job["gain_match_channels"] = gain_match_channels

        mip: dict = {
            "projection": self.mip_projection_var.get(),
            "z_range": self._parse_z_range(),
            "preprocess": {"gain_match": self.mip_gain_match_var.get() == "1"},
            "norm": {
                "p_low": _parse_float(self.mip_p_low_var.get(), 2.0),
                "p_high": _parse_float(self.mip_p_high_var.get(), 99.8),
                "clip": self.mip_clip_var.get() == "1",
            },
        }
        mip_output_dir = self.mip_output_dir_var.get().strip()
        if mip_output_dir:
            mip["output_dir"] = mip_output_dir
        include_kw = _parse_csv(self.mip_include_var.get())
        if include_kw:
            mip["include_keywords"] = include_kw
        exclude_kw = _parse_csv(self.mip_exclude_var.get())
        if exclude_kw:
            mip["exclude_keywords"] = exclude_kw
        job["mip"] = mip

        return job

    def _on_run(self) -> None:
        if self.running:
            return
        try:
            job = self._build_job()
        except Exception as e:
            messagebox.showerror("Invalid input", str(e))
            return

        self.output_dir = _resolve(job["root_dir"], job["output_dir"])

        self.running = True
        self.run_button.configure(state="disabled")
        self.open_folder_button.configure(state="disabled")
        self.log_widget.configure(state="normal")
        self.log_widget.delete("1.0", "end")
        self.log_widget.configure(state="disabled")

        thread = threading.Thread(target=self._run_pipeline, args=(job,), daemon=True)
        thread.start()

    def _run_pipeline(self, job: dict) -> None:
        old_stdout, old_stderr = sys.stdout, sys.stderr
        writer = QueueWriter(self.log_queue)
        sys.stdout = writer
        sys.stderr = writer
        try:
            run_job(job)
            print("\nDone.")
        except Exception as e:
            print(f"\nERROR: {e}")
            traceback.print_exc()
            self.log_queue.put("__ERROR__")
        finally:
            sys.stdout, sys.stderr = old_stdout, old_stderr
            self.log_queue.put("__DONE__")

    def _poll_log(self) -> None:
        try:
            while True:
                text = self.log_queue.get_nowait()
                if text == "__DONE__":
                    self.running = False
                    self.run_button.configure(state="normal")
                    if self.output_dir and os.path.isdir(self.output_dir):
                        self.open_folder_button.configure(state="normal")
                elif text == "__ERROR__":
                    messagebox.showerror("Job failed", "See the log for details.")
                else:
                    self._log(text)
        except queue.Empty:
            pass
        self.after(100, self._poll_log)


def main() -> None:
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
