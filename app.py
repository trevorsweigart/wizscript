"""
App — orchestrator that wires together the client manager, game info
polling, teleporter, auto quester, auto combat, and GUI panels.

Uses a background thread for the asyncio event loop so that wizwalker's
async calls never block the tkinter UI.
"""

import asyncio
import logging
import math
import threading
import traceback
import tkinter as tk
from tkinter import ttk
from typing import Optional

from client_manager import ClientManager
from game_info import fetch_all
from teleporter import teleport_absolute, teleport_relative, teleport_to_quest
from auto_quester import AutoQuester
from auto_combat import AutoCombat
from auto_healer import (
    AutoHealer,
    EXIT_BATTLE, EXIT_CANCELLED, EXIT_COMPLETED, EXIT_NO_SOURCE,
)
from zone_mapper import ZoneMapper
from zone_walker import walk_zone_path
from gui.theme import apply_theme
from gui.client_panel import ClientPanel
from gui.info_panel import InfoPanel
from gui.teleport_panel import TeleportPanel
from gui.auto_quest_panel import AutoQuestPanel
from gui.auto_combat_panel import AutoCombatPanel
from gui.debug_panel import DebugPanel
from gui.zone_graph_window import ZoneGraphWindow

debug_log = logging.getLogger("debug")

# How often to poll game info (milliseconds)
POLL_INTERVAL_MS = 500

# Default heal trigger threshold (percentage of max HP)
DEFAULT_HEAL_THRESHOLD_PCT = 50.0
DEFAULT_MANA_THRESHOLD_PCT = 50.0


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

        self._zone_mapper = ZoneMapper()

        self._auto_quester = AutoQuester()
        self._auto_quester.set_status_callback(self._on_auto_quest_status)
        self._auto_quester.set_stopped_callback(self._on_auto_quest_auto_stopped)
        self._auto_quester.set_resource_request_callback(self._on_resource_requested)
        self._auto_quester.set_heal_threshold_pct(DEFAULT_HEAL_THRESHOLD_PCT)
        self._auto_quester.set_mana_threshold_pct(DEFAULT_MANA_THRESHOLD_PCT)
        self._auto_quester.set_zone_mapper(self._zone_mapper)

        self._auto_combat = AutoCombat()
        self._auto_combat.set_status_callback(self._on_auto_combat_status)

        self._auto_healer = AutoHealer()
        self._auto_healer.set_status_callback(self._on_auto_heal_status)
        self._auto_healer.set_done_callback(self._on_heal_done)
        self._auto_healer.set_heal_threshold_pct(DEFAULT_HEAL_THRESHOLD_PCT)
        self._auto_healer.set_mana_threshold_pct(DEFAULT_MANA_THRESHOLD_PCT)
        self._auto_healer.set_zone_mapper(self._zone_mapper)

        # User-intent flags  --  reflect what the user toggled in the UI.
        # The actual loops may be paused during heal mode.
        self._user_wants_quest = False
        self._user_wants_combat = False

        # Tkinter root
        self._root = tk.Tk()
        self._root.title("WizScript")
        self._root.geometry("520x720")
        self._root.minsize(480, 660)
        self._root.resizable(True, True)

        apply_theme(self._root)
        self._build_ui()

        self._root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._polling = False
        self._latest_zone: Optional[str] = None
        self._zone_graph_window = None

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
            on_teleport_zone=self._handle_zone_teleport,
            on_get_known_zones=self._get_known_zones,
        )
        self._teleport_panel.pack(fill="x", pady=(0, 6))

        self._auto_quest_panel = AutoQuestPanel(
            container,
            on_toggle=self._handle_auto_quest_toggle,
            on_heal_threshold_change=self._handle_heal_threshold_change,
            on_mana_threshold_change=self._handle_mana_threshold_change,
            initial_heal_pct=DEFAULT_HEAL_THRESHOLD_PCT,
            initial_mana_pct=DEFAULT_MANA_THRESHOLD_PCT,
        )
        self._auto_quest_panel.pack(fill="x", pady=(0, 6))

        self._auto_combat_panel = AutoCombatPanel(
            container,
            on_toggle=self._handle_auto_combat_toggle,
        )
        self._auto_combat_panel.pack(fill="x", pady=(0, 6))

        self._debug_panel = DebugPanel(
            container,
            on_print_entities=self._handle_debug_entities,
            on_print_stats=self._handle_debug_stats,
            on_print_state=self._handle_debug_state,
            on_print_zone_map=self._handle_debug_zone_map,
            on_show_zone_graph=self._handle_show_zone_graph,
        )
        self._debug_panel.pack(fill="x")

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
        if self._auto_healer.is_running:
            self._auto_healer.stop()
        if self._auto_quester.is_running:
            self._auto_quester.stop()
            self._auto_quest_panel.force_stop()
        if self._auto_combat.is_running:
            self._auto_combat.stop()
            self._auto_combat_panel.force_stop()
        self._user_wants_quest = False
        self._user_wants_combat = False
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
    # Zone teleport (multi-hop via ZoneMapper)
    # ------------------------------------------------------------------

    def _get_known_zones(self) -> list:
        data = self._zone_mapper.dump()
        return sorted(data.get("zones", {}).keys())

    def _handle_zone_teleport(self, target_zone: str):
        client = self._client_mgr.active_client
        if client is None:
            self._teleport_panel.set_status("Not connected")
            return

        # Refuse if a managed loop is already moving the player around
        if self._auto_quester.is_running or self._auto_healer.is_running:
            self._teleport_panel.set_status(
                "Stop auto-quest / auto-heal before zone-teleporting"
            )
            return

        self._teleport_panel.set_status(f"Planning route to {target_zone}...")

        # Resolve current zone and the path on the async loop, then walk
        self._run_async(
            self._plan_and_walk_zone(client, target_zone),
            on_error=lambda e: self._teleport_panel.set_status(f"Error: {e}"),
        )

    async def _plan_and_walk_zone(self, client, target_zone: str):
        try:
            current_zone = await client.zone_name()
        except Exception as e:
            self._set_teleport_status(f"Could not read current zone: {e}")
            return

        if not current_zone:
            self._set_teleport_status("Current zone is unknown")
            return

        known = set(self._zone_mapper.dump().get("zones", {}).keys())
        if current_zone not in known:
            self._set_teleport_status(
                f"Current zone {current_zone!r} not in zone map  --  walk through it once first"
            )
            return

        if target_zone == current_zone:
            self._set_teleport_status(f"Already in {target_zone}")
            return

        path = self._zone_mapper.find_path(current_zone, target_zone)
        if path is None:
            self._set_teleport_status(
                f"No path found from {current_zone} to {target_zone}"
            )
            return

        self._set_teleport_status(
            f"Route: {len(path)} hop(s) from {current_zone} to {target_zone}"
        )
        for i, t in enumerate(path):
            logging.getLogger("zone_walker").info(
                f"  hop {i + 1}: {t['from_zone']} -> {t['to_zone']} "
                f"via ({t['from_pos'][0]:.0f},{t['from_pos'][1]:.0f},{t['from_pos'][2]:.0f})"
            )

        ok = await self._walk_zone_path(client, path)
        if ok:
            self._set_teleport_status(f"Arrived at {target_zone}")
        # Failure case already set its own status

    async def _walk_zone_path(self, client, path: list) -> bool:
        """Step through each transition. Returns True on success."""
        return await walk_zone_path(client, path, on_status=self._set_teleport_status)

    def _set_teleport_status(self, message: str):
        """Thread-safe status update for the teleport panel."""
        self._root.after(0, self._teleport_panel.set_status, message)

    # ------------------------------------------------------------------
    # Auto quest handlers
    # ------------------------------------------------------------------

    def _handle_auto_quest_toggle(self, enabled: bool):
        client = self._client_mgr.active_client
        if client is None:
            self._auto_quest_panel.set_status("Not connected")
            self._auto_quest_panel.force_stop()
            return

        self._user_wants_quest = enabled
        if enabled:
            # If a heal is in progress, defer start  --  heal-done callback resumes us
            if not self._auto_healer.is_running:
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

        self._user_wants_combat = enabled
        if enabled:
            if not self._auto_healer.is_running:
                self._auto_combat.start(client, self._loop)
        else:
            self._auto_combat.stop()

    def _on_auto_combat_status(self, message: str):
        self._root.after(0, self._auto_combat_panel.set_status, message)

    # ------------------------------------------------------------------
    # Auto heal handlers (triggered by AutoQuester on low HP)
    # ------------------------------------------------------------------

    def _handle_heal_threshold_change(self, pct: float):
        self._auto_quester.set_heal_threshold_pct(pct)
        self._auto_healer.set_heal_threshold_pct(pct)

    def _handle_mana_threshold_change(self, pct: float):
        self._auto_quester.set_mana_threshold_pct(pct)
        self._auto_healer.set_mana_threshold_pct(pct)

    def _on_resource_requested(self, mode: str):
        """AutoQuester signalled low health/mana  --  hand off to AutoHealer."""
        self._root.after(0, self._enter_collect_mode, mode)

    def _enter_collect_mode(self, mode: str):
        client = self._client_mgr.active_client
        if client is None:
            return
        if self._auto_healer.is_running:
            return
        # Pause quest + combat loops while collecting (user intent preserved)
        if self._auto_quester.is_running:
            self._auto_quester.stop()
        if self._auto_combat.is_running:
            self._auto_combat.stop()
        self._auto_healer.start(client, self._loop, mode=mode)

    def _on_auto_heal_status(self, message: str):
        # Surface heal/collect status in the auto-quest panel (no dedicated UI row)
        self._root.after(0, self._auto_quest_panel.set_status, f"[heal] {message}")

    def _on_heal_done(self, reason: str, mode: str):
        """AutoHealer finished  --  always resume user-intended modes.

        If the reason is `no_source`, mark the resource unavailable in
        AutoQuester so we don't immediately re-trigger heal mode for
        the same resource. The flag is cleared automatically on the
        next zone change, giving the player a chance to pick up wisps
        in a newly-explored area.
        """
        self._root.after(0, self._exit_collect_mode, reason, mode)

    def _exit_collect_mode(self, reason: str, mode: str):
        client = self._client_mgr.active_client
        if client is None:
            return

        # User-facing summary
        status_map = {
            EXIT_COMPLETED: f"{mode.title()} collect complete",
            EXIT_BATTLE: f"{mode.title()} collect interrupted by battle",
            EXIT_NO_SOURCE: (
                f"No {mode} wisps in zone or reachable  --  "
                f"continuing quest, will retry when zone changes"
            ),
            EXIT_CANCELLED: f"{mode.title()} collect cancelled",
        }
        self._auto_quest_panel.set_status(status_map.get(reason, f"Heal exit: {reason}"))

        # Always restore user-intended modes (no more stop-all on heal failure)
        if self._user_wants_combat and not self._auto_combat.is_running:
            self._auto_combat.start(client, self._loop)
        if self._user_wants_quest and not self._auto_quester.is_running:
            self._auto_quester.start(client, self._loop)

        # Suppress re-trigger of THIS resource until something changes
        if reason == EXIT_NO_SOURCE:
            self._auto_quester.mark_resource_unavailable(mode)

    # ------------------------------------------------------------------
    # Debug handlers
    # ------------------------------------------------------------------

    def _handle_debug_entities(self):
        client = self._client_mgr.active_client
        if client is None:
            self._debug_panel.set_status("Not connected")
            return
        self._debug_panel.set_status("Scanning entities...")
        self._run_async(
            self._dump_nearby_entities(client),
            on_done=lambda count: self._debug_panel.set_status(
                f"Logged {count} entities  --  see wizscript.log"
            ),
            on_error=lambda e: self._debug_panel.set_status(f"Error: {e}"),
        )

    def _handle_debug_stats(self):
        client = self._client_mgr.active_client
        if client is None:
            self._debug_panel.set_status("Not connected")
            return
        self._debug_panel.set_status("Reading stats...")
        self._run_async(
            self._dump_player_stats(client),
            on_done=lambda _: self._debug_panel.set_status("Stats logged  --  see wizscript.log"),
            on_error=lambda e: self._debug_panel.set_status(f"Error: {e}"),
        )

    def _handle_debug_state(self):
        client = self._client_mgr.active_client
        if client is None:
            self._debug_panel.set_status("Not connected")
            return
        self._debug_panel.set_status("Reading game state...")
        self._run_async(
            self._dump_game_state(client),
            on_done=lambda _: self._debug_panel.set_status("State logged  --  see wizscript.log"),
            on_error=lambda e: self._debug_panel.set_status(f"Error: {e}"),
        )

    def _handle_show_zone_graph(self):
        """Open (or focus) the live zone-graph window."""
        try:
            if self._zone_graph_window is not None and self._zone_graph_window._win.winfo_exists():
                self._zone_graph_window._win.deiconify()
                self._zone_graph_window._win.lift()
                self._zone_graph_window._win.focus_set()
                return
        except Exception:
            pass
        try:
            self._zone_graph_window = ZoneGraphWindow(
                self._root,
                self._zone_mapper,
                get_current_zone=lambda: self._latest_zone,
            )
        except Exception as e:
            self._debug_panel.set_status(f"Could not open graph: {e}")

    def _handle_debug_zone_map(self):
        """Dump the persisted zone graph to the log (no client required)."""
        s = self._zone_mapper.summary()
        data = self._zone_mapper.dump()

        debug_log.info("=== Zone Map ===")
        debug_log.info(
            f"  zones={s['zone_count']}  transitions={s['transition_count']}  "
            f"health_zones={s['zones_with_health_wisps']}  "
            f"mana_zones={s['zones_with_mana_wisps']}"
        )

        debug_log.info("--- Zones ---")
        for name, z in sorted(data.get("zones", {}).items()):
            flags = []
            if z.get("has_health_wisps"):
                flags.append("H")
            if z.get("has_mana_wisps"):
                flags.append("M")
            flag_str = f"[{''.join(flags)}]" if flags else "[ ]"
            debug_log.info(
                f"  {flag_str} {name}  visits={z.get('visit_count', 0)}  "
                f"last_seen={z.get('last_seen', '?')}"
            )

        debug_log.info("--- Transitions ---")
        for t in data.get("transitions", []):
            fp = t["from_pos"]
            tp = t["to_pos"]
            debug_log.info(
                f"  {t['from_zone']}@({fp[0]:.0f},{fp[1]:.0f},{fp[2]:.0f})"
                f"  ->  {t['to_zone']}@({tp[0]:.0f},{tp[1]:.0f},{tp[2]:.0f})"
                f"  count={t.get('count', 1)}"
            )

        self._debug_panel.set_status(
            f"Zone map: {s['zone_count']} zones, {s['transition_count']} transitions"
            f"  --  see wizscript.log + zone_map.json"
        )

    async def _dump_nearby_entities(self, client) -> int:
        entities = await client.get_base_entity_list()
        player_pos = await client.body.position()

        rows = []
        for entity in entities:
            try:
                template = await entity.object_template()
                if template is None:
                    continue
                obj_name = await template.object_name() or ""
                try:
                    disp = await entity.display_name() or ""
                except Exception:
                    disp = ""
                body = await entity.actor_body()
                if body is None:
                    continue
                e_pos = await body.position()
                dist = math.sqrt(
                    (player_pos.x - e_pos.x) ** 2
                    + (player_pos.y - e_pos.y) ** 2
                    + (player_pos.z - e_pos.z) ** 2
                )
                rows.append((dist, obj_name, disp, e_pos))
            except Exception:
                continue

        rows.sort(key=lambda r: r[0])
        debug_log.info(f"=== Nearby entities (n={len(rows)}) ===")
        for dist, name, disp, pos in rows:
            debug_log.info(
                f"  dist={dist:7.1f}  obj={name!r}  disp={disp!r}  "
                f"pos=({pos.x:.0f},{pos.y:.0f},{pos.z:.0f})"
            )
        return len(rows)

    async def _dump_player_stats(self, client):
        stats = client.stats
        debug_log.info("=== Player stats ===")

        async def _safe(label, coro):
            try:
                value = await coro
                debug_log.info(f"  {label}: {value}")
            except Exception as e:
                debug_log.info(f"  {label}: <read failed: {e}>")

        try:
            cur_hp = await stats.current_hitpoints()
            mx_hp = await stats.max_hitpoints()
            debug_log.info(f"  HP: {cur_hp} / {mx_hp}")
        except Exception as e:
            debug_log.info(f"  HP: <read failed: {e}>")

        try:
            cur_mp = await stats.current_mana()
            mx_mp = await stats.max_mana()
            debug_log.info(f"  Mana: {cur_mp} / {mx_mp}")
        except Exception as e:
            debug_log.info(f"  Mana: <read failed: {e}>")

        await _safe("Gold", stats.current_gold())
        await _safe("Energy max", stats.energy_max())
        try:
            energy = await client.current_energy()
            debug_log.info(f"  Energy current: {energy}")
        except Exception as e:
            debug_log.info(f"  Energy current: <read failed: {e}>")

        await _safe("Reference level", stats.reference_level())
        await _safe("School ID", stats.school_id())
        await _safe("Dmg bonus % (all)", stats.dmg_bonus_percent_all())
        await _safe("Heal bonus % (all)", stats.heal_bonus_percent_all())

    async def _dump_game_state(self, client):
        debug_log.info("=== Game state ===")

        async def _safe(label, coro):
            try:
                debug_log.info(f"  {label}: {await coro}")
            except Exception as e:
                debug_log.info(f"  {label}: <read failed: {e}>")

        await _safe("Zone", client.zone_name())
        await _safe("In battle", client.in_battle())
        await _safe("In dialog", client.is_in_dialog())
        await _safe("In NPC range", client.is_in_npc_range())
        await _safe("Is loading", client.is_loading())

        try:
            pos = await client.body.position()
            debug_log.info(f"  Position: ({pos.x:.1f}, {pos.y:.1f}, {pos.z:.1f})")
        except Exception as e:
            debug_log.info(f"  Position: <read failed: {e}>")

        try:
            qpos = await client.quest_position.position()
            debug_log.info(f"  Quest pos: ({qpos.x:.1f}, {qpos.y:.1f}, {qpos.z:.1f})")
        except Exception as e:
            debug_log.info(f"  Quest pos: <read failed: {e}>")

        await _safe("Quest ID", client.quest_id())
        await _safe("Goal ID", client.goal_id())

        try:
            used, cap = await client.backpack_space()
            debug_log.info(f"  Backpack: {used} / {cap}")
        except Exception as e:
            debug_log.info(f"  Backpack: <read failed: {e}>")

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
                if state.valid and state.zone and state.zone != "N/A":
                    self._latest_zone = state.zone

        self._run_async(fetch_all(client), on_done=_on_state)
        self._root.after(POLL_INTERVAL_MS, self._poll_tick)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _on_close(self):
        if self._auto_healer.is_running:
            self._auto_healer.stop()
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
