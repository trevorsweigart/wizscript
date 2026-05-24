"""
Entity helpers  --  shared utilities for scanning loaded WizClientObjects.

Provides:
  - `list_nearby_entities` to enumerate every loaded entity with its
    distance, name, display name, and position
  - Wisp predicates (`is_health_wisp`, `is_mana_wisp`, `is_any_wisp`)
    used to identify resource pickups in the world
  - `detect_wisps_in_zone` -- one-shot check whether the current zone
    contains health and/or mana wisps
"""

import logging
import math
from typing import List, Optional, Tuple

from wizwalker.client import Client
from wizwalker.utils import XYZ

log = logging.getLogger("entities")


def distance(a: XYZ, b: XYZ) -> float:
    return math.sqrt((a.x - b.x) ** 2 + (a.y - b.y) ** 2 + (a.z - b.z) ** 2)


# ---------------------------------------------------------------------------
# Wisp identification (verified from real-zone log output)
# ---------------------------------------------------------------------------

def is_health_wisp(obj_name: Optional[str]) -> bool:
    return bool(obj_name) and obj_name.lower().startswith("wc_wisphealth")


def is_mana_wisp(obj_name: Optional[str]) -> bool:
    return bool(obj_name) and obj_name.lower().startswith("wc_wispmana")


def is_any_wisp(obj_name: Optional[str]) -> bool:
    return is_health_wisp(obj_name) or is_mana_wisp(obj_name)


# ---------------------------------------------------------------------------
# Scan helpers
# ---------------------------------------------------------------------------

EntityRow = Tuple[float, str, str, XYZ]  # (dist, obj_name, display_name, pos)


async def list_nearby_entities(client: Client) -> List[EntityRow]:
    """Return every loaded entity, sorted by distance from the player."""
    try:
        entities = await client.get_base_entity_list()
    except Exception as e:
        log.warning(f"get_base_entity_list failed: {e}")
        return []

    try:
        player_pos = await client.body.position()
    except Exception as e:
        log.warning(f"player position read failed: {e}")
        return []

    out: List[EntityRow] = []
    for entity in entities:
        try:
            template = await entity.object_template()
            if template is None:
                continue
            obj_name = await template.object_name() or ""
            try:
                disp = await entity.display_name() or ""
            except Exception:
                disp = ""
            body = await entity.actor_body()
            if body is None:
                continue
            pos = await body.position()
            out.append((distance(player_pos, pos), obj_name, disp, pos))
        except Exception:
            continue

    out.sort(key=lambda r: r[0])
    return out


async def detect_wisps_in_zone(client: Client) -> Tuple[bool, bool]:
    """Return (has_health_wisp, has_mana_wisp) for the currently loaded zone."""
    entities = await list_nearby_entities(client)
    has_h = any(is_health_wisp(name) for _, name, _, _ in entities)
    has_m = any(is_mana_wisp(name) for _, name, _, _ in entities)
    return has_h, has_m
