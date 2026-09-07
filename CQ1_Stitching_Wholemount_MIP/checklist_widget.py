# file: checklist_widget.py
"""Reusable Tkinter checkbox-list widget for showing scanned well labels.

Copied verbatim into each CQ1 tool's project root -- not imported across
projects (see CQ1_TOOLKIT_LAUNCHER_PLAN.md section 1 for why these tools
deliberately don't share an importable package: PyInstaller freezes by
module name, and a real shared import would need its own packaging step
for one small widget). Keep this file domain-agnostic (plain "labels +
checkboxes"); any project-specific wording/logic belongs in that
project's own gui.py, not here, so this file can stay byte-identical
between the two copies.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import List


class WellChecklist(ttk.Frame):
    """A status label OR a wrapped grid of checkboxes, one per well label.

    Usage:
        checklist = WellChecklist(parent, columns=6)
        checklist.grid(...)
        checklist.clear()                        # before a new scan starts
        checklist.set_status("Scanning wells…")   # while scanning
        checklist.set_wells(["W0001", "W0002"])   # scan finished, all pre-checked
        checklist.get_checked()                   # -> list[str], current selection
        checklist.all_checked()                   # -> bool (also True if empty/unscanned)
        checklist.set_note("...")                 # optional one-line note under the list
    """

    def __init__(self, parent, columns: int = 6):
        super().__init__(parent)
        self.columns = columns
        self._vars: "dict[str, tk.BooleanVar]" = {}

        self._button_row = ttk.Frame(self)
        ttk.Button(self._button_row, text="Select all", command=self.select_all).pack(side="left")
        ttk.Button(self._button_row, text="Select none", command=self.select_none).pack(
            side="left", padx=(6, 0)
        )

        self._status_label = ttk.Label(self, foreground="#666666")
        self._checks_frame = ttk.Frame(self)
        self._note_label = ttk.Label(self, foreground="#666666", wraplength=560)

        self.clear()

    # ---- state transitions ----

    def clear(self) -> None:
        """Reset to an empty, unscanned state -- nothing shown until the next
        set_status()/set_wells() call (called at the top of every new scan so
        a stale checklist from a previously browsed folder never lingers)."""
        self._vars = {}
        for w in self._checks_frame.winfo_children():
            w.destroy()
        self._button_row.grid_remove()
        self._checks_frame.grid_remove()
        self._status_label.grid_remove()
        self._note_label.grid_remove()

    def set_status(self, text: str, error: bool = False) -> None:
        """Show a one-line status message instead of checkboxes (scanning/error/empty)."""
        self._vars = {}
        for w in self._checks_frame.winfo_children():
            w.destroy()
        self._button_row.grid_remove()
        self._checks_frame.grid_remove()
        self._note_label.grid_remove()
        self._status_label.configure(text=text, foreground="#CC0000" if error else "#666666")
        self._status_label.grid(row=0, column=0, sticky="w")

    def set_wells(self, labels: List[str]) -> None:
        """Populate the checklist from a freshly scanned list of well labels, all pre-checked."""
        if not labels:
            self.set_status("No wells detected in this dataset.", error=True)
            return

        self._vars = {}
        for w in self._checks_frame.winfo_children():
            w.destroy()
        self._status_label.grid_remove()

        self._button_row.grid(row=0, column=0, sticky="w")
        self._checks_frame.grid(row=1, column=0, sticky="w", pady=(4, 0))
        for i, label in enumerate(labels):
            var = tk.BooleanVar(value=True)
            self._vars[label] = var
            r, c = divmod(i, self.columns)
            ttk.Checkbutton(self._checks_frame, text=label, variable=var).grid(
                row=r, column=c, sticky="w", padx=(0, 12), pady=1
            )

    def set_note(self, text: str) -> None:
        """Show (or clear, if `text` is falsy) a one-line note under the checklist."""
        if text:
            self._note_label.configure(text=text)
            self._note_label.grid(row=2, column=0, sticky="w", pady=(4, 0))
        else:
            self._note_label.grid_remove()

    # ---- selection ----

    def select_all(self) -> None:
        for var in self._vars.values():
            var.set(True)

    def select_none(self) -> None:
        for var in self._vars.values():
            var.set(False)

    def get_checked(self) -> List[str]:
        return [label for label, var in self._vars.items() if var.get()]

    def all_checked(self) -> bool:
        """True if every known well is checked -- also True when nothing has
        been scanned yet, so callers can treat that the same as "no filter"."""
        return all(var.get() for var in self._vars.values())

    def has_labels(self) -> bool:
        """True once a scan has populated at least one well label."""
        return bool(self._vars)
