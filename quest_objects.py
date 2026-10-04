"""Identify ground objectives from the active main quest and loaded objects."""

import re
import math

from wizwalker.utils import XYZ

from quest_navigation import plain_text


def sigil_approach(location, orientation, attempt=0, scale=1.0):
    # Generic four-player semicircle geometry (SigilSubCircle asset data).
    # Model yaw rotates local coordinates clockwise in the game's XY plane.
    pad_angles = (162, 200, 126, 236)
    angle = math.radians(pad_angles[attempt % 4]) - orientation.yaw
    if attempt >= 4:
        angle += math.pi
    radius = 270 * scale
    return XYZ(location.x + math.cos(angle) * radius,
               location.y + math.sin(angle) * radius,
               location.z)


def defeat_target(rows, ui):
    labels = [plain_text(n.text).casefold() for n in ui.walk() if n.name == "txtGoalName"] if ui else []
    if len(labels) != 1 or not labels[0].startswith("defeat "):
        return None
    label = labels[0][7:]
    candidates = [row for row in rows if row[2] and
                  (label == row[2].casefold() or label.startswith(row[2].casefold() + " "))]
    if not candidates:
        return None
    # Prefer the complete creature name over an overlapping shorter name.
    return min(candidates, key=lambda row: (-len(row[2]), row[0]))


def active_usage_goal(quests, quest_id, ui):
    labels = [plain_text(n.text) for n in ui.walk() if n.name == "txtGoalName"] if ui else []
    if len(labels) != 1:
        return None
    label = labels[0]
    for quest in quests or []:
        if quest.id != quest_id or quest.mainline is not True:
            continue
        matches = [g for g in quest.goals if g.kind in ("usage", "scavenge", "scavengefake")
                   and g.text and label.casefold().startswith(plain_text(g.text).casefold() + " ")]
        if len(matches) == 1:
            return matches[0], label
    return None


def matches_object(goal, label, object_name, display_name):
    if object_name.casefold() in {tag.casefold() for tag in goal.tags}:
        return True
    name = plain_text(display_name).casefold()
    target = label[len(plain_text(goal.text)):].strip()
    target = re.sub(r"\s*\(\d+\s+of\s+\d+\)\s*$", "", target, flags=re.I).casefold()
    return bool(name) and (target == name or target.startswith(name + " in "))
