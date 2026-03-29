"""
Combat — spell selection and casting logic for Wizard101 battles.

Analyzes cards in hand, selects the highest-damage affordable spell,
and casts it on the first available monster.
"""

from wizwalker.combat.handler import CombatHandler
from wizwalker.memory.memory_objects.enums import DuelPhase, SpellEffects
from wizwalker.memory.memory_objects.spell_effect import (
    DynamicSpellEffect,
    CompoundSpellEffect,
    HangingConversionSpellEffect,
)


# ------------------------------------------------------------------
# Spell effect analysis
# ------------------------------------------------------------------

async def get_damage_from_effect(effect) -> list[dict]:
    """Recursively extract damage values from a spell effect."""
    damages = []

    if isinstance(effect, CompoundSpellEffect):
        for sub_effect in await effect.effects_list():
            damages.extend(await get_damage_from_effect(sub_effect))

    elif isinstance(effect, HangingConversionSpellEffect):
        min_damage = await effect.min_effect_value()
        max_damage = await effect.max_effect_value()
        if min_damage > -1 or max_damage > 0:
            damages.append({"min": min_damage, "max": max_damage})

    elif isinstance(effect, DynamicSpellEffect):
        effect_type = await effect.effect_type()
        damage = await effect.effect_param()
        if damage > -1 and effect_type == SpellEffects.damage:
            damages.append({"min": damage, "max": damage})

    return damages


# ------------------------------------------------------------------
# Card analysis
# ------------------------------------------------------------------

async def get_card_info(card) -> dict:
    """Get a card's name, school, pip cost, and damage range."""
    effects = await card.get_spell_effects()
    all_damages = []
    for effect in effects:
        all_damages.extend(await get_damage_from_effect(effect))

    min_damage = 0
    max_damage = 0
    if all_damages:
        min_damage = min(d["min"] for d in all_damages)
        max_damage = max(d["max"] for d in all_damages)

    graphical_spell = await card.get_graphical_spell()
    pip_cost_obj = await graphical_spell.pip_cost()
    pip_cost = 0
    if pip_cost_obj:
        pip_cost = await pip_cost_obj.spell_rank()

    school_id = await graphical_spell.magic_school_id()

    return {
        "name": await card.name(),
        "school": school_id,
        "pip_cost": pip_cost,
        "min_damage": min_damage,
        "max_damage": max_damage,
    }


async def get_all_card_info(cards) -> list[dict]:
    """Get info for all cards in hand."""
    return [await get_card_info(card) for card in cards]


# ------------------------------------------------------------------
# Card selection
# ------------------------------------------------------------------

def select_best_damage_card(
    card_list: list[dict],
    player_school_id: int,
    normal_pips: int,
    power_pips: int,
) -> str:
    """
    Pick the card with the highest minimum damage that the player can afford.

    Returns the card name, or empty string if no card qualifies.
    """
    best_name = ""
    best_damage = 0

    for card in card_list:
        power_pip_mult = 2 if player_school_id == card["school"] else 1
        available_pips = normal_pips + (power_pip_mult * power_pips)

        if card["min_damage"] > best_damage and card["pip_cost"] <= available_pips:
            best_damage = card["min_damage"]
            best_name = card["name"]

    return best_name


# ------------------------------------------------------------------
# Main combat entry point
# ------------------------------------------------------------------

async def combat_main(client) -> str:
    """
    Run one tick of combat logic.

    Returns:
        "acted"   — a spell was cast or the battle ended
        "waiting" — not in planning phase yet
        "skipped" — no cards or no valid spell
    """
    current_duel = client.duel
    duel_phase = await current_duel.duel_phase()

    if duel_phase == DuelPhase.planning:
        combat_handler = CombatHandler(client)

        # Get cards in hand
        cards = await combat_handler.get_cards()
        if not cards:
            return "skipped"

        # Get player info
        player = await combat_handler.get_client_member()
        player_stats = await player.get_stats()
        school_id = await player_stats.school_id()
        normal_pips = await player.normal_pips()
        power_pips = await player.power_pips()

        # Get monsters
        monsters = await combat_handler.get_all_monster_members()

        # Analyze cards and pick the best
        card_list = await get_all_card_info(cards)
        best_name = select_best_damage_card(card_list, school_id, normal_pips, power_pips)

        if not best_name:
            return "skipped"

        # Cast the spell
        try:
            spell_card = await combat_handler.get_card_named(best_name)
        except ValueError:
            return "skipped"

        if monsters:
            target = monsters[0]
            await spell_card.cast(target)
            return "acted"

        return "skipped"

    elif duel_phase == DuelPhase.ended:
        return "acted"

    return "waiting"
