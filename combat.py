"""
Combat  --  decision-tree state machine for Wizard101 battles.

Classifies cards in hand, computes decision variables, walks the
flowchart branches, and executes the chosen action.

Decision tree mirrors the user's flowchart exactly:
  Start -> enchant check -> hit check ->
  no-hit / one-enemy / multi-enemy branches.

Enchanting does NOT consume a turn  --  it modifies a card in hand,
then the flow continues to the hit/buff/pass decision.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

log = logging.getLogger("combat")

from wizwalker.combat.handler import CombatHandler
from wizwalker.combat.card import CombatCard
from wizwalker.combat.member import CombatMember
from wizwalker.memory.memory_objects.enums import (
    DuelPhase,
    SpellEffects,
    EffectTarget,
)
from wizwalker.memory.memory_objects.spell_effect import (
    DynamicSpellEffect,
    CompoundSpellEffect,
    HangingConversionSpellEffect,
)


# ------------------------------------------------------------------
# Enums
# ------------------------------------------------------------------

class CardCategory(Enum):
    ENCHANT = "enchant"
    HIT = "hit"          # single-target damage
    AOE = "aoe"          # AOE damage
    BUFF_ZERO = "buff_zero"   # zero-pip buff (blade/trap/charm)
    BUFF_PIP = "buff_pip"     # pip-cost buff
    OTHER = "other"      # heals, shields, utility, etc.


class Action(Enum):
    HIT = "hit"
    HIT_AOE = "hit_aoe"
    CAST_ZERO_PIP_BUFF = "cast_zero_pip_buff"
    CAST_PIP_BUFF = "cast_pip_buff"
    DISCARD_AND_PASS = "discard_and_pass"
    PASS = "pass"
    FLEE = "flee"


# ------------------------------------------------------------------
# Card info
# ------------------------------------------------------------------

@dataclass
class CardInfo:
    """Classified card with metadata."""
    card: CombatCard
    category: CardCategory
    name: str
    pip_cost: int
    min_damage: int
    max_damage: int
    is_castable: bool
    is_enchanted: bool
    school_id: int = 0
    school_name: str = ""  # e.g. "Fire", "Ice", etc.
    buff_value: int = 0  # effect strength for buffs/enchants


@dataclass
class EnemyInfo:
    """Enemy metadata."""
    member: CombatMember
    name: str
    health: int
    max_health: int
    is_boss: bool
    traps: list = field(default_factory=list)  # [(school_name, pct), ...]


# ------------------------------------------------------------------
# Combat state  --  all decision variables
# ------------------------------------------------------------------

@dataclass
class CombatState:
    """All information the decision tree needs."""
    # Cards by category
    enchants: list = field(default_factory=list)
    hits: list = field(default_factory=list)
    aoes: list = field(default_factory=list)
    zero_pip_buffs: list = field(default_factory=list)
    pip_buffs: list = field(default_factory=list)
    other_cards: list = field(default_factory=list)
    all_cards: list = field(default_factory=list)

    # Enemies
    enemies: list = field(default_factory=list)

    # Player
    available_pips: int = 0
    player_school_id: int = 0
    player_school_name: str = ""
    gear_dmg_bonus: float = 0.0      # gear damage boost (0-100+ as percent)
    player_blades: list = field(default_factory=list)  # [(school_name, pct), ...]

    # The specific cards chosen for actions (set during decision)
    chosen_hit: Optional[CardInfo] = None
    chosen_aoe: Optional[CardInfo] = None
    chosen_buff: Optional[CardInfo] = None
    chosen_enchant: Optional[CardInfo] = None
    enchant_target: Optional[CardInfo] = None
    hit_target: Optional[EnemyInfo] = None

    # --- Effective damage calculation ---

    def effective_damage(self, card: 'CardInfo', enemy: 'EnemyInfo' = None) -> float:
        """Calculate effective damage including gear boost, blades, and traps.

        Wizard101 damage formula (simplified):
          effective = base_dmg * (1 + gear_boost/100)
                   * product(1 + blade_pct/100 for each matching blade)
                   * product(1 + trap_pct/100  for each matching trap on target)
        """
        base = card.min_damage
        if base <= 0:
            return 0.0

        card_school = card.school_name.lower()

        # Gear damage boost
        dmg = base * (1.0 + self.gear_dmg_bonus / 100.0)

        # Apply blades (multiply each separately)
        for blade_school, blade_pct in self.player_blades:
            bs = blade_school.lower()
            if bs in ("", "any", "universal", "all") or bs == card_school:
                dmg *= (1.0 + blade_pct / 100.0)

        # Apply traps on target
        if enemy:
            for trap_school, trap_pct in enemy.traps:
                ts = trap_school.lower()
                if ts in ("", "any", "universal", "all") or ts == card_school:
                    dmg *= (1.0 + trap_pct / 100.0)

        return dmg

    # --- Derived decision booleans ---

    @property
    def enemy_count(self) -> int:
        return len(self.enemies)

    @property
    def has_enchant(self) -> bool:
        return len(self.enchants) > 0

    @property
    def has_card_for_enchant(self) -> bool:
        """Is there a non-enchanted hit or AOE to apply an enchant to?"""
        for c in self.hits + self.aoes:
            if not c.is_enchanted:
                return True
        return False

    @property
    def has_any_hit_card(self) -> bool:
        """Have any hit or AOE card in hand (castable or not)."""
        return len(self.hits) > 0 or len(self.aoes) > 0

    @property
    def has_hit(self) -> bool:
        return any(c.is_castable for c in self.hits)

    @property
    def has_killing_hit(self) -> bool:
        """A castable single-target hit that can kill any enemy (using effective dmg)."""
        if not self.enemies:
            return False
        weakest = min(self.enemies, key=lambda e: e.health)
        return any(
            c.is_castable and self.effective_damage(c, weakest) >= weakest.health
            for c in self.hits
        )

    @property
    def has_hit_needing_pips(self) -> bool:
        """A hit that could kill but isn't castable (needs more pips)."""
        if not self.enemies:
            return False
        weakest = min(self.enemies, key=lambda e: e.health)
        return any(
            not c.is_castable and self.effective_damage(c, weakest) >= weakest.health
            for c in self.hits
        )

    @property
    def has_zero_pip_buff(self) -> bool:
        return any(c.is_castable for c in self.zero_pip_buffs)

    @property
    def has_pip_buff(self) -> bool:
        return any(c.is_castable for c in self.pip_buffs)

    @property
    def has_multiple_hits_combined_kill(self) -> bool:
        """Total effective damage of all castable hits >= weakest enemy health."""
        if not self.enemies:
            return False
        weakest = min(self.enemies, key=lambda e: e.health)
        total_dmg = sum(self.effective_damage(c, weakest) for c in self.hits if c.is_castable)
        return total_dmg >= weakest.health and sum(1 for c in self.hits if c.is_castable) >= 2

    @property
    def has_cards_other_than_hits_and_buffs(self) -> bool:
        return len(self.other_cards) > 0

    @property
    def has_seven_hits(self) -> bool:
        return len(self.all_cards) == 7 and all(
            c.category in (CardCategory.HIT, CardCategory.AOE) for c in self.all_cards
        )

    @property
    def has_any_cards(self) -> bool:
        return len(self.all_cards) > 0

    @property
    def has_aoe(self) -> bool:
        return any(c.is_castable for c in self.aoes)

    @property
    def has_aoe_that_kills_all(self) -> bool:
        """A castable AOE whose effective damage >= every enemy's health."""
        if not self.enemies:
            return False
        strongest = max(self.enemies, key=lambda e: e.health)
        return any(
            c.is_castable and self.effective_damage(c, strongest) >= strongest.health
            for c in self.aoes
        )

    @property
    def has_aoe_kills_all_with_more_pips(self) -> bool:
        """An AOE that could kill all but isn't castable (needs pips)."""
        if not self.enemies:
            return False
        strongest = max(self.enemies, key=lambda e: e.health)
        return any(
            not c.is_castable and self.effective_damage(c, strongest) >= strongest.health
            for c in self.aoes
        )

    @property
    def has_multiple_hits_kill_all(self) -> bool:
        """Total effective damage of all castable hits+aoes >= total enemy health."""
        if not self.enemies:
            return False
        total_enemy_hp = sum(e.health for e in self.enemies)
        # Use weakest enemy for modifier estimation (conservative)
        weakest = min(self.enemies, key=lambda e: e.health)
        total_dmg = sum(
            self.effective_damage(c, weakest) for c in self.hits + self.aoes if c.is_castable
        )
        return total_dmg >= total_enemy_hp and sum(1 for c in self.hits + self.aoes if c.is_castable) >= 2

    @property
    def single_hit_can_kill_strongest(self) -> bool:
        """A castable single-target hit can kill the strongest enemy."""
        if not self.enemies:
            return False
        strongest = max(self.enemies, key=lambda e: e.health)
        return any(
            c.is_castable and self.effective_damage(c, strongest) >= strongest.health
            for c in self.hits
        )


# ------------------------------------------------------------------
# Card classification helpers
# ------------------------------------------------------------------

# Buff-related spell effects (blades, traps, charms, auras)
_BUFF_EFFECTS = {
    SpellEffects.modify_outgoing_damage,
    SpellEffects.modify_outgoing_damage_flat,
    SpellEffects.modify_outgoing_armor_piercing,
    SpellEffects.modify_incoming_damage,       # traps (on enemy)
    SpellEffects.modify_incoming_damage_flat,
    SpellEffects.push_charm,
    SpellEffects.cloaked_charm,
    SpellEffects.modify_accuracy,
    SpellEffects.crit_boost,
    SpellEffects.modify_power_pip_chance,
}

# Damage-related spell effects
_DAMAGE_EFFECTS = {
    SpellEffects.damage,
    SpellEffects.damage_no_crit,
    SpellEffects.steal_health,
    SpellEffects.damage_over_time,
    SpellEffects.damage_per_total_pip_power,
    SpellEffects.max_health_damage,
}


async def _extract_damages(effect) -> list[dict]:
    """Recursively extract min/max damage values from a spell effect."""
    damages = []
    try:
        if isinstance(effect, CompoundSpellEffect):
            for sub_effect in await effect.effects_list():
                damages.extend(await _extract_damages(sub_effect))
        elif isinstance(effect, HangingConversionSpellEffect):
            min_damage = await effect.min_effect_value()
            max_damage = await effect.max_effect_value()
            if min_damage > -1 or max_damage > 0:
                damages.append({"min": min_damage, "max": max_damage})
        elif isinstance(effect, DynamicSpellEffect):
            effect_type = await effect.effect_type()
            if effect_type in _DAMAGE_EFFECTS:
                damage = await effect.effect_param()
                if damage > -1:
                    damages.append({"min": damage, "max": damage})
    except Exception as e:
        log.debug(f"Error extracting damage: {e}")
    return damages


async def _get_card_damage(card: CombatCard) -> tuple[int, int]:
    """Extract min/max damage from a card's spell effects."""
    try:
        effects = await card.get_spell_effects()
    except Exception:
        return 0, 0
    all_damages = []
    for effect in effects:
        all_damages.extend(await _extract_damages(effect))
    if not all_damages:
        return 0, 0
    return min(d["min"] for d in all_damages), max(d["max"] for d in all_damages)


async def _is_aoe_card(card: CombatCard) -> bool:
    """Check if a card targets all enemies (AOE)."""
    try:
        type_name = await card.type_name()
        if type_name not in ("AOE", "Steal"):
            return False
        effects = await card.get_spell_effects()
        for effect in effects:
            try:
                effect_type_name = await effect.maybe_read_type_name()
                if any(s in effect_type_name.lower() for s in ("variable", "random")):
                    for sub in await effect.maybe_effect_list():
                        target = await sub.effect_target()
                        if target in (EffectTarget.enemy_team, EffectTarget.enemy_team_all_at_once):
                            return True
                else:
                    target = await effect.effect_target()
                    if target in (EffectTarget.enemy_team, EffectTarget.enemy_team_all_at_once):
                        return True
            except Exception:
                pass
        return False
    except Exception:
        return False


async def _is_buff_card(card: CombatCard) -> bool:
    """Check if a card is a buff (blade, trap, charm, aura)."""
    try:
        type_name = await card.type_name()
        if type_name in ("Charm", "Ward", "Aura", "Global"):
            return True
        effects = await card.get_spell_effects()
        for effect in effects:
            try:
                etype = await effect.effect_type()
                if etype in _BUFF_EFFECTS:
                    return True
            except Exception:
                pass
        return False
    except Exception:
        return False


async def _is_enchant_card(card: CombatCard) -> bool:
    """Check if a card is a damage enchantment (Sun school)."""
    try:
        type_name = await card.type_name()
        if type_name != "Enchantment":
            return False
        effects = await card.get_spell_effects()
        for effect in effects:
            try:
                if await effect.effect_type() == SpellEffects.modify_card_damage:
                    return True
            except Exception:
                pass
        return False
    except Exception:
        return False


async def _is_damage_card(card: CombatCard) -> bool:
    """Check if a card deals damage (hit or steal)."""
    try:
        type_name = await card.type_name()
        if type_name in ("Damage", "Steal", "AOE"):
            return True
        effects = await card.get_spell_effects()
        for effect in effects:
            try:
                if await effect.effect_type() in _DAMAGE_EFFECTS:
                    return True
            except Exception:
                pass
        return False
    except Exception:
        return False


async def _get_buff_value(card: CombatCard) -> int:
    """Extract the buff/enchant effect parameter (percentage or flat value)."""
    try:
        effects = await card.get_spell_effects()
    except Exception:
        return 0
    max_val = 0
    for effect in effects:
        try:
            etype = await effect.effect_type()
            if etype in _BUFF_EFFECTS or etype == SpellEffects.modify_card_damage:
                param = await effect.effect_param()
                if abs(param) > abs(max_val):
                    max_val = abs(param)
        except Exception:
            pass
    return max_val


# School ID to name lookup (for matching blade/trap schools to card schools)
_SCHOOL_ID_TO_NAME = {
    2343174: "fire",
    83375795: "ice",
    83375893: "storm",
    2330892: "life",
    78483: "myth",        # may vary by version
    78318724: "death",
    1027491821: "balance",
}


async def classify_card(card: CombatCard) -> CardInfo:
    """Classify a single card and extract its metadata."""
    name = ""
    pip_cost = 0
    school_id = 0
    is_enchanted = False
    is_castable = False
    raw_type_name = "???"

    try:
        name = await card.name()
    except Exception:
        pass
    try:
        is_castable = await card.is_castable()
    except Exception:
        pass
    try:
        is_enchanted = await card.is_enchanted()
    except Exception:
        pass
    try:
        raw_type_name = await card.type_name()
    except Exception:
        pass
    try:
        graphical_spell = await card.get_graphical_spell()
        pip_cost_obj = await graphical_spell.pip_cost()
        if pip_cost_obj:
            pip_cost = await pip_cost_obj.spell_rank()
        school_id = await graphical_spell.magic_school_id()
    except Exception:
        pass

    school_name = _SCHOOL_ID_TO_NAME.get(school_id, "")
    # If lookup missed, try to derive from card effects
    if not school_name and school_id != 0:
        try:
            effects = await card.get_spell_effects()
            for eff in effects:
                try:
                    sdt = await eff.string_damage_type()
                    if sdt:
                        school_name = sdt.lower()
                        break
                except Exception:
                    pass
        except Exception:
            pass

    min_dmg, max_dmg = await _get_card_damage(card)
    buff_value = 0

    is_enchant = await _is_enchant_card(card)
    is_aoe = await _is_aoe_card(card)
    is_dmg = await _is_damage_card(card)
    is_buff = await _is_buff_card(card)

    if is_enchant:
        category = CardCategory.ENCHANT
        buff_value = await _get_buff_value(card)
        if buff_value > 0 and min_dmg == 0:
            min_dmg = buff_value
            max_dmg = buff_value
    elif is_aoe:
        category = CardCategory.AOE
    elif is_dmg and not is_aoe:
        category = CardCategory.HIT
    elif is_buff:
        buff_value = await _get_buff_value(card)
        if pip_cost == 0:
            category = CardCategory.BUFF_ZERO
        else:
            category = CardCategory.BUFF_PIP
    else:
        category = CardCategory.OTHER

    log.info(
        f"  Card: {name!r}  type={raw_type_name!r}  cat={category.value}  "
        f"cast={is_castable}  ench={is_enchanted}  pips={pip_cost}  "
        f"dmg={min_dmg}-{max_dmg}  buff={buff_value}  school={school_id}"
    )

    return CardInfo(
        card=card, category=category, name=name, pip_cost=pip_cost,
        min_damage=min_dmg, max_damage=max_dmg, is_castable=is_castable,
        is_enchanted=is_enchanted, school_id=school_id,
        school_name=school_name, buff_value=buff_value,
    )


# ------------------------------------------------------------------
# Build combat state
# ------------------------------------------------------------------

async def build_combat_state(combat_handler: CombatHandler) -> CombatState:
    """Gather all info needed for the decision tree."""
    state = CombatState()

    cards = await combat_handler.get_cards()
    log.info(f"=== COMBAT ROUND  --  {len(cards)} cards in hand ===")
    for card in cards:
        info = await classify_card(card)
        state.all_cards.append(info)
        if info.category == CardCategory.ENCHANT:
            state.enchants.append(info)
        elif info.category == CardCategory.HIT:
            state.hits.append(info)
        elif info.category == CardCategory.AOE:
            state.aoes.append(info)
        elif info.category == CardCategory.BUFF_ZERO:
            state.zero_pip_buffs.append(info)
        elif info.category == CardCategory.BUFF_PIP:
            state.pip_buffs.append(info)
        else:
            state.other_cards.append(info)

    state.hits.sort(key=lambda c: c.min_damage, reverse=True)
    state.aoes.sort(key=lambda c: c.min_damage, reverse=True)

    try:
        player = await combat_handler.get_client_member()
        player_stats = await player.get_stats()
        state.player_school_id = await player_stats.school_id()
        state.player_school_name = _SCHOOL_ID_TO_NAME.get(state.player_school_id, "")
        normal_pips = await player.normal_pips()
        power_pips = await player.power_pips()
        state.available_pips = normal_pips + (power_pips * 2)

        # Gear damage boost (dmg_bonus_percent_all is universal gear boost)
        try:
            state.gear_dmg_bonus = await player_stats.dmg_bonus_percent_all()
            log.info(f"Player: school={state.player_school_name} pips={state.available_pips} gear_dmg={state.gear_dmg_bonus:.1f}%")
        except Exception:
            log.info(f"Player: school={state.player_school_name} pips={state.available_pips}")

        # Read active blades (hanging charms on player)
        try:
            participant = await player.get_participant()
            hanging = await participant.hanging_effects()
            for effect in hanging:
                try:
                    etype = await effect.effect_type()
                    if etype in (SpellEffects.modify_outgoing_damage,
                                 SpellEffects.modify_outgoing_damage_flat):
                        param = await effect.effect_param()
                        if param > 0:  # positive = blade (boosts outgoing)
                            school = ""
                            try:
                                school = await effect.string_damage_type()
                            except Exception:
                                pass
                            state.player_blades.append((school, param))
                            log.info(f"  Blade: {school or 'universal'} +{param}%")
                except Exception:
                    pass
        except Exception as e:
            log.debug(f"Failed to read hanging effects: {e}")

    except Exception as e:
        log.warning(f"Failed to get player info: {e}")

    monsters = await combat_handler.get_all_monster_members()
    for monster in monsters:
        try:
            enemy = EnemyInfo(
                member=monster, name=await monster.name(),
                health=await monster.health(), max_health=await monster.max_health(),
                is_boss=await monster.is_boss(),
            )
            if enemy.health > 0:
                # Read traps on this enemy
                try:
                    participant = await monster.get_participant()
                    hanging = await participant.hanging_effects()
                    for effect in hanging:
                        try:
                            etype = await effect.effect_type()
                            if etype in (SpellEffects.modify_incoming_damage,
                                         SpellEffects.modify_incoming_damage_flat):
                                param = await effect.effect_param()
                                if param > 0:  # positive = increases damage taken
                                    school = ""
                                    try:
                                        school = await effect.string_damage_type()
                                    except Exception:
                                        pass
                                    enemy.traps.append((school, param))
                        except Exception:
                            pass
                except Exception:
                    pass
                state.enemies.append(enemy)
                trap_str = f" traps={enemy.traps}" if enemy.traps else ""
                log.info(f"  Enemy: {enemy.name!r} hp={enemy.health}/{enemy.max_health} boss={enemy.is_boss}{trap_str}")
        except Exception:
            pass

    state.enemies.sort(key=lambda e: e.health, reverse=True)

    log.info(
        f"Summary: {len(state.hits)} hits, {len(state.aoes)} aoes, "
        f"{len(state.enchants)} enchants, {len(state.zero_pip_buffs)} zero-buffs, "
        f"{len(state.pip_buffs)} pip-buffs, {len(state.other_cards)} other | "
        f"{len(state.enemies)} enemies"
    )
    return state


# ------------------------------------------------------------------
# Picker helpers (school-aware, value-ranked)
# ------------------------------------------------------------------

def _get_planned_hit(state: CombatState) -> Optional[CardInfo]:
    """Determine what hit we're planning for (for buff school matching)."""
    for c in state.hits:
        if c.is_castable:
            return c
    for c in state.aoes:
        if c.is_castable:
            return c
    # Non-castable hits we might be saving pips for
    if state.hits:
        return state.hits[0]
    if state.aoes:
        return state.aoes[0]
    return None


def _is_buff_applicable(buff: CardInfo, hit: Optional[CardInfo]) -> bool:
    """Check if a buff's school matches the planned hit's school."""
    if hit is None:
        return True
    if buff.school_id == 0:
        return True
    return buff.school_id == hit.school_id


def _is_blade_already_active(buff: CardInfo, state: CombatState) -> bool:
    """Check if a blade with the same name/school is already on the player.

    In Wizard101, two copies of the same blade don't stack -- the second
    overwrites the first, wasting a turn. We compare the buff's school and
    value against active blades to detect duplicates.
    """
    buff_school = buff.school_name.lower() if buff.school_name else ""
    for blade_school, blade_pct in state.player_blades:
        bs = blade_school.lower() if blade_school else ""
        # Same school and same value = same blade, already active
        if bs == buff_school and abs(blade_pct - buff.buff_value) < 1:
            return True
    return False


def _pick_applicable_zero_pip_buff(state: CombatState, hit: Optional[CardInfo] = None) -> Optional[CardInfo]:
    """Pick the best castable zero-pip buff matching the hit's school.
    Skips blades that are already active (duplicates don't stack)."""
    if hit is None:
        hit = _get_planned_hit(state)
    candidates = [
        c for c in state.zero_pip_buffs
        if c.is_castable and _is_buff_applicable(c, hit)
        and not _is_blade_already_active(c, state)
    ]
    if candidates:
        candidates.sort(key=lambda c: c.buff_value, reverse=True)
        return candidates[0]
    return None


def _pick_best_zero_pip_buff(state: CombatState) -> Optional[CardInfo]:
    """Pick the best castable zero-pip buff (any school), preferring applicable.
    Skips blades that are already active."""
    hit = _get_planned_hit(state)
    # Try applicable first
    applicable = _pick_applicable_zero_pip_buff(state, hit)
    if applicable:
        return applicable
    # Fall back to any (still skip duplicates)
    candidates = [
        c for c in state.zero_pip_buffs
        if c.is_castable and not _is_blade_already_active(c, state)
    ]
    if candidates:
        candidates.sort(key=lambda c: c.buff_value, reverse=True)
        return candidates[0]
    return None


def _pick_applicable_pip_buff(state: CombatState, hit: Optional[CardInfo] = None) -> Optional[CardInfo]:
    """Pick the best castable pip-cost buff matching the hit's school.
    Skips blades that are already active."""
    if hit is None:
        hit = _get_planned_hit(state)
    candidates = [
        c for c in state.pip_buffs
        if c.is_castable and _is_buff_applicable(c, hit)
        and not _is_blade_already_active(c, state)
    ]
    if candidates:
        candidates.sort(key=lambda c: c.buff_value, reverse=True)
        return candidates[0]
    return None


def _pick_best_pip_buff(state: CombatState) -> Optional[CardInfo]:
    """Pick the best castable pip-cost buff (any school), preferring applicable.
    Skips blades that are already active."""
    hit = _get_planned_hit(state)
    applicable = _pick_applicable_pip_buff(state, hit)
    if applicable:
        return applicable
    candidates = [
        c for c in state.pip_buffs
        if c.is_castable and not _is_blade_already_active(c, state)
    ]
    if candidates:
        candidates.sort(key=lambda c: c.buff_value, reverse=True)
        return candidates[0]
    return None


def _pick_killing_hit(state: CombatState) -> Optional[CardInfo]:
    """Find the most pip-efficient castable hit that can kill the weakest enemy."""
    if not state.enemies:
        return None
    weakest = min(state.enemies, key=lambda e: e.health)
    candidates = [c for c in state.hits
                  if c.is_castable and state.effective_damage(c, weakest) >= weakest.health]
    if candidates:
        # Sort by effective damage ascending (lowest overkill)
        candidates.sort(key=lambda c: state.effective_damage(c, weakest))
        return candidates[0]
    return None


def _pick_best_hit(state: CombatState) -> Optional[CardInfo]:
    """Find the highest effective-damage castable hit."""
    candidates = [c for c in state.hits if c.is_castable]
    if not candidates:
        return None
    # Sort by effective damage (accounts for blades/gear/traps)
    weakest = min(state.enemies, key=lambda e: e.health) if state.enemies else None
    candidates.sort(key=lambda c: state.effective_damage(c, weakest), reverse=True)
    return candidates[0]


def _pick_killing_aoe(state: CombatState) -> Optional[CardInfo]:
    """Find a castable AOE that kills all enemies (using effective damage)."""
    if not state.enemies:
        return None
    strongest = max(state.enemies, key=lambda e: e.health)
    for c in state.aoes:
        if c.is_castable and state.effective_damage(c, strongest) >= strongest.health:
            return c
    return None


def _pick_best_aoe(state: CombatState) -> Optional[CardInfo]:
    """Find the highest-damage castable AOE."""
    candidates = [c for c in state.aoes if c.is_castable]
    return candidates[0] if candidates else None


def _pick_strongest_killable_target(state: CombatState) -> Optional[tuple]:
    """Find the strongest enemy a single hit can kill, plus the hit card."""
    for enemy in sorted(state.enemies, key=lambda e: e.health, reverse=True):
        for hit in state.hits:
            if hit.is_castable and state.effective_damage(hit, enemy) >= enemy.health:
                return hit, enemy
    return None


def _pick_enchant_and_target(state: CombatState) -> Optional[tuple]:
    """Pick the best enchant and the best non-enchanted hit/aoe to apply it to."""
    if not state.enchants:
        return None
    targets = [c for c in state.hits + state.aoes if not c.is_enchanted]
    targets.sort(key=lambda c: c.min_damage, reverse=True)
    if not targets:
        return None
    enchants = sorted(state.enchants, key=lambda c: c.buff_value, reverse=True)
    return enchants[0], targets[0]


def _pick_card_to_discard(state: CombatState) -> Optional[CardInfo]:
    """Pick the least useful card to discard."""
    # Enchants with no target
    if state.enchants and not state.has_card_for_enchant:
        return state.enchants[0]
    if state.other_cards:
        return state.other_cards[0]
    # Lowest damage hit
    castable_hits = [c for c in state.hits if c.is_castable]
    if castable_hits:
        return min(castable_hits, key=lambda c: c.min_damage)
    if state.all_cards:
        return state.all_cards[-1]
    return None


# ------------------------------------------------------------------
# Decision tree branches (mirrors flowchart exactly)
# ------------------------------------------------------------------

def _branch_no_hit(state: CombatState) -> Action:
    """No castable hit path."""
    log.info("Branch: NO HIT")
    planned = _get_planned_hit(state)

    # Zero-pip buff for hit? (applicable to planned hit's school)
    buff = _pick_applicable_zero_pip_buff(state, planned)
    if buff:
        state.chosen_buff = buff
        log.info(f"  -> ZERO_BUFF (applicable): {buff.name}")
        return Action.CAST_ZERO_PIP_BUFF

    # Any zero-pip buff?
    buff = _pick_best_zero_pip_buff(state)
    if buff:
        state.chosen_buff = buff
        log.info(f"  -> ZERO_BUFF (any): {buff.name}")
        return Action.CAST_ZERO_PIP_BUFF

    # Any non-hit/buff cards?
    if state.has_cards_other_than_hits_and_buffs:
        log.info("  -> DISCARD_AND_PASS (other cards)")
        return Action.DISCARD_AND_PASS

    # 7 hits?
    if state.has_seven_hits:
        log.info("  -> DISCARD_AND_PASS (7 hits)")
        return Action.DISCARD_AND_PASS

    log.info("  -> PASS")
    return Action.PASS


def _branch_buff_check(state: CombatState) -> Action:
    """Shared BuffCheck node  --  zero buff -> pip buff -> multi kill -> discard -> flee."""
    log.info("Branch: BUFF CHECK")

    buff = _pick_best_zero_pip_buff(state)
    if buff:
        state.chosen_buff = buff
        log.info(f"  -> ZERO_BUFF: {buff.name}")
        return Action.CAST_ZERO_PIP_BUFF

    buff = _pick_best_pip_buff(state)
    if buff:
        state.chosen_buff = buff
        log.info(f"  -> PIP_BUFF: {buff.name}")
        return Action.CAST_PIP_BUFF

    # Multiple hits combined can kill?
    if state.has_multiple_hits_combined_kill:
        state.chosen_hit = _pick_best_hit(state)
        if state.enemies:
            state.hit_target = state.enemies[-1]  # weakest
        log.info(f"  -> HIT (combined kill): {state.chosen_hit.name if state.chosen_hit else '?'}")
        return Action.HIT

    # Any non-hit cards?
    if state.has_cards_other_than_hits_and_buffs:
        log.info("  -> DISCARD_AND_PASS (other cards)")
        return Action.DISCARD_AND_PASS

    # 7 hits?
    if state.has_seven_hits:
        log.info("  -> DISCARD_AND_PASS (7 hits)")
        return Action.DISCARD_AND_PASS

    log.info("  -> FLEE")
    return Action.FLEE


def _branch_single_enemy(state: CombatState) -> Action:
    """One enemy decision branch."""
    log.info("Branch: ONE ENEMY")

    # Can a hit kill now?
    if state.has_killing_hit:
        state.chosen_hit = _pick_killing_hit(state)
        if state.enemies:
            state.hit_target = state.enemies[-1]  # weakest (only one)
        log.info(f"  -> HIT (kill): {state.chosen_hit.name if state.chosen_hit else '?'}")
        return Action.HIT

    # Hit can kill with more pips?
    if state.has_hit_needing_pips:
        log.info("  Hit needs more pips  --  check for zero-pip buffs first")
        planned = _get_planned_hit(state)

        # Applicable zero-pip buff?
        buff = _pick_applicable_zero_pip_buff(state, planned)
        if buff:
            state.chosen_buff = buff
            log.info(f"  -> ZERO_BUFF (applicable, saving pips): {buff.name}")
            return Action.CAST_ZERO_PIP_BUFF

        # Any zero-pip buff?
        buff = _pick_best_zero_pip_buff(state)
        if buff:
            state.chosen_buff = buff
            log.info(f"  -> ZERO_BUFF (any, saving pips): {buff.name}")
            return Action.CAST_ZERO_PIP_BUFF

        # Fall through to BuffCheck
        return _branch_buff_check(state)

    # No hit can kill even with more pips -> BuffCheck
    return _branch_buff_check(state)


def _branch_multiple_enemies(state: CombatState) -> Action:
    """Multiple enemies decision branch."""
    log.info("Branch: MULTIPLE ENEMIES")

    # AOE that kills all now?
    if state.has_aoe_that_kills_all:
        state.chosen_aoe = _pick_killing_aoe(state)
        log.info(f"  -> AOE (kills all): {state.chosen_aoe.name if state.chosen_aoe else '?'}")
        return Action.HIT_AOE

    # AOE that kills all with more pips?
    if state.has_aoe_kills_all_with_more_pips:
        log.info("  AOE can kill all with more pips  --  buffing up")
        planned = _get_planned_hit(state)  # will find the AOE

        # Applicable zero-pip buff?
        buff = _pick_applicable_zero_pip_buff(state, planned)
        if buff:
            state.chosen_buff = buff
            log.info(f"  -> ZERO_BUFF (applicable for AOE): {buff.name}")
            return Action.CAST_ZERO_PIP_BUFF

        # Any zero-pip buff?
        buff = _pick_best_zero_pip_buff(state)
        if buff:
            state.chosen_buff = buff
            log.info(f"  -> ZERO_BUFF (any, AOE pips): {buff.name}")
            return Action.CAST_ZERO_PIP_BUFF

        # Any pip buff?
        buff = _pick_best_pip_buff(state)
        if buff:
            state.chosen_buff = buff
            log.info(f"  -> PIP_BUFF (AOE pips): {buff.name}")
            return Action.CAST_PIP_BUFF

        # Multiple hits that kill all combined?
        if state.has_multiple_hits_kill_all:
            state.chosen_hit = _pick_best_hit(state)
            if state.enemies:
                state.hit_target = state.enemies[0]  # strongest first
            log.info(f"  -> HIT (multi kill all): {state.chosen_hit.name if state.chosen_hit else '?'}")
            return Action.HIT

        # Any non-hit cards?
        if state.has_cards_other_than_hits_and_buffs:
            log.info("  -> DISCARD_AND_PASS")
            return Action.DISCARD_AND_PASS
        if state.has_seven_hits:
            log.info("  -> DISCARD_AND_PASS (7 hits)")
            return Action.DISCARD_AND_PASS
        log.info("  -> FLEE")
        return Action.FLEE

    # No AOE that can kill all -> check for single hits
    log.info("  No killing AOE  --  checking single hits")

    if state.has_hit:
        # Can single hit kill strongest mob?
        if state.single_hit_can_kill_strongest:
            result = _pick_strongest_killable_target(state)
            if result:
                state.chosen_hit, state.hit_target = result
                log.info(f"  -> HIT strongest: {state.chosen_hit.name}")
                return Action.HIT

        # Can't kill strongest -> BuffCheck
        return _branch_buff_check(state)

    # No single hit at all
    buff = _pick_best_zero_pip_buff(state)
    if buff:
        state.chosen_buff = buff
        log.info(f"  -> ZERO_BUFF (no hits): {buff.name}")
        return Action.CAST_ZERO_PIP_BUFF

    if state.has_any_cards:
        log.info("  -> DISCARD_AND_PASS (no hits, has cards)")
        return Action.DISCARD_AND_PASS

    log.info("  -> FLEE (no options)")
    return Action.FLEE


def decide_action(state: CombatState) -> Action:
    """Walk the decision tree and return the action to take.

    Note: enchanting is handled separately in combat_main before this
    function is called, since enchants don't consume the turn.
    """
    log.info("--- DECISION TREE ---")

    # Branch by: have any hit/aoe cards AND enemy count
    if not state.has_any_hit_card:
        return _branch_no_hit(state)

    if state.enemy_count <= 1:
        return _branch_single_enemy(state)
    else:
        return _branch_multiple_enemies(state)


# ------------------------------------------------------------------
# Action executor
# ------------------------------------------------------------------

async def execute_action(
    action: Action,
    state: CombatState,
    combat_handler: CombatHandler,
) -> str:
    """Execute the chosen action. Returns a description of what happened."""

    if action == Action.HIT:
        hit = state.chosen_hit or _pick_best_hit(state)
        if hit:
            target = state.hit_target
            if target:
                eff_dmg = state.effective_damage(hit, target)
                await hit.card.cast(target.member)
                return f"Hit {target.name} with {hit.name} (base={hit.min_damage} eff={eff_dmg:.0f})"
            elif state.enemies:
                await hit.card.cast(state.enemies[0].member)
                return f"Hit {state.enemies[0].name} with {hit.name}"
            else:
                await hit.card.cast(None)
                return f"Cast {hit.name}"
        return "Hit failed  --  no valid card"

    elif action == Action.HIT_AOE:
        aoe = state.chosen_aoe or _pick_best_aoe(state)
        if aoe:
            eff_dmg = state.effective_damage(aoe, state.enemies[0] if state.enemies else None)
            await aoe.card.cast(None)
            return f"AOE {aoe.name} (base={aoe.min_damage} eff={eff_dmg:.0f})"
        return "AOE failed  --  no valid card"

    elif action == Action.CAST_ZERO_PIP_BUFF:
        buff = state.chosen_buff or _pick_best_zero_pip_buff(state)
        if buff:
            try:
                player = await combat_handler.get_client_member()
                await buff.card.cast(player)
                return f"Zero-pip buff: {buff.name} (val={buff.buff_value})"
            except Exception:
                await buff.card.cast(None)
                return f"Zero-pip buff: {buff.name}"
        return "Zero-pip buff failed  --  no valid card"

    elif action == Action.CAST_PIP_BUFF:
        buff = state.chosen_buff or _pick_best_pip_buff(state)
        if buff:
            try:
                player = await combat_handler.get_client_member()
                await buff.card.cast(player)
                return f"Pip buff: {buff.name} ({buff.pip_cost} pips, val={buff.buff_value})"
            except Exception:
                await buff.card.cast(None)
                return f"Pip buff: {buff.name}"
        return "Pip buff failed  --  no valid card"

    elif action == Action.DISCARD_AND_PASS:
        discard_card = _pick_card_to_discard(state)
        if discard_card:
            await discard_card.card.discard()
            await asyncio.sleep(0.3)
        await combat_handler.pass_button()
        card_name = discard_card.name if discard_card else "nothing"
        return f"Discarded {card_name} and passed"

    elif action == Action.PASS:
        await combat_handler.pass_button()
        return "Passed"

    elif action == Action.FLEE:
        await combat_handler.flee_button()
        # Handle the "Are you sure?" confirmation dialog
        await asyncio.sleep(0.5)
        try:
            client = combat_handler.client
            confirm_windows = await client.root_window.get_windows_with_name(
                "MessageBoxModalWindow"
            )
            if confirm_windows:
                confirm_window = confirm_windows[0]
                if await confirm_window.is_visible():
                    yes_buttons = await confirm_window.get_windows_with_name(
                        "centerButton"
                    )
                    if yes_buttons:
                        await client.mouse_handler.click_window(yes_buttons[0])
                        log.info("Confirmed flee dialog")
                    else:
                        log.warning("Flee confirm dialog found but no centerButton")
                else:
                    log.debug("Flee confirm dialog not visible")
            else:
                log.debug("No flee confirm dialog found")
        except Exception as e:
            log.warning(f"Error handling flee confirmation: {e}")
        return "Fled combat"

    return "Unknown action"


# ------------------------------------------------------------------
# Enchant helper  --  applies enchant in-hand (does NOT consume turn)
# ------------------------------------------------------------------

async def _apply_enchant(
    combat_handler: CombatHandler,
    enchant: CardInfo,
    target: CardInfo,
) -> bool:
    """Apply an enchant card to a target card in hand.

    Returns True if successful. Per Wizard101 mechanics, enchanting
    modifies the card in-hand and consumes the enchant card, but does
    NOT end the turn  --  the player can still cast a spell afterward.

    Pattern adapted from wizsprinter (sprinty_combat.py lines 1673-1712).
    """
    try:
        pre_count = len(await combat_handler.get_cards())
        await enchant.card.cast(target.card)

        # Wait for card list to update (enchant card is consumed)
        for _ in range(20):  # up to 2 seconds
            await asyncio.sleep(0.1)
            new_count = len(await combat_handler.get_cards())
            if new_count != pre_count:
                break

        await asyncio.sleep(0.3)  # settle time
        log.info(f"Enchanted {target.name} with {enchant.name}")
        return True
    except Exception as e:
        log.error(f"Enchant failed: {e}", exc_info=True)
        return False


# ------------------------------------------------------------------
# Main entry point
# ------------------------------------------------------------------

async def combat_main(client) -> tuple[str, str]:
    """
    Run one tick of combat logic.

    Returns:
        (result, description) where result is one of:
            "acted"    --  an action was taken
            "waiting"  --  not in planning phase yet
            "skipped"  --  no cards or error
        and description is a human-readable action summary.
    """
    current_duel = client.duel
    try:
        duel_phase = await current_duel.duel_phase()
    except Exception:
        return "waiting", ""

    if duel_phase == DuelPhase.planning:
        combat_handler = CombatHandler(client)

        try:
            state = await build_combat_state(combat_handler)
        except Exception as e:
            log.error(f"Error building combat state: {e}", exc_info=True)
            return "skipped", f"Error building state: {e}"

        if not state.all_cards:
            log.info("No cards in hand  --  skipping")
            return "skipped", "No cards in hand"

        # --- Phase 1: Enchant if possible (does NOT consume turn) ---
        enchant_msg = ""
        if state.has_enchant and state.has_card_for_enchant:
            result = _pick_enchant_and_target(state)
            if result:
                enchant_card, target_card = result
                log.info(f"Enchanting {target_card.name} with {enchant_card.name}")
                success = await _apply_enchant(combat_handler, enchant_card, target_card)
                if success:
                    enchant_msg = f"Enchanted {target_card.name} with {enchant_card.name}. "
                    # Rebuild state with updated cards
                    try:
                        state = await build_combat_state(combat_handler)
                    except Exception as e:
                        log.error(f"Error rebuilding state after enchant: {e}", exc_info=True)
                        return "skipped", f"Error after enchant: {e}"

        # --- Phase 2: Decide and execute ---
        action = decide_action(state)
        log.info(f">>> DECISION: {action.value}")

        try:
            result_msg = await execute_action(action, state, combat_handler)
            log.info(f">>> EXECUTED: {result_msg}")
            return "acted", enchant_msg + result_msg
        except Exception as e:
            log.error(f"Error executing {action.value}: {e}", exc_info=True)
            return "skipped", f"Error executing {action.value}: {e}"

    elif duel_phase == DuelPhase.ended:
        return "acted", "Combat ended"

    return "waiting", ""

