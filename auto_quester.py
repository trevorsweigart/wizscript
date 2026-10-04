"""
Auto Quester — automated quest progression loop.

Teleports to quest objectives, handles NPC dialog, and includes a safety
mechanism that disables itself if a teleport fails repeatedly (player snaps back).
"""

import asyncio
import math
import time
import logging
from collections import deque
from typing import Callable, Optional

from wizwalker.client import Client
from wizwalker.constants import Keycode
from wizwalker.utils import XYZ

from game_info import fetch_position, fetch_quest_position
from entities import detect_wisps_in_zone, list_nearby_entities
from zone_mapper import ZoneMapper
from zone_walker import walk_zone_path
from quest_state import safe_read, visible_ui, quest_entries
from wizwalker.memory.memory_objects.enums import ObjectType
from quest_navigation import MainQuestNavigator, plain_text
from quest_dialog import advance_dialog, DialogNavigator
from quest_npc import approach_point, prompt_name
from quest_objects import active_usage_goal, matches_object, defeat_target, sigil_approach
from quest_mobs import approach_enemy
from spell_management import SchoolTrainer, DeckManager, SchoolVisits
from quest_search import ZoneSearch

log = logging.getLogger(__name__)

# How close (in units) two positions must be to count as "same location"
SNAP_BACK_THRESHOLD = 50.0

# Delay after teleport to check if player snapped back
POST_TELEPORT_CHECK_DELAY = 0.6

# How many consecutive snap-backs before auto-quest disables
MAX_CONSECUTIVE_FAILURES = 3

# Main loop tick rate
TICK_DELAY = 0.2

# How long to wait between loading screen checks
LOADING_CHECK_DELAY = 0.5

# A pending transition is only committed if its teleport happened within
# this many seconds of the observed zone change. Bounds the risk of
# falsely attributing a zone change to a much-earlier teleport.
PENDING_TRANSITION_TTL = 30.0

# If the active goal_id hasn't changed in this many seconds of active
# questing (not counting time spent in combat/dialog/loading/heal), we
# assume we've hit the "stale-objective" bug and try to recover.
STUCK_THRESHOLD_SECS = 30.0

# How many recent advancement positions to remember for the recovery
# routine to revisit.
OBJECTIVE_HISTORY_MAX = 5

# After each recovery attempt, wait this long for goal_id to advance
# before declaring the attempt failed.
RECOVERY_WAIT_SECS = 3.0


def _distance(a: XYZ, b: XYZ) -> float:
    """Euclidean distance between two XYZ points (all axes)."""
    return math.sqrt(
        (a.x - b.x) ** 2 + (a.y - b.y) ** 2 + (a.z - b.z) ** 2
    )


class AutoQuester:
    """
    Runs an async loop that:
      1. Teleports to the current quest objective
      2. Interacts with NPCs if in range
      3. Advances through dialog with SPACEBAR
      4. Automatically stops if teleport fails repeatedly (player snaps back)

    Does NOT handle combat — it pauses while the player is in battle.
    """

    def __init__(self):
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._on_status: Optional[Callable[[str], None]] = None
        self._on_stopped: Optional[Callable[[], None]] = None
        self._on_resource_request: Optional[Callable[[str], None]] = None
        self._consecutive_failures = 0
        self._heal_threshold_pct: float = 50.0
        self._mana_threshold_pct: float = 50.0
        self._resource_requested: bool = False
        self._zone_mapper: Optional[ZoneMapper] = None
        # Set when a teleport that may have triggered a zone change is in flight
        # Tuple of (pre_zone_name, teleport_target_xyz_as_tuple, timestamp)
        self._pending_transition: Optional[tuple] = None
        # Zones we've already scanned for wisps this session (avoid rescanning)
        self._scanned_zones: set = set()
        # Zone observed at the top of the previous tick
        self._last_known_zone: Optional[str] = None

        # ---- Stale-objective / stuck detection ----
        # The currently observed goal_id and when we first observed it
        self._last_goal_id: Optional[int] = None
        self._stuck_timer_start: Optional[float] = None
        # Recent (zone, (x,y,z)) tuples captured each time goal_id advanced
        self._objective_history: deque = deque(maxlen=OBJECTIVE_HISTORY_MAX)
        # Re-entrancy guard for the recovery routine
        self._in_recovery: bool = False

        # Modes ("health" / "mana") AutoHealer reported as unreachable.
        # Suppressed from re-requesting until the next zone change, which
        # gives the player a chance to reach a zone that does have wisps.
        self._unavailable_modes: set = set()
        self._quest_navigator = MainQuestNavigator(self._emit_status)
        self._dialog_navigator = DialogNavigator()
        self._school_trainer = SchoolTrainer(self._emit_status)
        self._deck_manager = DeckManager(self._emit_status)
        self._school_visits = SchoolVisits(self._emit_status)
        self._last_dialog_point = None
        self._followup_attempts = 0
        self._was_in_dialog = False
        self._quest_client = None
        self._expected_talk_name = None
        self._object_attempts = {}
        self._object_progress = {}
        self._object_search = {}
        self._zone_search = ZoneSearch()
        self._search_visited = {}
        self._entry_attempts = {}
        self._entry_attempt_key = None
        self._entry_walk_target = None
        self._npc_walk_target = None
        self._talk_attempts = {}

    @property
    def is_running(self) -> bool:
        return self._running

    def set_status_callback(self, callback: Callable[[str], None]):
        self._on_status = callback

    def set_stopped_callback(self, callback: Callable[[], None]):
        """Called when auto-quest stops itself (e.g. failed teleport)."""
        self._on_stopped = callback

    def set_resource_request_callback(self, callback: Callable[[str], None]):
        """Called with mode ("health" or "mana") when that resource drops
        below its threshold and a collect cycle should run.
        """
        self._on_resource_request = callback

    def set_heal_threshold_pct(self, pct: float):
        """Trigger health-collect mode when HP/max < this percentage. 0 disables."""
        self._heal_threshold_pct = max(0.0, min(100.0, pct))

    def set_mana_threshold_pct(self, pct: float):
        """Trigger mana-collect mode when mana/max < this percentage. 0 disables."""
        self._mana_threshold_pct = max(0.0, min(100.0, pct))

    def mark_resource_unavailable(self, mode: str):
        """Called by App after AutoHealer reports `no_source` for `mode`.

        Suppresses re-triggering heal mode for that resource until the
        next zone change, so we don't immediately re-fire heal mode
        knowing there's nothing to collect. Continuing to quest will
        eventually move the player into a new zone, where this flag is
        cleared and the resource gets a fresh chance.
        """
        if mode:
            self._unavailable_modes.add(mode)
            self._emit_status(
                f"Suppressing {mode} heal-mode until zone changes"
            )

    def set_zone_mapper(self, mapper: Optional[ZoneMapper]):
        """Optional ZoneMapper that will receive transition + wisp observations."""
        self._zone_mapper = mapper

    def _emit_status(self, msg: str):
        if msg != getattr(self, "_last_status", None):
            log.info(msg)
            self._last_status = msg
        if self._on_status:
            self._on_status(msg)

    # ------------------------------------------------------------------
    # Start / Stop
    # ------------------------------------------------------------------

    def start(self, client: Client, loop: asyncio.AbstractEventLoop):
        """Begin the auto-quest loop."""
        if self._running:
            return
        self._running = True
        if self._quest_client is not client:
            self._quest_client = client
            self._last_known_zone = None
            self._unavailable_modes.clear()
            self._last_dialog_point = None
            self._followup_attempts = 0
            self._was_in_dialog = False
            self._quest_navigator = MainQuestNavigator(self._emit_status)
            self._school_trainer = SchoolTrainer(self._emit_status)
            self._deck_manager = DeckManager(self._emit_status)
            self._school_visits = SchoolVisits(self._emit_status)
        self._consecutive_failures = 0
        self._resource_requested = False
        self._pending_transition = None
        self._last_goal_id = None
        self._stuck_timer_start = None
        self._objective_history.clear()
        self._in_recovery = False
        self._object_attempts.clear()
        self._object_progress.clear()
        self._object_search.clear()
        self._search_visited.clear()
        self._entry_attempts.clear()
        self._entry_attempt_key = None
        self._entry_walk_target = None
        self._talk_attempts.clear()
        self._task = asyncio.run_coroutine_threadsafe(
            self._quest_loop(client), loop
        )
        self._emit_status("Auto-quest started")

    def stop(self):
        """Stop the auto-quest loop."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None
        self._consecutive_failures = 0
        self._resource_requested = False
        self._pending_transition = None
        self._last_goal_id = None
        self._stuck_timer_start = None
        self._objective_history.clear()
        self._in_recovery = False
        self._emit_status("Auto-quest stopped")

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def _quest_loop(self, client: Client):
        """Core quest automation loop."""
        try:
            while self._running:
                try:
                    # Wait out loading screens before doing anything
                    if await self._is_loading(client):
                        self._emit_status("Loading — waiting...")
                        await self._wait_for_loading_done(client)
                        # Game time during a load isn't "stuck" time
                        self._reset_stuck_timer()
                        # Give the game a moment to settle after loading
                        await asyncio.sleep(1.0)
                        continue

                    # Now that we're settled in a (possibly new) zone, see if
                    # the zone changed since the last tick. If so, commit any
                    # pending transition and scan for wisps.
                    await self._observe_zone(client)

                    # Update goal_id tracking + history. Recorded BEFORE any
                    # state checks so the position we capture is roughly where
                    # the previous goal advanced.
                    await self._track_goal_change(client)

                    in_battle = await client.in_battle()
                    in_dialog = await client.is_in_dialog()

                    if in_battle:
                        # Pause during combat — don't interfere
                        self._reset_stuck_timer()
                        self._emit_status("In battle — waiting...")
                        await asyncio.sleep(1.0)

                    elif await self._school_trainer.step(client, await visible_ui(client)):
                        self._reset_stuck_timer()
                        await asyncio.sleep(TICK_DELAY)

                    elif await self._school_visits.step(client, await visible_ui(client), self._zone_mapper,
                            lambda target, current: self._try_teleport(client, target, current),
                            lambda: self._running, self._school_trainer):
                        # School trips use already recorded routes and the
                        # Commons shortcut, not the current quest door.
                        self._pending_transition = None
                        self._reset_stuck_timer()
                        await asyncio.sleep(TICK_DELAY)

                    elif not in_dialog and await self._deck_manager.step(client, await visible_ui(client)):
                        self._reset_stuck_timer()
                        await asyncio.sleep(TICK_DELAY)

                    elif (mode := await self._should_request_resource(client)) is not None:
                        # HP or mana below threshold — hand off to auto-collect
                        self._reset_stuck_timer()
                        self._emit_status(f"{mode.title()} low — requesting collect mode")
                        self._resource_requested = True
                        if self._on_resource_request:
                            self._on_resource_request(mode)
                        # Loop will exit when App calls stop()
                        await asyncio.sleep(0.5)

                    elif in_dialog:
                        # Advance dialog (counts as progress, not stuck)
                        self._reset_stuck_timer()
                        if not self._was_in_dialog:
                            self._dialog_navigator.begin()
                            pos = await fetch_position(client)
                            zone = await safe_read(client.zone_name)
                            if pos is not None and zone:
                                self._last_dialog_point = (zone, pos)
                        self._was_in_dialog = True
                        await advance_dialog(client, self._emit_status, self._dialog_navigator)
                        await asyncio.sleep(TICK_DELAY)

                    else:
                        self._was_in_dialog = False
                        tracking = await self._quest_navigator.ensure_tracked(client)
                        if tracking == "ready":
                            self._followup_attempts = 0
                        if tracking in ("changed", "unavailable"):
                            await asyncio.sleep(0.4)
                            continue
                        if tracking == "missing":
                            # A hand-in can close dialog before the next offer
                            # opens. Revisit the last NPC before considering any
                            # Finder target; do not follow stale world coordinates.
                            if self._last_dialog_point is not None and self._followup_attempts < 4:
                                zone, pos = self._last_dialog_point
                                if await safe_read(client.zone_name) == zone:
                                    self._followup_attempts += 1
                                    current = await fetch_position(client)
                                    if current and _distance(current, pos) > 150:
                                        await self._try_teleport(client, pos, current)
                                    self._emit_status("No main quest — checking the last NPC for the next offer")
                                    await self._nudge_and_interact(client)
                                    await asyncio.sleep(0.6)
                                    continue
                            # Initial characters may only have Finder. Allow
                            # approaching a named NPC actually loaded here; never
                            # teleport toward Finder's unverified arrow/exit.
                            if self._last_dialog_point is not None or await self._talk_target(client) is None:
                                self._emit_status("No accepted main quest — waiting for a main quest offer")
                                await asyncio.sleep(1)
                                continue
                        if tracking == "ready" and await self._collect_objective(client):
                            await asyncio.sleep(0.5)
                            continue
                        if tracking == "ready" and self._is_stuck() and not self._in_recovery:
                            recovered = await self._attempt_recovery(client)
                            if not recovered:
                                self._emit_status("Recovery failed after all attempts — auto-quest disabled")
                                self._running = False
                                if self._on_stopped:
                                    self._on_stopped()
                                break
                            continue
                        # Not in battle, not in dialog → teleport to quest
                        result = await self._teleport_to_quest_safe(client)

                        if result == "failed":
                            self._consecutive_failures += 1
                            self._emit_status(
                                f"Teleport failed ({self._consecutive_failures}/{MAX_CONSECUTIVE_FAILURES})"
                            )
                            if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                                self._emit_status("Too many failures — auto-quest disabled")
                                self._running = False
                                if self._on_stopped:
                                    self._on_stopped()
                                break
                            # Wait before retrying
                            await asyncio.sleep(1.0)
                        elif result == "success":
                            # Reset failure counter on any successful teleport
                            self._consecutive_failures = 0
                            # Try interacting with NPC after teleport
                            if self._entry_attempt_key is not None or self._npc_walk_target is not None:
                                await self._nudge_and_interact(client)
                            else:
                                await self._try_interact(client)
                        elif result == "skipped":
                            # Already at quest objective — nudge and interact
                            await self._nudge_and_interact(client)

                except Exception as e:
                    self._emit_status(f"Error: {e}")
                    await asyncio.sleep(1.0)

                await asyncio.sleep(TICK_DELAY)

        except asyncio.CancelledError:
            pass
        finally:
            self._running = False

    # ------------------------------------------------------------------
    # Loading screen handling
    # ------------------------------------------------------------------

    async def _is_loading(self, client: Client) -> bool:
        """Check if the client is on a loading screen (swallow errors)."""
        try:
            return await client.is_loading()
        except Exception:
            return False

    async def _observe_zone(self, client: Client):
        """Detect zone changes and feed them to the ZoneMapper.

        If a teleport was recently issued and the zone has now changed,
        commit a transition record (pre_zone, teleport_target -> new_zone,
        current_pos) and scan the new zone for wisps once.
        """
        try:
            zone = await client.zone_name()
        except Exception:
            return
        if not zone:
            return

        # First observation of the session
        if self._last_known_zone is None:
            self._last_known_zone = zone
            if self._zone_mapper is not None:
                self._zone_mapper.record_visit(zone)
            await self._scan_wisps_once(client, zone)
            return

        if zone == self._last_known_zone:
            return

        # Zone has changed
        prev_zone = self._last_known_zone
        self._last_known_zone = zone
        if self._zone_mapper is not None:
            self._zone_mapper.record_visit(zone)

        # Give previously-unavailable heal modes a fresh chance now that
        # we're in a new zone (which may have wisps locally, or may have
        # connected the graph enough for a path to exist).
        if self._unavailable_modes:
            cleared = ", ".join(sorted(self._unavailable_modes))
            self._unavailable_modes.clear()
            self._emit_status(f"Re-enabling heal modes after zone change: {cleared}")

        if self._pending_transition is not None:
            pending_zone, pending_target, ts = self._pending_transition
            self._pending_transition = None
            age = time.monotonic() - ts
            if pending_zone == prev_zone and age < PENDING_TRANSITION_TTL:
                cur_pos = await fetch_position(client)
                if cur_pos is not None and self._zone_mapper is not None:
                    self._zone_mapper.record_transition(
                        from_zone=pending_zone,
                        from_pos=pending_target,
                        to_zone=zone,
                        to_pos=(cur_pos.x, cur_pos.y, cur_pos.z),
                    )
                    self._emit_status(f"Recorded transition {pending_zone} -> {zone}")
            elif pending_zone == prev_zone:
                self._emit_status(
                    f"Transition {prev_zone} -> {zone} not recorded "
                    f"(pending teleport was {age:.1f}s old, > {PENDING_TRANSITION_TTL}s TTL)"
                )

        await self._scan_wisps_once(client, zone)

    async def _scan_wisps_once(self, client: Client, zone: str):
        """Scan the current zone for wisps once (cached for the session)."""
        if self._zone_mapper is None or zone in self._scanned_zones:
            return
        self._scanned_zones.add(zone)
        try:
            has_h, has_m = await detect_wisps_in_zone(client)
        except Exception as e:
            self._emit_status(f"Wisp scan failed: {e}")
            return
        self._zone_mapper.mark_wisps(zone, has_h, has_m)

    # ------------------------------------------------------------------
    # Stale-objective / stuck detection
    # ------------------------------------------------------------------

    def _reset_stuck_timer(self):
        """Push the stuck-detection clock forward to 'now'. Called when
        we're in a state that counts as forward progress (combat, dialog,
        loading, heal hand-off).
        """
        self._stuck_timer_start = time.monotonic()

    def _is_stuck(self) -> bool:
        """True if the active goal_id hasn't changed within STUCK_THRESHOLD_SECS."""
        if self._stuck_timer_start is None:
            return False
        return (time.monotonic() - self._stuck_timer_start) > STUCK_THRESHOLD_SECS

    async def _track_goal_change(self, client: Client):
        """Read goal_id. On change, snapshot the current (zone, position)
        as an 'advancement point' for later recovery use, and reset the
        stuck timer.
        """
        try:
            goal = await client.goal_id()
        except Exception:
            return

        # First observation of the session
        if self._last_goal_id is None:
            self._last_goal_id = goal
            self._stuck_timer_start = time.monotonic()
            return

        if goal == self._last_goal_id:
            return

        # Goal advanced — record where we were (best-effort)
        try:
            pos = await fetch_position(client)
        except Exception:
            pos = None
        if pos is not None and self._last_known_zone is not None:
            entry = (self._last_known_zone, (pos.x, pos.y, pos.z))
            self._objective_history.append(entry)
            self._emit_status(
                f"Goal advanced ({self._last_goal_id} -> {goal})  --  "
                f"recorded {self._last_known_zone}"
            )
        self._last_goal_id = goal
        self._stuck_timer_start = time.monotonic()

    async def _attempt_recovery(self, client: Client) -> bool:
        """Try to break out of a stale-objective bug by revisiting recent
        advancement points and interacting at each. Returns True if
        goal_id advances at any point (recovery succeeded), False if all
        attempts are exhausted.
        """
        if self._in_recovery:
            return False
        if not self._objective_history:
            self._emit_status("Stuck but no advancement history — cannot recover")
            return False

        self._in_recovery = True
        try:
            initial_goal = self._last_goal_id
            history_snapshot = list(self._objective_history)
            self._emit_status(
                f"Stuck on goal {initial_goal} for >{STUCK_THRESHOLD_SECS:.0f}s  --  "
                f"attempting recovery ({len(history_snapshot)} candidate(s))"
            )

            # Newest first — most likely to be the relevant NPC
            for i, (zone, pos) in enumerate(reversed(history_snapshot)):
                if not self._running:
                    return False
                label = f"[recovery {i + 1}/{len(history_snapshot)}]"

                if not await self._walk_to_recovery_zone(client, zone, label):
                    continue

                target = XYZ(float(pos[0]), float(pos[1]), float(pos[2]))
                self._emit_status(
                    f"{label} teleporting to ({pos[0]:.0f},{pos[1]:.0f},{pos[2]:.0f}) in {zone}"
                )
                pre_pos = await fetch_position(client)
                if pre_pos is None:
                    continue
                await self._try_teleport(client, target, pre_pos)

                # Nudge + interact (handles "have to step into range" case)
                await self._nudge_and_interact(client)

                # Give the game time to open dialog / advance goal
                await asyncio.sleep(RECOVERY_WAIT_SECS)

                # If a dialog opened, advance it a few times
                for _ in range(5):
                    if not self._running:
                        return False
                    try:
                        in_dialog = await client.is_in_dialog()
                    except Exception:
                        in_dialog = False
                    if not in_dialog:
                        break
                    if not await advance_dialog(client, self._emit_status):
                        break
                    await asyncio.sleep(0.4)

                # Did the goal advance?
                try:
                    new_goal = await client.goal_id()
                except Exception:
                    new_goal = initial_goal
                if new_goal != initial_goal:
                    self._emit_status(
                        f"{label} recovery succeeded  --  goal advanced to {new_goal}"
                    )
                    self._last_goal_id = new_goal
                    self._stuck_timer_start = time.monotonic()
                    # Record this new advancement point too
                    try:
                        cur = await fetch_position(client)
                        z = await client.zone_name()
                        if cur is not None and z:
                            self._objective_history.append((z, (cur.x, cur.y, cur.z)))
                    except Exception:
                        pass
                    return True

                self._emit_status(f"{label} no goal change")

            return False
        finally:
            self._in_recovery = False

    async def _walk_to_recovery_zone(
        self, client: Client, target_zone: str, label: str
    ) -> bool:
        """If we're not already in `target_zone`, use the zone walker to
        get there. Returns True if we end up in the right zone.
        """
        try:
            current = await client.zone_name()
        except Exception:
            current = None
        if current == target_zone:
            return True
        if self._zone_mapper is None or not current:
            self._emit_status(
                f"{label} cannot reach {target_zone} from {current!r} (no mapper)"
            )
            return False

        path = self._zone_mapper.find_path(current, target_zone)
        if path is None:
            self._emit_status(
                f"{label} no path from {current} to {target_zone}  --  skipping"
            )
            return False
        if not path:
            return True  # already there

        self._emit_status(
            f"{label} walking {len(path)} hop(s) to {target_zone}"
        )
        return await walk_zone_path(
            client, path,
            on_status=self._emit_status,
            is_active=lambda: self._running and self._in_recovery,
        )

    async def _should_request_resource(self, client: Client) -> Optional[str]:
        """Return 'health' or 'mana' if either is below its threshold, else None.

        Health takes priority when both are low.
        """
        if self._resource_requested:
            return None
        if self._on_resource_request is None:
            return None

        stats = client.stats
        if self._heal_threshold_pct > 0 and "health" not in self._unavailable_modes:
            try:
                cur = await stats.current_hitpoints()
                mx = await stats.max_hitpoints()
                if mx > 0 and (cur / mx) * 100.0 < self._heal_threshold_pct:
                    return "health"
            except Exception:
                pass
        if self._mana_threshold_pct > 0 and "mana" not in self._unavailable_modes:
            try:
                cur = await stats.current_mana()
                mx = await stats.max_mana()
                if mx > 0 and (cur / mx) * 100.0 < self._mana_threshold_pct:
                    return "mana"
            except Exception:
                pass
        return None

    async def _wait_for_loading_done(self, client: Client):
        """Block until the client is no longer on a loading screen."""
        while self._running:
            try:
                if not await client.is_loading():
                    return
            except Exception:
                pass
            await asyncio.sleep(LOADING_CHECK_DELAY)

    # ------------------------------------------------------------------
    # NPC interaction helpers
    # ------------------------------------------------------------------

    async def _try_interact(self, client: Client):
        """Press X to interact, then wait briefly to see if dialog opens."""
        if not await self._matches_interaction(client):
            return
        transition = await self._transition_destination(client)
        self._emit_status("At quest objective — interacting...")
        await client.send_key(Keycode.X, 0.02 if transition else 0.1)
        await asyncio.sleep(0.3)
        if transition:
            await self._wait_for_transition(client, *transition)

        # Check if dialog opened
        try:
            if await client.is_in_dialog():
                self._emit_status("Dialog opened")
                return
        except Exception:
            pass

    async def _nudge_and_interact(self, client: Client):
        """
        Tap W to step forward, triggering the game's NPC interaction zone,
        then press X. The game sometimes doesn't register proximity until
        the player moves slightly.
        """
        self._emit_status("Near objective — stepping forward...")

        if self._entry_attempt_key and self._entry_walk_target:
            target = self._entry_walk_target
            await asyncio.wait_for(client.goto(target.x, target.y), timeout=4)
            await asyncio.sleep(1.0)
            log.debug("Entrance movement target=%s actual=%s npc_range=%s", target, await fetch_position(client), await safe_read(client.is_in_npc_range))
        elif self._npc_walk_target:
            target = self._npc_walk_target
            await asyncio.wait_for(client.goto(target.x, target.y), timeout=3)
            await asyncio.sleep(0.3)
        else:
            await client.send_key(Keycode.W, 0.1)
            await asyncio.sleep(0.15)
            await client.send_key(Keycode.S, 0.1)
            await asyncio.sleep(0.15)

        if not await self._matches_interaction(client):
            return
        transition = await self._transition_destination(client)
        # Press X to interact
        # Wizwalker's held-key helper repeats keydown every 50 ms. A second
        # X cancels sigil entry, so send a single short press for transitions.
        await client.send_key(Keycode.X, 0.02 if transition else 0.1)
        await asyncio.sleep(0.3)
        if transition:
            await self._wait_for_transition(client, *transition)

        # Check if dialog opened or we entered battle
        try:
            if await client.is_in_dialog():
                self._emit_status("Dialog opened")
                return
            if await client.in_battle():
                self._emit_status("Entered battle")
                return
        except Exception:
            pass

    async def _transition_destination(self, client):
        ui = await visible_ui(client)
        labels = [plain_text(n.text).casefold() for n in ui.walk() if n.name == "txtGoalName"] if ui else []
        if len(labels) != 1:
            return None
        if (self._expected_talk_name and labels[0].startswith("talk to ") and
                self._expected_talk_name.casefold() in labels[0]):
            return None
        self._expected_talk_name = None
        active = await safe_read(client.quest_id)
        zone = await safe_read(client.zone_name)
        quests = await quest_entries(client)
        destinations = {g.destination for q in quests or [] if q.id == active and q.mainline is True
                        for g in q.goals if g.destination and g.destination != zone
                        and ((plain_text(g.text).strip() and labels[0].startswith(plain_text(g.text).strip().casefold()))
                             or (g.kind == "waypoint" and labels[0].startswith("go to ")))}
        return (zone, destinations.pop()) if zone and len(destinations) == 1 else None

    async def _wait_for_transition(self, client, initial_zone, destination):
        # Entry sigils count down after X. More movement/X presses cancel that
        # countdown, so hold still while observing the actual zone transition.
        key = self._entry_attempt_key
        self._emit_status(f"Waiting for entry to {destination}")
        deadline = time.monotonic() + 15
        while self._running and time.monotonic() < deadline:
            self._reset_stuck_timer()
            if (await safe_read(client.zone_name) != initial_zone or await self._is_loading(client)
                    or await safe_read(client.is_in_dialog, False) or await safe_read(client.in_battle, False)):
                return
            await asyncio.sleep(0.5)
        if key:
            self._entry_attempts[key] = self._entry_attempts.get(key, 0) + 1

    async def _matches_interaction(self, client):
        if self._expected_talk_name:
            actual = prompt_name(await visible_ui(client))
            if not actual:
                self._emit_status(f"Waiting for the interaction prompt for {self._expected_talk_name}")
                return False
            if actual.casefold() != self._expected_talk_name.casefold():
                self._emit_status(f"Adjusting NPC approach: prompt is {actual}, objective is {self._expected_talk_name}")
                return False
        return True

    # ------------------------------------------------------------------
    # Safe teleport with snap-back detection
    # ------------------------------------------------------------------

    # Offsets to try on each axis when a direct teleport fails
    _OFFSET_DISTANCES = [50, 100, 200]

    async def _collect_objective(self, client):
        observed = active_usage_goal(await quest_entries(client), await safe_read(client.quest_id),
                                     await visible_ui(client))
        if observed is None:
            return False
        goal, label = observed
        if goal.destination and goal.destination != await safe_read(client.zone_name):
            return False  # Follow the zone entrance arrow first.
        self._expected_talk_name = None
        self._reset_stuck_timer()
        rows = await list_nearby_entities(client)
        candidates = [row for row in rows if matches_object(goal, label, row[1], row[2])]
        if not candidates:
            # Usage goals can omit their destination while the finder points
            # to a zone exit. Preserve that navigation; only search locally
            # when the finder has no usable location (as with scattered drops).
            arrow = await fetch_quest_position(client)
            if arrow and any((arrow.x, arrow.y, arrow.z)):
                return False
            return await self._search_object_area(client, goal, label)
        key = (await safe_read(client.quest_id), goal.id, label)
        progress_key = key[:2]
        previous_label, previous_point, collected = self._object_progress.get(progress_key, (label, None, set()))
        if previous_label != label and previous_point is not None:
            collected.add(previous_point)
        candidates = [row for row in candidates if (row[3].x, row[3].y, row[3].z) not in collected]
        # Progress changes the HUD counter, so failed locations are forgotten
        # only after a fresh objective observation, not on each teleport.
        self._object_attempts = {key: self._object_attempts.get(key, {})}
        attempts = self._object_attempts[key]
        offsets = [(0, 0, 0)] + [(dx, dy, dz) for size in self._OFFSET_DISTANCES
                    for dx, dy, dz in ((size, 0, 0), (-size, 0, 0), (0, size, 0),
                                       (0, -size, 0), (0, 0, size), (0, 0, -size))]
        candidates = [row for row in candidates if attempts.get((row[3].x, row[3].y, row[3].z), 0) < len(offsets)]
        if not candidates:
            return await self._search_object_area(client, goal, label)
        _, _, name, target = candidates[0]
        point = (target.x, target.y, target.z)
        self._object_progress[progress_key] = (label, point, collected)
        attempt = attempts.get(point, 0)
        attempts[point] = attempt + 1
        # Static origins may be inside collision geometry; use the same
        # bounded surrounding-position search as arrow navigation.
        dx, dy, dz = offsets[attempt]
        target = XYZ(target.x + dx, target.y + dy, target.z + dz)
        self._emit_status(f"Collecting quest object: {name or label}")
        player = await fetch_position(client)
        if player is None:
            return True
        result = await self._try_teleport(client, target, player)
        log.info("Quest object approach %s at %s: %s", name, target, result)
        if result == "failed" or await client.in_battle() or await self._is_loading(client):
            return True
        # A memory teleport alone does not always update the server's range
        # trigger. Walk through the pickup, then observe its actual prompt.
        try:
            await asyncio.wait_for(client.goto(point[0], point[1]), 2)
            await client.send_key(Keycode.W, 0.12)
        except TimeoutError:
            return True
        for _ in range(3):
            await asyncio.sleep(0.3)
            if await self._is_loading(client) or await client.in_battle() or await client.is_in_dialog():
                return True
            observed = active_usage_goal(await quest_entries(client), await safe_read(client.quest_id),
                                         await visible_ui(client))
            if observed is None or observed[1] != label:
                self._reset_stuck_timer()
                return True
            actual = prompt_name(await visible_ui(client))
            close = await fetch_position(client)
            matching = actual and matches_object(goal, label, "", actual)
            unnamed = (not actual and close and _distance(close, XYZ(*point)) < 200
                       and await safe_read(client.is_in_npc_range, False))
            if matching or unnamed:
                await client.send_key(Keycode.X, 0.04)
                await asyncio.sleep(0.6)
        return True

    async def _search_object_area(self, client, goal, label):
        """Expand the observed area instead of waiting for out-of-range objects."""
        if await self._transition_destination(client):
            return False
        player = await fetch_position(client)
        if player is None:
            return False
        key = (await safe_read(client.quest_id), goal.id, label)
        origin, step = self._object_search.get(key, (player, 0))
        self._object_search = {key: (origin, step + 1)}
        arrow = await fetch_quest_position(client)
        if (step == 0 and arrow and any((arrow.x, arrow.y, arrow.z))
                and _distance(player, arrow) > 300):
            target = arrow
        else:
            zone = await safe_read(client.zone_name)
            visited = self._search_visited.get(key, [origin])
            target = await self._zone_search.point(zone, origin, visited) if zone else None
            if target is not None:
                self._search_visited = {key: visited + [target]}
                target = XYZ(target.x, target.y, target.z + 10)
            else:
                # Fallback for maps without navigation data; continue expanding
                # rather than waiting indefinitely for a zero quest arrow.
                ring = step // 8 + 1
                dx, dy = ((1, 0), (1, 1), (0, 1), (-1, 1),
                          (-1, 0), (-1, -1), (0, -1), (1, -1))[step % 8]
                target = XYZ(origin.x + dx * ring * 450, origin.y + dy * ring * 450, origin.z)
        self._emit_status(f"Searching beyond loaded objects: {label}")
        result = await self._try_teleport(client, target, player)
        if result != "failed" and not await self._is_loading(client) and not await client.in_battle():
            try:
                await asyncio.wait_for(client.goto(target.x + 80, target.y + 80), 2)
            except TimeoutError:
                pass
            await asyncio.sleep(0.4)
        return True

    async def _talk_target(self, client):
        """Resolve a Talk To HUD objective against actual NPCs, not door arrows.

        The initial quest finder can point at an exit even while the requested
        NPC is loaded in the room. No quest IDs, NPC names or coordinates are
        prescribed here; the current HUD and object metadata identify the NPC.
        """
        ui = await visible_ui(client)
        self._expected_talk_name = None
        labels = [plain_text(node.text)
                  for node in ui.walk() if node.name == "txtGoalName"] if ui else []
        if len(labels) != 1 or not labels[0].casefold().startswith("talk to "):
            log.debug("No unique Talk To HUD label: %r", labels)
            return None
        text = labels[0].casefold()[8:]
        matches = []
        neighbors = []
        player = await fetch_position(client)
        if player is None:
            return None
        for entity in await safe_read(client.get_base_entity_list, []):
            template = await safe_read(entity.object_template)
            if template is None:
                continue
            kind = await safe_read(template.object_type)
            object_name = await safe_read(template.object_name, "")
            # Some quest-givers are generic objects with NPC services. Player
            # avatars have no NPC template display name and must be excluded.
            if kind in (ObjectType.player, ObjectType.pet, ObjectType.door) or object_name == "Player Object":
                continue
            name = await safe_read(entity.display_name, "") or ""
            normalized = name.casefold().strip()
            if not normalized:
                continue
            body = await safe_read(entity.actor_body)
            # Dormant quest stand-ins have template locations but no active
            # actor body. Their display names are not usable interaction targets.
            pos = await safe_read(body.position) if body else None
            if pos is not None:
                neighbors.append(pos)
                if text == normalized or text.startswith(normalized + " in "):
                    # Decorative stand-ins can share a quest giver's display
                    # name. Require the actual NPC behavior before targeting.
                    behavior = await safe_read(lambda: entity.fetch_npc_behavior_template())
                    if behavior is None:
                        log.debug("Ignoring noninteractive named object %s", object_name)
                        continue
                    key = (await safe_read(client.zone_name), await safe_read(lambda: client.goal_id()),
                           name, pos.x, pos.y, pos.z)
                    if self._talk_attempts.get(key, 0) >= 8:
                        # A scripted stand-in can retain NPC behavior while
                        # having no usable services. After a complete approach
                        # ring, use the HUD route to locate the real quest giver.
                        self._reset_stuck_timer()
                        continue
                    matches.append((_distance(player, pos), name, pos))
        if not matches:
            return None
        _, name, pos = min(matches, key=lambda row: row[0])
        self._expected_talk_name = name
        self._emit_status(f"Talk objective — approaching {name}")
        if (prompt_name(ui) or "").casefold() == name.casefold():
            self._npc_walk_target = None
            return player
        key = (await safe_read(client.zone_name), await safe_read(lambda: client.goal_id()),
               name, pos.x, pos.y, pos.z)
        attempt = self._talk_attempts.get(key, 0)
        if len(self._talk_attempts) > 32:
            self._talk_attempts.clear()
        self._talk_attempts[key] = attempt + 1
        self._npc_walk_target = approach_point(player, pos, neighbors, 80, attempt)
        return approach_point(player, pos, neighbors, 180, attempt)

    async def _teleport_to_quest_safe(self, client: Client) -> str:
        """
        Teleport to the quest objective with snap-back detection.
        If the direct teleport fails, tries offset positions on each axis.

        Returns:
            "success" — teleported and position confirmed near target
            "failed"  — all attempts (direct + offsets) failed
            "skipped" — skipped teleport (already close, no quest, etc.)
        """
        self._entry_attempt_key = None
        self._entry_walk_target = None
        self._npc_walk_target = None
        quest_pos = await self._talk_target(client)
        entry_key = None
        if quest_pos is None:
            quest_pos = await self._entry_target(client)
            if quest_pos is not None:
                entry_key = self._entry_attempt_key
        if quest_pos is None:
            ui = await visible_ui(client)
            if ui and any(n.name == "txtGoalName" and plain_text(n.text).casefold().startswith("defeat ") for n in ui.walk()):
                enemy = defeat_target(await list_nearby_entities(client), ui)
                if enemy is not None:
                    if await approach_enemy(client, enemy[2], self._try_teleport,
                                            self._emit_status, lambda: self._running):
                        self._reset_stuck_timer()
                        return "handled"
        if quest_pos is None:
            quest_pos = await fetch_quest_position(client)
        if quest_pos is None or (quest_pos.x == 0 and quest_pos.y == 0 and quest_pos.z == 0):
            self._emit_status("No quest objective found")
            return "waiting"

        pre_pos = await fetch_position(client)
        if pre_pos is None:
            return "skipped"

        dist_to_quest = _distance(pre_pos, quest_pos)
        if dist_to_quest < SNAP_BACK_THRESHOLD:
            self._emit_status("Already near quest objective")
            return "skipped"

        # Try direct teleport first
        self._emit_status(f"Teleporting to quest ({dist_to_quest:.0f} units away)")
        result = await self._try_teleport(client, quest_pos, pre_pos)
        if result != "failed":
            return result

        # Direct teleport failed — try offsets
        self._emit_status("Direct teleport failed, trying nearby offsets...")

        for offset_dist in self._OFFSET_DISTANCES:
            # Try ±offset on X, Y, Z axes
            offsets = [
                XYZ(quest_pos.x + offset_dist, quest_pos.y, quest_pos.z),
                XYZ(quest_pos.x - offset_dist, quest_pos.y, quest_pos.z),
                XYZ(quest_pos.x, quest_pos.y + offset_dist, quest_pos.z),
                XYZ(quest_pos.x, quest_pos.y - offset_dist, quest_pos.z),
                XYZ(quest_pos.x, quest_pos.y, quest_pos.z + offset_dist),
                XYZ(quest_pos.x, quest_pos.y, quest_pos.z - offset_dist),
            ]

            for i, target in enumerate(offsets):
                axis = ["X+", "X-", "Y+", "Y-", "Z+", "Z-"][i]
                self._emit_status(f"Trying offset {axis}{offset_dist}...")

                # Re-read position since we may have moved
                current_pos = await fetch_position(client)
                if current_pos is None:
                    continue

                result = await self._try_teleport(client, target, current_pos)
                if result == "success":
                    self._emit_status(f"Offset {axis}{offset_dist} worked!")
                    return "success"

                if not self._running:
                    return "failed"

        # All offsets exhausted
        self._emit_status("All offset attempts failed")
        if entry_key:
            self._entry_attempts[entry_key] = self._entry_attempts.get(entry_key, 0) + 1
            return "waiting"
        return "failed"

    async def _entry_target(self, client):
        self._entry_attempt_key = None
        self._entry_walk_target = None
        if await self._transition_destination(client) is None:
            return None
        arrow = await fetch_quest_position(client)
        if arrow is None or (arrow.x == 0 and arrow.y == 0 and arrow.z == 0):
            return None
        entrances = []
        for entity in await safe_read(client.get_base_entity_list, []):
            if "CountdownBehavior" not in await safe_read(entity.list_behavior_names, []):
                continue
            name = (await safe_read(entity.object_name, "") or "").casefold()
            if "4 player" not in name or "circle" not in name:
                continue
            location = await safe_read(entity.location)
            orientation = await safe_read(entity.orientation)
            if location is not None and orientation is not None and _distance(arrow, location) < 1200:
                key = (await safe_read(client.quest_id), location.x, location.y, location.z)
                scale = await safe_read(entity.scale, 1.0)
                if not isinstance(scale, (int, float)) or not math.isfinite(scale) or not 0.1 <= scale <= 10:
                    scale = 1.0
                entrances.append((_distance(arrow, location), location, orientation, key, scale))
        if not entrances:
            return None
        _, location, orientation, key, scale = min(entrances, key=lambda row: row[0])
        self._entry_attempt_key = key
        attempt = self._entry_attempts.get(key, 0)
        if attempt >= 8:
            return None
        self._emit_status("Approaching the dungeon entry pads")
        log.debug("Entrance geometry scale=%s yaw=%s attempt=%s", scale, orientation.yaw, attempt)
        target = sigil_approach(location, orientation, attempt, scale)
        self._entry_walk_target = target
        # Walk onto the pad after teleporting nearby so the game registers
        # entry proximity through ordinary movement.
        angle = math.atan2(target.y - location.y, target.x - location.x)
        return XYZ(target.x + math.cos(angle) * 180,
                   target.y + math.sin(angle) * 180, target.z)

    async def _try_teleport(self, client: Client, target: XYZ, pre_pos: XYZ) -> str:
        """
        Attempt a single teleport and check for snap-back.

        Stashes a pending transition on any non-failed outcome  --  the load
        screen for a door teleport doesn't always appear within the 0.6s
        check window, so we treat every successful teleport as a candidate
        and let `_observe_zone` confirm it if the zone actually changes.

        Returns:
            "success" — teleport held (or entered battle/dialog/loading)
            "failed"  — player snapped back to pre_pos
            "skipped" — couldn't verify (position unreadable)
        """
        try:
            await client.teleport(target, wait_on_inuse=True)
        except Exception as e:
            self._emit_status(f"Teleport error: {e}")
            return "failed"

        await asyncio.sleep(POST_TELEPORT_CHECK_DELAY)

        # If we entered a battle, dialog, or loading screen — that's success
        try:
            if await client.in_battle():
                self._stash_pending_transition(target)
                return "success"
            if await client.is_in_dialog():
                self._stash_pending_transition(target)
                return "success"
            if await self._is_loading(client):
                self._stash_pending_transition(target)
                return "success"
        except Exception:
            self._stash_pending_transition(target)
            return "success"

        # Check if we snapped back
        post_pos = await fetch_position(client)
        if post_pos is None:
            return "skipped"

        dist_from_original = _distance(post_pos, pre_pos)
        dist_from_target = _distance(post_pos, target)

        if dist_from_original < SNAP_BACK_THRESHOLD and dist_from_target > SNAP_BACK_THRESHOLD:
            log.debug("Teleport snap-back: before=%s after=%s target=%s", pre_pos, post_pos, target)
            # NPC range at the starting location cannot prove we reached a
            # different objective. Allow only the target NPC's small standoff.
            if dist_from_target <= 200 and await safe_read(client.is_in_npc_range, False):
                return "success"
            return "failed"

        # Teleport held but no immediate transition signal. The load
        # screen may still be on its way -- stash anyway and let the
        # TTL clear it if no zone change happens.
        self._stash_pending_transition(target)
        return "success"

    def _stash_pending_transition(self, target: XYZ):
        """Record `target` as the most recent teleport candidate that could
        have triggered a zone change. Committed by `_observe_zone` only if
        the zone actually changes within PENDING_TRANSITION_TTL seconds.
        """
        if self._last_known_zone is None:
            return
        self._pending_transition = (
            self._last_known_zone,
            (target.x, target.y, target.z),
            time.monotonic(),
        )

