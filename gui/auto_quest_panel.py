"""
Auto Quest Panel — toggle button, elapsed timer, and status display.
"""

import time
import tkinter as tk
from tkinter import ttk
from typing import Callable

from gui.collapsible import CollapsiblePanel


class AutoQuestPanel(CollapsiblePanel):
    """Panel with a toggle button, elapsed timer, and status label."""

    def __init__(
        self,
        parent: tk.Widget,
        on_toggle: Callable[[bool], None],
        on_heal_threshold_change: Callable[[float], None] = None,
        on_mana_threshold_change: Callable[[float], None] = None,
        initial_heal_pct: float = 50.0,
        initial_mana_pct: float = 50.0,
    ):
        super().__init__(parent, title="Auto Quest")

        self._on_toggle = on_toggle
        self._on_heal_threshold_change = on_heal_threshold_change
        self._on_mana_threshold_change = on_mana_threshold_change
        self._enabled = False
        self._start_time: float = 0.0
        self._timer_id = None
        self._initial_heal_pct = initial_heal_pct
        self._initial_mana_pct = initial_mana_pct

        self._build_ui()

    def _build_ui(self):
        c = self.content

        top_frame = ttk.Frame(c)
        top_frame.grid(row=0, column=0, sticky="ew")

        self._toggle_btn = ttk.Button(
            top_frame, text="Start Auto Quest",
            style="Success.TButton", command=self._handle_toggle,
        )
        self._toggle_btn.pack(side="left")

        # Heal threshold: triggers auto-collect (health) when HP drops below this %
        ttk.Label(top_frame, text="Heal at:").pack(side="left", padx=(10, 2))
        self._heal_pct_var = tk.StringVar(value=str(int(self._initial_heal_pct)))
        self._heal_spinbox = ttk.Spinbox(
            top_frame,
            from_=0, to=100, increment=5,
            width=4,
            textvariable=self._heal_pct_var,
            command=self._handle_heal_threshold_change,
        )
        self._heal_spinbox.pack(side="left")
        self._heal_spinbox.bind("<FocusOut>", lambda _e: self._handle_heal_threshold_change())
        self._heal_spinbox.bind("<Return>", lambda _e: self._handle_heal_threshold_change())
        ttk.Label(top_frame, text="%").pack(side="left", padx=(1, 0))

        # Mana threshold: triggers auto-collect (mana) when mana drops below this %
        ttk.Label(top_frame, text="Mana at:").pack(side="left", padx=(8, 2))
        self._mana_pct_var = tk.StringVar(value=str(int(self._initial_mana_pct)))
        self._mana_spinbox = ttk.Spinbox(
            top_frame,
            from_=0, to=100, increment=5,
            width=4,
            textvariable=self._mana_pct_var,
            command=self._handle_mana_threshold_change,
        )
        self._mana_spinbox.pack(side="left")
        self._mana_spinbox.bind("<FocusOut>", lambda _e: self._handle_mana_threshold_change())
        self._mana_spinbox.bind("<Return>", lambda _e: self._handle_mana_threshold_change())
        ttk.Label(top_frame, text="%").pack(side="left", padx=(1, 0))

        self._timer_var = tk.StringVar(value="")
        ttk.Label(top_frame, textvariable=self._timer_var,
                  font=("Consolas", 10), foreground="#8cb4ff",
                  ).pack(side="left", padx=(10, 0))

        self._status_var = tk.StringVar(value="Idle")
        ttk.Label(top_frame, textvariable=self._status_var,
                  style="Status.TLabel").pack(side="left", padx=(12, 0))

        c.columnconfigure(0, weight=1)

    def _handle_heal_threshold_change(self):
        if not self._on_heal_threshold_change:
            return
        try:
            value = float(self._heal_pct_var.get())
        except ValueError:
            return
        value = max(0.0, min(100.0, value))
        # Normalize displayed value if it was clamped
        self._heal_pct_var.set(str(int(value)))
        self._on_heal_threshold_change(value)

    def _handle_mana_threshold_change(self):
        if not self._on_mana_threshold_change:
            return
        try:
            value = float(self._mana_pct_var.get())
        except ValueError:
            return
        value = max(0.0, min(100.0, value))
        self._mana_pct_var.set(str(int(value)))
        self._on_mana_threshold_change(value)

    def _handle_toggle(self):
        self._enabled = not self._enabled
        if self._enabled:
            self._toggle_btn.configure(text="Stop Auto Quest", style="Danger.TButton")
            self._start_time = time.time()
            self._tick_timer()
        else:
            self._toggle_btn.configure(text="Start Auto Quest", style="Success.TButton")
            self._stop_timer()
        self._on_toggle(self._enabled)

    # ------------------------------------------------------------------
    # Timer
    # ------------------------------------------------------------------

    def _tick_timer(self):
        if not self._enabled:
            return
        elapsed = int(time.time() - self._start_time)
        hours, remainder = divmod(elapsed, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours > 0:
            self._timer_var.set(f"{hours}:{minutes:02d}:{seconds:02d}")
        else:
            self._timer_var.set(f"{minutes}:{seconds:02d}")
        self._timer_id = self.after(1000, self._tick_timer)

    def _stop_timer(self):
        if self._timer_id is not None:
            self.after_cancel(self._timer_id)
            self._timer_id = None
        self._timer_var.set("")

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def set_status(self, message: str):
        self._status_var.set(message)

    def force_stop(self):
        self._enabled = False
        self._toggle_btn.configure(text="Start Auto Quest", style="Success.TButton")
        self._stop_timer()
