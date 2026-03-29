"""
Auto Quester — automated quest progression loop.

Teleports to quest objectives, handles NPC dialog, and includes a safety
mechanism that disables itself if a teleport fails repeatedly (player snaps back).
"""

import asyncio
import math
from typing import Callable, Optional

from wizwalker.client import Client
from wizwalker.constants import Keycode
from wizwalker.utils import XYZ

from game_info import fetch_position, fetch_quest_position

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
        self._consecutive_failures = 0

    @property
    def is_running(self) -> bool:
        return self._running

    def set_status_callback(self, callback: Callable[[str], None]):
        self._on_status = callback

    def set_stopped_callback(self, callback: Callable[[], None]):
        """Called when auto-quest stops itself (e.g. failed teleport)."""
        self._on_stopped = callback

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
                        # Give the game a moment to settle after loading
                        await asyncio.sleep(1.0)
                        continue

                    in_battle = await client.in_battle()
                    in_dialog = await client.is_in_dialog()

                    if in_battle:
                        # Pause during combat — don't interfere
                        self._emit_status("In battle — waiting...")
                        await asyncio.sleep(1.0)

                    elif in_dialog:
                        # Advance dialog
                        self._emit_status("In dialog — advancing...")
                        await client.send_key(Keycode.SPACEBAR, 0)
                        await asyncio.sleep(TICK_DELAY)

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
                return "success"
            if await client.is_in_dialog():
                return "success"
            if await self._is_loading(client):
                return "success"
        except Exception:
            return "success"

        # Check if we snapped back
        post_pos = await fetch_position(client)
        if post_pos is None:
            return "skipped"

        dist_from_original = _distance(post_pos, pre_pos)
        dist_from_target = _distance(post_pos, target)

        if dist_from_original < SNAP_BACK_THRESHOLD and dist_from_target > SNAP_BACK_THRESHOLD:
            return "failed"

        return "success"

