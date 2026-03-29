"""
Client Panel — UI component for selecting and managing Wizard101 clients.
"""

import tkinter as tk
from tkinter import ttk
from typing import Callable, Optional

from gui.collapsible import CollapsiblePanel


class ClientPanel(CollapsiblePanel):
    """Panel for client detection, selection, connection, and disconnection."""

    def __init__(
        self,
        parent: tk.Widget,
        on_refresh: Callable,
        on_connect: Callable[[int], None],
        on_disconnect: Callable,
    ):
        super().__init__(parent, title="Client Management")

        self._on_refresh = on_refresh
        self._on_connect = on_connect
        self._on_disconnect = on_disconnect

        self._build_ui()

    def _build_ui(self):
        c = self.content

        # Row 0 — Client selector
        ttk.Label(c, text="Client:").grid(row=0, column=0, sticky="w", padx=(0, 6))

        self._client_var = tk.StringVar()
        self._combo = ttk.Combobox(c, textvariable=self._client_var, state="readonly", width=35)
        self._combo.grid(row=0, column=1, sticky="ew", pady=2)

        # Row 1 — Action buttons
        btn_frame = ttk.Frame(c)
        btn_frame.grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 2))

        self._refresh_btn = ttk.Button(btn_frame, text="Refresh", command=self._handle_refresh)
        self._refresh_btn.pack(side="left", padx=(0, 4))

        self._connect_btn = ttk.Button(
            btn_frame, text="Connect", style="Success.TButton", command=self._handle_connect,
        )
        self._connect_btn.pack(side="left", padx=(0, 4))

        self._disconnect_btn = ttk.Button(
            btn_frame, text="Disconnect", style="Danger.TButton",
            command=self._handle_disconnect, state="disabled",
        )
        self._disconnect_btn.pack(side="left")

        # Row 2 — Status
        self._status_var = tk.StringVar(value="No clients detected")
        ttk.Label(c, textvariable=self._status_var, style="Status.TLabel").grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(2, 0),
        )

        c.columnconfigure(1, weight=1)

    def _handle_refresh(self):
        labels = self._on_refresh()
        self._combo["values"] = labels
        if labels:
            self._combo.current(0)

    def _handle_connect(self):
        idx = self._combo.current()
        if idx < 0:
            self.set_status("Select a client first")
            return
        self._on_connect(idx)

    def _handle_disconnect(self):
        self._on_disconnect()

    def set_status(self, message: str):
        self._status_var.set(message)

    def set_connected(self, connected: bool):
        if connected:
            self._connect_btn.configure(state="disabled")
            self._disconnect_btn.configure(state="normal")
            self._combo.configure(state="disabled")
        else:
            self._connect_btn.configure(state="normal")
            self._disconnect_btn.configure(state="disabled")
            self._combo.configure(state="readonly")
