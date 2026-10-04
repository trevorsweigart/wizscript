"""Behavioral checks for API boundaries, fresh state, and turn handling."""

import asyncio
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from wizwalker.memory.memory_objects.enums import DuelPhase

from combat import JevCombat
from combat_actions import CombatAction, available_actions, execute
from combat_history import CombatHistory
from combat_state import CombatSnapshot, CombatStateCollector, Read, read_effect
from jev_client import JevClient, JevDecision, JevError
from jev_credentials import load_api_key, save_api_key


def card(identifier, name="Fire Cat", target="enemy_single", *, enchanted=0, castable=True, no_discard=False):
    return {"id": identifier, "name": name, "spell_id": identifier, "template_id": identifier,
            "window_id": identifier, "enchantment": enchanted, "is_castable": castable,
            "template": {"type_name": "Enchantment" if target == "spell" else "Damage",
                         "name": name, "no_discard": no_discard},
            "effects": [{"effect_type": "modify_card_damage" if target == "spell" else "damage",
                         "effect_param": 100, "effect_target": target}]}


def member(identifier, relation, *, health=500, name="Imp"):
    return {"id": identifier, "name": name, "owner_id_full": identifier, "team_id": 0 if relation != "enemy" else 1,
            "relation": relation, "is_self": relation == "self", "is_minion": False,
            "player_health": health, "untargetable": False, "exit_combat": False, "graveyard": [],
            "stats": {"max_hitpoints": 500, "current_mana": 100, "max_mana": 100},
            "pips": {"generic_pips": 1, "power_pips": 0, "shadow_pips": 0}}


def snapshot():
    state = {"duel": {"duel_id_full": 1, "round_num": 1, "alt_turn_counter": 0, "duel_phase": "planning"},
             "player_id": "player", "hand": [card("hit"), card("enchant", "Strong", "spell"), card("junk", castable=False)],
             "combatants": [member("player", "self", name="Wizard"), member("enemy", "enemy")],
             "buttons": {"pass": True, "flee": True, "draw": False},
             "global_effect": None, "battlefield_effects": []}
    return CombatSnapshot(state, cards={"hit": object(), "enchant": object(), "junk": object()},
                          members={"player": object(), "enemy": object()}, buttons={"pass": object(), "flee": object()})


class Collector:
    def __init__(self):
        self.current = snapshot()

    async def collect(self, client, **kwargs):
        return copy.deepcopy(self.current)

    async def hand(self, client):
        return await self.collect(client)


class Mouse:
    def __init__(self):
        self.entries = 0
        self.click_window = AsyncMock()

    async def __aenter__(self):
        self.entries += 1
        return self

    async def __aexit__(self, *args):
        pass


def client_for(collector):
    def fact(name):
        async def get():
            return collector.current.state["duel"][name]
        return get
    duel = SimpleNamespace(duel_id_full=fact("duel_id_full"), round_num=fact("round_num"),
                           alt_turn_counter=fact("alt_turn_counter"), participant_list=AsyncMock(return_value=[]),
                           duel_phase=AsyncMock(return_value=DuelPhase.planning))
    return SimpleNamespace(is_loading=AsyncMock(return_value=False), in_battle=AsyncMock(return_value=True),
                           duel=duel, mouse_handler=Mouse(), send_key=AsyncMock())


class ChoiceAI:
    def __init__(self, kinds, collector=None):
        self.kinds = iter(kinds)
        self.calls = []
        self.close = AsyncMock()
        self.collector = collector

    async def choose(self, state, actions):
        self.calls.append(copy.deepcopy(state))
        action = next(a for a in actions if a.kind == next_kind) if (next_kind := next(self.kinds)) else actions[0]
        return JevDecision(action.label, 0.99, {a.label: float(a == action) for a in actions}, "jev-test", {})


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_documented_choice_shape_and_authentication(self):
        requests = []
        def respond(request):
            requests.append(request)
            body = json.loads(request.content)
            options = body["questions"]["action"]["criteria"]
            return httpx.Response(200, json={"model": "jev-test", "usage": {}, "answers": {"action": {
                "type": "choice", "choice": "pass", "confidence": 0.99,
                "probabilities": {name: float(name == "pass") for name in options}}}})
        ai = JevClient(api_key="test-key", transport=httpx.MockTransport(respond))
        try:
            choice = await ai.choose({"hand": []}, [CombatAction("pass", "pass", {}), CombatAction("flee", "flee", {})])
            self.assertEqual(choice.choice, "pass")
            self.assertEqual(requests[0].headers["Authorization"], "Bearer test-key")
            self.assertEqual(str(requests[0].url), "https://api.typesafe.ai/v1/systemone")
            body = json.loads(requests[0].content)
            self.assertEqual(body["questions"]["action"]["type"], "choice")
            self.assertIn("Wizard101", body["questions"]["action"]["instructions"])
        finally:
            await ai.close()

    async def test_bad_choice_and_probability_distribution_are_rejected(self):
        for choice, probabilities in (("invented spell", {"pass": 1}), ("pass", {"pass": float("nan")})):
            def respond(request):
                if choice == "pass":
                    return httpx.Response(200, content=b'{"answers":{"action":{"type":"choice","choice":"pass","confidence":0.9,"probabilities":{"pass":NaN}}},"model":"jev-test"}')
                return httpx.Response(200, json={"answers": {"action": {"type": "choice", "choice": choice,
                    "confidence": 0.9, "probabilities": probabilities}}, "model": "jev-test"})
            ai = JevClient(api_key="test-key", transport=httpx.MockTransport(respond))
            try:
                with self.assertRaises(JevError):
                    await ai.choose({}, [CombatAction("pass", "pass", {})])
            finally:
                await ai.close()

    async def test_errors_do_not_echo_key_or_response_body(self):
        secret = "secret-do-not-print"
        ai = JevClient(api_key=secret, transport=httpx.MockTransport(lambda request: httpx.Response(401, text=secret)))
        try:
            with self.assertRaises(JevError) as error:
                await ai.choose({}, [CombatAction("pass", "pass", {})])
            self.assertNotIn(secret, str(error.exception))
            self.assertIn("401", str(error.exception))
        finally:
            await ai.close()

    async def test_option_limit_uses_jev_for_group_and_final_choices(self):
        sizes = []
        def respond(request):
            options = json.loads(request.content)["questions"]["action"]["criteria"]
            sizes.append(len(options))
            selected = next(iter(options))
            return httpx.Response(200, json={"model": "jev-test", "answers": {"action": {
                "type": "choice", "choice": selected, "confidence": 1,
                "probabilities": {name: float(name == selected) for name in options}}}})
        ai = JevClient(api_key="test-key", transport=httpx.MockTransport(respond))
        try:
            await ai.choose({}, [CombatAction(str(i), "cast", {}) for i in range(256)])
            self.assertEqual(sizes, [255, 1, 2])
        finally:
            await ai.close()


class EngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {"WIZSCRIPT_DATA_DIR": self.directory.name})
        self.environment.start()

    async def asyncTearDown(self):
        self.environment.stop()
        self.directory.cleanup()

    async def test_enchant_then_discard_then_cast_gets_three_fresh_choices(self):
        collector = Collector()
        ai = ChoiceAI(["enchant", "discard", "cast"])
        client = client_for(collector)
        engine = JevCombat(ai=ai, collector=collector)
        async def apply(action, fresh, game, reader):
            if action.kind == "enchant":
                collector.current.state["hand"] = [c for c in collector.current.state["hand"] if c["id"] != "enchant"]
                collector.current.state["hand"][0]["enchantment"] = 123
            elif action.kind == "discard":
                collector.current.state["hand"] = [c for c in collector.current.state["hand"] if c["id"] != action.card_id]
            return True
        # Discard the junk, leaving the enchanted hit for the terminal pick.
        original_choose = ai.choose
        async def choose(state, options):
            if len(ai.calls) == 1:
                options = [a for a in options if a.kind != "discard" or a.card_id == "junk"]
            return await original_choose(state, options)
        ai.choose = choose
        with patch("combat.execute", side_effect=apply) as inputs:
            for _ in range(3):
                self.assertEqual((await engine.tick(client))[0], "acted")
            self.assertEqual((await engine.tick(client))[0], "waiting")
            self.assertEqual(inputs.await_count, 3)
        self.assertEqual(len(ai.calls), 3)
        self.assertEqual(ai.calls[1]["hand"][0]["enchantment"], 123)
        self.assertEqual([c["id"] for c in ai.calls[2]["hand"]], ["hit"])
        self.assertEqual([e["kind"] for e in engine.history.events], ["enchant", "discard", "cast"])

    async def test_stale_hand_health_global_and_round_never_click(self):
        mutations = [lambda s: s["hand"][0].update(enchantment=99),
                     lambda s: s["combatants"][1].update(player_health=10),
                     lambda s: s.update(global_effect={"effect_type": "modify_outgoing_damage"}),
                     lambda s: s["duel"].update(round_num=2)]
        for mutation in mutations:
            collector = Collector()
            ai = ChoiceAI(["cast"])
            original = ai.choose
            async def choose(state, options):
                decision = await original(state, options)
                mutation(collector.current.state)
                return decision
            ai.choose = choose
            engine = JevCombat(ai=ai, collector=collector)
            with patch("combat.execute", new_callable=AsyncMock) as inputs:
                self.assertEqual((await engine.tick(client_for(collector)))[0], "waiting")
                inputs.assert_not_awaited()

    async def test_cancel_during_api_request_sends_no_game_input(self):
        collector = Collector()
        started = asyncio.Event()
        ai = ChoiceAI([])
        async def choose(*args):
            started.set()
            await asyncio.Event().wait()
        ai.choose = choose
        engine = JevCombat(ai=ai, collector=collector)
        client = client_for(collector)
        task = asyncio.create_task(engine.tick(client))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(client.mouse_handler.entries, 0)

    async def test_api_failure_never_falls_back_to_passing_or_casting(self):
        collector = Collector()
        ai = ChoiceAI([])
        ai.choose = AsyncMock(side_effect=JevError("service unavailable"))
        engine = JevCombat(ai=ai, collector=collector)
        with patch("combat.execute", new_callable=AsyncMock) as inputs:
            with self.assertRaises(JevError):
                await engine.tick(client_for(collector))
            inputs.assert_not_awaited()

    async def test_round_submission_is_not_repeated_while_planning(self):
        collector = Collector()
        ai = ChoiceAI(["pass"])
        engine = JevCombat(ai=ai, collector=collector)
        with patch("combat.execute", new_callable=AsyncMock, return_value=True) as inputs:
            client = client_for(collector)
            await engine.tick(client)
            await engine.tick(client)
            self.assertEqual(inputs.await_count, 1)
            self.assertEqual(len(ai.calls), 1)

    async def test_real_discard_executor_requires_observed_hand_change(self):
        collector = Collector()
        before = collector.current
        async def discard(**kwargs):
            collector.current = copy.deepcopy(before)
            collector.current.state["hand"] = [c for c in before.state["hand"] if c["id"] != "junk"]
        before.cards["junk"] = SimpleNamespace(discard=discard)
        action = next(a for a in available_actions(before) if a.kind == "discard" and a.card_id == "junk")
        client = client_for(collector)
        self.assertTrue(await execute(action, before, client, collector))
        self.assertEqual(client.mouse_handler.entries, 1)

    async def test_real_enchant_executor_checks_target_change(self):
        collector = Collector()
        before = collector.current
        async def enchant(target, **kwargs):
            collector.current = copy.deepcopy(before)
            collector.current.state["hand"] = [c for c in collector.current.state["hand"] if c["id"] != "enchant"]
            collector.current.state["hand"][0]["enchantment"] = 123
            collector.current.state["hand"][0]["effects"][0]["effect_param"] += 100
        before.cards["enchant"] = SimpleNamespace(cast=enchant)
        action = next(a for a in available_actions(before) if a.kind == "enchant" and a.target_ids == ("hit",))
        self.assertTrue(await execute(action, before, client_for(collector), collector))

    async def test_last_moment_round_change_prevents_mouse_click(self):
        collector = Collector()
        before = copy.deepcopy(collector.current)
        collector.current.state["duel"]["round_num"] = 2
        client = client_for(collector)
        action = next(a for a in available_actions(before) if a.kind == "pass")
        self.assertFalse(await execute(action, before, client, collector))
        client.mouse_handler.click_window.assert_not_awaited()


class StateTests(unittest.IsolatedAsyncioTestCase):
    async def test_production_collector_exports_health_pips_and_spell_effects(self):
        def memory(**facts):
            return SimpleNamespace(**{name: AsyncMock(return_value=value) for name, value in facts.items()})
        effect = memory(effect_type="damage", effect_param=125, effect_target="enemy_single", num_rounds=0)
        spell = memory(template_id=99, spell_id=77, enchantment=0, accuracy=85, magic_school_id=2343174,
                       spell_template=memory(name="FireCat", type_name="Damage", no_discard=False),
                       pip_cost=memory(spell_rank=1, shadow_pips=0, fire_pips=0, is_xpip_spell=False),
                       spell_effects=[effect])
        combat_card = memory(get_graphical_spell=spell, is_castable=True)
        combat_card._spell_window = memory(read_base_address=42, maybe_checked=False)
        participants = []
        combat_members = []
        for owner, team, health in ((100, 0, 350), (200, 1, 220)):
            stats = memory(current_hitpoints=500, max_hitpoints=500, current_mana=80, max_mana=100,
                           dmg_bonus_percent_all=0.4)
            participant = memory(owner_id_full=owner, team_id=team, player_health=health, max_player_health=500, is_minion=False,
                                 untargetable=False, exit_combat=False, game_stats=stats,
                                 pip_count=memory(generic_pips=1, power_pips=2, shadow_pips=1, fire_pips=1),
                                 hanging_effects=[], public_hanging_effects=[], aura_effects=[],
                                 shadow_spell_effects=[], death_activated_effects=[], delay_cast_effects=[],
                                 intercept_effect=None, polymorph_effect=None)
            participants.append(participant)
            combat_members.append(memory(get_participant=participant, name=f"Combatant {owner}"))
        game = memory(is_loading=False)
        game.duel = memory(duel_id_full=1, round_num=1, alt_turn_counter=0, duel_phase=DuelPhase.planning,
                           combat_resolver=memory(global_effect=effect, battlefield_effects=[]))
        game.client_object = memory(global_id_full=100)
        game.root_window = memory(get_windows_with_name=[])
        handler = memory(get_cards=[combat_card], get_members=combat_members)
        with patch("combat_state.CombatHandler", return_value=handler):
            result = await CombatStateCollector().collect(game, include_decks=False)
        self.assertEqual(result.state["hand"][0]["accuracy"], 85)
        self.assertEqual(result.state["hand"][0]["pip_cost"]["spell_rank"], 1)
        self.assertEqual(result.state["hand"][0]["effects"][0]["effect_param"], 125)
        self.assertEqual(result.state["combatants"][0]["stats"]["max_mana"], 100)
        self.assertEqual(result.state["combatants"][0]["pips"]["fire_pips"], 1)
        self.assertEqual(result.state["combatants"][1]["relation"], "enemy")
        self.assertEqual(result.state["combatants"][1]["player_health"], 220)
        self.assertEqual(result.state["combatants"][1]["health"],
                         {"current": 220, "maximum": 500, "source": "combat_participant"})
        self.assertEqual(result.state["combatants"][1]["stats"]["current_hitpoints"], 500)
        self.assertEqual(result.state["global_effect"]["effect_param"], 125)

    async def test_unreadable_values_are_null_not_zero(self):
        reader = Read()
        stats = SimpleNamespace(current_hitpoints=AsyncMock(side_effect=RuntimeError("loading")),
                                max_hitpoints=AsyncMock(return_value=500))
        data = await reader.fields(stats, ("current_hitpoints", "max_hitpoints"), "stats")
        self.assertIsNone(data["current_hitpoints"])
        self.assertEqual(data["max_hitpoints"], 500)
        self.assertIn("stats.current_hitpoints", reader.unavailable[0])

    async def test_nested_effects_keep_target_damage_and_rounds(self):
        child = SimpleNamespace(effect_type=AsyncMock(return_value="damage_over_time"),
                                effect_param=AsyncMock(return_value=400), effect_target=AsyncMock(return_value="enemy_single"),
                                num_rounds=AsyncMock(return_value=3))
        parent = SimpleNamespace(effects_list=AsyncMock(return_value=[child]))
        result = await read_effect(Read(), parent)
        self.assertEqual(result["effects_list"][0]["effect_param"], 400)
        self.assertEqual(result["effects_list"][0]["num_rounds"], 3)

    def test_target_choices_are_specific_and_unknown_cards_do_not_cast(self):
        state = snapshot()
        state.state["combatants"].append(member("enemy2", "enemy", name="Imp"))
        actions = available_actions(state)
        casts = [a for a in actions if a.kind == "cast"]
        self.assertEqual({a.target_ids for a in casts}, {("enemy",), ("enemy2",)})
        self.assertEqual(len({a.label for a in actions}), len(actions))
        state.state["hand"][0]["effects"] = None
        self.assertFalse(any(a.kind == "cast" for a in available_actions(state)))

    def test_heals_target_allies_and_aoe_and_global_have_no_single_target(self):
        state = snapshot()
        state.state["combatants"].append(member("ally", "ally", name="Ally"))
        state.state["hand"] = [card("heal", "Heal", "friendly_single"),
                               card("aoe", "AOE", "enemy_team"), card("bubble", "Bubble", "target_global")]
        casts = [a for a in available_actions(state) if a.kind == "cast"]
        self.assertEqual({a.target_ids for a in casts if a.card_id == "heal"}, {("player",), ("ally",)})
        self.assertTrue(all(not a.target_ids for a in casts if a.card_id in ("aoe", "bubble")))

    def test_localization_and_optional_decks_do_not_invalidate_decisions(self):
        state = snapshot()
        fresh = copy.deepcopy(state)
        fresh.state["hand"][0]["name"] = "Localized Fire Cat"
        fresh.state["hand"][0]["template"]["description_text"] = "Deal 100 damage"
        fresh.state["combatants"][0]["remaining_deck"] = [{"template_id": 1}]
        self.assertEqual(state.fingerprint(), fresh.fingerprint())

    def test_history_does_not_claim_graveyard_cards_are_confirmed_casts(self):
        history = CombatHistory()
        state = snapshot()
        history.bind(state)
        state.state["combatants"][1]["graveyard"] = [{"template_id": 99, "enchantment": 0}]
        history.bind(state)
        history.bind(state)
        self.assertEqual(len(history.events), 1)
        self.assertFalse(history.events[0]["confirmed_cast"])
        self.assertIsNone(history.as_dict()["confirmed_enemy_casts"])
        state.state["duel"]["duel_id_full"] = 2
        state.state["combatants"][1]["graveyard"] = []
        history.bind(state)
        self.assertEqual(history.events, [])


@unittest.skipUnless(os.name == "nt", "Windows DPAPI")
class CredentialTests(unittest.TestCase):
    def test_saved_key_is_encrypted_and_not_plaintext(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "jev_credentials.json"
            with patch("jev_credentials.credentials_path", return_value=target), patch.dict(os.environ, {"TYPESAFE_API_KEY": ""}):
                save_api_key("test-secret-key")
                self.assertNotIn("test-secret-key", target.read_text())
                self.assertEqual(load_api_key(), "test-secret-key")


if __name__ == "__main__":
    unittest.main()
