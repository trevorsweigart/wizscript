"""Free own-school training and a compact PvE deck, using observed game UI."""

import asyncio
import math
import re
from collections import Counter
from dataclasses import asdict, dataclass, field

from wizwalker.constants import Keycode
from wizwalker.memory.memory_objects.enums import MagicSchool
from wizwalker.memory.memory_objects.window import (
    DynamicDeckListControl, DynamicGraphicalSpellWindow, DynamicSpellListControl,
    SpellListControlSpellEntry, DeckListControlSpellEntry,
)
from wizwalker.memory.memory_object import Primitive

from quest_navigation import plain_text
from quest_state import safe_read, visible_ui
from quest_npc import approach_point, prompt_name
from zone_walker import walk_zone_path
from combat_state import Read, effects as read_effects
from quest_search import ZoneSearch


# Teacher roles, rather than quest targets or hard-coded movement coordinates.
TEACHERS = {"death": "Malorn Ashthorn", "balance": "Arthur Wethersfield",
            "fire": "Dalia Falmea", "ice": "Lydia Greyrose",
            "storm": "Halston Balestrom", "myth": "Cyrus Drake", "life": "Moolinda Wu"}
TRAINING_LEVELS = tuple(range(2, 51))


class LiveListLayout:
    """Validate the current Window extension before reading list metadata.

    Wizwalker's list widgets still use the older 640-byte Window base; the
    current client's base is 712 bytes. Card dimensions from the installed
    DeckConfiguration asset distinguish these layouts before dereferencing.
    """
    async def layout_shift(self):
        if hasattr(self, "_verified_shift"):
            return self._verified_shift
        deck = isinstance(self, DynamicDeckListControl)
        offset = 0x2A4 if deck else 0x30C
        expected = (33, 33) if deck else (97, 148)
        for shift in (0, 72):
            width = await self.read_value_from_offset(offset + shift, Primitive.uint32)
            height = await self.read_value_from_offset(offset + shift + 4, Primitive.uint32)
            if (width, height) == expected:
                self._verified_shift = shift
                return shift
        raise ValueError("Card list layout does not match the installed deck UI")

    async def spell_entries(self):
        shift = await self.layout_shift()
        deck = isinstance(self, DynamicDeckListControl)
        cls = DeckListControlSpellEntry if deck else SpellListControlSpellEntry
        return await self.read_inlined_vector(0x280 + shift, 0x28 if deck else 0xA8, cls)

    async def card_size_horizontal(self):
        shift = await self.layout_shift()
        offset = 0x2A4 if isinstance(self, DynamicDeckListControl) else 0x30C
        return await self.read_value_from_offset(offset + shift, Primitive.uint32)

    async def card_size_vertical(self):
        shift = await self.layout_shift()
        offset = 0x2A8 if isinstance(self, DynamicDeckListControl) else 0x310
        return await self.read_value_from_offset(offset + shift, Primitive.uint32)

    async def start_index(self):
        return await self.read_value_from_offset(0x308 + await self.layout_shift(), Primitive.uint32)

    async def card_spacing(self):
        return await self.read_value_from_offset(0x2AC + await self.layout_shift(), Primitive.uint32)


class LiveSpellListControl(LiveListLayout, DynamicSpellListControl):
    pass


class LiveDeckListControl(LiveListLayout, DynamicDeckListControl):
    pass


@dataclass
class SchoolSpell:
    id: int
    name: str
    school: str
    pips: int | None
    accuracy: int | None = None
    effects: list = field(default_factory=list)
    max_copies: int = 3


def school_name(identifier):
    try:
        return MagicSchool(identifier).name
    except (ValueError, TypeError):
        return None


def number(text):
    text = plain_text(text).strip()
    return int(text) if re.fullmatch(r"\d+", text) else None


def compact_deck(spells, school, capacity=20):
    """Keep reliable attacks, a finisher, buffs and an emergency heal.

    The policy chooses from learned cards by school, cost and effects; it has
    no spell-name table and never determines the action taken in combat.
    """
    def has(card, types, positive=False):
        return any(e.get("type") in types and (not positive or (e.get("amount") or 0) > 0)
                   for e in card.effects)

    def damage(card):
        amounts = [abs(e.get("amount") or 0) for e in card.effects
                   if e.get("type") in {"damage", "damage_no_crit", "steal_health", "damage_over_time"}]
        return max(amounts, default=0) * (card.accuracy or 80) / 100

    own = [c for c in spells if c.school.casefold() == school.casefold() and
           isinstance(c.pips, int) and 0 <= c.pips <= 6]
    attacks = [c for c in own if damage(c) > 0 and c.pips > 0]
    if not attacks:
        return {}  # Incomplete metadata must not empty a usable deck.
    plan = {}

    def add(card, copies):
        if card is not None:
            limit = card.max_copies if isinstance(card.max_copies, int) else 3
            plan[card.id] = min(max(plan.get(card.id, 0), copies), limit,
                                max(0, capacity - sum(v for k, v in plan.items() if k != card.id)))

    cheapest = min(c.pips for c in attacks)
    fast = max((c for c in attacks if c.pips == cheapest), key=damage)
    efficient = max((c for c in attacks if c.pips <= 2),
                    key=lambda c: (damage(c) / c.pips, damage(c)), default=fast)
    heavy = max((c for c in attacks if c.pips <= 4), key=damage, default=efficient)
    add(efficient, 4)
    add(fast, 2 if fast.id != efficient.id else 4)
    add(heavy, 2 if heavy.id != efficient.id else 4)
    blades = [c for c in own if has(c, {"modify_outgoing_damage"}, True)]
    traps = [c for c in own if has(c, {"modify_incoming_damage"}, True)]
    add(min(blades, key=lambda c: c.pips, default=None), 2)
    add(min(traps, key=lambda c: c.pips, default=None), 1)
    heals = [c for c in spells if c.pips is not None and c.pips <= 2 and
             has(c, {"heal", "heal_over_time"}, True) and
             any(e.get("target") in {"self", "friendly_single", "friendly_team", "friendly_team_all_at_once"}
                 for e in c.effects)]
    direct = [c for c in heals if has(c, {"heal"}, True)]
    heal = max(direct or heals, key=lambda c: max(e.get("amount") or 0 for e in c.effects), default=None)
    add(heal, 1 if any(has(c, {"steal_health"}) for c in attacks) else 2)
    return {identifier: count for identifier, count in plan.items() if count > 0}


async def read_spell(client, spell, max_copies=3):
    if spell is None:
        return None
    identifier = await safe_read(spell.template_id)
    template = await safe_read(spell.spell_template)
    if identifier is None or template is None:
        return None
    school = await safe_read(template.magic_school_name, "")
    key = await safe_read(template.display_name, "")
    name = await safe_read(lambda: client.cache_handler.get_langcode_name(key), key) if key else ""
    rank = await safe_read(spell.pip_cost)
    pips = await safe_read(rank.spell_rank) if rank else None
    if pips is None:
        rank = await safe_read(template.spell_rank)
        pips = await safe_read(rank.spell_rank) if rank else None
    reader = Read(seconds=2)
    raw_effects = await read_effects(reader, spell, "spell_effects", "deck.spell.effects")
    if not raw_effects:
        raw_effects = await read_effects(reader, template, "effects", "deck.template.effects")
    effects = summarize_effects(raw_effects)
    return SchoolSpell(identifier, name, school or "", pips,
                       await safe_read(template.accuracy), effects, max_copies)


def summarize_effects(raw):
    result = []
    for effect in raw or []:
        if not effect:
            continue
        amount = effect.get("effect_param")
        low, high = effect.get("min_effect_value"), effect.get("max_effect_value")
        if isinstance(low, (int, float)) and isinstance(high, (int, float)) and high >= low:
            amount = (low + high) / 2
        result.append(dict(type=effect.get("effect_type"), amount=amount, target=effect.get("effect_target")))
        for name in ("effects_list", "output_effect", "effect_list"):
            children = summarize_effects(effect.get(name))
            if effect.get("kind") == "RandomSpellEffect":
                groups = {}
                for child in children:
                    if isinstance(child.get("amount"), (int, float)):
                        groups.setdefault((child["type"], child["target"]), []).append(child["amount"])
                result.extend(dict(type=kind, target=target, amount=sum(amounts) / len(amounts))
                              for (kind, target), amounts in groups.items())
            else:
                result.extend(children)
        for branch in effect.get("conditional_effects") or []:
            result.extend(summarize_effects([branch.get("effect")]))
    return result


def control(ui, name):
    return next((n for n in ui.walk() if n.name == name and n.enabled), None) if ui else None


async def click(client, window):
    async with client.mouse_handler:
        await client.mouse_handler.click_window(window)
        await asyncio.sleep(0.25)


class SchoolTrainer:
    """Only train a selected own-school card whose displayed point cost is zero."""

    def __init__(self, status):
        self.status = status
        self.inspected = set()
        self.attempted = set()
        self.stopped = False
        self.finished = False

    async def step(self, client, ui):
        panel = control(ui, "NPCTrainingGUI")
        if panel is None:
            self.inspected.clear()
            return False
        if self.stopped:
            exit_button = control(panel, "ExitButton")
            if exit_button:
                await click(client, exit_button.window)
            return True
        self.finished = False
        school = school_name(await safe_read(client.stats.school_id))
        level = await safe_read(client.stats.reference_level)
        if school is None or not isinstance(level, int):
            self.status("Waiting for school and level before training")
            return True
        # The selected preview is a real graphical spell, not a text-only row.
        cards = [n for n in panel.walk() if n.kind in ("GraphicalSpellWindow", "SpellCheckBox")]
        train = control(panel, "TrainButton")
        if len(cards) == 1 and train is not None and not await safe_read(train.window.is_control_grayed, True):
            if cards[0].kind == "SpellCheckBox":
                spell = await safe_read(cards[0].window.maybe_graphical_spell)
            else:
                raw = DynamicGraphicalSpellWindow(cards[0].window.hook_handler,
                                                 await cards[0].window.read_base_address())
                spell = await safe_read(raw.graphical_spell)
            info = await read_spell(client, spell)
            selected = [n for n in panel.walk() if n.name.startswith("Option_")
                        and await safe_read(n.window.maybe_checked, False)]
            cost = next((number(n.text) for row in selected for n in row.walk() if n.name == "Cost"), None)
            points = control(panel, "TrainingPoints")
            before = number(points.text) if points else None
            book = await safe_read(client.client_object.try_get_spellbook_behavior)
            learned = await safe_read(book.trained_spell_ids) if book else None
            if (info and cost == 0 and before is not None and learned is not None and
                    info.school.casefold() == school and info.id not in learned and info.id not in self.attempted):
                self.attempted.add(info.id)
                self.status(f"Training free {school} spell: {info.name}")
                await click(client, train.window)
                for _ in range(12):
                    await asyncio.sleep(0.25)
                    if await client.is_loading() or await client.in_battle():
                        return True
                    learned = await safe_read(book.trained_spell_ids)
                    fresh = await visible_ui(client)
                    observed_points = control(fresh, "TrainingPoints")
                    after = number(observed_points.text) if observed_points else None
                    if learned is not None and info.id in learned:
                        if after is None:
                            continue
                        if after != before:
                            self.stopped = True
                            self.status("Training points changed unexpectedly; stopping school training")
                            return True
                        self.status(f"Learned {info.name}; training points unchanged ({before})")
                        self.inspected.clear()
                        return True
                self.stopped = True
                self.status(f"Training paused: the game did not confirm learning {info.name}")
                return True
        options = []
        for row in panel.walk():
            if not row.name.startswith("Option_") or not row.enabled:
                continue
            values = {n.name: plain_text(n.text) for n in row.walk() if n.name in ("Name", "Cost", "Level")}
            required = number(values.get("Level", ""))
            name = values.get("Name", "")
            if name and name not in self.inspected and number(values.get("Cost", "")) == 0 and required is not None and required <= level:
                options.append((required, name, row))
        if options:
            _, name, row = min(options, key=lambda x: (x[0], x[1]))
            self.inspected.add(name)
            self.status(f"Inspecting free training offer: {name}")
            await click(client, row.window)
            return True
        exit_button = control(panel, "ExitButton")
        if exit_button:
            self.status("Finished checking available free school spells")
            self.finished = True
            await click(client, exit_button.window)
        return True


async def deck_lists(client, ui):
    """Read the actual displayed card lists and their live geometry."""
    result = {}
    for node in ui.walk() if ui else []:
        if node.kind not in ("SpellListControl", "DeckListControl"):
            continue
        cls = LiveSpellListControl if node.kind == "SpellListControl" else LiveDeckListControl
        widget = cls(node.window.hook_handler, await node.window.read_base_address())
        entries = await safe_read(widget.spell_entries)
        if entries is None:
            continue
        spells = []
        entry_rectangles = []
        for entry in entries[:128]:
            max_copies = await safe_read(entry.max_copies, 3) if node.kind == "SpellListControl" else 3
            spells.append(await read_spell(client, await safe_read(entry.graphical_spell), max_copies))
            entry_rectangles.append(await safe_read(entry.window_rectangle) if node.kind == "SpellListControl" else None)
        result[node.name] = dict(widget=widget, spells=spells,
                                 entry_rectangles=entry_rectangles,
                                 start_index=await safe_read(widget.start_index) if node.kind == "SpellListControl" else 0,
                                 rectangle=await safe_read(widget.scale_to_client),
                                 spacing=await safe_read(widget.card_spacing) if node.kind == "DeckListControl" else None,
                                 card_width=await safe_read(widget.card_size_horizontal),
                                 card_height=await safe_read(widget.card_size_vertical))
    return result


async def spell_ui_report(client, ui):
    lists = await deck_lists(client, ui)
    report = {name: dict(cards=[asdict(c) if c else None for c in values["spells"]],
                       rectangle=str(values["rectangle"]),
                       entry_rectangles=[str(r) for r in values["entry_rectangles"]],
                       start_index=values["start_index"], spacing=values["spacing"],
                       card_width=values["card_width"], card_height=values["card_height"])
            for name, values in lists.items()}
    for node in ui.walk() if ui else []:
        if node.kind in ("SpellListControl", "DeckListControl") and node.name not in report:
            widget = LiveSpellListControl(node.window.hook_handler, await node.window.read_base_address())
            report[node.name] = dict(unverified_layout={str(offset): await safe_read(
                lambda offset=offset: widget.read_value_from_offset(offset, Primitive.uint32))
                for offset in range(700, 920, 4)})
    return report


def deck_counts(contents):
    if contents is None or any(row.get("enchantment") for row in contents):
        return None
    counts = Counter()
    for row in contents:
        identifier, quantity = row.get("template_id"), row.get("quantity")
        if not isinstance(identifier, int) or not isinstance(quantity, int) or quantity < 0:
            return None
        counts[identifier] += quantity
    return +counts


def next_deck_change(current, desired, capacity=20):
    """Add useful cards before removing extras, so interruptions leave attacks."""
    if sum(current.values()) >= capacity:
        for identifier, count in current.items():
            if count > desired.get(identifier, 0):
                return "remove", identifier
    for identifier, count in desired.items():
        if current.get(identifier, 0) < count:
            return "add", identifier
    for identifier, count in current.items():
        if count > desired.get(identifier, 0):
            return "remove", identifier
    return None


class DeckManager:
    def __init__(self, status):
        self.status = status
        self.checked = None
        self.opened = False
        self.failed = False

    async def step(self, client, ui):
        panel = control(ui, "DeckConfigurationWindow")
        if self.failed:
            return False
        if not self.opened:
            if panel or await client.is_in_dialog():
                return False
            book = await safe_read(client.client_object.try_get_spellbook_behavior)
            identifiers = await safe_read(book.trained_spell_ids) if book else None
            identity = await safe_read(client.client_object.global_id_full)
            level = await safe_read(client.stats.reference_level)
            if identifiers is None or identity is None or level is None:
                return False
            signature = (identity, level, tuple(sorted(identifiers)))
            if signature == self.checked:
                return False
            self.checked = signature
            self.opened = True
            self.status("Checking the learned spells and equipped deck")
            await client.send_key(Keycode.P, 0.04)
            await asyncio.sleep(0.4)
            return True
        if panel is None:
            self.opened = False
            return False
        all_cards = control(panel, "AllPageSpellList")
        if all_cards is None:
            tab = control(panel, "Cards_All")
            if tab:
                await click(client, tab.window)
                return True
            return await self.finish(client, "Deck checks paused: the learned-card list is unavailable")
        lists = await deck_lists(client, panel)
        library = lists.get("AllPageSpellList")
        equipped = lists.get("CardsInDeck")
        school = school_name(await safe_read(client.stats.school_id))
        deck = await safe_read(client.client_object.try_get_deck_behavior)
        current = deck_counts(await safe_read(deck.deck_contents)) if deck else None
        if not library or not equipped or not school or current is None:
            return await self.finish(client, "Deck checks paused: card data is unavailable")
        desired = compact_deck([s for s in library["spells"] if s], school)
        if not desired:
            return await self.finish(client, "Keeping the current deck until attack metadata is available")
        change = next_deck_change(current, desired)
        if change is None:
            return await self.finish(client, f"Deck ready: {sum(desired.values())} cards with school attacks and support")
        action, identifier = change
        values = library if action == "add" else equipped
        index = next((i for i, card in enumerate(values["spells"]) if card and card.id == identifier), None)
        if index is None:
            return await self.finish(client, "Deck checks paused: the requested card is unavailable")
        if action == "add":
            start = values["start_index"]
            # The live SpellListControl exposes the six visible cards per page.
            if not isinstance(start, int):
                return await self.finish(client, "Deck checks paused: the displayed page is unavailable")
            if not start <= index < start + 6:
                button = control(panel, "PageDown" if index >= start + 6 else "PageUp")
                if button is None or await safe_read(button.window.is_control_grayed, True):
                    return await self.finish(client, "Deck checks paused: the card page cannot be reached")
                await click(client, button.window)
                return True
            rectangle = values["entry_rectangles"][index]
            # Entry rectangles are checked against the displayed list before input.
            bounds = values["rectangle"]
            if rectangle is None or bounds is None:
                return await self.finish(client, "Deck checks paused: card geometry is unavailable")
            x, y = rectangle.center()
            if not bounds.x1 <= x <= bounds.x2 or not bounds.y1 <= y <= bounds.y2:
                return await self.finish(client, "Deck checks paused: card geometry does not match the displayed list")
        else:
            bounds, width, height, spacing = (values[k] for k in ("rectangle", "card_width", "card_height", "spacing"))
            scale = await safe_read(client.render_context.ui_scale)
            if bounds is None or not scale or not width or not height or spacing is None:
                return await self.finish(client, "Deck checks paused: deck geometry is unavailable")
            stride_x, stride_y = (width + spacing) * scale, (height + spacing) * scale
            columns = max(1, math.floor((bounds.x2 - bounds.x1 + spacing * scale) / stride_x))
            x = bounds.x1 + (index % columns) * stride_x + width * scale / 2
            y = bounds.y1 + (index // columns) * stride_y + height * scale / 2
            if y > bounds.y2:
                return await self.finish(client, "Deck checks paused: the deck slot is not visible")
        self.status(f"Updating deck: {action} {values['spells'][index].name}")
        expected = current.copy()
        expected[identifier] += 1 if action == "add" else -1
        expected = +expected
        async with client.mouse_handler:
            await client.mouse_handler.click(round(x), round(y))
        for _ in range(8):
            await asyncio.sleep(0.2)
            if await client.is_loading() or await client.in_battle():
                self.opened = False
                return True
            after = deck_counts(await safe_read(deck.deck_contents))
            if after == expected:
                return True
        self.failed = True
        return await self.finish(client, "Deck editing paused: the game did not confirm the expected card change")

    async def finish(self, client, message):
        self.status(message)
        if not await client.is_loading() and not await client.in_battle():
            ui = await visible_ui(client)
            if control(ui, "DeckConfigurationWindow"):
                await client.send_key(Keycode.P, 0.04)
                await asyncio.sleep(0.25)
        self.opened = False
        return True


class SchoolVisits:
    """Visit the player's teacher using learned routes, then resume questing."""

    def __init__(self, status):
        self.status = status
        self.checked = set()
        self.trip = None
        self.search = ZoneSearch()

    async def step(self, client, ui, mapper, teleport, active, trainer):
        if mapper is None or not active():
            return False
        zone = await safe_read(client.zone_name)
        if self.trip is None:
            if await client.is_in_dialog() or any(control(ui, name) for name in
                                                ("DeckConfigurationWindow", "NPCTrainingGUI")):
                return False
            identity = await safe_read(client.client_object.global_id_full)
            level = await safe_read(client.stats.reference_level)
            school = school_name(await safe_read(client.stats.school_id))
            if identity is None or level not in TRAINING_LEVELS or school not in TEACHERS:
                return False
            key = (identity, level)
            if key in self.checked or not zone or not zone.startswith("WizardCity/"):
                return False
            zones = mapper.dump()["zones"]
            suffix = "WC_Ravenwood" if school in ("death", "balance") else "WC_School" + school.title()
            destination = next((z for z in zones if z.endswith("/" + suffix)), None)
            if destination is None:
                return False  # A classroom must be observed before using its route.
            hub = next((z for z in zones if z.endswith("/WC_Hub")), None)
            outbound = mapper.find_path(zone, destination)
            from_hub = mapper.find_path(hub, destination) if hub else None
            if (outbound is None and from_hub is None) or mapper.find_path(destination, zone) is None:
                return False  # Do not abandon a quest without a known return route.
            position = await safe_read(client.body.position)
            if position is None:
                return False
            self.checked.add(key)
            self.trip = dict(zone=zone, position=position, destination=destination,
                             teacher=TEACHERS[school], hub=hub, phase="outbound", attempts=0)
            trainer.finished = False
            trainer.inspected.clear()
            self.status(f"Visiting {TEACHERS[school]} to check free level-{level} school spells")
        trip = self.trip
        if trip["phase"] == "outbound":
            route = mapper.find_path(zone, trip["destination"])
            if route is None:
                await client.send_key(Keycode.END, 0.04)
                for _ in range(60):
                    await asyncio.sleep(0.2)
                    if not active() or await client.in_battle():
                        return True
                    if not await client.is_loading() and await safe_read(client.zone_name) == trip["hub"]:
                        await asyncio.sleep(0.7)
                        break
                zone = await safe_read(client.zone_name)
                route = mapper.find_path(zone, trip["destination"])
            if route is None or not await walk_zone_path(client, route, self.status, active, mapper.record_transition):
                trip["phase"] = "return"
            else:
                trip["phase"] = "teacher"
            return True
        if trip["phase"] == "teacher":
            if trainer.finished or trainer.stopped:
                trip["phase"] = "return"
                return True
            services = control(ui, "NPCServicesWin")
            if services:
                button = next((n for n in services.walk() if n.enabled and
                               n.name.startswith("OptionButton") and plain_text(n.text).casefold().startswith("train")), None)
                if button:
                    await click(client, button.window)
                    return True
                cancel = next((n for n in services.walk() if n.enabled and n.name in ("Cancel", "Exit", "Back")), None)
                if cancel:
                    await click(client, cancel.window)
                trip["phase"] = "return"
                return True
            if await client.is_in_dialog():
                # The existing story-dialog handler may finish a greeting, but
                # still accepts only a verified mainline offer.
                return False
            trip["attempts"] += 1
            if trip["attempts"] > 8:
                trip["phase"] = "return"
                self.status("Teacher interaction unavailable; returning to the quest")
                return True
            target = None
            neighbors = []
            for entity in await safe_read(client.get_base_entity_list, []):
                name = (await safe_read(entity.display_name, "") or "").casefold()
                object_name = (await safe_read(entity.object_name, "") or "").casefold()
                teacher = name == trip["teacher"].casefold() and "standin" not in object_name
                body = await safe_read(entity.actor_body)
                pos = await safe_read(body.position) if body else None
                if teacher and pos is None and await safe_read(entity.fetch_npc_behavior_template) is not None:
                    pos = await safe_read(entity.location)
                if pos is None:
                    continue
                neighbors.append(pos)
                if teacher:
                    target = pos
            player = await safe_read(client.body.position)
            if player is None:
                return True
            if target is None:
                visited = trip.setdefault("searched", [player])
                point = await self.search.point(zone, trip["position"] if zone == trip["zone"] else visited[0], visited)
                if point is None:
                    trip["phase"] = "return"
                    self.status("Teacher was not found on the loaded walkable paths; returning to the quest")
                    return True
                visited.append(point)
                self.status(f"Searching the school grounds for {trip['teacher']}")
                await teleport(type(point)(point.x, point.y, point.z + 10), player)
                await asyncio.sleep(0.5)
                return True
            stage = approach_point(player, target, neighbors, radius=180, attempt=trip["attempts"] - 1)
            if await teleport(stage, player) == "success":
                close = approach_point(player, target, neighbors, radius=80, attempt=trip["attempts"] - 1)
                try:
                    await asyncio.wait_for(client.goto(close.x, close.y), 3)
                except TimeoutError:
                    pass
                await asyncio.sleep(0.3)
                fresh = await visible_ui(client)
                prompt = prompt_name(fresh)
                if prompt and trip["teacher"].casefold() in plain_text(prompt).casefold():
                    await client.send_key(Keycode.X, 0.04)
                    await asyncio.sleep(0.5)
            return True
        if await client.is_in_dialog():
            return False
        route = mapper.find_path(zone, trip["zone"])
        if route is None:
            self.status("School visit paused: the return route is unavailable")
            return True
        if not await walk_zone_path(client, route, self.status, active, mapper.record_transition):
            self.status("Retrying the recorded return route after school training")
            return True
        current = await safe_read(client.body.position)
        if current and active():
            await teleport(trip["position"], current)
        self.status("School visit finished; resuming the main quest")
        self.trip = None
        return True
