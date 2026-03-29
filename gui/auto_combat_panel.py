"""
Auto Combat Panel — toggle button and status display for auto-combat.
"""

import tkinter as tk
from tkinter import ttk
from typing import Callable

from gui.collapsible import CollapsiblePanel


class AutoCombatPanel(CollapsiblePanel):
    """Panel with a toggle button to start/stop auto-combat."""

    def __init__(self, parent: tk.Widget, on_toggle: Callable[[bool], None]):
        super().__init__(parent, title="Auto Combat")

        self._on_toggle = on_toggle
        self._enabled = False

        self._build_ui()

    def _build_ui(self):
        c = self.content

        top_frame = ttk.Frame(c)
        top_frame.grid(row=0, column=0, sticky="ew")

        self._toggle_btn = ttk.Button(
            top_frame, text="Start Auto Combat",
            style="Success.TButton", command=self._handle_toggle,
        )
        self._toggle_btn.pack(side="left")

        self._status_var = tk.StringVar(value="Idle")
        ttk.Label(top_frame, textvariable=self._status_var,
                  style="Status.TLabel").pack(side="left", padx=(12, 0))

        c.columnconfigure(0, weight=1)

    def _handle_toggle(self):
        self._enabled = not self._enabled
        if self._enabled:
            self._toggle_btn.configure(text="Stop Auto Combat", style="Danger.TButton")
        else:
            self._toggle_btn.configure(text="Start Auto Combat", style="Success.TButton")
        self._on_toggle(self._enabled)

    def set_status(self, message: str):
        self._status_var.set(message)

    def force_stop(self):
        self._enabled = False
        self._toggle_btn.configure(text="Start Auto Combat", style="Success.TButton")
