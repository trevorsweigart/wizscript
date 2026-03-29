"""
Info Panel — displays live game info with player and quest positions side-by-side.
"""

import tkinter as tk
from tkinter import ttk
from typing import Callable

from game_info import GameState
from gui.collapsible import CollapsiblePanel


class InfoPanel(CollapsiblePanel):
    """
    Read-only panel showing game state.
    Player position and quest objective displayed side-by-side.
    """

    def __init__(self, parent: tk.Widget, on_toggle_quest_finder: Callable = None):
        super().__init__(parent, title="Game Info")
        self._vars: dict[str, tk.StringVar] = {}
        self._on_toggle_quest_finder = on_toggle_quest_finder
        self._build_ui()

    def _build_ui(self):
        c = self.content
        row = 0

        # ---- Position row: Player left, Quest right ----
        pos_frame = ttk.Frame(c)
        pos_frame.grid(row=row, column=0, sticky="ew")
        pos_frame.columnconfigure(0, weight=1)
        pos_frame.columnconfigure(1, weight=1)

        # Left: Player Position
        left = ttk.Frame(pos_frame)
        left.grid(row=0, column=0, sticky="nw")

        ttk.Label(left, text="Player Position", font=("Segoe UI", 9, "bold"),
                  foreground="#999999").grid(row=0, column=0, columnspan=2, sticky="w")

        for i, (label, key) in enumerate([("X:", "pos_x"), ("Y:", "pos_y"), ("Z:", "pos_z")]):
            ttk.Label(left, text=label).grid(row=i + 1, column=0, sticky="w", padx=(4, 6), pady=1)
            var = tk.StringVar(value="—")
            self._vars[key] = var
            ttk.Label(left, textvariable=var, style="Value.TLabel").grid(
                row=i + 1, column=1, sticky="w", pady=1,
            )

        # Right: Quest Objective
        right = ttk.Frame(pos_frame)
        right.grid(row=0, column=1, sticky="nw")

        ttk.Label(right, text="Quest Objective", font=("Segoe UI", 9, "bold"),
                  foreground="#999999").grid(row=0, column=0, columnspan=2, sticky="w")

        for i, (label, key) in enumerate([("X:", "quest_x"), ("Y:", "quest_y"), ("Z:", "quest_z")]):
            ttk.Label(right, text=label).grid(row=i + 1, column=0, sticky="w", padx=(4, 6), pady=1)
            var = tk.StringVar(value="—")
            self._vars[key] = var
            ttk.Label(right, textvariable=var, style="Value.TLabel").grid(
                row=i + 1, column=1, sticky="w", pady=1,
            )

        row += 1

        # ---- Separator ----
        ttk.Separator(c, orient="horizontal").grid(row=row, column=0, sticky="ew", pady=4)
        row += 1

        # ---- Zone + Quest IDs ----
        info_frame = ttk.Frame(c)
        info_frame.grid(row=row, column=0, sticky="ew")

        info_items = [
            ("Zone:", "zone"),
            ("Quest ID:", "quest_id"),
            ("Goal ID:", "goal_id"),
        ]

        for i, (label, key) in enumerate(info_items):
            ttk.Label(info_frame, text=label).grid(row=i, column=0, sticky="w", padx=(4, 6), pady=1)
            var = tk.StringVar(value="—")
            self._vars[key] = var
            ttk.Label(info_frame, textvariable=var, style="Value.TLabel").grid(
                row=i, column=1, sticky="w", pady=1,
            )

        # Quest Finder row with toggle button
        qf_row = len(info_items)
        ttk.Label(info_frame, text="Quest Finder:").grid(row=qf_row, column=0, sticky="w", padx=(4, 6), pady=1)
        qf_frame = ttk.Frame(info_frame)
        qf_frame.grid(row=qf_row, column=1, sticky="w", pady=1)
        var = tk.StringVar(value="—")
        self._vars["quest_finder"] = var
        ttk.Label(qf_frame, textvariable=var, style="Value.TLabel").pack(side="left")
        ttk.Button(qf_frame, text="Toggle", command=self._handle_quest_finder_toggle,
                   style="Small.TButton").pack(side="left", padx=(8, 0))

        c.columnconfigure(0, weight=1)

        self._quest_finder_state = False

    def _handle_quest_finder_toggle(self):
        if self._on_toggle_quest_finder:
            self._on_toggle_quest_finder(not self._quest_finder_state)

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def update_state(self, state: GameState):
        if state.valid:
            self._vars["pos_x"].set(f"{state.pos_x:.2f}")
            self._vars["pos_y"].set(f"{state.pos_y:.2f}")
            self._vars["pos_z"].set(f"{state.pos_z:.2f}")
        else:
            self._vars["pos_x"].set("—")
            self._vars["pos_y"].set("—")
            self._vars["pos_z"].set("—")

        self._vars["zone"].set(state.zone or "N/A")
        self._vars["quest_id"].set(str(state.quest_id) if state.quest_id else "—")
        self._vars["goal_id"].set(str(state.goal_id) if state.goal_id else "—")
        self._vars["quest_finder"].set("Enabled" if state.quest_finder_enabled else "Disabled")
        self._quest_finder_state = state.quest_finder_enabled
        self._vars["quest_x"].set(f"{state.quest_x:.2f}")
        self._vars["quest_y"].set(f"{state.quest_y:.2f}")
        self._vars["quest_z"].set(f"{state.quest_z:.2f}")

    def clear(self):
        for var in self._vars.values():
            var.set("—")
