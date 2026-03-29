"""
Game Info — async data-fetching layer that reads live game state
from a connected wizwalker Client.
"""

from dataclasses import dataclass, field
from typing import Optional

from wizwalker.client import Client
from wizwalker.utils import XYZ


@dataclass
class GameState:
    """Snapshot of the current game state."""

    # Player position
    pos_x: float = 0.0
    pos_y: float = 0.0
    pos_z: float = 0.0

    # Current zone
    zone: str = "N/A"

    # Quest objective position
    quest_x: float = 0.0
    quest_y: float = 0.0
    quest_z: float = 0.0

    # Quest identifiers
    quest_id: int = 0
    goal_id: int = 0
    quest_finder_enabled: bool = False

    # Whether data was successfully read
    valid: bool = False


async def fetch_position(client: Client) -> Optional[XYZ]:
    """Read the player's current XYZ position."""
    try:
        return await client.body.position()
    except Exception:
        return None


async def fetch_zone(client: Client) -> Optional[str]:
    """Read the client's current zone name."""
    try:
        return await client.zone_name()
    except Exception:
        return None


async def fetch_quest_position(client: Client) -> Optional[XYZ]:
    """Read the current quest objective's XYZ position."""
    try:
        return await client.quest_position.position()
    except Exception:
        return None


async def fetch_all(client: Client) -> GameState:
    """
    Fetch all game info in one call.

    Returns:
        A GameState dataclass with all available data populated.
    """
    state = GameState()

    pos = await fetch_position(client)
    if pos is not None:
        state.pos_x = pos.x
        state.pos_y = pos.y
        state.pos_z = pos.z
        state.valid = True

    zone = await fetch_zone(client)
    if zone is not None:
        state.zone = zone

    quest_pos = await fetch_quest_position(client)
    if quest_pos is not None:
        state.quest_x = quest_pos.x
        state.quest_y = quest_pos.y
        state.quest_z = quest_pos.z

    try:
        state.quest_id = await client.quest_id()
    except Exception:
        pass

    try:
        state.goal_id = await client.goal_id()
    except Exception:
        pass

    try:
        state.quest_finder_enabled = await client.quest_finder_enabled()
    except Exception:
        pass

    return state
