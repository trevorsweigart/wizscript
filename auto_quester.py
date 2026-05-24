"""
Auto Quester — automated quest progression loop.

Teleports to quest objectives, handles NPC dialog, and includes a safety
mechanism that disables itself if a teleport fails repeatedly (player snaps back).
"""

import asyncio
import math
import time
from collections import deque
from typing import Callable, Optional

from wizwalker.client import Client
from wizwalker.constants import Keycode
from wizwalker.utils import XYZ

from game_info import fetch_position, fetch_quest_position
from entities import detect_wisps_in_zone
from zone_mapper import ZoneMapper
from zone_walker import walk_zone_path

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
        self._consecutive_failures = 0
        self._resource_requested = False
        self._pending_transition = None
        self._last_known_zone = None
        self._last_goal_id = None
        self._stuck_timer_start = None
        self._objective_history.clear()
        self._in_recovery = False
        self._unavailable_modes.clear()
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
        self._last_known_zone = None
        self._last_goal_id = None
        self._stuck_timer_start = None
        self._objective_history.clear()
        self._in_recovery = False
        self._unavailable_modes.clear()
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
                        self._emit_status("In dialog — advancing...")
                        await client.send_key(Keycode.SPACEBAR, 0)
                        await asyncio.sleep(TICK_DELAY)

                    elif self._is_stuck() and not self._in_recovery:
                        # Quest objective hasn't changed for too long — try to
                        # recover by revisiting recent advancement points.
                        recovered = await self._attempt_recovery(client)
                        if not recovered:
                            self._emit_status(
                                "Recovery failed after all attempts — auto-quest disabled"
                            )
                            self._running = False
                            if self._on_stopped:
                                self._on_stopped()
                            break

                    else:
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
                    await client.send_key(Keycode.SPACEBAR, 0)
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
        self._emit_status("At quest objective — interacting...")
        await client.send_key(Keycode.X, 0)
        await asyncio.sleep(0.3)

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

        # Tap W to move a step forward, then S to step back
        await client.send_key(Keycode.W, 0.1)
        await asyncio.sleep(0.15)
        await client.send_key(Keycode.S, 0.1)
        await asyncio.sleep(0.15)

        # Press X to interact
        await client.send_key(Keycode.X, 0)
        await asyncio.sleep(0.3)

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

    # ------------------------------------------------------------------
    # Safe teleport with snap-back detection
    # ------------------------------------------------------------------

    # Offsets to try on each axis when a direct teleport fails
    _OFFSET_DISTANCES = [50, 100, 200]

    async def _teleport_to_quest_safe(self, client: Client) -> str:
        """
        Teleport to the quest objective with snap-back detection.
        If the direct teleport fails, tries offset positions on each axis.

        Returns:
            "success" — teleported and position confirmed near target
            "failed"  — all attempts (direct + offsets) failed
            "skipped" — skipped teleport (already close, no quest, etc.)
        """
        quest_pos = await fetch_quest_position(client)
        if quest_pos is None:
            self._emit_status("No quest objective found")
            return "skipped"

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
        return "failed"

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

