"""
Debug Panel  --  buttons that dump game info to the log for inspection.
"""

import tkinter as tk
from tkinter import ttk
from typing import Callable

from gui.collapsible import CollapsiblePanel


class DebugPanel(CollapsiblePanel):
    """Row of small buttons that dump diagnostic info to wizscript.log."""

    def __init__(
        self,
        parent: tk.Widget,
        on_print_entities: Callable[[], None],
        on_print_stats: Callable[[], None],
        on_print_state: Callable[[], None],
        on_print_zone_map: Callable[[], None],
        on_show_zone_graph: Callable[[], None],
    ):
        super().__init__(parent, title="Debug")
        self._on_print_entities = on_print_entities
        self._on_print_stats = on_print_stats
        self._on_print_state = on_print_state
        self._on_print_zone_map = on_print_zone_map
        self._on_show_zone_graph = on_show_zone_graph
        self._build_ui()

    def _build_ui(self):
        c = self.content

        row = ttk.Frame(c)
        row.grid(row=0, column=0, sticky="ew")

        ttk.Button(
            row, text="Print Nearby Entities",
            style="Small.TButton",
            command=self._on_print_entities,
        ).pack(side="left", padx=(0, 4))

        ttk.Button(
            row, text="Print Stats",
            style="Small.TButton",
            command=self._on_print_stats,
        ).pack(side="left", padx=(0, 4))

        ttk.Button(
            row, text="Print Game State",
            style="Small.TButton",
            command=self._on_print_state,
        ).pack(side="left", padx=(0, 4))

        ttk.Button(
            row, text="Print Zone Map",
            style="Small.TButton",
            command=self._on_print_zone_map,
        ).pack(side="left", padx=(0, 4))

        ttk.Button(
            row, text="Show Zone Graph",
            style="Small.TButton",
            command=self._on_show_zone_graph,
        ).pack(side="left", padx=(0, 4))

        self._status_var = tk.StringVar(value="Output goes to wizscript.log")
        ttk.Label(c, textvariable=self._status_var, style="Status.TLabel").grid(
            row=1, column=0, sticky="w", pady=(4, 0)
        )

        c.columnconfigure(0, weight=1)

    def set_status(self, message: str):
        self._status_var.set(message)
