# file: gui.py
"""Single-dataset GUI front-end for the FieldToStacks + MIP pipeline.

Lets a colleague without Python/JSON experience pick one dataset folder,
fill in a form (with an optional Advanced section for the tuning knobs
normally set in jobs_ome.json), and run the same pipeline the CLI
(run_from_config_ome_parallel.py) uses -- via the same functions, not a
subprocess. Batch/JSON-array runs are unaffected; this is an additive
single-job front end for the PyInstaller --windowed build.
"""
from __future__ import annotations

import os
import queue
import sys
import threading
import traceback
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

from ome_xml_assembler import assemble_one_job, list_wells
from run_from_config_ome_parallel import _inject_paths, _validate_job, _run_mip_for_job
from checklist_widget import WellChecklist


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
        root.title("CQ1 Field-to-Stacks + MIP")
        root.geometry("720x760")
        self.pack(fill="both", expand=True)

        self.log_queue: "queue.Queue[str]" = queue.Queue()
        self.well_scan_queue: "queue.Queue[tuple]" = queue.Queue()
        self.output_dir: str | None = None
        self.running = False

        self._build_form()
        self._build_actions()
        self.after(100, self._poll_log)
        self.after(100, self._poll_well_scan)

    # ---- form ----

    def _build_form(self) -> None:
        scroller = ScrollableFrame(self)
        scroller.pack(fill="both", expand=True, padx=8, pady=8)
        form = scroller.body

        row = 0
        self.advanced_widgets: list[tk.Widget] = []

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

        form.columnconfigure(1, weight=1)

        # -- Basic --
        add_label("Dataset", bold=True)

        self.base_path_var = tk.StringVar()
        ttk.Label(form, text="Dataset folder").grid(row=row, column=0, sticky="w", padx=(0, 8))
        path_frame = ttk.Frame(form)
        path_frame.grid(row=row, column=1, columnspan=2, sticky="we")
        ttk.Entry(path_frame, textvariable=self.base_path_var).pack(side="left", fill="x", expand=True)
        ttk.Button(path_frame, text="Browse...", command=self._browse_base_path).pack(side="left", padx=(6, 0))
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

        self.channels_var = add_field(
            "Channel IDs in raw filenames (comma-separated, e.g. C1,C2,C3,C4)",
            entry_factory, "C1,C2,C3,C4",
        )
        self.channel_names_var = add_field(
            "Channel names (comma-separated, one per ID above, same order)",
            entry_factory, "DAPI,GFP,RFP,FarRed",
        )
        ttk.Label(
            form,
            text="1st name = channel 1 (the 1st ID above, e.g. C1), 2nd name = "
                 "channel 2 (C2), and so on -- same order as the IDs above. Applies "
                 "to every checked well -- wells stained differently? Check just "
                 "one staining group above, run, then check the next group and run "
                 "again.",
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

        add_label("Field selection", bold=True, advanced=True)
        self.field_start_var = add_field("Field start", entry_factory, "1", advanced=True)
        self.field_end_var = add_field("Field end (blank = no limit)", entry_factory, "", advanced=True)
        self.fields_var = add_field(
            "Explicit field numbers (blank = auto, comma-sep)", entry_factory, "", advanced=True
        )

        add_label("Assembly options", bold=True, advanced=True)
        self.dtype_var = add_field(
            "Output dtype", combo_factory(["uint16", "uint8", "auto"]), "uint16", advanced=True
        )
        self.enforce_equal_z_var = add_field(
            "Enforce equal Z across channels", check_factory, "1", advanced=True
        )
        self.split_by_w_var = add_field(
            "Split by well within field (on: one stack per well, works with the "
            "Wells checklist above; off: one stack per field, first well only)",
            check_factory, "1", advanced=True,
        )
        self.split_by_w_var.trace_add("write", lambda *_: self._update_well_note())
        self.only_well_var = add_field(
            "Only well (override -- leave blank to use the Wells checklist above; "
            "only needed if the automatic scan failed)",
            entry_factory, "", advanced=True,
        )
        self.save_dir_var = add_field(
            "Output folder override (blank = <dataset>\\Stacks)", entry_factory, "", advanced=True
        )

        add_label("MIP generation", bold=True, advanced=True)
        self.generate_mip_var = add_field("Generate MIP after assembly", check_factory, "1", advanced=True)
        self.mip_projection_var = add_field("Projection", combo_factory(["max", "mean"]), "max", advanced=True)
        self.mip_colors_var = add_field(
            "Channel colors (hex, comma-separated)",
            entry_factory,
            "#0000FF,#FF00FF,#00FFFF,#FFB000",
            advanced=True,
        )
        self.mip_p_low_var = add_field("Normalize: low percentile", entry_factory, "2.0", advanced=True)
        self.mip_p_high_var = add_field("Normalize: high percentile", entry_factory, "99.8", advanced=True)
        self.mip_clip_var = add_field("Normalize: clip", check_factory, "1", advanced=True)
        self.mip_fmt_var = add_field("Save format", combo_factory(["png", "tif"]), "png", advanced=True)
        self.mip_dpi_var = add_field("PNG dpi", entry_factory, "300", advanced=True)
        self.mip_panel_var = add_field("Save per-channel panel PNG", check_factory, "1", advanced=True)
        self.mip_panel_cols_var = add_field("Panel columns", entry_factory, "4", advanced=True)
        self.mip_zstack_panel_var = add_field("Save Z-thirds panel PNG", check_factory, "1", advanced=True)

        self._toggle_advanced()

    def _toggle_advanced(self) -> None:
        show = self.show_advanced_var.get()
        for widget in self.advanced_widgets:
            if show:
                widget.grid()
            else:
                widget.grid_remove()

    def _browse_base_path(self) -> None:
        path = filedialog.askdirectory(title="Select dataset folder")
        if path:
            self.base_path_var.set(path)
            self.well_checklist.clear()
            self._scan_wells(path)

    def _scan_wells(self, base_path: str) -> None:
        image_dir = os.path.join(base_path, "Image")
        if not os.path.isdir(image_dir):
            self.well_checklist.set_status("No Image\\ subfolder found in this dataset folder.", error=True)
            return
        self.well_checklist.set_status("Scanning wells…")
        threading.Thread(target=self._scan_wells_worker, args=(image_dir,), daemon=True).start()

    def _scan_wells_worker(self, image_dir: str) -> None:
        try:
            wells = list_wells(image_dir)
            self.well_scan_queue.put(("ok", wells))
        except Exception as e:
            self.well_scan_queue.put(("err", str(e)))

    def _poll_well_scan(self) -> None:
        try:
            while True:
                status, payload = self.well_scan_queue.get_nowait()
                if status == "ok":
                    if payload:
                        self.well_checklist.set_wells([f"W{w:04d}" for w in payload])
                    else:
                        self.well_checklist.set_status(
                            "No wells detected in this dataset.", error=True
                        )
                    self._update_well_note()
                else:
                    self.well_checklist.set_status(f"Could not read wells: {payload}", error=True)
        except queue.Empty:
            pass
        self.after(100, self._poll_well_scan)

    def _update_well_note(self) -> None:
        if self.split_by_w_var.get() != "1" and self.well_checklist.has_labels():
            self.well_checklist.set_note(
                "Note: only the first well listed will be processed unless "
                "'Split by well within field' is enabled above."
            )
        else:
            self.well_checklist.set_note("")

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

    def _build_raw_job(self) -> dict:
        base_path = self.base_path_var.get().strip()
        if not base_path:
            raise ValueError("Please choose a dataset folder.")

        channels = _parse_csv(self.channels_var.get())
        channel_names = _parse_csv(self.channel_names_var.get())
        if not channels or not channel_names:
            raise ValueError("Channels and channel names are required.")
        if len(channels) != len(channel_names):
            raise ValueError("Channels and channel names must have the same count.")

        job: dict = {
            "base_path": base_path,
            "channels": channels,
            "channel_names": channel_names,
            # Fixed at "intersection" rather than exposed as a choice: only
            # processes a field when both the on-disk FieldXXXX folder and
            # the OME-XML metadata agree it exists, which is what avoids
            # noisy per-field warnings for stray leftover folders or a
            # miscounted XML. Matches every job in this project's own
            # jobs_ome.json.
            "field_strategy": "intersection",
            "field_start": _parse_int(self.field_start_var.get(), 1),
            "enforce_equal_z": self.enforce_equal_z_var.get() == "1",
            "split_by_w_within_field": self.split_by_w_var.get() == "1",
            "generate_mip": self.generate_mip_var.get() == "1",
        }

        field_end = _parse_int(self.field_end_var.get(), None)
        if field_end is not None:
            job["field_end"] = field_end

        fields = _parse_csv(self.fields_var.get())
        if fields:
            job["fields"] = [int(f) for f in fields]

        dtype = self.dtype_var.get()
        if dtype and dtype != "auto":
            job["dtype"] = dtype

        save_dir = self.save_dir_var.get().strip()
        if save_dir:
            job["save_dir"] = save_dir

        only_well = _parse_int(self.only_well_var.get(), None)
        if only_well is not None:
            # Advanced override always wins -- guarantees a run is still
            # possible even if the automatic well scan failed or is stale.
            job["only_well"] = only_well
        elif not self.well_checklist.all_checked():
            checked = self.well_checklist.get_checked()
            if checked:
                job["only_wells"] = [int(label.lstrip("W")) for label in checked]

        job["mip"] = {
            "projection": self.mip_projection_var.get(),
            "channel_colors": _parse_csv(self.mip_colors_var.get()),
            "norm": {
                "p_low": _parse_float(self.mip_p_low_var.get(), 2.0),
                "p_high": _parse_float(self.mip_p_high_var.get(), 99.8),
                "clip": self.mip_clip_var.get() == "1",
            },
            "save": {
                "fmt": self.mip_fmt_var.get(),
                "dpi": _parse_int(self.mip_dpi_var.get(), 300),
                "panel": self.mip_panel_var.get() == "1",
                "panel_cols": _parse_int(self.mip_panel_cols_var.get(), 4),
                "zstack_panel": self.mip_zstack_panel_var.get() == "1",
            },
        }
        return job

    def _on_run(self) -> None:
        if self.running:
            return
        try:
            raw_job = self._build_raw_job()
        except Exception as e:
            messagebox.showerror("Invalid input", str(e))
            return

        self.running = True
        self.run_button.configure(state="disabled")
        self.open_folder_button.configure(state="disabled")
        self.log_widget.configure(state="normal")
        self.log_widget.delete("1.0", "end")
        self.log_widget.configure(state="disabled")

        thread = threading.Thread(target=self._run_pipeline, args=(raw_job,), daemon=True)
        thread.start()

    def _run_pipeline(self, raw_job: dict) -> None:
        old_stdout, old_stderr = sys.stdout, sys.stderr
        writer = QueueWriter(self.log_queue)
        sys.stdout = writer
        sys.stderr = writer
        try:
            job = _inject_paths(raw_job)
            job_cfg = _validate_job(job)
            outputs = assemble_one_job(job_cfg)
            print(f"\nWrote {len(outputs)} stack file(s) to {job_cfg.save_dir}\n")
            self.output_dir = job_cfg.save_dir

            if outputs and job.get("generate_mip", True):
                _run_mip_for_job(job, job_cfg, mip_defaults=None)

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
