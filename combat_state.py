"""Read fresh Wizard101 combat facts without making strategic decisions."""

import asyncio
import hashlib
import inspect
import json
import math
import time
from dataclasses import dataclass, field
from enum import Enum

from wizwalker.combat.handler import CombatHandler
from wizwalker.memory.memory_objects.enums import MagicSchool
from wizwalker.memory.memory_objects.spell_effect import cast_effect_variant
from wizwalker.memory.memory_objects.spell_template import DynamicSpellTemplate


SCHOOLS = ("balance", "death", "fire", "ice", "life", "myth", "storm")
PIP_FIELDS = ("generic_pips", "power_pips", "shadow_pips") + tuple(f"{s}_pips" for s in SCHOOLS)
COST_FIELDS = ("spell_rank", "shadow_pips", "is_xpip_spell") + tuple(f"{s}_pips" for s in SCHOOLS)
STATS_FIELDS = tuple("""
current_hitpoints max_hitpoints current_mana max_mana base_hitpoints bonus_hitpoints
base_mana bonus_mana reference_level level_scaled school_id secondary_school
dmg_bonus_percent dmg_bonus_flat acc_bonus_percent ap_bonus_percent
dmg_reduce_percent dmg_reduce_flat acc_reduce_percent heal_bonus_percent
heal_inc_bonus_percent dmg_bonus_percent_all dmg_bonus_flat_all acc_bonus_percent_all
ap_bonus_percent_all dmg_reduce_percent_all dmg_reduce_flat_all acc_reduce_percent_all
heal_bonus_percent_all heal_inc_bonus_percent_all critical_hit_percent_all block_percent_all
critical_hit_rating_all block_rating_all critical_hit_percent_by_school block_percent_by_school
critical_hit_rating_by_school block_rating_by_school power_pip_base power_pip_bonus_percent_all
balance_mastery death_mastery fire_mastery ice_mastery life_mastery myth_mastery storm_mastery
stun_resistance_percent shadow_pip_max shadow_pip_bonus_percent shadow_pip_rating
bonus_shadow_pip_rating shadow_pip_rate_accumulated shadow_pip_rate_threshold shadow_pip_rate_percentage
pip_conversion_rating_all pip_conversion_rating_per_school pip_conversion_percent_all
pip_conversion_percent_per_school archmastery_base archmastery_bonus_flat archmastery_bonus_percentage
pet_act_chance spell_charge_base spell_charge_bonus spell_charge_bonus_all
""".split())
PARTICIPANT_FIELDS = tuple("""
owner_id_full template_id_full team_id original_team primary_magic_school_id subcircle
player_health max_player_health cur_max_hp is_player is_minion boss_mob stunned mindcontrolled
pips_suspended aura_turn_length polymorph_turn_length rounds_dead max_hand_size
accuracy_bonus polymorph_spell_template_id shadow_spells_disabled backlash shadow_creature_level
rounds_since_shadow_pip confused confused_target untargetable untargetable_rounds restricted_target
exit_combat auto_pass vanish my_team_turn shadow_pip_rate_threshold base_spell_damage
stat_damage stat_resist stat_pierce mob_level deck_fullness archmastery_points
max_archmastery_points archmastery_school archmastery_flags shadow_pact_target
""".split())
EFFECT_FIELDS = tuple("""
effect_type effect_param disposition string_damage_type damage_type pip_num act_num effect_target
num_rounds param_per_round heal_modifier spell_template_id enchantment_spell_template_id
act cloaked bypass_protection armor_piercing_param chance_per_target protected converted rank
""".split())
TEMPLATE_FIELDS = tuple("""
name display_name description advanced_description description_combat_hud type_name magic_school_name
secondary_school_name required_school_name accuracy valid_target_spells no_discard leaves_play_when_cast
pvp pve no_pvp_enchant no_pve_enchant ignore_charms ignore_dispel always_fizzle
spell_category spell_fusion delay_enchantment delay_enchantment_order
""".split())
SPELL_FIELDS = tuple("""
template_id spell_id enchantment magic_school_id secondary_school_id accuracy regular_adjust
treasure_card battle_card item_card side_board cloaked enchanted_this_combat
enchantment_spell_is_item_card premutation_spell_id leaves_play_when_cast_override
delay_enchantment delay_enchantment_order pve fusion_state
""".split())
DUEL_FIELDS = tuple("""
duel_id_full round_num duel_phase planning_timer disable_timer pvp raid battleground
first_team_to_act original_first_team_to_act dynamic_turn dynamic_turn_counter alt_turn_counter
initiative_switch_mode initiative_switch_rounds execution_order scalar_damage scalar_resist scalar_pierce
damage_limit d_k0 d_n0 resist_limit r_k0 r_n0 shadow_threshold_factor shadow_pip_rating_factor
shadow_pip_threshold_team0 shadow_pip_threshold_team1 pass_penalty match_timer min_turn_time
""".split())
EFFECT_GROUPS = ("hanging_effects", "public_hanging_effects", "aura_effects", "shadow_spell_effects",
                 "death_activated_effects", "delay_cast_effects")


def plain(value):
    if isinstance(value, Enum):
        return value.name
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return None  # Never send memory-object reprs or addresses as facts.


class Read:
    """Budget optional reads and explicitly mark transient/unsupported fields."""

    def __init__(self, seconds=12):
        self.deadline = time.monotonic() + seconds
        self.unavailable = []

    async def get(self, obj, name, label=None, *args):
        label = label or name
        try:
            method = getattr(obj, name, None)
            if not callable(method):
                raise AttributeError()
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError()
            return await asyncio.wait_for(method(*args), min(1.5, remaining))
        except Exception as error:
            self.unavailable.append(f"{label}: {type(error).__name__}")
            return None

    async def fields(self, obj, names, label):
        return {name: plain(await self.get(obj, name, f"{label}.{name}")) for name in names}

    def bounded(self, items, label, limit=128):
        if items is None:
            return []
        if len(items) > limit:
            self.unavailable.append(f"{label}: truncated to {limit} entries")
        return items[:limit]


async def _requirements(reader, requirement, depth=0):
    if requirement is None or depth > 5:
        return None
    data = {"kind": await reader.get(requirement, "maybe_read_type_name", "condition.kind")}
    # Only zero-argument, public metadata readers in the conditionals module.
    for name, method in inspect.getmembers(type(requirement), inspect.iscoroutinefunction):
        if name.startswith(("_", "read_", "write_")) or name in ("apply", "get_target", "requirements"):
            continue
        if not method.__module__.endswith(".conditionals"):
            continue
        if any(p.default is p.empty and p.name != "self" for p in inspect.signature(method).parameters.values()):
            continue
        data[name] = plain(await reader.get(requirement, name, f"condition.{name}"))
    if hasattr(requirement, "requirements"):
        children = await reader.get(requirement, "requirements", "condition.children")
        data["requirements"] = [await _requirements(reader, child, depth + 1)
                                for child in reader.bounded(children, "conditions", 32)]
    return data


async def read_effect(reader, effect, depth=0):
    # Some Wizwalker CountBasedSpellEffect versions return unawaited variant
    # promotions in their child list; resolve them before reading metadata.
    if inspect.isawaitable(effect):
        try:
            effect = await asyncio.wait_for(effect, 1.5)
        except Exception as error:
            reader.unavailable.append(f"effect.variant: {type(error).__name__}")
            return None
    if effect is None:
        return None
    if depth > 6:
        reader.unavailable.append("effect: nesting depth exceeded")
        return {"unavailable": "nesting depth exceeded"}
    try:
        if hasattr(effect, "hook_handler"):
            effect = await asyncio.wait_for(cast_effect_variant(effect), 1.5)
    except Exception as error:
        reader.unavailable.append(f"effect.variant: {type(error).__name__}")
    data = await reader.fields(effect, EFFECT_FIELDS, "effect")
    data["kind"] = await reader.get(effect, "maybe_read_type_name", "effect.kind")
    for name in ("hanging_effect_type", "specific_effect_types", "min_effect_value", "max_effect_value",
                 "min_effect_count", "max_effect_count", "output_selector", "scale_source_effect_percent",
                 "apply_to_effect_source", "mode", "initial_backlash", "caster_sc", "target_sc",
                 "pact_effect_kind", "backlash_per_round", "added_in_round"):
        if hasattr(effect, name):
            data[name] = plain(await reader.get(effect, name, f"effect.{name}"))
    # Some variants use the same offset for scalars instead of a child list.
    for name in ("effects_list", "output_effect", "effect_list"):
        if hasattr(effect, name):
            children = await reader.get(effect, name, f"effect.{name}")
            data[name] = None if children is None else [
                await read_effect(reader, child, depth + 1)
                for child in reader.bounded(children, f"effect.{name}", 32)]
    if hasattr(effect, "elements"):
        elements = await reader.get(effect, "elements", "effect.conditions")
        data["conditional_effects"] = []
        for element in reader.bounded(elements, "effect.conditions", 32):
            data["conditional_effects"].append({
                "requirements": await _requirements(reader, await reader.get(element, "reqs")),
                "effect": await read_effect(reader, await reader.get(element, "effect"), depth + 1),
            })
    return data


async def effects(reader, obj, method, label):
    values = await reader.get(obj, method, label)
    if values is None:
        return None
    return [await read_effect(reader, value) for value in reader.bounded(values, label)]


async def read_spell(reader, spell, client=None):
    data = await reader.fields(spell, SPELL_FIELDS, "spell")
    template = await reader.get(spell, "spell_template", "spell.template")
    data["template"] = await reader.fields(template, TEMPLATE_FIELDS, "template")
    cost = await reader.get(spell, "pip_cost", "spell.pip_cost")
    data["pip_cost"] = await reader.fields(cost, COST_FIELDS, "pip_cost")
    data["effects"] = await effects(reader, spell, "spell_effects", "spell.effects")
    # Name/description strings can be localization codes; keep both raw and resolved.
    if client is not None:
        for name in ("display_name", "description", "advanced_description", "description_combat_hud"):
            code = data["template"].get(name)
            if code:
                data["template"][f"{name}_text"] = await reader.get(client.cache_handler, "get_langcode_name", f"template.{name}_text", code)
    data["name"] = (data["template"].get("display_name_text") or data["template"].get("name")
                    or f"Spell {data['template_id']}")
    return data


@dataclass
class CombatSnapshot:
    state: dict
    cards: dict = field(default_factory=dict)
    members: dict = field(default_factory=dict)
    buttons: dict = field(default_factory=dict)

    @property
    def key(self):
        duel = self.state["duel"]
        return (duel["duel_id_full"], duel["round_num"], duel.get("alt_turn_counter"))

    def fingerprint(self):
        # Timer ticks and deck metadata do not invalidate a choice. All actionable
        # cards, combatants, stats, effects, buttons, and turn identity do.
        value = {name: self.state.get(name) for name in ("global_effect", "battlefield_effects", "buttons")}
        value["hand"] = [{**{k: v for k, v in card.items() if k not in ("name", "template")},
                          "template": {k: v for k, v in card["template"].items() if k in TEMPLATE_FIELDS}}
                         for card in self.state["hand"]]
        value["combatants"] = [{k: v for k, v in member.items()
                               if k not in ("remaining_deck", "graveyard", "memory_hand")}
                              for member in self.state["combatants"]]
        value["turn"] = self.key
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


class CombatStateCollector:
    async def hand(self, client):
        """Quick read for confirming a free action without re-reading all stats."""
        if await client.is_loading():
            raise RuntimeError("Client is loading.")
        reader = Read(seconds=4)
        handler = CombatHandler(client)
        duel = await reader.fields(client.duel, ("duel_id_full", "round_num", "alt_turn_counter", "duel_phase"), "duel")
        cards = await reader.get(handler, "get_cards", "hand")
        if cards is None or any(duel.get(k) is None for k in ("duel_id_full", "round_num", "duel_phase")):
            raise RuntimeError("Cannot verify the current hand.")
        state = {"duel": duel, "hand": []}
        for card in reader.bounded(cards, "hand", 32):
            spell = await reader.get(card, "get_graphical_spell")
            data = await reader.fields(spell, ("spell_id", "template_id", "enchantment"), "card")
            data["effects"] = await effects(reader, spell, "spell_effects", "card.effects")
            state["hand"].append(data)
        return CombatSnapshot(state)

    async def _button(self, reader, client, names):
        for name in names:
            windows = await reader.get(client.root_window, "get_windows_with_name", f"button.{name}", name)
            for window in reader.bounded(windows, f"button.{name}", 16):
                if await reader.get(window, "is_visible") is True and await reader.get(window, "enabled") is True:
                    return window
        return None

    async def collect(self, client, *, include_decks=True) -> CombatSnapshot:
        if await client.is_loading():
            raise RuntimeError("Client is loading; combat state is unavailable.")
        reader = Read()
        handler = CombatHandler(client)  # Never reuse cached hand/member windows across decisions.
        duel = await reader.fields(client.duel, DUEL_FIELDS, "duel")
        if any(duel.get(name) is None for name in ("duel_id_full", "round_num", "duel_phase")):
            raise RuntimeError("Cannot read the current duel and round.")
        state = {
            "duel": duel, "hand": [], "combatants": [], "buttons": {},
            "school_ids": {s.name: s.value for s in MagicSchool},
            "notes": ["Raw Wizwalker memory values: percentage units vary by field; no inferred zero defaults.",
                      "School-stat arrays retain the game's raw order; do not assume a school index.",
                      "Hidden opponent decks/hand are only included if client memory exposes them.",
                      "No public Wizwalker API provides a complete, reliable cast-event log."],
        }
        snapshot = CombatSnapshot(state)
        player_id = await reader.get(client.client_object, "global_id_full", "player_id")
        state["player_id"] = player_id
        if player_id is None:
            raise RuntimeError("Cannot identify the local combatant.")
        cards = await reader.get(handler, "get_cards", "hand")
        members = await reader.get(handler, "get_members", "combatants")
        if cards is None or members is None:
            raise RuntimeError("Hand or combatant list is unreadable.")
        # First gather essential facts for EVERY hand card and combatant.
        for index, card in enumerate(reader.bounded(cards, "hand", 32)):
            spell = await reader.get(card, "get_graphical_spell", "card.spell")
            data = await read_spell(reader, spell)  # Localization is optional, handled later.
            data.update(id=f"card_{index}", is_castable=await reader.get(card, "is_castable"),
                        selected=await reader.get(card._spell_window, "maybe_checked", "card.selected"),
                        window_id=await reader.get(card._spell_window, "read_base_address", "card.window"))
            state["hand"].append(data)
            snapshot.cards[data["id"]] = card
        for index, member in enumerate(reader.bounded(members, "combatants", 16)):
            participant = await reader.get(member, "get_participant", "participant")
            data = await reader.fields(participant, PARTICIPANT_FIELDS, "participant")
            data.update(id=f"member_{index}", name=await reader.get(member, "name", "participant.name"))
            data["is_self"] = data["owner_id_full"] == player_id
            pips = await reader.get(participant, "pip_count", "participant.pips")
            data["pips"] = await reader.fields(pips, PIP_FIELDS, "pips")
            data["stats"] = await reader.fields(await reader.get(participant, "game_stats"), STATS_FIELDS, "stats")
            for group in EFFECT_GROUPS:
                data[group] = await effects(reader, participant, group, f"participant.{group}")
            for single in ("intercept_effect", "polymorph_effect"):
                data[single] = await read_effect(reader, await reader.get(participant, single))
            state["combatants"].append(data)
            snapshot.members[data["id"]] = member
        player = next((m for m in state["combatants"] if m["is_self"]), None)
        if player is None or player["team_id"] is None:
            raise RuntimeError("The local combatant's team is unavailable.")
        for member in state["combatants"]:
            member["relation"] = ("self" if member["is_self"] else "unknown" if member["team_id"] is None
                                  else "ally" if member["team_id"] == player["team_id"] else "enemy")
        for kind, names in {"pass": ("DefeatedPassButton", "Focus"),
                            "flee": ("DefeatedFleeButton", "Flee"), "draw": ("Draw",)}.items():
            button = await self._button(reader, client, names)
            state["buttons"][kind] = button is not None
            if button is not None:
                snapshot.buttons[kind] = button
        resolver = await reader.get(client.duel, "combat_resolver", "combat_resolver")
        state["global_effect"] = await read_effect(reader, await reader.get(resolver, "global_effect"))
        state["battlefield_effects"] = await effects(reader, resolver, "battlefield_effects", "battlefield_effects")
        # Optional inventory/deck facts after all actionable facts have been read.
        if include_decks:
            for data in state["hand"]:
                for name in ("display_name", "description", "advanced_description", "description_combat_hud"):
                    code = data["template"].get(name)
                    if code:
                        data["template"][f"{name}_text"] = await reader.get(client.cache_handler, "get_langcode_name", f"template.{name}_text", code)
                data["name"] = data["template"].get("display_name_text") or data["name"]
            for data in state["combatants"]:
                participant = await reader.get(snapshot.members[data["id"]], "get_participant")
                deck = await reader.get(participant, "play_deck", "participant.play_deck")
                for label, method in (("remaining_deck", "deck_to_save"), ("graveyard", "graveyard_to_save")):
                    entries = await reader.get(deck, method, f"participant.{label}")
                    data[label] = None if entries is None else [
                        await reader.fields(entry, ("template_id", "enchantment"), label)
                        for entry in reader.bounded(entries, label, 256)]
                # Do not add an enemy's private hand as playable local cards.
                if not data["is_self"]:
                    hand = await reader.get(participant, "hand", "participant.memory_hand")
                    spells = await reader.get(hand, "spell_list", "participant.memory_hand.spells")
                    data["memory_hand"] = None if spells is None else [
                        await read_spell(reader, spell) for spell in reader.bounded(spells, "memory_hand", 16)]
            deck_behavior = await reader.get(client.client_object, "try_get_deck_behavior", "equipped_deck")
            state["equipped_deck"] = plain(await reader.get(deck_behavior, "deck_contents", "equipped_deck.contents"))
            spellbook = await reader.get(client.client_object, "try_get_spellbook_behavior", "spellbook")
            state["trained_spell_ids"] = plain(await reader.get(spellbook, "trained_spell_ids", "spellbook.trained_spell_ids"))
            entries = await reader.get(spellbook, "spell_id_list", "spellbook.entries")
            state["trained_spells"] = None if entries is None else [
                await reader.fields(entry, ("spell_id", "is_retired", "tiered_spell_group_index"), "trained_spell")
                for entry in reader.bounded(entries, "trained_spells", 512)]
            ids = set(state.get("trained_spell_ids") or [])
            ids.update(c["template_id"] for c in state["hand"] if c["template_id"] is not None)
            for data in state["combatants"]:
                for name in ("remaining_deck", "graveyard"):
                    ids.update(c["template_id"] for c in data.get(name) or [] if c["template_id"] is not None)
            state["template_id_to_name"] = {}
            for identifier in sorted(ids):
                if time.monotonic() >= reader.deadline:
                    reader.unavailable.append("template_id_to_name: read budget exceeded")
                    break
                state["template_id_to_name"][str(identifier)] = await reader.get(client.cache_handler, "get_template_name", "template_name", identifier)
            state["spell_template_catalog"] = []
            if spellbook is not None:
                # Current Wizwalker ClientSpellbookBehavior documents +0x70 as
                # its linked list of loaded SpellTemplate shared pointers.
                pointers = await reader.get(spellbook, "read_shared_linked_list", "spellbook.loaded_templates", 112)
                for address in reader.bounded(pointers, "loaded_spell_templates", 256):
                    if not address or time.monotonic() >= reader.deadline:
                        reader.unavailable.append("spell_template_catalog: read budget exceeded or null template")
                        break
                    template = DynamicSpellTemplate(spellbook.hook_handler, address)
                    metadata = await reader.fields(template, TEMPLATE_FIELDS, "catalog.template")
                    metadata["effects"] = await effects(reader, template, "effects", "catalog.effects")
                    rank = await reader.get(template, "spell_rank", "catalog.rank")
                    metadata["pip_cost"] = await reader.fields(rank, COST_FIELDS, "catalog.pip_cost")
                    state["spell_template_catalog"].append(metadata)
            treasure = await reader.get(client.client_object, "try_get_treasure_book_behavior", "treasure_cards")
            state["treasure_card_collection"] = plain(await reader.get(treasure, "treasure_card_contents", "treasure_cards.contents"))
        state["unavailable"] = reader.unavailable
        # Refuse a snapshot assembled across a round transition or loading screen.
        ending = await client.duel.round_num()
        phase = await client.duel.duel_phase()
        if (ending != duel["round_num"] or plain(phase) != duel["duel_phase"]
                or await client.duel.duel_id_full() != duel["duel_id_full"]
                or await client.duel.alt_turn_counter() != duel["alt_turn_counter"] or await client.is_loading()):
            raise RuntimeError("Battle changed while collecting state; refresh before deciding.")
        return snapshot
