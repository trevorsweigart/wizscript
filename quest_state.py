"""Fresh quest metadata and visible UI observations for quest automation."""

import asyncio
import json
from dataclasses import asdict, dataclass, field

from wizwalker.memory.memory_objects.enums import WindowFlags

from runtime_paths import data_directory


async def safe_read(call, default=None):
    try:
        return await asyncio.wait_for(call(), 1.5)
    except Exception:
        return default


@dataclass
class QuestGoal:
    id: int
    text: str = ""
    status: bool | None = None
    destination: str = ""
    kind: str = ""
    tags: list[str] = field(default_factory=list)
    no_helper: bool | None = None


@dataclass
class QuestEntry:
    id: int
    name: str = ""
    mainline: bool | None = None
    ready: bool | None = None
    goals: list[QuestGoal] = field(default_factory=list)


async def quest_entries(client):
    """Read the quest manager's current entries; failure is distinct from empty."""
    try:
        return await asyncio.wait_for(_quest_entries(client), 8)
    except TimeoutError:
        return None


async def _quest_entries(client):
    manager = await safe_read(client.quest_manager)
    if manager is None:
        return None
    entries = await safe_read(manager.quest_data)
    if entries is None:
        return None
    result = []
    for quest_id, quest in list(entries.items())[:256]:
        key = await safe_read(quest.name_lang_key, "")
        name = await safe_read(lambda: client.cache_handler.get_langcode_name(key), key) if key else ""
        entry = QuestEntry(quest_id, name, await safe_read(quest.mainline), await safe_read(quest.ready_to_turn_in))
        goals = await safe_read(quest.goal_data, {})
        for goal_id, goal in list(goals.items())[:32]:
            key = await safe_read(goal.name_lang_key, "")
            text = await safe_read(lambda: client.cache_handler.get_langcode_name(key), key) if key else ""
            kind = await safe_read(goal.goal_type)
            tag_list = await safe_read(goal.client_tag_list)
            entry.goals.append(QuestGoal(goal_id, text, await safe_read(goal.goal_status),
                                         await safe_read(goal.goal_destination_zone, ""),
                                         getattr(kind, "name", ""),
                                         await safe_read(tag_list.client_tags, []) if tag_list else [],
                                         await safe_read(goal.no_quest_helper)))
        result.append(entry)
    return result


@dataclass
class UiNode:
    name: str
    kind: str
    text: str
    enabled: bool
    window: object = field(repr=False)
    children: list = field(default_factory=list)

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()

    def report(self):
        return dict(name=self.name, kind=self.kind, text=self.text, enabled=self.enabled,
                    children=[child.report() for child in self.children])


async def visible_ui(client):
    """Bounded traversal; parent visibility applies to all descendants."""
    remaining = 768

    async def visit(window, depth=0, root=False, parent_enabled=True):
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > 16:
            return None
        flags = await safe_read(window.flags)
        if flags is None or (not root and WindowFlags.visible not in flags):
            return None
        name = await safe_read(window.name, "")
        kind = await safe_read(window.maybe_read_type_name, "")
        # Only text controls have the documented wide-string layout.
        text = await safe_read(window.maybe_text, "") if kind in ("ControlText", "ControlList", "ControlButton", "ControlCheckBox") else ""
        enabled = parent_enabled and WindowFlags.disabled not in flags
        node = UiNode(name, kind, text, enabled, window)
        for child in await safe_read(window.children, []):
            observed = await visit(child, depth + 1, parent_enabled=enabled)
            if observed is not None:
                node.children.append(observed)
        return node

    return await asyncio.wait_for(visit(client.root_window, root=True), 8)


async def export_quest_snapshot(client):
    from entities import list_nearby_entities
    if await client.is_loading():
        raise RuntimeError("Wait for loading to finish before exporting quests")
    quests = await quest_entries(client)
    ui = await visible_ui(client)
    report = dict(zone=await safe_read(client.zone_name), quest_id=await safe_read(client.quest_id),
                  goal_id=await safe_read(client.goal_id), finder=await safe_read(client.stats.quest_finder_enabled),
                  quests=None if quests is None else [asdict(q) for q in quests],
                  ui=None if ui is None else ui.report())
    report["entities"] = [dict(distance=dist, object_name=obj, display_name=name,
                                position=dict(x=pos.x, y=pos.y, z=pos.z))
                          for dist, obj, name, pos in await list_nearby_entities(client)]
    report["entrances"] = []
    report["npcs"] = []
    from spell_management import spell_ui_report
    report["spell_ui"] = await spell_ui_report(client, ui)
    report["school_id"] = await safe_read(client.stats.school_id)
    report["level"] = await safe_read(client.stats.reference_level)
    book = await safe_read(client.client_object.try_get_spellbook_behavior)
    report["trained_spell_ids"] = await safe_read(book.trained_spell_ids) if book else None
    deck = await safe_read(client.client_object.try_get_deck_behavior)
    report["deck"] = await safe_read(deck.deck_contents) if deck else None
    labels = [n.text.casefold() for n in ui.walk() if n.name == "txtGoalName"] if ui else []
    for entity in await safe_read(client.get_base_entity_list, []):
        name = await safe_read(entity.object_name, "") or ""
        display = await safe_read(entity.display_name, "") or ""
        if display and any(display.casefold() in label for label in labels):
            body = await safe_read(entity.actor_body)
            pos = await safe_read(body.position) if body else None
            report["npcs"].append(dict(object_name=name, display_name=display,
                location=str(await safe_read(entity.location)), body_position=str(pos),
                behaviors=await safe_read(entity.list_behavior_names, [])))
        if "teleport" not in name.casefold() and "door" not in name.casefold():
            continue
        location = await safe_read(entity.location)
        orientation = await safe_read(entity.orientation)
        report["entrances"].append(dict(object_name=name, debug_name=await safe_read(entity.debug_name, ""),
            location=None if location is None else dict(x=location.x, y=location.y, z=location.z),
            orientation=str(orientation), behaviors=await safe_read(entity.list_behavior_names, [])))
    path = data_directory() / "quest_snapshot.json"
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return path
