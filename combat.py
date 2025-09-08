from wizwalker.memory.memory_objects.enums import DuelPhase
from wizwalker.combat.handler import CombatHandler
from wizwalker.memory.memory_objects.spell_effect import (
    DynamicSpellEffect,
    CompoundSpellEffect,
    HangingConversionSpellEffect,
)
from wizwalker.memory.memory_objects.enums import SpellEffects

async def get_damage_from_effect(effect):
    """Analyze spell effects to extract damage values. (Your existing function)"""
    damages = []
    
    # If it is a compound effect, recursively check the sub-effects
    if isinstance(effect, CompoundSpellEffect):
        print("found compund")
        for sub_effect in await effect.effects_list():
            damages.extend(await get_damage_from_effect(sub_effect))
            
    # If it is a hanging conversion, get the min/max values
    elif isinstance(effect, HangingConversionSpellEffect):
        print("found hanging")
        min_damage = await effect.min_effect_value()
        max_damage = await effect.max_effect_value()
        if min_damage > -1 or max_damage > 0:
            damages.append({"min": min_damage, "max": max_damage})
            
    # Fallback for simple damage effects
    elif isinstance(effect, DynamicSpellEffect):
        print("found dynamic")
        effect_type = await effect.effect_type()
        damage = await effect.effect_param()
        # Also check damage_type to make sure it's a damage spell
        if damage > -1 and effect_type == SpellEffects.damage:
            damages.append({"min": damage, "max": damage})
            
    return damages

async def get_player_info(combat_handler):
    """Get player stats, school, and pip information."""
    player = await combat_handler.get_client_member()
    player_stats = await player.get_stats()
    
    return {
        "player": player,
        "school_id": await player_stats.school_id(),
        "normal_pips": await player.normal_pips(),
        "power_pips": await player.power_pips()
    }

async def get_card_info(card):
    """Get card information including damage, cost, and school."""
    effects = await card.get_spell_effects()
    all_damages = []
    for effect in effects:
        all_damages.extend(await get_damage_from_effect(effect))
    
    min_damage = 0
    max_damage = 0
    if all_damages:
        min_damage = min(d['min'] for d in all_damages)
        max_damage = max(d['max'] for d in all_damages)

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
        "max_damage": max_damage
    }

async def get_all_card_info(cards):
    """Get information for all cards in hand."""
    card_list = []
    for card in cards:
        card_info = await get_card_info(card)
        card_list.append(card_info)
    return card_list

def select_best_damage_card(card_list, player_school_id, normal_pips, power_pips):
    """Select the best damage card based on available pips and damage output."""
    max_min_damage = 0
    best_card_name = ""

    for card in card_list:
        power_pip_mult = 2 if player_school_id == card["school"] else 1
        available_pips = normal_pips + (power_pip_mult * power_pips)
        
        # Check if we can afford the card and if it does more damage
        if card['min_damage'] > max_min_damage and card["pip_cost"] <= available_pips:
            max_min_damage = card["min_damage"]
            best_card_name = card["name"]

    return best_card_name

async def cast_spell_on_target(combat_handler, spell_name, monsters):
    """Cast the selected spell on the first available target."""
    if not spell_name:
        print("No valid spell selected. Skipping turn.")
        return

    try:
        spell_card = await combat_handler.get_card_named(spell_name)
    except ValueError:
        print(f"No cards named {spell_name} in hand. Skipping turn.")
        return

    if monsters:
        target_monster = monsters[0]
        print(f"Casting {spell_name} on {target_monster.name}")
        await spell_card.cast(target_monster)
    else:
        print("No monsters to target.")

async def combat_main(client):
    """Main combat logic - orchestrates spell selection and casting."""
    currentDuel = client.duel
    duelPhase = await currentDuel.duel_phase()
    
    if duelPhase == DuelPhase.planning:
        print("Planning")
        combat_handler = CombatHandler(client)
        
        # Get cards in hand
        cards_in_hand = await combat_handler.get_cards()
        if not cards_in_hand:
            print("No cards in hand. Skipping turn.")
            return

        # Get player info
        player_info = await get_player_info(combat_handler)
        
        # Get monsters
        monsters = await combat_handler.get_all_monster_members()
        
        # Get all card information
        card_list = await get_all_card_info(cards_in_hand)
        print(card_list)

        # Select best card
        best_card_name = select_best_damage_card(
            card_list, 
            player_info["school_id"],
            player_info["normal_pips"], 
            player_info["power_pips"]
        )

        print(f"Attempting to cast: {best_card_name}")
        
        # Cast the spell
        await cast_spell_on_target(combat_handler, best_card_name, monsters)

    elif duelPhase == DuelPhase.ended:
        print("Ended")