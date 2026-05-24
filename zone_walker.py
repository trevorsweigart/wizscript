"""
Zone Walker  --  step through a recorded transition path.

Shared between App's "Teleport to Zone" feature and AutoHealer's
fallback-to-another-zone behaviour. Given a list of transition dicts
(as produced by `ZoneMapper.find_path` or `find_nearest`), teleports to
each `from_pos`, waits for the loading screen, and verifies arrival.
"""

import asyncio
import logging
from typing import Callable, List, Optional

from wizwalker.client import Client
from wizwalker.utils import XYZ

log = logging.getLogger("zone_walker")

# Loading-screen wait windows
LOAD_START_POLL_INTERVAL = 0.2
LOAD_START_MAX_POLLS = 15        # 3.0s
LOAD_FINISH_POLL_INTERVAL = 0.2
LOAD_FINISH_MAX_POLLS = 300      # 60s
SETTLE_AFTER_LOAD = 1.0


async def walk_zone_path(
    client: Client,
    path: List[dict],
    on_status: Optional[Callable[[str], None]] = None,
    is_active: Optional[Callable[[], bool]] = None,
) -> bool:
    """Step through each transition in `path`.

    Args:
        client: connected wizwalker Client
        path: list of transition dicts; each must have `to_zone` and
              `from_pos` (3-element XYZ list).
        on_status: optional status sink (called with progress messages).
        is_active: optional polled flag; if it returns False at a check
                   point, the walk aborts cleanly.

    Returns:
        True on full successful arrival at the final `to_zone`,
        False on any failure.
    """
    def emit(msg: str):
        log.info(msg)
        if on_status:
            on_status(msg)

    if not path:
        return True

    for i, t in enumerate(path):
        if is_active is not None and not is_active():
            emit("Walk cancelled")
            return False

        hop = f"[{i + 1}/{len(path)}]"
        to_zone = t["to_zone"]
        fp = t["from_pos"]
        target = XYZ(float(fp[0]), float(fp[1]), float(fp[2]))

        emit(f"{hop} teleporting to door for {to_zone}...")
        try:
            await client.teleport(target, wait_on_inuse=True)
        except Exception as e:
            emit(f"{hop} teleport failed: {e}")
            return False

        # Wait for loading screen to appear
        load_started = False
        for _ in range(LOAD_START_MAX_POLLS):
            await asyncio.sleep(LOAD_START_POLL_INTERVAL)
            try:
                if await client.is_loading():
                    load_started = True
                    break
            except Exception:
                pass
        if not load_started:
            emit(f"{hop} no load screen triggered  --  stale path?")
            return False

        emit(f"{hop} loading {to_zone}...")

        # Wait for loading screen to finish
        load_finished = False
        for _ in range(LOAD_FINISH_MAX_POLLS):
            if is_active is not None and not is_active():
                return False
            await asyncio.sleep(LOAD_FINISH_POLL_INTERVAL)
            try:
                if not await client.is_loading():
                    load_finished = True
                    break
            except Exception:
                pass
        if not load_finished:
            emit(f"{hop} loading never finished")
            return False

        await asyncio.sleep(SETTLE_AFTER_LOAD)

        try:
            actual = await client.zone_name()
        except Exception:
            actual = None
        if actual != to_zone:
            emit(f"{hop} expected {to_zone}, got {actual!r}  --  aborting")
            return False

        emit(f"{hop} arrived at {to_zone}")

    return True
