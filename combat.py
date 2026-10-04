"""Jev chooses combat actions from fresh state; no local combat strategy tree."""

import asyncio
import json
import logging
from dataclasses import asdict

from combat_actions import available_actions, execute
from combat_history import CombatHistory
from combat_state import CombatStateCollector
from jev_client import JevClient, JevError
from runtime_paths import data_directory


log = logging.getLogger("combat")


def write_report(filename, report):
    target = data_directory() / filename
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(target)


class JevCombat:
    def __init__(self, *, ai=None, collector=None):
        self.ai = ai or JevClient()
        self.collector = collector or CombatStateCollector()
        self.history = CombatHistory()
        self.submitted_round = None
        self.failed_actions = set()
        self.failed_round = None
        self.free_action_counts = {}
        self.lock = asyncio.Lock()

    async def close(self):
        await self.ai.close()

    def reset(self):
        self.history.reset()
        self.submitted_round = None
        self.failed_round = None
        self.failed_actions.clear()
        self.free_action_counts.clear()

    async def export_snapshot(self, client):
        async with self.lock:
            snapshot = await self.collector.collect(client)
            self.history.bind(snapshot)
            snapshot.state["fight_history"] = self.history.as_dict()
            write_report("combat_snapshot.json", snapshot.state)
            return snapshot

    async def tick(self, client):
        async with self.lock:
            if await client.is_loading():
                return "waiting", "Loading..."
            if not await client.in_battle():
                self.reset()
                return "waiting", "Monitoring for battles..."
            await self.history.observe(client)
            phase = await client.duel.duel_phase()
            if phase.name != "planning":
                return "waiting", "Observing combat..."
            key = (await client.duel.duel_id_full(), await client.duel.round_num(),
                   await client.duel.alt_turn_counter())
            if self.submitted_round == key:
                return "waiting", "Turn submitted; observing combat..."
            if self.failed_round != key:
                self.failed_round = key
                self.failed_actions.clear()
                self.free_action_counts.clear()
            snapshot = await self.collector.collect(client)
            if snapshot.state["duel"]["duel_phase"] != "planning":
                return "waiting", "Planning phase changed..."
            self.history.bind(snapshot)
            key = snapshot.key
            snapshot.state["fight_history"] = self.history.as_dict()
            fingerprint = snapshot.fingerprint()
            options = [a for a in available_actions(snapshot) if (fingerprint, a.label) not in self.failed_actions]
            free_count = self.free_action_counts.get(key, 0)
            if free_count >= 16:
                options = [a for a in options if a.ends_turn]
            if not options:
                write_report("combat_snapshot.json", snapshot.state)
                return "skipped", "No readable actions; waiting for manual input."
            snapshot.state["available_actions"] = [asdict(a) for a in options]
            write_report("combat_snapshot.json", snapshot.state)
            try:
                decision = await asyncio.wait_for(self.ai.choose(snapshot.state, options), timeout=8)
            except TimeoutError:
                raise JevError("Jev decision exceeded the turn time budget; no combat input was sent.") from None
            action = next((a for a in options if a.label == decision.choice), None)
            if action is None:
                raise JevError("Jev selected an unavailable action; no combat input was sent.")
            write_report("combat_decision.json", asdict(decision))
            if await client.is_loading() or not await client.in_battle():
                return "waiting", "Battle changed while Jev was deciding."
            refreshed = await self.collector.collect(client, include_decks=False)
            if (refreshed.state["duel"]["duel_phase"] != "planning" or refreshed.fingerprint() != fingerprint):
                return "waiting", "State changed; asking Jev again with fresh facts."
            current_action = next((a for a in available_actions(refreshed)
                                   if (a.kind, a.card_id, a.target_ids) == (action.kind, action.card_id, action.target_ids)), None)
            if current_action is None:
                return "waiting", "Selected action is no longer available."
            if not await execute(current_action, refreshed, client, self.collector):
                self.failed_actions.add((fingerprint, action.label))
                return "skipped", "Game did not accept the action; refreshing before another choice."
            self.history.record_action(action, snapshot)
            if action.ends_turn:
                self.submitted_round = key
            else:
                self.free_action_counts[key] = free_count + 1
            log.info("Jev %s selected %s (confidence=%.3f)", decision.model, action.label, decision.confidence)
            return "acted", action.label
