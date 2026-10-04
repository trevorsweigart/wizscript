"""
Auto Combat Panel — toggle button and status display for auto-combat.
"""

import tkinter as tk
from tkinter import ttk
from typing import Callable

from gui.collapsible import CollapsiblePanel
from jev_credentials import save_api_key


class AutoCombatPanel(CollapsiblePanel):
    """Panel with a toggle button to start/stop auto-combat."""

    def __init__(self, parent: tk.Widget, on_toggle: Callable[[bool], None], on_snapshot=None):
        super().__init__(parent, title="Auto Combat")

        self._on_toggle = on_toggle
        self._on_snapshot = on_snapshot
        self._enabled = False

        self._build_ui()

    def _build_ui(self):
        c = self.content

        top_frame = ttk.Frame(c)
        top_frame.grid(row=0, column=0, sticky="ew")

        self._toggle_btn = ttk.Button(
            top_frame, text="Start Jev Combat",
            style="Success.TButton", command=self._handle_toggle,
        )
        self._toggle_btn.pack(side="left")

        self._status_var = tk.StringVar(value="Idle")
        ttk.Label(top_frame, textvariable=self._status_var,
                  style="Status.TLabel", wraplength=240).pack(side="left", padx=(12, 0))

        key_frame = ttk.Frame(c)
        key_frame.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        ttk.Label(key_frame, text="Jev API key").pack(side="left")
        self._key_var = tk.StringVar()
        ttk.Entry(key_frame, textvariable=self._key_var, show="*", width=25).pack(side="left", padx=6, fill="x", expand=True)
        ttk.Button(key_frame, text="Save key", command=self._save_key).pack(side="left")
        if self._on_snapshot:
            ttk.Button(c, text="Export combat snapshot", command=self._on_snapshot).grid(row=2, column=0, sticky="w", pady=(6, 0))

        c.columnconfigure(0, weight=1)

    def _handle_toggle(self):
        self._enabled = not self._enabled
        if self._enabled:
            self._toggle_btn.configure(text="Stop Jev Combat", style="Danger.TButton")
        else:
            self._toggle_btn.configure(text="Start Jev Combat", style="Success.TButton")
        self._on_toggle(self._enabled)

    def set_status(self, message: str):
        self._status_var.set(message)

    def _save_key(self):
        try:
            save_api_key(self._key_var.get())
            self._key_var.set("")
            self.set_status("Jev key saved. Restart Jev Combat to use it.")
        except (RuntimeError, ValueError, OSError) as error:
            self.set_status(str(error))

    def force_stop(self):
        self._enabled = False
        self._toggle_btn.configure(text="Start Jev Combat", style="Success.TButton")
