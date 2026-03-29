"""
Teleporter — teleportation logic decoupled from UI.
Supports absolute, relative, and quest-objective teleportation.
"""

from wizwalker.client import Client
from wizwalker.utils import XYZ

from game_info import fetch_position, fetch_quest_position


async def teleport_absolute(client: Client, x: float, y: float, z: float):
    """
    Teleport the player to exact world coordinates.

    Args:
        client: Connected wizwalker Client with active hooks.
        x, y, z: Target world coordinates.
    """
    target = XYZ(x, y, z)
    await client.teleport(target, wait_on_inuse=True)


async def teleport_relative(client: Client, dx: float, dy: float, dz: float):
    """
    Teleport the player relative to their current position.

    Args:
        client: Connected wizwalker Client with active hooks.
        dx, dy, dz: Offset to add to current position.

    Raises:
        RuntimeError: If the current position cannot be read.
    """
    current = await fetch_position(client)
    if current is None:
        raise RuntimeError("Could not read current position")

    target = XYZ(current.x + dx, current.y + dy, current.z + dz)
    await client.teleport(target, wait_on_inuse=True)


async def teleport_to_quest(client: Client):
    """
    Teleport the player to their current quest objective.

    Args:
        client: Connected wizwalker Client with active hooks.

    Raises:
        RuntimeError: If the quest position cannot be read.
    """
    quest_pos = await fetch_quest_position(client)
    if quest_pos is None:
        raise RuntimeError("Could not read quest position")

    await client.teleport(quest_pos, wait_on_inuse=True)
