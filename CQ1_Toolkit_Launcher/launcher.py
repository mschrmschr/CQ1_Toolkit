# file: launcher.py
"""Fiji-style launcher for the CQ1 toolkit.

Unzip the whole CQ1_Toolkit folder anywhere and double-click this exe (or
run this script directly with Python) to pick which CQ1 tool to run. Each
button starts the corresponding tool's own already-built standalone exe as
a separate process; this launcher has no import dependency on either
tool's pipeline code, so it can't collide with it.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tkinter as tk
from tkinter import messagebox, ttk

TOOLS = [
    (
        "Field → Stacks + MIP",
        "Generate CQ1 *.ome.tif stacks - field and well sensitive.",
        os.path.join("tools", "FieldToStacks", "CQ1_FieldtoStacks_parallel_MIP.exe"),
    ),
    (
        "Stitching + MIP",
        "Generate stitched CQ1 *.ome.tif stacks - field and well sensitive.",
        os.path.join("tools", "Stitching", "CQ1_Stitching_Wholemount_MIP.exe"),
    ),
]


def _base_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _launch(exe_path: str) -> None:
    if not os.path.isfile(exe_path):
        messagebox.showerror(
            "Tool not found",
            f"Could not find:\n{exe_path}\n\n"
            "This launcher expects the tool folders to stay next to it "
            "under 'tools\\'. Did you move this exe out of the CQ1_Toolkit folder?",
        )
        return
    try:
        subprocess.Popen([exe_path], cwd=os.path.dirname(exe_path))
    except OSError as e:
        messagebox.showerror("Launch failed", f"Could not start:\n{exe_path}\n\n{e}")


def main() -> None:
    base = _base_dir()

    root = tk.Tk()
    root.title("CQ1 Toolkit")
    root.geometry("480x260")
    root.resizable(False, False)

    ttk.Label(root, text="CQ1 Toolkit", font=("Segoe UI", 14, "bold")).pack(pady=(16, 4))
    ttk.Label(
        root,
        text="Pick a tool to launch. Each opens in its own window.\nBe patient, it will take 10-20 seconds to open.",
        justify="center",
        foreground="#555555",
    ).pack(pady=(0, 12))

    body = ttk.Frame(root)
    body.pack(fill="both", expand=True, padx=16)

    for title, desc, rel_path in TOOLS:
        card = ttk.Frame(body, relief="groove", padding=10)
        card.pack(fill="x", pady=6)

        ttk.Button(
            card,
            text=title,
            command=lambda p=os.path.join(base, rel_path): _launch(p),
        ).pack(side="left")
        ttk.Label(card, text=desc, wraplength=300, foreground="#555555").pack(
            side="left", padx=(10, 0)
        )

    root.mainloop()


if __name__ == "__main__":
    main()
