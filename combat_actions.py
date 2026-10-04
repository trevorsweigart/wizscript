"""Enumerate UI actions from fresh facts; execute only Jev's selected option."""

import asyncio
import json
from collections import Counter
from dataclasses import dataclass
from itertools import combinations

from wizwalker import Keycode


@dataclass(frozen=True)
class CombatAction:
    label: str
    kind: str
    description: dict
    card_id: str | None = None
    target_ids: tuple = ()
    ends_turn: bool = True


def effect_targets(effects):
    targets = []
    for effect in effects or []:
        if not isinstance(effect, dict):
            continue
        target = effect.get("effect_target")
        if target and target != "invalid_target":
            targets.append(target)
        for name in ("effects_list", "effect_list", "output_effect"):
            targets.extend(effect_targets(effect.get(name)))
        for element in effect.get("conditional_effects", []):
            targets.extend(effect_targets([element.get("effect")]))
    return targets


def is_enchantment(card):
    return card["template"].get("type_name") == "Enchantment" or any(
        target in ("spell", "specific_spells") for target in effect_targets(card.get("effects")))


def _target_sets(card, members):
    targets = effect_targets(card.get("effects"))
    enemies = [m for m in members if m["relation"] == "enemy" and isinstance(m.get("player_health"), int)
               and m["player_health"] > 0 and m.get("untargetable") is False and m.get("exit_combat") is False]
    allies = [m for m in members if m["relation"] in ("self", "ally")
              and m.get("untargetable") is False and m.get("exit_combat") is False]
    player = next((m for m in members if m["relation"] == "self"), None)
    # Self side-effects of a drain do not change its enemy targeting contract.
    primary = next((t for t in targets if t not in ("spell", "specific_spells")), None)
    if primary in ("multi_target_enemy", "multi_target_friendly"):
        available = enemies if primary == "multi_target_enemy" else allies
        for count in range(1, len(available) + 1):
            for group in combinations(available, count):
                yield tuple(m["id"] for m in group), "selected " + ", ".join(f"{m['name']} [{m['id']}]" for m in group), True
    elif primary in ("enemy_single", "preselected_enemy_single", "at_least_one_enemy"):
        for member in enemies:
            yield (member["id"],), f"enemy {member['name']} [{member['id']}]", False
    elif primary in ("friendly_single", "friendly_single_not_me", "minion", "friendly_minion"):
        for member in allies:
            if primary == "friendly_single_not_me" and member["is_self"]:
                continue
            if primary in ("minion", "friendly_minion") and member["is_minion"] is not True:
                continue
            yield (member["id"],), f"ally {member['name']} [{member['id']}]", False
    elif primary == "self" and player:
        yield (player["id"],), f"yourself [{player['id']}]", False
    elif primary in ("enemy_team", "enemy_team_all_at_once") and enemies:
        yield (), "all enemies", False
    elif primary in ("friendly_team", "friendly_team_all_at_once"):
        yield (), "your team", False
    elif primary == "target_global":
        yield (), "the battlefield", False


def available_actions(snapshot):
    state = snapshot.state
    actions = []
    for kind in ("pass", "flee"):
        if state["buttons"].get(kind):
            actions.append(CombatAction(kind, kind, {"action": kind, "ends_turn": True}))
    player = next((m for m in state["combatants"] if m["is_self"]), None)
    if player is None or not isinstance(player.get("player_health"), int) or player["player_health"] <= 0:
        return actions
    hand = state["hand"]
    for card in hand:
        identifier = card["id"]
        name = f"{card['name']} [{identifier}]"
        if card["template"].get("no_discard") is False:
            actions.append(CombatAction(f"discard {name}", "discard",
                {"card_id": identifier, "ends_turn": False}, identifier, ends_turn=False))
        if card.get("is_castable") is not True or card.get("effects") is None:
            continue
        if is_enchantment(card):
            for target in hand:
                if target["id"] == identifier or is_enchantment(target) or target.get("enchantment") != 0:
                    continue
                actions.append(CombatAction(f"enchant {target['name']} [{target['id']}] with {name}", "enchant",
                    {"card_id": identifier, "target_card_id": target["id"], "ends_turn": False,
                     "compatibility": "Choose only if spell effects/types allow this pair; UI acceptance is verified afterward."},
                    identifier, (target["id"],), ends_turn=False))
        else:
            for target_ids, target_label, multiple in _target_sets(card, state["combatants"]):
                actions.append(CombatAction(f"use {name} on {target_label}", "cast",
                    {"card_id": identifier, "target_member_ids": list(target_ids), "ends_turn": True,
                     "multiple_targets": multiple}, identifier, target_ids))
    if state["buttons"].get("draw"):
        actions.append(CombatAction("draw a treasure card", "draw", {"ends_turn": False}, ends_turn=False))
    return actions


def hand_identity(snapshot):
    return tuple((c.get("spell_id"), c.get("template_id"), c.get("enchantment"), c.get("effects"))
                 for c in snapshot.state["hand"])


def _card_signature(card):
    return json.dumps({k: card.get(k) for k in ("spell_id", "template_id", "enchantment", "effects")}, sort_keys=True)


async def execute(action, snapshot, client, collector):
    if await client.is_loading() or not await client.in_battle():
        return False
    async with client.mouse_handler:
        # Check again after capturing the mouse, immediately before sending input.
        current_key = (await client.duel.duel_id_full(), await client.duel.round_num(), await client.duel.alt_turn_counter())
        if current_key != snapshot.key or (await client.duel.duel_phase()).name != "planning":
            return False
        if action.kind in ("pass", "flee", "draw"):
            await client.mouse_handler.click_window(snapshot.buttons[action.kind])
            await asyncio.sleep(0.25)
            if action.kind == "flee":
                dialogs = await client.root_window.get_windows_with_name("MessageBoxModalWindow")
                for dialog in dialogs:
                    if await dialog.is_visible():
                        buttons = await dialog.get_windows_with_name("centerButton")
                        for button in buttons:
                            if await button.is_visible():
                                await client.mouse_handler.click_window(button)
                                break
        elif action.kind == "discard":
            await asyncio.wait_for(snapshot.cards[action.card_id].discard(sleep_time=0.2), timeout=3)
        elif action.kind == "enchant":
            await asyncio.wait_for(snapshot.cards[action.card_id].cast(
                snapshot.cards[action.target_ids[0]], sleep_time=0.2), timeout=3)
        elif action.kind == "cast":
            targets = [snapshot.members[i] for i in action.target_ids]
            target = targets if action.description.get("multiple_targets") else targets[0] if targets else None
            await asyncio.wait_for(snapshot.cards[action.card_id].cast(target, sleep_time=0.2), timeout=3)
        else:
            raise ValueError("Unknown combat action.")
    if action.ends_turn:
        return True  # Submitted, NOT proof that a spell actually resolved.
    before = Counter(_card_signature(c) for c in snapshot.state["hand"])
    source = next((c for c in snapshot.state["hand"] if c["id"] == action.card_id), None)
    target = next((c for c in snapshot.state["hand"] if action.target_ids and c["id"] == action.target_ids[0]), None)
    for _ in range(6):
        await asyncio.sleep(0.2)
        changed = await collector.hand(client)
        if changed.key != snapshot.key or changed.state["duel"]["duel_phase"] != "planning":
            return False
        after = Counter(_card_signature(c) for c in changed.state["hand"])
        removed = before - after
        added = after - before
        if action.kind == "discard" and sum(after.values()) < sum(before.values()) and removed[_card_signature(source)] > 0:
            return True
        if action.kind == "draw" and sum(after.values()) > sum(before.values()):
            return True
        if (action.kind == "enchant" and sum(after.values()) == sum(before.values()) - 1
                and removed[_card_signature(source)] > 0 and removed[_card_signature(target)] > 0
                and sum(added.values()) == 1):
            return True
    await client.send_key(Keycode.ESC, 0.1)
    return False
