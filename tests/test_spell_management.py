import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from wizwalker.utils import Rectangle
from quest_state import UiNode

from spell_management import SchoolSpell, SchoolTrainer, DeckManager, compact_deck, number, deck_counts, next_deck_change, summarize_effects, LiveSpellListControl, LiveDeckListControl


class CardLayoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_verified_list_layouts_select_the_matching_vector(self):
        for cls, offset, dimensions, stride in ((LiveSpellListControl, 0x30C, (97, 148), 0xA8),
                                                (LiveDeckListControl, 0x2A4, (33, 33), 0x28)):
            for shift in (0, 72):
                widget = object.__new__(cls)
                values = {offset + shift: dimensions[0], offset + shift + 4: dimensions[1]}
                widget.read_value_from_offset = AsyncMock(side_effect=lambda address, _: values.get(address, 0))
                widget.read_inlined_vector = AsyncMock(return_value=[])
                self.assertEqual(await widget.layout_shift(), shift)
                await widget.spell_entries()
                self.assertEqual(widget.read_inlined_vector.await_args.args[:2], (0x280 + shift, stride))

    async def test_unrecognized_layout_never_dereferences_entries(self):
        widget = object.__new__(LiveSpellListControl)
        widget.read_value_from_offset = AsyncMock(return_value=455)
        widget.read_inlined_vector = AsyncMock()
        with self.assertRaises(ValueError):
            await widget.spell_entries()
        widget.read_inlined_vector.assert_not_awaited()


def spell(identifier, school="death", pips=1, kind="damage", amount=100,
          target="enemy_single", copies=3, accuracy=85):
    return SchoolSpell(identifier, str(identifier), school, pips, accuracy,
                       [dict(type=kind, amount=amount, target=target)], copies)


class DeckPolicyTests(unittest.TestCase):
    def test_random_damage_children_are_read_as_an_attack(self):
        effects = summarize_effects([dict(kind="RandomSpellEffect", effect_type="invalid_spell_effect",
            effect_param=-1, effect_target="enemy_single", effects_list=[
                dict(effect_type="damage", effect_param=65, effect_target="enemy_single"),
                dict(effect_type="damage", effect_param=95, effect_target="enemy_single")])])
        self.assertIn(dict(type="damage", amount=80, target="enemy_single"), effects)
        self.assertEqual(compact_deck([SchoolSpell(1, "Attack", "Death", 1, 85, effects)], "death"), {1: 3})

    def test_deck_adds_useful_cards_before_removing_extras(self):
        self.assertEqual(next_deck_change({1: 3, 9: 2}, {1: 2, 2: 3}), ("add", 2))
        self.assertEqual(next_deck_change({1: 3, 2: 3, 9: 2}, {1: 2, 2: 3}), ("remove", 1))
        self.assertIsNone(next_deck_change({1: 2, 2: 3}, {1: 2, 2: 3}))

    def test_incomplete_or_pre_enchanted_deck_is_not_modified(self):
        self.assertIsNone(deck_counts(None))
        self.assertIsNone(deck_counts([dict(template_id=1, quantity=1, enchantment=2)]))
        self.assertIsNone(deck_counts([dict(template_id=1, quantity=None)]))
        self.assertEqual(deck_counts([dict(template_id=1, quantity=2), dict(template_id=1, quantity=1)]), {1: 3})

    def test_two_pip_attack_blade_and_heal_have_room(self):
        cards = [spell(1), spell(2, pips=2, amount=300),
                 spell(3, pips=3, kind="steal_health", amount=350),
                 spell(4, pips=0, kind="modify_outgoing_damage", amount=35, target="self"),
                 spell(5, pips=0, kind="modify_incoming_damage", amount=25),
                 spell(6, school="life", pips=2, kind="heal", amount=400, target="friendly_single"),
                 spell(7, school="fire", amount=500)]
        plan = compact_deck(cards, "Death")
        self.assertEqual(plan, {2: 3, 1: 2, 3: 2, 4: 2, 5: 1, 6: 1})
        self.assertNotIn(7, plan)

    def test_unknown_attack_metadata_preserves_existing_deck(self):
        self.assertEqual(compact_deck([spell(1, pips=None)], "death"), {})
        self.assertEqual(compact_deck([spell(1, school="fire")], "death"), {})

    def test_capacity_and_copy_limits_are_respected(self):
        cards = [spell(1, copies=2), spell(2, pips=2, amount=400, copies=3),
                 spell(3, pips=0, kind="modify_outgoing_damage", amount=30)]
        plan = compact_deck(cards, "death", capacity=5)
        self.assertEqual(sum(plan.values()), 5)
        self.assertLessEqual(plan[1], 2)
        self.assertLessEqual(plan[2], 3)

    def test_shield_is_not_selected_as_a_damage_buff(self):
        plan = compact_deck([spell(1), spell(2, pips=0, kind="modify_incoming_damage", amount=-80)], "death")
        self.assertNotIn(2, plan)

    def test_training_cost_requires_an_exact_displayed_integer(self):
        self.assertEqual(number("<center>0</center>"), 0)
        self.assertIsNone(number("Free"))
        self.assertIsNone(number("Training Points: 0"))


if __name__ == "__main__":
    unittest.main()


class SpellControlTests(unittest.IsolatedAsyncioTestCase):
    def training_ui(self, cost="0", points="2"):
        train = SimpleNamespace(is_control_grayed=AsyncMock(return_value=False))
        raw = SimpleNamespace(hook_handler=object(), read_base_address=AsyncMock(return_value=123))
        row = SimpleNamespace(maybe_checked=AsyncMock(return_value=True))
        panel = UiNode("NPCTrainingGUI", "Window", "", True, object(), [
            UiNode("Preview", "GraphicalSpellWindow", "", True, raw),
            UiNode("TrainButton", "ControlButton", "", True, train),
            UiNode("Option_1", "ControlCheckBox", "", True, row, [
                UiNode("Cost", "ControlText", cost, True, object())]),
            UiNode("TrainingPoints", "ControlText", points, True, object())])
        return panel, train

    def client(self, learned):
        book = SimpleNamespace(trained_spell_ids=AsyncMock(side_effect=learned))
        return SimpleNamespace(stats=SimpleNamespace(school_id=AsyncMock(return_value=78318724),
            reference_level=AsyncMock(return_value=5)),
            client_object=SimpleNamespace(try_get_spellbook_behavior=AsyncMock(return_value=book)),
            is_loading=AsyncMock(return_value=False), in_battle=AsyncMock(return_value=False))

    async def test_only_own_school_zero_cost_card_is_trained(self):
        for school, cost, allowed in (("death", "0", True), ("fire", "0", False),
                                     ("death", "1", False), ("death", "", False)):
            ui, train = self.training_ui(cost)
            trainer = SchoolTrainer(lambda _: None)
            client = self.client([[], [1]])
            with patch("spell_management.DynamicGraphicalSpellWindow", return_value=SimpleNamespace(graphical_spell=AsyncMock(return_value=object()))), \
                 patch("spell_management.read_spell", AsyncMock(return_value=spell(1, school=school))), \
                 patch("spell_management.visible_ui", AsyncMock(return_value=ui)), \
                 patch("spell_management.click", AsyncMock()) as click, \
                 patch("spell_management.asyncio.sleep", AsyncMock()):
                await trainer.step(client, ui)
                self.assertEqual(any(call.args[1] is train for call in click.await_args_list), allowed)

    async def test_unconfirmed_training_is_not_clicked_repeatedly(self):
        ui, _ = self.training_ui()
        trainer = SchoolTrainer(lambda _: None)
        client = self.client([[]] * 20)
        with patch("spell_management.DynamicGraphicalSpellWindow", return_value=SimpleNamespace(graphical_spell=AsyncMock(return_value=object()))), \
             patch("spell_management.read_spell", AsyncMock(return_value=spell(1))), \
             patch("spell_management.visible_ui", AsyncMock(return_value=ui)), \
             patch("spell_management.click", AsyncMock()) as click, \
             patch("spell_management.asyncio.sleep", AsyncMock()):
            await trainer.step(client, ui)
            await trainer.step(client, ui)
            self.assertTrue(trainer.stopped)
            self.assertEqual(click.await_count, 1)

    async def test_deck_rejects_an_unexpected_change_and_stops_editing(self):
        ui = UiNode("DeckConfigurationWindow", "Window", "", True, object(), [
            UiNode("AllPageSpellList", "SpellListControl", "", True, object())])
        library = dict(spells=[spell(1)], start_index=0,
                       entry_rectangles=[Rectangle(250, 250, 350, 400)], rectangle=Rectangle(200, 200, 400, 600))
        deck = SimpleNamespace(deck_contents=AsyncMock(side_effect=[[]] + [[dict(template_id=99, quantity=1)]] * 8))
        client = SimpleNamespace(stats=SimpleNamespace(school_id=AsyncMock(return_value=78318724)),
            client_object=SimpleNamespace(try_get_deck_behavior=AsyncMock(return_value=deck)),
            is_loading=AsyncMock(return_value=False), in_battle=AsyncMock(return_value=False),
            mouse_handler=AsyncMock(), send_key=AsyncMock())
        manager = DeckManager(lambda _: None)
        manager.opened = True
        with patch("spell_management.deck_lists", AsyncMock(return_value={"AllPageSpellList": library, "CardsInDeck": dict(spells=[])})), \
             patch("spell_management.visible_ui", AsyncMock(return_value=ui)), \
             patch("spell_management.asyncio.sleep", AsyncMock()):
            await manager.step(client, ui)
            self.assertTrue(manager.failed)
            await manager.step(client, ui)
            self.assertEqual(client.mouse_handler.click.await_count, 1)
