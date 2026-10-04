"""Monitor battles and let Jev select actions on the background asyncio loop."""

import asyncio
import logging
from typing import Callable, Optional

from combat import JevCombat, write_report
from combat_state import CombatStateCollector
from jev_credentials import load_api_key


log = logging.getLogger("auto_combat")


class AutoCombat:
    def __init__(self):
        self._running = False
        self._task = None
        self._engine = None
        self._generation = 0
        self._on_status: Optional[Callable[[str], None]] = None
        self._on_failed = None

    @property
    def is_running(self):
        return self._running

    def set_status_callback(self, callback):
        self._on_status = callback

    def set_failed_callback(self, callback):
        self._on_failed = callback

    def _emit_status(self, message):
        if self._on_status:
            self._on_status(message)

    def _failed(self):
        if self._on_failed:
            self._on_failed()

    def start(self, client, loop):
        if self._running:
            return
        try:
            if not load_api_key():
                raise RuntimeError("Save a Jev API key first.")
        except RuntimeError as error:
            self._emit_status(str(error))
            self._failed()
            return
        self._generation += 1
        self._running = True
        self._task = asyncio.run_coroutine_threadsafe(self._combat_loop(client, self._generation), loop)
        self._emit_status("Jev auto-combat enabled")

    def stop(self):
        self._running = False
        self._generation += 1
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None
        self._engine = None
        self._emit_status("Jev auto-combat disabled")

    async def export_snapshot(self, client):
        if self._engine:
            return await self._engine.export_snapshot(client)
        snapshot = await CombatStateCollector().collect(client)
        write_report("combat_snapshot.json", snapshot.state)
        return snapshot

    async def _combat_loop(self, client, generation):
        engine = None
        failures = 0
        try:
            engine = JevCombat()
            self._engine = engine
            while self._running and self._generation == generation:
                try:
                    result, description = await engine.tick(client)
                    failures = 0
                    self._emit_status(description)
                    await asyncio.sleep(0.15 if result == "acted" else 0.5)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    failures += 1
                    log.warning("Jev combat tick failed: %s", error)
                    self._emit_status(f"Combat error: {error}")
                    if failures >= 3:
                        self._emit_status(f"Jev stopped after 3 errors: {error}")
                        self._failed()
                        break
                    await asyncio.sleep(3.0)
        except asyncio.CancelledError:
            pass
        except Exception as error:
            self._emit_status(f"Jev could not start: {error}")
            self._failed()
        finally:
            if engine:
                await engine.close()
            if self._generation == generation:
                self._running = False
                self._engine = None
