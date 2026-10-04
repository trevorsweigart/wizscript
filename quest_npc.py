"""Resolve interaction names and approach points from current observations."""

import math

from wizwalker.utils import XYZ

from quest_navigation import plain_text


def prompt_name(ui):
    names = [plain_text(node.text) for node in ui.walk()
             if node.name == "NPCRangeTxtTitle"] if ui else []
    return names[0] if len(names) == 1 else None


def approach_point(player, target, neighbors, radius=100, attempt=0):
    # Stand on the side away from a nearby NPC so overlapping interaction
    # radii do not select the previous quest giver.
    nearby = [pos for pos in neighbors
              if 0 < math.hypot(pos.x - target.x, pos.y - target.y) < 1000
              and abs(pos.z - target.z) < 200]
    if nearby:
        other = min(nearby, key=lambda pos: math.hypot(pos.x - target.x, pos.y - target.y))
        dx, dy = target.x - other.x, target.y - other.y
    else:
        dx, dy = player.x - target.x, player.y - target.y
    length = math.hypot(dx, dy)
    angle = math.atan2(dy, dx) + (attempt % 8) * math.pi / 4 if length else attempt * math.pi / 4
    return XYZ(target.x + math.cos(angle) * radius,
               target.y + math.sin(angle) * radius, target.z)
