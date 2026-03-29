"""
Collapsible Panel — a frame with a clickable header that toggles content visibility.
"""

import tkinter as tk
from tkinter import ttk


class CollapsiblePanel(ttk.Frame):
    """
    A panel with a header bar that can be clicked to collapse/expand the content.

    Subclass this or pass content widgets into the `content` frame.

    Args:
        parent: Parent widget.
        title: Panel title displayed in the header.
        collapsed: Whether to start collapsed.
    """

    def __init__(self, parent: tk.Widget, title: str, collapsed: bool = False):
        super().__init__(parent)

        self._title = title
        self._collapsed = collapsed

        # Header bar
        self._header = ttk.Frame(self, style="Panel.TFrame")
        self._header.pack(fill="x")

        self._arrow_var = tk.StringVar()
        self._arrow_label = ttk.Label(
            self._header, textvariable=self._arrow_var,
            font=("Segoe UI", 9), cursor="hand2",
        )
        self._arrow_label.pack(side="left", padx=(6, 4))

        self._title_label = ttk.Label(
            self._header, text=title,
            font=("Segoe UI", 10, "bold"), cursor="hand2",
        )
        self._title_label.pack(side="left", pady=4)

        # Make the entire header clickable
        for widget in (self._header, self._arrow_label, self._title_label):
            widget.bind("<Button-1>", self._toggle)

        # Content area
        self.content = ttk.Frame(self, style="Panel.TFrame", padding=(10, 6))

        # Set initial state
        self._update_state()

    def _toggle(self, _event=None):
        self._collapsed = not self._collapsed
        self._update_state()

    def _update_state(self):
        self._arrow_var.set("▶" if self._collapsed else "▼")
        if self._collapsed:
            self.content.pack_forget()
        else:
            self.content.pack(fill="x")

    @property
    def is_collapsed(self) -> bool:
        return self._collapsed
