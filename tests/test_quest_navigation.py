import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from wizwalker.memory.memory_objects.enums import ObjectType, WindowFlags
from wizwalker.utils import XYZ, Orient

from auto_quester import AutoQuester
from quest_dialog import dialog_action, DialogNavigator
from quest_navigation import MainQuestNavigator, main_quests
from quest_state import QuestEntry, QuestGoal, UiNode, visible_ui
from quest_npc import approach_point
from quest_objects import active_usage_goal, matches_object, defeat_target, sigil_approach
from entities import list_nearby_entities, is_health_wisp, is_mana_wisp
from quest_mobs import intercept_points, approach_enemy


def node(name, text="", kind="Window", children=(), enabled=True):
    return UiNode(name, kind, text, enabled, object(), list(children))


class DialogTests(unittest.TestCase):
    def test_wisps_match_world_and_zone_prefixes(self):
        self.assertTrue(is_health_wisp("WC_UW_WispHealth"))
        self.assertTrue(is_mana_wisp("KT_WispMana02"))
        self.assertFalse(is_health_wisp("QuestWispHealthItem"))
    def test_sigil_approach_rotates_with_the_entrance(self):
        import math
        target = sigil_approach(XYZ(10, 20, 3), Orient(0, 0, math.pi / 2))
        self.assertAlmostEqual(target.x, 10 + math.cos(math.radians(72)) * 270)
        self.assertAlmostEqual(target.y, 20 + math.sin(math.radians(72)) * 270)
        target = sigil_approach(XYZ(10, 20, 3), Orient(0, 0, 0))
        self.assertAlmostEqual(target.x, 10 + math.cos(math.radians(162)) * 270)
        self.assertAlmostEqual(target.y, 20 + math.sin(math.radians(162)) * 270)
    def test_defeat_objective_approaches_matching_moving_creature(self):
        target = (100, "Creature", "Scarlet Ghost", XYZ(100, 20, 0))
        rows = [(1, "Other", "Ghost", XYZ(1, 0, 0)), target,
                (200, "Creature", "Scarlet Ghost", XYZ(200, 0, 0))]
        ui = node("Root", children=[node("txtGoalName", "Defeat Scarlet Ghost and Collect Coil in Area (1 of 3)")])
        self.assertIs(defeat_target(rows, ui), target)
        ui.children[0].text = "Talk To Scarlet Ghost"
        self.assertIsNone(defeat_target(rows, ui))
    def test_ground_object_requires_active_main_goal(self):
        goal = QuestGoal(3, "Collect", kind="usage", tags=["Area_Object"])
        ui = node("Root", children=[node("txtGoalName", "Collect Gear in Somewhere (1 of 3)")])
        quests = [QuestEntry(2, mainline=True, goals=[goal])]
        self.assertEqual(active_usage_goal(quests, 2, ui), (goal, "Collect Gear in Somewhere (1 of 3)"))
        self.assertTrue(matches_object(goal, "Collect Gear in Somewhere (1 of 3)", "Area_Object", ""))
        self.assertTrue(matches_object(goal, "Collect Gear in Somewhere (1 of 3)", "", "Gear"))
        self.assertFalse(matches_object(goal, "Collect Gear in Somewhere (1 of 3)", "Other", "Other"))
        quests[0].mainline = False
        self.assertIsNone(active_usage_goal(quests, 2, ui))
    def test_approach_moves_away_from_neighboring_npc(self):
        target = XYZ(0, 0, 0)
        point = approach_point(XYZ(-100, 0, 0), target, [target, XYZ(-400, 0, 0)])
        self.assertEqual((point.x, point.y), (100, 0))
    def offer(self, starred=True):
        button = node("btnRight", "ACCEPT", "ControlButton")
        children = [node("txtName", "<center>A new story</center>", "ControlText")]
        if starred:
            children.append(node("LeftMainline", kind="ControlSprite"))
        ui = node("Root", children=[node("wndDialogMain", children=[button]),
                                    node("questInfoWindow", kind="QuestInfoWindow", children=children)])
        return ui, button

    def test_accepts_starred_offer_only(self):
        ui, button = self.offer()
        action, status = dialog_action(ui)
        self.assertIs(action, button)
        self.assertIn("A new story", status)
        self.assertIsNone(dialog_action(self.offer(False)[0])[0])

    def test_unknown_or_disabled_button_never_clicked(self):
        for text, enabled in [("BUY", True), ("MORE", False)]:
            ui = node("Root", children=[node("wndDialogMain", children=[
                node("btnRight", text, "ControlButton", enabled=enabled)])])
            self.assertIsNone(dialog_action(ui)[0])

    def test_multiple_offers_are_not_silently_accepted(self):
        ui, _ = self.offer()
        ui.children.append(self.offer()[0].children[1])
        self.assertIsNone(dialog_action(ui)[0])
        ui, _ = self.offer(False)
        ui.children.append(self.offer()[0].children[1])
        self.assertIsNone(dialog_action(ui)[0])

    def test_mainline_metadata_excludes_side_and_unknown(self):
        quests = [QuestEntry(1, mainline=True), QuestEntry(2, mainline=False), QuestEntry(3)]
        self.assertEqual([q.id for q in main_quests(quests)], [1])

    def test_route_choices_are_inspected_without_a_triton_restriction(self):
        options = [node("OptionButton", title, "ControlButton") for title in (
            "Putting Out the Fire!", "Trouble on TRITON AVENUE", "A Good Day to Cyclops")]
        ui = node("Root", children=[node("NPCServicesWin", children=options)])
        navigator = DialogNavigator()
        for option in options:
            self.assertIs(navigator.choose(ui)[0], option)
        self.assertIsNone(navigator.choose(ui)[0])
        navigator.begin()
        self.assertIs(navigator.choose(ui)[0], options[0])

    def test_unstarred_offer_can_be_declined_before_inspecting_another(self):
        ui, _ = self.offer(False)
        decline = node("btnLeft", "DECLINE", "ControlButton")
        ui.children[0].children.append(decline)
        self.assertIs(dialog_action(ui)[0], decline)

    def test_service_choice_matches_accepted_main_quest(self):
        options = [node("OptionButton", title, "ControlButton") for title in ("A side quest", "Current story")]
        ui = node("Root", children=[node("NPCServicesWin", children=options)])
        self.assertIs(dialog_action(ui, main_titles=["Current story"])[0], options[1])
        self.assertIs(dialog_action(ui)[0], options[0])

    def test_interception_leads_actual_motion_over_facing(self):
        stage, through = intercept_points(XYZ(0, 0, 7), XYZ(20, 0, 7), 0.2, 0)
        self.assertAlmostEqual(stage.x, 250)
        self.assertAlmostEqual(through.x, 0)
        self.assertEqual(stage.y, 0)
        self.assertEqual(stage.z, 7)

    def test_idle_interception_uses_game_facing_and_rejects_respawn_velocity(self):
        import math
        stage, through = intercept_points(XYZ(-10000, 0, 0), XYZ(0, 0, 0), .2, math.pi / 2)
        self.assertAlmostEqual(stage.x, -150)
        self.assertAlmostEqual(through.x, 100)


class LiveStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_unloaded_usage_object_without_destination_allows_arrow_navigation(self):
        quester = AutoQuester()
        goal = QuestGoal(3, "Use", destination="", kind="usage", tags=["Quest_Object"])
        client = SimpleNamespace(quest_id=AsyncMock(return_value=42), zone_name=AsyncMock(return_value="Other Area"))
        ui = node("Root", children=[node("txtGoalName", "Use Quest Object in Area")])
        with patch("auto_quester.quest_entries", AsyncMock(return_value=[QuestEntry(42, mainline=True, goals=[goal])])), \
             patch("auto_quester.visible_ui", AsyncMock(return_value=ui)), \
             patch("auto_quester.fetch_quest_position", AsyncMock(return_value=XYZ(10, 20, 30))), \
             patch("auto_quester.list_nearby_entities", AsyncMock(return_value=[])):
            self.assertFalse(await quester._collect_objective(client))

    async def test_completed_talk_target_does_not_block_usage_destination(self):
        quester = AutoQuester()
        quester._expected_talk_name = "Teacher"
        client = SimpleNamespace(quest_id=AsyncMock(return_value=42), zone_name=AsyncMock(return_value="School"))
        ui = node("Root", children=[node("txtGoalName", "Use Object in Street")])
        quests = [QuestEntry(42, mainline=True, goals=[QuestGoal(3, "Use", destination="Street", kind="usage")])]
        with patch("auto_quester.visible_ui", AsyncMock(return_value=ui)), \
             patch("auto_quester.quest_entries", AsyncMock(return_value=quests)):
            self.assertEqual(await quester._transition_destination(client), ("School", "Street"))
        self.assertIsNone(quester._expected_talk_name)

    async def test_unavailable_resources_survive_collect_handoffs_until_zone_changes(self):
        quester = AutoQuester()
        quester.set_resource_request_callback(lambda _: None)
        quester.set_heal_threshold_pct(50)
        quester.set_mana_threshold_pct(50)
        client = SimpleNamespace(zone_name=AsyncMock(return_value="Tower"), stats=SimpleNamespace(
            current_hitpoints=AsyncMock(return_value=10), max_hitpoints=AsyncMock(return_value=100),
            current_mana=AsyncMock(return_value=10), max_mana=AsyncMock(return_value=100)))
        def schedule(coroutine, loop):
            coroutine.close()
            return SimpleNamespace(done=lambda: True)
        with patch("auto_quester.asyncio.run_coroutine_threadsafe", side_effect=schedule):
            quester.start(client, object())
            await quester._observe_zone(client)
            quester.mark_resource_unavailable("health")
            quester.stop()
            quester.start(client, object())
            self.assertEqual(await quester._should_request_resource(client), "mana")
            quester.mark_resource_unavailable("mana")
            quester.stop()
            quester.start(client, object())
            await quester._observe_zone(client)
            self.assertIsNone(await quester._should_request_resource(client))
            client.zone_name.return_value = "Outside"
            await quester._observe_zone(client)
            self.assertEqual(await quester._should_request_resource(client), "health")
            quester.stop()

    async def test_defeat_retry_uses_quest_destination_for_tower_entry(self):
        quester = AutoQuester()
        client = SimpleNamespace(quest_id=AsyncMock(return_value=42), zone_name=AsyncMock(return_value="Outside"))
        ui = node("Root", children=[node("txtGoalName", "Defeat Boss in Area")])
        quests = [QuestEntry(42, mainline=True, goals=[QuestGoal(3, "Defeat", destination="Inside", kind="bounty")]),
                  QuestEntry(43, mainline=False, goals=[QuestGoal(4, "Defeat", destination="Other", kind="bounty")])]
        with patch("auto_quester.visible_ui", AsyncMock(return_value=ui)), \
             patch("auto_quester.quest_entries", AsyncMock(return_value=quests)):
            self.assertEqual(await quester._transition_destination(client), ("Outside", "Inside"))
            client.zone_name.return_value = "Inside"
            self.assertIsNone(await quester._transition_destination(client))

    async def test_talk_target_clears_previous_dungeon_approach(self):
        quester = AutoQuester()
        quester._entry_attempt_key = (1, 10, 20, 30)
        quester._entry_walk_target = XYZ(10, 20, 30)
        quester._talk_target = AsyncMock(return_value=XYZ(500, 0, 0))
        quester._entry_target = AsyncMock()
        quester._try_teleport = AsyncMock(return_value="success")
        with patch("auto_quester.fetch_position", AsyncMock(return_value=XYZ(0, 0, 0))):
            self.assertEqual(await quester._teleport_to_quest_safe(object()), "success")
        self.assertIsNone(quester._entry_attempt_key)
        self.assertIsNone(quester._entry_walk_target)
        quester._entry_target.assert_not_awaited()

    async def test_entry_interaction_avoids_repeated_keydown(self):
        quester = AutoQuester()
        quester._matches_interaction = AsyncMock(return_value=True)
        quester._transition_destination = AsyncMock(return_value=("Outside", "Inside"))
        quester._wait_for_transition = AsyncMock()
        client = SimpleNamespace(send_key=AsyncMock(), is_in_dialog=AsyncMock(return_value=False))
        with patch("auto_quester.asyncio.sleep", AsyncMock()):
            await quester._try_interact(client)
        self.assertEqual(client.send_key.await_count, 1)
        self.assertLess(client.send_key.await_args.args[1], 0.05)
        quester._wait_for_transition.assert_awaited_once_with(client, "Outside", "Inside")

    async def test_entry_wait_observes_zone_change_without_further_inputs(self):
        quester = AutoQuester()
        quester._running = True
        client = SimpleNamespace(zone_name=AsyncMock(side_effect=["Outside", "Outside", "Inside"]),
                                 is_loading=AsyncMock(return_value=False), in_battle=AsyncMock(return_value=False),
                                 is_in_dialog=AsyncMock(return_value=False), send_key=AsyncMock(), teleport=AsyncMock())
        with patch("auto_quester.asyncio.sleep", AsyncMock()) as sleep:
            await quester._wait_for_transition(client, "Outside", "Inside")
        self.assertEqual(sleep.await_count, 2)
        client.send_key.assert_not_awaited()
        client.teleport.assert_not_awaited()

    async def test_collection_continues_after_destination_clears_and_counter_advances(self):
        goal = QuestGoal(3, "Collect", destination="", kind="usage", tags=["Ground_Item"])
        quests = [QuestEntry(2, mainline=True, goals=[goal])]
        client = SimpleNamespace(quest_id=AsyncMock(return_value=2), zone_name=AsyncMock(return_value="Area"),
                                 in_battle=AsyncMock(return_value=False), is_loading=AsyncMock(return_value=False),
                                 is_in_dialog=AsyncMock(return_value=False), goto=AsyncMock(), send_key=AsyncMock())
        quester = AutoQuester()
        quester._try_teleport = AsyncMock(return_value="success")
        rows = [(10, "Ground_Item", "Item", XYZ(10, 0, 0)),
                (20, "Ground_Item", "Item", XYZ(20, 0, 0))]
        def hud(counter):
            return node("Root", children=[node("txtGoalName", f"Collect Item in Area ({counter} of 3)"),
                                          node("NPCRangeTxtTitle", "Item")])
        with patch("auto_quester.quest_entries", AsyncMock(return_value=quests)), \
             patch("auto_quester.list_nearby_entities", AsyncMock(return_value=rows)), \
             patch("auto_quester.fetch_position", AsyncMock(return_value=XYZ(0, 0, 0))), \
             patch("auto_quester.asyncio.sleep", AsyncMock()), \
             patch("auto_quester.visible_ui", AsyncMock(return_value=hud(0))) as observe:
            self.assertTrue(await quester._collect_objective(client))
            observe.return_value = hud(1)
            self.assertTrue(await quester._collect_objective(client))
        targets = [call.args[1].x for call in quester._try_teleport.await_args_list]
        self.assertEqual(targets, [10, 20])
        x_presses = [call for call in client.send_key.await_args_list if call.args[0].name == "X"]
        self.assertEqual(len(x_presses), 6)  # Retry the prompt until progress is observed.
        self.assertEqual(client.goto.await_count, 2)

    async def test_unloaded_objects_expand_search_area_even_without_arrow(self):
        quester = AutoQuester()
        client = SimpleNamespace(quest_id=AsyncMock(return_value=2), goto=AsyncMock(), zone_name=AsyncMock(return_value=None),
                                 in_battle=AsyncMock(return_value=False), is_loading=AsyncMock(return_value=False))
        quester._transition_destination = AsyncMock(return_value=None)
        quester._try_teleport = AsyncMock(return_value="success")
        goal = QuestGoal(3, "Collect", kind="usage")
        with patch("auto_quester.fetch_position", AsyncMock(return_value=XYZ(100, 200, 7))), \
             patch("auto_quester.fetch_quest_position", AsyncMock(return_value=XYZ(0, 0, 0))), \
             patch("auto_quester.asyncio.sleep", AsyncMock()):
            for _ in range(9):
                self.assertTrue(await quester._search_object_area(client, goal, "Collect Gear"))
        targets = [call.args[1] for call in quester._try_teleport.await_args_list]
        self.assertEqual(targets[0].x, 550)
        self.assertEqual(targets[8].x, 1000)
        self.assertTrue(all(target.z == 7 for target in targets))

    async def test_mob_approach_stops_input_when_teleport_enters_battle(self):
        body = SimpleNamespace(position=AsyncMock(return_value=XYZ(100, 0, 0)), yaw=AsyncMock(return_value=0))
        entity = SimpleNamespace(display_name=AsyncMock(return_value="Enemy"), actor_body=AsyncMock(return_value=body))
        client = SimpleNamespace(get_base_entity_list=AsyncMock(return_value=[entity]),
                                 is_loading=AsyncMock(return_value=False), in_battle=AsyncMock(side_effect=[False, True]),
                                 goto=AsyncMock())
        teleport = AsyncMock(return_value="success")
        with patch("quest_mobs.fetch_position", AsyncMock(return_value=XYZ(0, 0, 0))), \
             patch("quest_mobs.asyncio.sleep", AsyncMock()):
            self.assertTrue(await approach_enemy(client, "Enemy", teleport, lambda _: None, lambda: True))
        teleport.assert_awaited_once()
        client.goto.assert_not_awaited()

    async def test_static_quest_objects_have_locations_without_actor_bodies(self):
        position = XYZ(3, 4, 0)
        entity = SimpleNamespace(object_template=AsyncMock(return_value=SimpleNamespace(
            object_name=AsyncMock(return_value="Ground_Item"))), display_name=AsyncMock(return_value="Item"),
            actor_body=AsyncMock(return_value=None), location=AsyncMock(return_value=position))
        client = SimpleNamespace(get_base_entity_list=AsyncMock(return_value=[entity]),
                                 body=SimpleNamespace(position=AsyncMock(return_value=XYZ(0, 0, 0))))
        self.assertEqual(await list_nearby_entities(client), [(5, "Ground_Item", "Item", position)])

    async def test_missing_arrow_never_teleports_to_origin_or_interacts(self):
        quester = AutoQuester()
        quester._talk_target = AsyncMock(return_value=None)
        quester._entry_target = AsyncMock(return_value=None)
        quester._try_teleport = AsyncMock()
        with patch("auto_quester.fetch_quest_position", AsyncMock(return_value=XYZ(0, 0, 0))), \
             patch("auto_quester.visible_ui", AsyncMock(return_value=node("Root"))):
            self.assertEqual(await quester._teleport_to_quest_safe(object()), "waiting")
        quester._try_teleport.assert_not_awaited()
    async def test_hidden_parent_cannot_supply_stale_hud_text(self):
        def window(name, flags, children=(), kind="Window", text=""):
            return SimpleNamespace(name=AsyncMock(return_value=name), flags=AsyncMock(return_value=flags),
                                   children=AsyncMock(return_value=list(children)),
                                   maybe_read_type_name=AsyncMock(return_value=kind),
                                   maybe_text=AsyncMock(return_value=text))
        stale = window("txtGoalName", WindowFlags.visible, kind="ControlText", text="Talk To Wrong NPC")
        hidden = window("Hidden HUD", WindowFlags(0), [stale])
        current = window("txtGoalName", WindowFlags.visible, kind="ControlText", text="Talk To Current NPC")
        ui = await visible_ui(SimpleNamespace(root_window=window("Root", WindowFlags(0), [hidden, current])))
        self.assertEqual([n.text for n in ui.walk() if n.name == "txtGoalName"], ["Talk To Current NPC"])
        stale.maybe_text.assert_not_awaited()

    async def test_talk_target_resolves_generic_npc_instead_of_exit(self):
        template = SimpleNamespace(object_type=AsyncMock(return_value=ObjectType.undefined),
                                   object_name=AsyncMock(return_value="generic-npc"))
        entity = SimpleNamespace(object_template=AsyncMock(return_value=template),
                                 display_name=AsyncMock(return_value="A Quest Giver"),
                                 fetch_npc_behavior_template=AsyncMock(return_value=object()),
                                 actor_body=AsyncMock(return_value=SimpleNamespace(position=AsyncMock(return_value=XYZ(500, 200, 30)))))
        client = SimpleNamespace(get_base_entity_list=AsyncMock(return_value=[entity]),
                                 zone_name=AsyncMock(return_value="Area"))
        hud = node("Root", children=[node("txtGoalName", "<center>Talk To A Quest Giver in The Commons</center>")])
        with patch("auto_quester.visible_ui", AsyncMock(return_value=hud)), patch("auto_quester.fetch_position", AsyncMock(return_value=XYZ(0, 200, 30))):
            target = await AutoQuester()._talk_target(client)
        self.assertAlmostEqual(target.x, 320)
        self.assertAlmostEqual(target.y, 200)
        self.assertEqual(target.z, 30)

    async def test_dormant_named_npc_does_not_override_the_live_hud_route(self):
        template = SimpleNamespace(object_type=AsyncMock(return_value=ObjectType.npc),
                                   object_name=AsyncMock(return_value="Dormant_Object"))
        entity = SimpleNamespace(object_template=AsyncMock(return_value=template),
                                 display_name=AsyncMock(return_value="A Quest Giver"),
                                 actor_body=AsyncMock(return_value=None),
                                 location=AsyncMock(return_value=XYZ(20, 0, 0)),
                                 fetch_npc_behavior_template=AsyncMock(return_value=object()))
        client = SimpleNamespace(get_base_entity_list=AsyncMock(return_value=[entity]))
        hud = node("Root", children=[node("txtGoalName", "Talk To A Quest Giver in Area")])
        with patch("auto_quester.visible_ui", AsyncMock(return_value=hud)), \
             patch("auto_quester.fetch_position", AsyncMock(return_value=XYZ(0, 0, 0))):
            self.assertIsNone(await AutoQuester()._talk_target(client))
        entity.location.assert_not_awaited()

    async def test_missing_metadata_is_distinct_from_no_main_quest(self):
        client = SimpleNamespace(is_loading=AsyncMock(return_value=False), in_battle=AsyncMock(return_value=False))
        navigator = MainQuestNavigator(lambda _: None)
        with patch("quest_navigation.quest_entries", AsyncMock(return_value=None)):
            self.assertEqual(await navigator.ensure_tracked(client), "unavailable")
        with patch("quest_navigation.quest_entries", AsyncMock(return_value=[QuestEntry(1, mainline=False)])):
            self.assertEqual(await navigator.ensure_tracked(client), "missing")

    async def test_finder_is_disabled_then_tracking_verified_on_next_poll(self):
        client = SimpleNamespace(is_loading=AsyncMock(return_value=False), in_battle=AsyncMock(return_value=False),
                                 quest_id=AsyncMock(return_value=42), send_key=AsyncMock(),
                                 stats=SimpleNamespace(quest_finder_enabled=AsyncMock(side_effect=[True, False]),
                                                       write_quest_finder_enabled=AsyncMock()))
        navigator = MainQuestNavigator(lambda _: None)
        with patch("quest_navigation.quest_entries", AsyncMock(return_value=[QuestEntry(42, mainline=True)])), \
             patch("quest_navigation.visible_ui", AsyncMock(return_value=node("Root"))):
            self.assertEqual(await navigator.ensure_tracked(client), "changed")
            client.send_key.assert_not_awaited()
            client.stats.write_quest_finder_enabled.assert_awaited_once_with(False)
            self.assertEqual(await navigator.ensure_tracked(client), "ready")

    async def test_previous_npc_range_does_not_hide_a_failed_teleport(self):
        client = SimpleNamespace(teleport=AsyncMock(), in_battle=AsyncMock(return_value=False),
                                 is_in_dialog=AsyncMock(return_value=False),
                                 is_in_npc_range=AsyncMock(return_value=True),
                                 is_loading=AsyncMock(return_value=False))
        start = XYZ(0, 0, 0)
        with patch("auto_quester.fetch_position", AsyncMock(return_value=start)), \
             patch("auto_quester.asyncio.sleep", AsyncMock()):
            self.assertEqual(await AutoQuester()._try_teleport(client, XYZ(2000, 0, 0), start), "failed")

    async def test_wrong_interaction_prompt_blocks_the_x_key(self):
        quester = AutoQuester()
        quester._expected_talk_name = "Current NPC"
        client = SimpleNamespace(send_key=AsyncMock())
        ui = node("Root", children=[node("NPCRangeTxtTitle", "Previous NPC")])
        with patch("auto_quester.visible_ui", AsyncMock(return_value=ui)):
            await quester._try_interact(client)
        client.send_key.assert_not_awaited()

    async def test_missing_interaction_prompt_blocks_the_x_key(self):
        quester = AutoQuester()
        quester._expected_talk_name = "Current NPC"
        client = SimpleNamespace(send_key=AsyncMock())
        with patch("auto_quester.visible_ui", AsyncMock(return_value=node("Root"))):
            await quester._try_interact(client)
        client.send_key.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
