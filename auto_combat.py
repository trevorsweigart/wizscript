"""
Auto Combat — monitors for battles and handles them automatically.

Runs as an independent loop that checks if the player is in battle,
waits for the planning phase, and then runs the combat decision tree.
"""

import asyncio
from typing import Callable, Optional

from wizwalker.client import Client

from combat import combat_main

# Tick rate for checking battle state
TICK_DELAY = 0.5


class AutoCombat:
    """
    Async loop that monitors for battles and runs combat logic.

    Can be toggled independently from auto-questing.
    """

    def __init__(self):
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._on_status: Optional[Callable[[str], None]] = None

    @property
    def is_running(self) -> bool:
        return self._running

    def set_status_callback(self, callback: Callable[[str], None]):
        self._on_status = callback

    def _emit_status(self, msg: str):
        if self._on_status:
            self._on_status(msg)

    # ------------------------------------------------------------------
    # Start / Stop
    # ------------------------------------------------------------------

    def start(self, client: Client, loop: asyncio.AbstractEventLoop):
        """Begin the auto-combat monitoring loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.run_coroutine_threadsafe(
            self._combat_loop(client), loop
        )
        self._emit_status("Auto-combat enabled")

    def stop(self):
        """Stop the auto-combat loop."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None
        self._emit_status("Auto-combat disabled")

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def _combat_loop(self, client: Client):
        """Monitor for battles and handle combat automatically."""
        try:
            while self._running:
                try:
                    if await client.in_battle():
                        self._emit_status("In battle — analyzing hand...")
                        async with client.mouse_handler:
                            result, description = await combat_main(client)

                        if result == "acted":
                            self._emit_status(f"⚔ {description}")
                            # Wait for the round to play out
                            await asyncio.sleep(2.0)
                        elif result == "skipped":
                            self._emit_status(f"⏸ {description}")
                            await asyncio.sleep(1.0)
                        else:
                            # "waiting" — not in planning phase yet
                            self._emit_status("Waiting for planning phase...")
                            await asyncio.sleep(TICK_DELAY)
                    else:
                        self._emit_status("Monitoring for battles...")
                        await asyncio.sleep(TICK_DELAY)

                except Exception as e:
                    self._emit_status(f"Combat error: {e}")
                    await asyncio.sleep(1.0)

        except asyncio.CancelledError:
            pass
        finally:
            self._running = False
