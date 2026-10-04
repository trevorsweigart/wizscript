"""Intercept a loaded objective creature using fresh movement observations."""

import asyncio
import math
import time

from wizwalker.utils import XYZ

from entities import distance
from game_info import fetch_position
from quest_state import safe_read


def intercept_points(previous, current, elapsed, yaw, lead_seconds=0.8):
    """Stage ahead of a moving creature, then walk through its projected path."""
    vx = (current.x - previous.x) / max(elapsed, 0.05)
    vy = (current.y - previous.y) / max(elapsed, 0.05)
    speed = math.hypot(vx, vy)
    if not math.isfinite(speed) or speed > 600:
        # A teleport/respawn is not evidence of walking velocity.
        vx = vy = speed = 0
    if speed > 10:
        dx, dy = vx / speed, vy / speed
    else:
        yaw = yaw if isinstance(yaw, (float, int)) and math.isfinite(yaw) else 0
        dx, dy = -math.sin(yaw), -math.cos(yaw)
    center = XYZ(current.x + vx * lead_seconds, current.y + vy * lead_seconds, current.z)
    return (XYZ(center.x + dx * 150, center.y + dy * 150, center.z),
            XYZ(center.x - dx * 100, center.y - dy * 100, center.z))


async def approach_enemy(client, name, teleport, status, running):
    player = await fetch_position(client)
    if player is None:
        return False
    choices = []
    for entity in await safe_read(client.get_base_entity_list, []):
        if (await safe_read(entity.display_name, "") or "").casefold() != name.casefold():
            continue
        body = await safe_read(entity.actor_body)
        point = await safe_read(body.position) if body else None
        if point is not None:
            choices.append((distance(player, point), body, point))
    if not choices:
        return False
    _, body, previous = min(choices, key=lambda row: row[0])
    started = time.monotonic()
    await asyncio.sleep(0.2)
    current = await safe_read(body.position)
    yaw = await safe_read(body.yaw)
    if current is None or not running() or await client.is_loading() or await client.in_battle():
        return True
    stage, _ = intercept_points(previous, current, time.monotonic() - started, yaw)
    status(f"Intercepting {name} — walking into its path")
    if await teleport(client, stage, player) == "failed":
        return True
    for _ in range(3):
        if not running() or await client.is_loading() or await client.in_battle() or await client.is_in_dialog():
            return True
        observed_at = time.monotonic()
        fresh = await safe_read(body.position)
        if fresh is None:
            return True
        _, through = intercept_points(current, fresh, observed_at - started, await safe_read(body.yaw), 0.3)
        current, started = fresh, observed_at
        try:
            await asyncio.wait_for(client.goto(through.x, through.y), 2.5)
        except TimeoutError:
            return True
        await asyncio.sleep(0.25)
    return True
