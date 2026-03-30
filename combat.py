"""
Combat — decision-tree state machine for Wizard101 battles.

Classifies cards in hand, computes decision variables, walks the
flowchart branches, and executes the chosen action.

Decision tree mirrors the user's flowchart exactly:
  Start → enchant check → single vs. multiple enemies →
  hit/buff/discard/pass/flee branches.
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
    ENCHANT_BEST_CARD = "enchant_best_card"
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


@dataclass
class EnemyInfo:
    """Enemy metadata."""
    member: CombatMember
    name: str
    health: int
    max_health: int
    is_boss: bool


# ------------------------------------------------------------------
# Combat state — all decision variables
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

    # The specific cards chosen for actions (set during decision)
    chosen_hit: Optional[CardInfo] = None
    chosen_aoe: Optional[CardInfo] = None
    chosen_buff: Optional[CardInfo] = None
    chosen_enchant: Optional[CardInfo] = None
    enchant_target: Optional[CardInfo] = None
    hit_target: Optional[EnemyInfo] = None

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
    def has_hit(self) -> bool:
        return any(c.is_castable for c in self.hits)

    @property
    def has_single_hit(self) -> bool:
        return self.has_hit

    @property
    def has_killing_hit(self) -> bool:
        """A castable single-target hit that can kill any enemy right now."""
        if not self.enemies:
            return False
        weakest_hp = min(e.health for e in self.enemies)
        return any(
            c.is_castable and c.min_damage >= weakest_hp
            for c in self.hits
        )

    @property
    def has_hit_needing_pips(self) -> bool:
        """A hit that could kill but isn't castable (needs more pips)."""
        if not self.enemies:
            return False
        weakest_hp = min(e.health for e in self.enemies)
        return any(
            not c.is_castable and c.min_damage >= weakest_hp
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
        """Total damage of all castable hits >= weakest enemy health."""
        if not self.enemies:
            return False
        weakest_hp = min(e.health for e in self.enemies)
        total_dmg = sum(c.min_damage for c in self.hits if c.is_castable)
        return total_dmg >= weakest_hp and sum(1 for c in self.hits if c.is_castable) >= 2

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
        """A castable AOE whose min_damage >= every enemy's health."""
        if not self.enemies:
            return False
        max_hp = max(e.health for e in self.enemies)
        return any(c.is_castable and c.min_damage >= max_hp for c in self.aoes)

    @property
    def has_aoe_kills_all_with_more_pips(self) -> bool:
        """An AOE that could kill all but isn't castable (needs pips)."""
        if not self.enemies:
            return False
        max_hp = max(e.health for e in self.enemies)
        return any(
            not c.is_castable and c.min_damage >= max_hp
            for c in self.aoes
        )

    @property
    def has_multiple_hits_kill_all(self) -> bool:
        """Total damage of all castable hits+aoes >= total enemy health."""
        if not self.enemies:
            return False
        total_enemy_hp = sum(e.health for e in self.enemies)
        total_dmg = sum(
            c.min_damage for c in self.hits + self.aoes if c.is_castable
        )
        return total_dmg >= total_enemy_hp and sum(1 for c in self.hits + self.aoes if c.is_castable) >= 2

    @property
    def single_hit_can_kill_strongest(self) -> bool:
        """A castable single-target hit can kill the strongest enemy."""
        if not self.enemies:
            return False
        strongest = max(self.enemies, key=lambda e: e.health)
        return any(
            c.is_castable and c.min_damage >= strongest.health
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

    min_dmg = min(d["min"] for d in all_damages)
    max_dmg = max(d["max"] for d in all_damages)

    return min_dmg, max_dmg


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
                        if target in (
                            EffectTarget.enemy_team,
                            EffectTarget.enemy_team_all_at_once,
                        ):
                            return True
                else:
                    target = await effect.effect_target()
                    if target in (
                        EffectTarget.enemy_team,
                        EffectTarget.enemy_team_all_at_once,
                    ):
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

        # Some buffs might have other type names but have buff effects
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
    """Check if a card is a damage enchantment."""
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

    min_dmg, max_dmg = await _get_card_damage(card)

    # Determine category
    is_enchant = await _is_enchant_card(card)
    is_aoe = await _is_aoe_card(card)
    is_dmg = await _is_damage_card(card)
    is_buff = await _is_buff_card(card)

    if is_enchant:
        category = CardCategory.ENCHANT
    elif is_aoe:
        category = CardCategory.AOE
    elif is_dmg and not is_aoe:
        category = CardCategory.HIT
    elif is_buff:
        if pip_cost == 0:
            category = CardCategory.BUFF_ZERO
        else:
            category = CardCategory.BUFF_PIP
    else:
        category = CardCategory.OTHER

    log.info(
        f"  Card: {name!r}  type_name={raw_type_name!r}  "
        f"category={category.value}  castable={is_castable}  "
        f"enchanted={is_enchanted}  pips={pip_cost}  "
        f"dmg={min_dmg}-{max_dmg}  "
        f"[enchant={is_enchant} aoe={is_aoe} dmg_card={is_dmg} buff={is_buff}]"
    )

    return CardInfo(
        card=card,
        category=category,
        name=name,
        pip_cost=pip_cost,
        min_damage=min_dmg,
        max_damage=max_dmg,
        is_castable=is_castable,
        is_enchanted=is_enchanted,
        school_id=school_id,
    )


# ------------------------------------------------------------------
# Build combat state
# ------------------------------------------------------------------

async def build_combat_state(combat_handler: CombatHandler) -> CombatState:
    """Gather all info needed for the decision tree."""
    state = CombatState()

    # Classify all cards
    cards = await combat_handler.get_cards()
    log.info(f"=== COMBAT ROUND — {len(cards)} cards in hand ===")
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

    # Sort hits and AOEs by damage (highest first)
    state.hits.sort(key=lambda c: c.min_damage, reverse=True)
    state.aoes.sort(key=lambda c: c.min_damage, reverse=True)

    # Get player info
    try:
        player = await combat_handler.get_client_member()
        player_stats = await player.get_stats()
        state.player_school_id = await player_stats.school_id()
        normal_pips = await player.normal_pips()
        power_pips = await player.power_pips()
        state.available_pips = normal_pips + (power_pips * 2)
        log.info(f"Player: school={state.player_school_id}  normal_pips={normal_pips}  power_pips={power_pips}  available={state.available_pips}")
    except Exception as e:
        log.warning(f"Failed to get player info: {e}")

    # Get enemies
    monsters = await combat_handler.get_all_monster_members()
    for monster in monsters:
        try:
            enemy = EnemyInfo(
                member=monster,
                name=await monster.name(),
                health=await monster.health(),
                max_health=await monster.max_health(),
                is_boss=await monster.is_boss(),
            )
            if enemy.health > 0:
                state.enemies.append(enemy)
                log.info(f"  Enemy: {enemy.name!r}  hp={enemy.health}/{enemy.max_health}  boss={enemy.is_boss}")
        except Exception:
            pass

    # Sort enemies: strongest first
    state.enemies.sort(key=lambda e: e.health, reverse=True)

    # Log summary
    log.info(
        f"Hand summary: {len(state.hits)} hits, {len(state.aoes)} aoes, "
        f"{len(state.enchants)} enchants, {len(state.zero_pip_buffs)} zero-pip buffs, "
        f"{len(state.pip_buffs)} pip buffs, {len(state.other_cards)} other  |  "
        f"{len(state.enemies)} enemies"
    )
    log.info(
        f"Decision vars: has_hit={state.has_hit}  has_killing_hit={state.has_killing_hit}  "
        f"has_hit_needing_pips={state.has_hit_needing_pips}  "
        f"has_multiple_hits_combined_kill={state.has_multiple_hits_combined_kill}  "
        f"has_zero_pip_buff={state.has_zero_pip_buff}  has_pip_buff={state.has_pip_buff}  "
        f"has_cards_other={state.has_cards_other_than_hits_and_buffs}  "
        f"has_seven_hits={state.has_seven_hits}  has_any_cards={state.has_any_cards}"
    )
    if state.enemy_count > 1:
        log.info(
            f"Multi-enemy vars: has_aoe={state.has_aoe}  "
            f"has_aoe_kills_all={state.has_aoe_that_kills_all}  "
            f"has_aoe_kills_all_more_pips={state.has_aoe_kills_all_with_more_pips}  "
            f"has_multi_hits_kill_all={state.has_multiple_hits_kill_all}  "
            f"single_hit_can_kill_strongest={state.single_hit_can_kill_strongest}"
        )

    return state


# ------------------------------------------------------------------
# Decision tree
# ------------------------------------------------------------------

def _pick_killing_hit(state: CombatState) -> Optional[CardInfo]:
    """Find the best castable hit that can kill an enemy."""
    if not state.enemies:
        return None
    weakest_hp = min(e.health for e in state.enemies)
    # Pick the lowest-damage hit that still kills (efficient pip usage)
    candidates = [
        c for c in state.hits
        if c.is_castable and c.min_damage >= weakest_hp
    ]
    if candidates:
        candidates.sort(key=lambda c: c.min_damage)
        return candidates[0]
    return None


def _pick_best_hit(state: CombatState) -> Optional[CardInfo]:
    """Find the highest-damage castable hit."""
    candidates = [c for c in state.hits if c.is_castable]
    if candidates:
        return candidates[0]  # already sorted highest first
    return None


def _pick_killing_aoe(state: CombatState) -> Optional[CardInfo]:
    """Find a castable AOE that kills all enemies."""
    if not state.enemies:
        return None
    max_hp = max(e.health for e in state.enemies)
    for c in state.aoes:
        if c.is_castable and c.min_damage >= max_hp:
            return c
    return None


def _pick_best_aoe(state: CombatState) -> Optional[CardInfo]:
    """Find the highest-damage castable AOE."""
    candidates = [c for c in state.aoes if c.is_castable]
    return candidates[0] if candidates else None


def _pick_strongest_killable_target(state: CombatState) -> Optional[tuple]:
    """Find the strongest enemy that a single hit can kill, plus the hit."""
    strongest = sorted(state.enemies, key=lambda e: e.health, reverse=True)
    for enemy in strongest:
        for hit in state.hits:
            if hit.is_castable and hit.min_damage >= enemy.health:
                return hit, enemy
    return None


def _pick_zero_pip_buff(state: CombatState) -> Optional[CardInfo]:
    """Pick the first castable zero-pip buff."""
    for c in state.zero_pip_buffs:
        if c.is_castable:
            return c
    return None


def _pick_pip_buff(state: CombatState) -> Optional[CardInfo]:
    """Pick the first castable pip buff."""
    for c in state.pip_buffs:
        if c.is_castable:
            return c
    return None


def _pick_enchant_and_target(state: CombatState) -> Optional[tuple]:
    """Pick the best enchant and the best non-enchanted hit/aoe to apply it to."""
    if not state.enchants:
        return None
    enchant = state.enchants[0]

    # Prefer un-enchanted hits/aoes sorted by damage (highest first)
    targets = [c for c in state.hits + state.aoes if not c.is_enchanted]
    targets.sort(key=lambda c: c.min_damage, reverse=True)
    if targets:
        return enchant, targets[0]
    return None


def _pick_card_to_discard(state: CombatState) -> Optional[CardInfo]:
    """Pick the least useful card to discard.
    Priority: enchants (if no target) > other > lowest-damage hits > buffs.
    """
    # Enchants with no target
    if state.enchants and not state.has_card_for_enchant:
        return state.enchants[0]

    # Other cards
    if state.other_cards:
        return state.other_cards[0]

    # Lowest damage hit
    castable_hits = [c for c in state.hits if c.is_castable]
    if castable_hits:
        return min(castable_hits, key=lambda c: c.min_damage)

    # Any card at all
    if state.all_cards:
        return state.all_cards[-1]

    return None


# --- Branch functions ---

def _branch_no_hit(state: CombatState) -> Action:
    """No-hit path (single enemy or generic fallback)."""
    log.info("Branch: NO HIT path")
    if state.has_zero_pip_buff:
        state.chosen_buff = _pick_zero_pip_buff(state)
        log.info(f"  → CAST_ZERO_PIP_BUFF: {state.chosen_buff.name if state.chosen_buff else '???'}")
        return Action.CAST_ZERO_PIP_BUFF

    if state.has_cards_other_than_hits_and_buffs:
        log.info("  → DISCARD_AND_PASS (has other cards)")
        return Action.DISCARD_AND_PASS

    if state.has_seven_hits:
        log.info("  → DISCARD_AND_PASS (7 hits)")
        return Action.DISCARD_AND_PASS

    log.info("  → PASS (nothing else to do)")
    return Action.PASS


def _branch_single_enemy(state: CombatState) -> Action:
    """Single enemy decision branch."""
    log.info("Branch: SINGLE ENEMY")
    if not state.has_hit:
        log.info("  No castable hits in hand")
        return _branch_no_hit(state)

    log.info(f"  Has {sum(1 for c in state.hits if c.is_castable)} castable hits")

    # Has a hit — can it kill?
    if state.has_killing_hit:
        state.chosen_hit = _pick_killing_hit(state)
        if state.enemies:
            state.hit_target = state.enemies[0]
        log.info(f"  → HIT (killing): {state.chosen_hit.name if state.chosen_hit else '???'} on {state.hit_target.name if state.hit_target else '???'}")
        return Action.HIT

    log.info("  No killing hit available")

    # If no kill is immediately available, prioritize buffing
    if state.has_zero_pip_buff:
        state.chosen_buff = _pick_zero_pip_buff(state)
        log.info(f"  → CAST_ZERO_PIP_BUFF (no kill): {state.chosen_buff.name if state.chosen_buff else '???'}")
        return Action.CAST_ZERO_PIP_BUFF

    if state.has_pip_buff:
        state.chosen_buff = _pick_pip_buff(state)
        log.info(f"  → CAST_PIP_BUFF (no kill): {state.chosen_buff.name if state.chosen_buff else '???'}")
        return Action.CAST_PIP_BUFF

    log.info("  No buffs available")

    # Hit exists that can kill but needs more pips (and we have no buffs)
    if state.has_hit_needing_pips:
        log.info("  Hit needs more pips to kill (waiting)")
        # We want to wait for pips, so we discard/pass
        if state.has_cards_other_than_hits_and_buffs:
            log.info("  → DISCARD_AND_PASS (waiting for pips)")
            return Action.DISCARD_AND_PASS
        if state.has_seven_hits:
            log.info("  → PASS (waiting for pips, hand full of hits)")
            return Action.PASS
        log.info("  → DISCARD_AND_PASS (waiting for pips fallback)")
        return Action.DISCARD_AND_PASS

    # Multiple hits combined can kill
    if state.has_multiple_hits_combined_kill:
        state.chosen_hit = _pick_best_hit(state)
        if state.enemies:
            state.hit_target = state.enemies[0]
        log.info(f"  → HIT (combined kill): {state.chosen_hit.name if state.chosen_hit else '???'}")
        return Action.HIT

    log.info("  No combined kill possible either — fallback")

    # No buffs — check for other options
    if state.has_any_cards:
        if state.has_cards_other_than_hits_and_buffs:
            log.info("  → DISCARD_AND_PASS (has other cards)")
            return Action.DISCARD_AND_PASS
        if state.has_seven_hits:
            log.info("  → PASS (7 hits, no other options)")
            return Action.PASS
        log.info("  → DISCARD_AND_PASS (fallback)")
        return Action.DISCARD_AND_PASS

    log.info("  → FLEE (no cards at all)")
    return Action.FLEE


def _branch_no_aoe(state: CombatState) -> Action:
    """Multiple enemies, no AOE available."""
    log.info("Branch: NO AOE (multi-enemy)")
    if not state.has_single_hit:
        log.info("  No single hit")
        if state.has_zero_pip_buff:
            state.chosen_buff = _pick_zero_pip_buff(state)
            log.info(f"  → CAST_ZERO_PIP_BUFF: {state.chosen_buff.name if state.chosen_buff else '???'}")
            return Action.CAST_ZERO_PIP_BUFF
        if state.has_any_cards:
            log.info("  → DISCARD_AND_PASS")
            return Action.DISCARD_AND_PASS
        log.info("  → FLEE")
        return Action.FLEE

    if state.single_hit_can_kill_strongest:
        result = _pick_strongest_killable_target(state)
        if result:
            state.chosen_hit, enemy = result
            state.hit_target = enemy
        log.info(f"  → HIT strongest: {state.chosen_hit.name if state.chosen_hit else '???'}")
        return Action.HIT

    log.info("  Cannot kill strongest")

    # Prioritize buffs if we can't kill strongest
    if state.has_zero_pip_buff:
        state.chosen_buff = _pick_zero_pip_buff(state)
        log.info(f"  → CAST_ZERO_PIP_BUFF: {state.chosen_buff.name if state.chosen_buff else '???'}")
        return Action.CAST_ZERO_PIP_BUFF

    if state.has_pip_buff:
        state.chosen_buff = _pick_pip_buff(state)
        log.info(f"  → CAST_PIP_BUFF: {state.chosen_buff.name if state.chosen_buff else '???'}")
        return Action.CAST_PIP_BUFF

    log.info("  No buffs available")

    if state.has_hit_needing_pips:
        log.info("  Hit needs more pips")
        if state.has_cards_other_than_hits_and_buffs:
            log.info("  → DISCARD_AND_PASS (waiting for pips)")
            return Action.DISCARD_AND_PASS
        if state.has_seven_hits:
            log.info("  → DISCARD_AND_PASS (7 hits)")
            return Action.DISCARD_AND_PASS
        log.info("  → DISCARD_AND_PASS (waiting fallback)")
        return Action.DISCARD_AND_PASS

    if state.has_multiple_hits_combined_kill:
        state.chosen_hit = _pick_best_hit(state)
        if state.enemies:
            state.hit_target = state.enemies[0]
        log.info(f"  → HIT (combined): {state.chosen_hit.name if state.chosen_hit else '???'}")
        return Action.HIT

    if state.has_cards_other_than_hits_and_buffs:
        log.info("  → DISCARD_AND_PASS")
        return Action.DISCARD_AND_PASS

    if state.has_seven_hits:
        log.info("  → DISCARD_AND_PASS (7 hits)")
        return Action.DISCARD_AND_PASS

    log.info("  → FLEE")
    return Action.FLEE


def _branch_multiple_enemies(state: CombatState) -> Action:
    """Multiple enemies decision branch."""
    log.info("Branch: MULTIPLE ENEMIES")
    if not state.has_aoe:
        log.info("  No castable AOE")
        return _branch_no_aoe(state)

    log.info(f"  Has {sum(1 for c in state.aoes if c.is_castable)} castable AOEs")

    if state.has_aoe_that_kills_all:
        state.chosen_aoe = _pick_killing_aoe(state)
        log.info(f"  → HIT_AOE (kills all): {state.chosen_aoe.name if state.chosen_aoe else '???'}")
        return Action.HIT_AOE

    # Prioritize buffs if we can't kill all
    log.info("  No AOE kill available")

    if state.has_zero_pip_buff:
        state.chosen_buff = _pick_zero_pip_buff(state)
        log.info(f"  → CAST_ZERO_PIP_BUFF: {state.chosen_buff.name if state.chosen_buff else '???'}")
        return Action.CAST_ZERO_PIP_BUFF

    if state.has_pip_buff:
        state.chosen_buff = _pick_pip_buff(state)
        log.info(f"  → CAST_PIP_BUFF: {state.chosen_buff.name if state.chosen_buff else '???'}")
        return Action.CAST_PIP_BUFF

    log.info("  No buffs available")

    if state.has_aoe_kills_all_with_more_pips:
        log.info("  AOE can kill all but needs more pips")
        if state.has_cards_other_than_hits_and_buffs:
            log.info("  → DISCARD_AND_PASS (waiting for pips)")
            return Action.DISCARD_AND_PASS
        if state.has_seven_hits:
            log.info("  → DISCARD_AND_PASS (7 hits)")
            return Action.DISCARD_AND_PASS
        log.info("  → PASS (waiting)")
        return Action.PASS

    if state.has_multiple_hits_kill_all:
        state.chosen_hit = _pick_best_hit(state)
        if state.enemies:
            state.hit_target = state.enemies[0]
        log.info(f"  → HIT (multi kill all): {state.chosen_hit.name if state.chosen_hit else '???'}")
        return Action.HIT

    if state.has_cards_other_than_hits_and_buffs:
        log.info("  → DISCARD_AND_PASS")
        return Action.DISCARD_AND_PASS

    if state.has_seven_hits:
        log.info("  → DISCARD_AND_PASS (7 hits)")
        return Action.DISCARD_AND_PASS

    log.info("  → FLEE")
    return Action.FLEE


def decide_action(state: CombatState) -> Action:
    """Walk the decision tree and return the action to take."""
    log.info("--- DECISION TREE START ---")

    # Step 1: Enchant check
    log.info(f"Enchant check: has_enchant={state.has_enchant}  has_card_for_enchant={state.has_card_for_enchant}")
    if state.has_enchant and state.has_card_for_enchant:
        result = _pick_enchant_and_target(state)
        if result:
            state.chosen_enchant, state.enchant_target = result
            log.info(f"  → ENCHANT: {state.chosen_enchant.name} on {state.enchant_target.name}")
            return Action.ENCHANT_BEST_CARD

    # Step 2: Branch by enemy count
    log.info(f"Enemy count: {state.enemy_count}")
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

    if action == Action.ENCHANT_BEST_CARD:
        if state.chosen_enchant and state.enchant_target:
            await state.chosen_enchant.card.cast(state.enchant_target.card)
            return f"Enchanted {state.enchant_target.name} with {state.chosen_enchant.name}"
        return "Enchant failed — no valid target"

    elif action == Action.HIT:
        hit = state.chosen_hit or _pick_best_hit(state)
        if hit:
            target = state.hit_target
            if target:
                await hit.card.cast(target.member)
                return f"Hit {target.name} with {hit.name} ({hit.min_damage} dmg)"
            else:
                # No specific target, cast on first monster
                monsters = state.enemies
                if monsters:
                    await hit.card.cast(monsters[0].member)
                    return f"Hit {monsters[0].name} with {hit.name}"
                else:
                    await hit.card.cast(None)
                    return f"Cast {hit.name}"
        return "Hit failed — no valid card"

    elif action == Action.HIT_AOE:
        aoe = state.chosen_aoe or _pick_best_aoe(state)
        if aoe:
            await aoe.card.cast(None)
            return f"AOE {aoe.name} ({aoe.min_damage} dmg)"
        return "AOE failed — no valid card"

    elif action == Action.CAST_ZERO_PIP_BUFF:
        buff = state.chosen_buff or _pick_zero_pip_buff(state)
        if buff:
            # Buffs target self (blades) or enemy (traps)
            # Cast with no target — the game auto-targets for self-buffs
            try:
                player = await combat_handler.get_client_member()
                await buff.card.cast(player)
                return f"Cast zero-pip buff: {buff.name}"
            except Exception:
                await buff.card.cast(None)
                return f"Cast zero-pip buff: {buff.name}"
        return "Zero-pip buff failed — no valid card"

    elif action == Action.CAST_PIP_BUFF:
        buff = state.chosen_buff or _pick_pip_buff(state)
        if buff:
            try:
                player = await combat_handler.get_client_member()
                await buff.card.cast(player)
                return f"Cast pip buff: {buff.name} ({buff.pip_cost} pips)"
            except Exception:
                await buff.card.cast(None)
                return f"Cast pip buff: {buff.name}"
        return "Pip buff failed — no valid card"

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
        return "Fled combat"

    return "Unknown action"


# ------------------------------------------------------------------
# Main entry point
# ------------------------------------------------------------------

async def combat_main(client) -> tuple[str, str]:
    """
    Run one tick of combat logic.

    Returns:
        (result, description) where result is one of:
            "acted"   — an action was taken
            "waiting" — not in planning phase yet
            "skipped" — no cards or error
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
            log.info("No cards in hand — skipping")
            return "skipped", "No cards in hand"

        action = decide_action(state)
        log.info(f">>> FINAL DECISION: {action.value}")

        try:
            result_msg = await execute_action(action, state, combat_handler)
            log.info(f">>> EXECUTED: {result_msg}")
            return "acted", result_msg
        except Exception as e:
            log.error(f"Error executing {action.value}: {e}", exc_info=True)
            return "skipped", f"Error executing {action.value}: {e}"

    elif duel_phase == DuelPhase.ended:
        return "acted", "Combat ended"

    return "waiting", ""
