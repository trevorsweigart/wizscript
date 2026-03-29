"""
App — orchestrator that wires together the client manager, game info
polling, teleporter, auto quester, auto combat, and GUI panels.

Uses a background thread for the asyncio event loop so that wizwalker's
async calls never block the tkinter UI.
"""

import asyncio
import threading
import traceback
import tkinter as tk
from tkinter import ttk

from client_manager import ClientManager
from game_info import fetch_all
from teleporter import teleport_absolute, teleport_relative, teleport_to_quest
from auto_quester import AutoQuester
from auto_combat import AutoCombat
from gui.theme import apply_theme
from gui.client_panel import ClientPanel
from gui.info_panel import InfoPanel
from gui.teleport_panel import TeleportPanel
from gui.auto_quest_panel import AutoQuestPanel
from gui.auto_combat_panel import AutoCombatPanel

# How often to poll game info (milliseconds)
POLL_INTERVAL_MS = 500


class App:
    """
    Main application class.

    Owns the tkinter root window, a background asyncio event loop,
    and wires all modules together.
    """

    def __init__(self):
        # Async setup — loop runs in a background thread
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(target=self._run_loop, daemon=True)
        self._loop_thread.start()

        # Core modules
        self._client_mgr = ClientManager()
        self._client_mgr.set_status_callback(self._on_client_status)

        self._auto_quester = AutoQuester()
        self._auto_quester.set_status_callback(self._on_auto_quest_status)
        self._auto_quester.set_stopped_callback(self._on_auto_quest_auto_stopped)

        self._auto_combat = AutoCombat()
        self._auto_combat.set_status_callback(self._on_auto_combat_status)

        # Tkinter root
        self._root = tk.Tk()
        self._root.title("WizTeleport")
        self._root.geometry("520x720")
        self._root.minsize(480, 660)
        self._root.resizable(True, True)

        apply_theme(self._root)
        self._build_ui()

        self._root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._polling = False

    # ------------------------------------------------------------------
    # Background async loop
    # ------------------------------------------------------------------

    def _run_loop(self):
        """Run the asyncio event loop forever in a background thread."""
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _run_async(self, coro, on_done=None, on_error=None):
        """
        Submit a coroutine to the background loop (non-blocking).

        Args:
            coro: The coroutine to run.
            on_done: Optional callback(result) called on the tkinter thread.
            on_error: Optional callback(exception) called on the tkinter thread.
        """
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)

        def _callback(fut):
            try:
                result = fut.result()
                if on_done:
                    self._root.after(0, on_done, result)
            except Exception as e:
                print(f"[async error] {e}")
                traceback.print_exc()
                if on_error:
                    self._root.after(0, on_error, e)

        future.add_done_callback(_callback)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        container = ttk.Frame(self._root, padding=8, style="Outer.TFrame")
        container.pack(fill="both", expand=True)

        self._client_panel = ClientPanel(
            container,
            on_refresh=self._handle_refresh,
            on_connect=self._handle_connect,
            on_disconnect=self._handle_disconnect,
        )
        self._client_panel.pack(fill="x", pady=(0, 6))

        self._info_panel = InfoPanel(
            container,
            on_toggle_quest_finder=self._handle_toggle_quest_finder,
        )
        self._info_panel.pack(fill="x", pady=(0, 6))

        self._teleport_panel = TeleportPanel(
            container,
            on_teleport=self._handle_teleport,
            on_teleport_quest=self._handle_quest_teleport,
        )
        self._teleport_panel.pack(fill="x", pady=(0, 6))

        self._auto_quest_panel = AutoQuestPanel(
            container,
            on_toggle=self._handle_auto_quest_toggle,
        )
        self._auto_quest_panel.pack(fill="x", pady=(0, 6))

        self._auto_combat_panel = AutoCombatPanel(
            container,
            on_toggle=self._handle_auto_combat_toggle,
        )
        self._auto_combat_panel.pack(fill="x")

    # ------------------------------------------------------------------
    # Client management handlers
    # ------------------------------------------------------------------

    def _handle_refresh(self) -> list:
        self._client_mgr.refresh()
        return self._client_mgr.get_client_labels()

    def _handle_connect(self, index: int):
        client = self._client_mgr.get_client_by_index(index)
        if client is None:
            self._client_panel.set_status("Invalid client selection")
            return

        self._client_panel.set_status("Connecting...")

        def _on_connected(success):
            if success:
                self._client_panel.set_connected(True)
                self._start_polling()

        self._run_async(
            self._client_mgr.connect(client),
            on_done=_on_connected,
        )

    def _handle_disconnect(self):
        # Stop all automation first
        if self._auto_quester.is_running:
            self._auto_quester.stop()
            self._auto_quest_panel.force_stop()
        if self._auto_combat.is_running:
            self._auto_combat.stop()
            self._auto_combat_panel.force_stop()
        self._stop_polling()

        def _on_disconnected(_result):
            self._client_panel.set_connected(False)
            self._info_panel.clear()

        self._run_async(
            self._client_mgr.disconnect(),
            on_done=_on_disconnected,
        )

    def _on_client_status(self, message: str):
        self._root.after(0, self._client_panel.set_status, message)

    def _handle_toggle_quest_finder(self, enabled: bool):
        client = self._client_mgr.active_client
        if client is None:
            return

        async def _toggle():
            game_stats = client.stats
            await game_stats.write_quest_finder_enabled(enabled)

        self._run_async(_toggle())

    # ------------------------------------------------------------------
    # Teleport handlers
    # ------------------------------------------------------------------

    def _handle_teleport(self, mode: str, x: float, y: float, z: float):
        client = self._client_mgr.active_client
        if client is None:
            self._teleport_panel.set_status("Not connected")
            return

        if mode == "absolute":
            coro = teleport_absolute(client, x, y, z)
            msg = f"Teleported to ({x:.1f}, {y:.1f}, {z:.1f})"
        else:
            coro = teleport_relative(client, x, y, z)
            msg = f"Moved by ({x:.1f}, {y:.1f}, {z:.1f})"

        self._run_async(
            coro,
            on_done=lambda _: self._teleport_panel.set_status(msg),
            on_error=lambda e: self._teleport_panel.set_status(f"Error: {e}"),
        )

    def _handle_quest_teleport(self):
        client = self._client_mgr.active_client
        if client is None:
            self._teleport_panel.set_status("Not connected")
            return

        self._run_async(
            teleport_to_quest(client),
            on_done=lambda _: self._teleport_panel.set_status("Teleported to quest objective"),
            on_error=lambda e: self._teleport_panel.set_status(f"Error: {e}"),
        )

    # ------------------------------------------------------------------
    # Auto quest handlers
    # ------------------------------------------------------------------

    def _handle_auto_quest_toggle(self, enabled: bool):
        client = self._client_mgr.active_client
        if client is None:
            self._auto_quest_panel.set_status("Not connected")
            self._auto_quest_panel.force_stop()
            return

        if enabled:
            self._auto_quester.start(client, self._loop)
        else:
            self._auto_quester.stop()

    def _on_auto_quest_status(self, message: str):
        self._root.after(0, self._auto_quest_panel.set_status, message)

    def _on_auto_quest_auto_stopped(self):
        self._root.after(0, self._auto_quest_panel.force_stop)

    # ------------------------------------------------------------------
    # Auto combat handlers
    # ------------------------------------------------------------------

    def _handle_auto_combat_toggle(self, enabled: bool):
        client = self._client_mgr.active_client
        if client is None:
            self._auto_combat_panel.set_status("Not connected")
            self._auto_combat_panel.force_stop()
            return

        if enabled:
            self._auto_combat.start(client, self._loop)
        else:
            self._auto_combat.stop()

    def _on_auto_combat_status(self, message: str):
        self._root.after(0, self._auto_combat_panel.set_status, message)

    # ------------------------------------------------------------------
    # Game info polling
    # ------------------------------------------------------------------

    def _start_polling(self):
        self._polling = True
        self._poll_tick()

    def _stop_polling(self):
        self._polling = False

    def _poll_tick(self):
        if not self._polling:
            return

        client = self._client_mgr.active_client
        if client is None:
            self._polling = False
            return

        def _on_state(state):
            if state and self._polling:
                self._info_panel.update_state(state)

        self._run_async(fetch_all(client), on_done=_on_state)
        self._root.after(POLL_INTERVAL_MS, self._poll_tick)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _on_close(self):
        if self._auto_quester.is_running:
            self._auto_quester.stop()
        if self._auto_combat.is_running:
            self._auto_combat.stop()
        self._stop_polling()

        # Synchronous shutdown — block briefly to clean up hooks
        future = asyncio.run_coroutine_threadsafe(
            self._client_mgr.close(), self._loop
        )
        try:
            future.result(timeout=5)
        except Exception:
            pass

        self._loop.call_soon_threadsafe(self._loop.stop)
        self._loop_thread.join(timeout=3)
        self._root.destroy()

    def run(self):
        self._root.mainloop()
