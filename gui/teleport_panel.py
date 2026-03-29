"""
Teleport Panel — UI controls for coordinate teleportation.
"""

import tkinter as tk
from tkinter import ttk
from typing import Callable

from gui.collapsible import CollapsiblePanel


class TeleportPanel(CollapsiblePanel):
    """Panel with XYZ coordinate entry, mode toggle, and teleport buttons."""

    def __init__(
        self,
        parent: tk.Widget,
        on_teleport: Callable[[str, float, float, float], None],
        on_teleport_quest: Callable,
    ):
        super().__init__(parent, title="Teleport")

        self._on_teleport = on_teleport
        self._on_teleport_quest = on_teleport_quest

        self._build_ui()

    def _build_ui(self):
        c = self.content

        # Row 0 — Coordinate entries
        self._x_var = tk.StringVar(value="0")
        self._y_var = tk.StringVar(value="0")
        self._z_var = tk.StringVar(value="0")

        coord_frame = ttk.Frame(c)
        coord_frame.grid(row=0, column=0, sticky="w", pady=(0, 4))

        for label, var in [("X:", self._x_var), ("Y:", self._y_var), ("Z:", self._z_var)]:
            ttk.Label(coord_frame, text=label).pack(side="left", padx=(0, 2))
            ttk.Entry(coord_frame, textvariable=var, width=9, justify="center").pack(
                side="left", padx=(0, 8),
            )

        # Row 1 — Mode toggle
        mode_frame = ttk.Frame(c)
        mode_frame.grid(row=1, column=0, sticky="w", pady=(0, 4))

        self._mode_var = tk.StringVar(value="absolute")
        ttk.Radiobutton(mode_frame, text="Absolute", variable=self._mode_var,
                        value="absolute").pack(side="left", padx=(0, 12))
        ttk.Radiobutton(mode_frame, text="Relative", variable=self._mode_var,
                        value="relative").pack(side="left")

        # Row 2 — Buttons
        btn_frame = ttk.Frame(c)
        btn_frame.grid(row=2, column=0, sticky="w")

        ttk.Button(btn_frame, text="Teleport",
                   command=self._handle_teleport).pack(side="left", padx=(0, 4))
        ttk.Button(btn_frame, text="Teleport to Quest", style="Success.TButton",
                   command=self._handle_quest_teleport).pack(side="left")

        # Row 3 — Status
        self._status_var = tk.StringVar(value="")
        ttk.Label(c, textvariable=self._status_var, style="Status.TLabel").grid(
            row=3, column=0, sticky="w", pady=(4, 0),
        )

    def _handle_teleport(self):
        try:
            x = float(self._x_var.get())
            y = float(self._y_var.get())
            z = float(self._z_var.get())
        except ValueError:
            self.set_status("Invalid coordinates — enter numbers")
            return
        self._on_teleport(self._mode_var.get(), x, y, z)

    def _handle_quest_teleport(self):
        self._on_teleport_quest()

    def set_status(self, message: str):
        self._status_var.set(message)
