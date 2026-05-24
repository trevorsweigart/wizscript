"""
Auto Healer  --  gathers nearby health or mana wisps until the chosen
resource rises above the configured threshold.

Triggered automatically by AutoQuester when HP (or mana) drops below
its threshold. While active, AutoQuester and AutoCombat are paused
(managed by App).

Exits on:
  - Resource rises above target percentage  (success)
  - No safe wisps found after MAX_FAILURES scans  (failure -> App stops everything)
  - Player enters a battle  (success-like; autocombat resumes)
  - Manually stopped (disconnect / shutdown)

Wisp identification uses the in-game template names confirmed from a
real session:
  - Health wisps: object_name starts with "WC_WispHealth"
  - Mana wisps:   object_name starts with "WC_WispMana"

Safety: a wisp is skipped if any non-wisp, non-player entity is within
SAFETY_RADIUS units of it (avoids teleporting next to a hostile mob).
"""

import asyncio
import logging
from typing import Callable, Optional, Tuple

from wizwalker.client import Client
from wizwalker.utils import XYZ

from entities import (
    distance as _distance,
    is_any_wisp,
    is_health_wisp as _is_health_wisp,
    is_mana_wisp as _is_mana_wisp,
)
from zone_mapper import ZoneMapper
from zone_walker import walk_zone_path

log = logging.getLogger("auto_healer")

# Modes
MODE_HEALTH = "health"
MODE_MANA = "mana"

# Exit reasons reported via the done callback
EXIT_COMPLETED = "completed"        # resource recovered to target
EXIT_BATTLE = "battle"              # battle started; hand off to autocombat
EXIT_NO_SOURCE = "no_source"        # no wisps locally and no reachable wisp zone
EXIT_CANCELLED = "cancelled"        # loop cancelled / stopped externally

# Tick rate for the collection loop
TICK_DELAY = 0.3

# Wait after teleport for the wisp to be picked up
POST_TELEPORT_WAIT = 0.8

# How many consecutive empty scans before giving up
MAX_FAILURES = 5

# Top-N nearby entities to log each scan (debug aid)
DEBUG_ENTITY_LOG_COUNT = 20

# A wisp is considered unsafe to teleport to if any non-wisp, non-player
# entity is within this many units of it.
SAFETY_RADIUS = 200.0


def _is_safe_neighbor(obj_name: Optional[str]) -> bool:
    """Entity is OK to be near a candidate wisp (won't trigger combat)."""
    if not obj_name:
        return True
    if obj_name.lower() == "player object":
        return True
    if is_any_wisp(obj_name):
        return True
    return False


class AutoHealer:
    """
    Async loop that hunts down a specific kind of wisp (health or mana)
    to restore that resource.

    Lifecycle is managed by App: started in response to AutoQuester's
    resource request, stopped when the resource recovers or no safe
    wisps are reachable.
    """

    def __init__(self):
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._on_status: Optional[Callable[[str], None]] = None
        self._on_done: Optional[Callable[[str, str], None]] = None
        self._zone_mapper: Optional[ZoneMapper] = None

        # Per-resource trigger thresholds (collected from UI / AutoQuester)
        self._heal_threshold_pct: float = 50.0
        self._mana_threshold_pct: float = 50.0

        # Mode and target captured at start() time
        self._mode: str = MODE_HEALTH
        self._target_pct: float = 70.0

        # Zones we've already tried to walk to this session (avoid loops)
        self._tried_zones: set = set()

    @property
    def is_running(self) -> bool:
        return self._running

    def set_status_callback(self, callback: Callable[[str], None]):
        self._on_status = callback

    def set_done_callback(self, callback: Callable[[str, str], None]):
        """Invoked when the loop exits, with (reason, mode).

        `reason` is one of EXIT_COMPLETED, EXIT_BATTLE, EXIT_NO_SOURCE,
        or EXIT_CANCELLED.
        """
        self._on_done = callback

    def set_heal_threshold_pct(self, pct: float):
        self._heal_threshold_pct = max(0.0, min(100.0, pct))

    def set_mana_threshold_pct(self, pct: float):
        self._mana_threshold_pct = max(0.0, min(100.0, pct))

    def set_zone_mapper(self, mapper: Optional[ZoneMapper]):
        """Optional ZoneMapper used to fall back to a connected zone with
        wisps when the current zone has none.
        """
        self._zone_mapper = mapper

    def _emit(self, msg: str):
        log.info(msg)
        if self._on_status:
            self._on_status(msg)

    # ------------------------------------------------------------------
    # Start / Stop
    # ------------------------------------------------------------------

    def start(
        self,
        client: Client,
        loop: asyncio.AbstractEventLoop,
        *,
        mode: str = MODE_HEALTH,
    ):
        if self._running:
            return
        self._running = True
        self._mode = mode
        self._tried_zones = set()
        threshold = (
            self._heal_threshold_pct if mode == MODE_HEALTH else self._mana_threshold_pct
        )
        # Heal a bit past the threshold to avoid bouncing right at the edge
        self._target_pct = min(100.0, threshold + 20.0)

        self._task = asyncio.run_coroutine_threadsafe(
            self._collect_loop(client), loop
        )
        self._emit(f"Collecting {mode} wisps (target {self._target_pct:.0f}%)")

    def stop(self):
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def _collect_loop(self, client: Client):
        exit_reason = EXIT_CANCELLED
        unsafe_retries = 0
        try:
            while self._running:
                try:
                    if await client.in_battle():
                        # Battle takes priority; let autocombat handle it
                        self._emit("Battle detected  --  exiting collect mode")
                        exit_reason = EXIT_BATTLE
                        break

                    if await self._is_loading(client):
                        self._emit("Loading  --  waiting...")
                        await asyncio.sleep(0.5)
                        continue

                    cur, mx = await self._read_resource(client)
                    if mx <= 0:
                        self._emit(f"Could not read {self._mode}  --  retrying")
                        await asyncio.sleep(0.5)
                        continue

                    pct = (cur / mx) * 100.0
                    self._emit(f"{self._mode.title()} {cur}/{mx} ({pct:.0f}%)")

                    if pct >= self._target_pct:
                        self._emit(
                            f"{self._mode.title()} restored "
                            f"({pct:.0f}% >= {self._target_pct:.0f}%)"
                        )
                        exit_reason = EXIT_COMPLETED
                        break

                    wisp, scan_reason = await self._find_nearest_safe_wisp(client)

                    if wisp is not None:
                        unsafe_retries = 0
                        name, pos, dist = wisp
                        self._emit(f"Teleporting to {name!r} ({dist:.0f}u)")
                        try:
                            await client.teleport(pos, wait_on_inuse=True)
                        except Exception as e:
                            self._emit(f"Teleport error: {e}")
                        await asyncio.sleep(POST_TELEPORT_WAIT)

                    elif scan_reason == "all_unsafe":
                        # Wisps exist but blocked by nearby threats. Wait
                        # and retry a few times — threats might move, or a
                        # different scan pass might pick a different one.
                        unsafe_retries += 1
                        self._emit(
                            f"All {self._mode} wisps blocked by threats "
                            f"({unsafe_retries}/{MAX_FAILURES})"
                        )
                        if unsafe_retries >= MAX_FAILURES:
                            self._emit(
                                f"Giving up locally  --  trying to walk to another {self._mode} zone"
                            )
                            walked = await self._try_walk_to_zone_with_wisps(client)
                            if walked:
                                unsafe_retries = 0
                                continue
                            self._emit(f"No reachable {self._mode} zone either")
                            exit_reason = EXIT_NO_SOURCE
                            break
                        await asyncio.sleep(1.0)

                    else:  # scan_reason == "no_candidates"
                        # Zero wisps of the active type here. Try walking
                        # to a connected zone that has some.
                        self._emit(f"No {self._mode} wisps in zone  --  checking other known zones")
                        walked = await self._try_walk_to_zone_with_wisps(client)
                        if walked:
                            unsafe_retries = 0
                            continue
                        self._emit(
                            f"No reachable {self._mode} zone known  --  yielding back to questing"
                        )
                        exit_reason = EXIT_NO_SOURCE
                        break

                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    self._emit(f"Error: {e}")
                    log.exception("AutoHealer iteration error")
                    await asyncio.sleep(1.0)

                await asyncio.sleep(TICK_DELAY)

        except asyncio.CancelledError:
            exit_reason = EXIT_CANCELLED
        finally:
            mode = self._mode
            self._running = False
            if self._on_done:
                self._on_done(exit_reason, mode)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _is_loading(self, client: Client) -> bool:
        try:
            return await client.is_loading()
        except Exception:
            return False

    async def _try_walk_to_zone_with_wisps(self, client: Client) -> bool:
        """If the current zone has no wisps of the active mode, look up the
        nearest known zone that does and walk there via the ZoneMapper.

        Returns True if we successfully walked into a new zone (the caller
        should then re-enter the main loop and re-scan), False if no path
        was attempted or it failed.
        """
        if self._zone_mapper is None:
            return False
        try:
            current = await client.zone_name()
        except Exception:
            return False
        if not current:
            return False

        # Don't bother walking to the same zone we just came from this session
        self._tried_zones.add(current)

        if self._mode == MODE_HEALTH:
            target = self._zone_mapper.find_nearest_with_health(
                current, exclude=self._tried_zones
            )
        else:
            target = self._zone_mapper.find_nearest_with_mana(
                current, exclude=self._tried_zones
            )

        if target is None:
            self._emit(
                f"No known zone with {self._mode} wisps reachable from {current}"
            )
            return False

        target_zone, path = target
        if not path:
            return False

        self._tried_zones.add(target_zone)
        self._emit(
            f"Walking to {target_zone} ({len(path)} hop(s)) for {self._mode}"
        )
        ok = await walk_zone_path(
            client,
            path,
            on_status=self._emit,
            is_active=lambda: self._running,
        )
        if not ok:
            self._emit(f"Walk to {target_zone} failed")
        return ok

    async def _read_resource(self, client: Client) -> Tuple[int, int]:
        """Read current/max for the active mode (health or mana)."""
        try:
            stats = client.stats
            if self._mode == MODE_MANA:
                return await stats.current_mana(), await stats.max_mana()
            return await stats.current_hitpoints(), await stats.max_hitpoints()
        except Exception as e:
            log.debug(f"Resource read failed: {e}")
            return 0, 0

    async def _find_nearest_safe_wisp(
        self, client: Client
    ) -> Tuple[Optional[Tuple[str, XYZ, float]], str]:
        """Scan loaded entities for the nearest wisp of the active mode
        with no threats inside SAFETY_RADIUS.

        Returns:
            (wisp_tuple_or_None, reason) where reason is one of:
              - "found"          --  wisp_tuple is (name, pos, dist)
              - "all_unsafe"     --  candidates exist but all blocked
              - "no_candidates"  --  zero wisps of this type in zone
        """
        try:
            entities = await client.get_base_entity_list()
        except Exception as e:
            log.warning(f"Failed to fetch entity list: {e}")
            return None, "no_candidates"

        try:
            player_pos = await client.body.position()
        except Exception as e:
            log.warning(f"Failed to read player position: {e}")
            return None, "no_candidates"

        # Tuple shape: (dist_from_player, obj_name, display_name, pos)
        all_entities: list[Tuple[float, str, str, XYZ]] = []

        for entity in entities:
            try:
                template = await entity.object_template()
                if template is None:
                    continue
                obj_name = await template.object_name()
                try:
                    disp = await entity.display_name()
                except Exception:
                    disp = None

                body = await entity.actor_body()
                if body is None:
                    continue
                e_pos = await body.position()
                dist = _distance(player_pos, e_pos)

                all_entities.append((dist, obj_name or "", disp or "", e_pos))
            except Exception:
                continue

        # Debug aid: log nearest entities each scan
        all_entities.sort(key=lambda x: x[0])
        log.info(f"--- Nearby entities (top {DEBUG_ENTITY_LOG_COUNT}) ---")
        for dist, name, disp, pos in all_entities[:DEBUG_ENTITY_LOG_COUNT]:
            log.info(
                f"  dist={dist:7.1f}  obj={name!r}  disp={disp!r}  "
                f"pos=({pos.x:.0f},{pos.y:.0f},{pos.z:.0f})"
            )

        # Candidates: wisps of the active mode
        is_target = _is_health_wisp if self._mode == MODE_HEALTH else _is_mana_wisp
        candidates = [(d, n, p) for d, n, _disp, p in all_entities if is_target(n)]
        candidates.sort(key=lambda c: c[0])

        if not candidates:
            return None, "no_candidates"

        # Pick the nearest candidate with no threats inside SAFETY_RADIUS
        for cand_dist, cand_name, cand_pos in candidates:
            threat = self._first_threat_near(cand_pos, all_entities)
            if threat is None:
                return (cand_name, cand_pos, cand_dist), "found"
            t_dist, t_name = threat
            log.info(
                f"  Skipping {cand_name!r} at ({cand_pos.x:.0f},{cand_pos.y:.0f},{cand_pos.z:.0f})"
                f"  --  threat {t_name!r} {t_dist:.0f}u away"
            )

        return None, "all_unsafe"

    @staticmethod
    def _first_threat_near(
        wisp_pos: XYZ,
        all_entities: list[Tuple[float, str, str, XYZ]],
    ) -> Optional[Tuple[float, str]]:
        """Return (distance, name) of the first non-safe entity within
        SAFETY_RADIUS of wisp_pos, or None if the spot is clear.
        """
        for _player_dist, name, _disp, pos in all_entities:
            if pos.x == wisp_pos.x and pos.y == wisp_pos.y and pos.z == wisp_pos.z:
                continue  # the wisp itself
            if _is_safe_neighbor(name):
                continue
            d = _distance(wisp_pos, pos)
            if d < SAFETY_RADIUS:
                return d, name
        return None
