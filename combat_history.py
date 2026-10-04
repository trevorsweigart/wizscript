"""Per-fight history, with explicit evidence instead of invented cast events."""

from collections import Counter

from combat_state import Read


class CombatHistory:
    def __init__(self):
        self.reset()

    def reset(self):
        self.duel_id = None
        self.started_round = None
        self.events = []
        self.previous_graveyards = {}
        self.known_spells = {}
        self.discards = Counter()
        self.roster = {}
        self.dropped_events = 0

    def bind(self, snapshot):
        if self.duel_id != snapshot.key[0]:
            self.reset()
            self.duel_id = snapshot.key[0]
            self.started_round = snapshot.key[1]
        for card in snapshot.state["hand"]:
            self.known_spells[(card.get("template_id"), card.get("enchantment"))] = card["name"]
        for member in snapshot.state["combatants"]:
            for card in member.get("memory_hand") or []:
                self.known_spells[(card.get("template_id"), card.get("enchantment"))] = card["name"]
            self.roster[member["owner_id_full"]] = {"name": member["name"], "relation": member["relation"]}
            if "graveyard" in member:
                self._graveyard(member["owner_id_full"], member["graveyard"], snapshot.key[1])

    def _append(self, event):
        self.events.append(event)
        if len(self.events) > 512:
            self.events.pop(0)
            self.dropped_events += 1

    def record_action(self, action, snapshot):
        card = next((c for c in snapshot.state["hand"] if c["id"] == action.card_id), None)
        event = {"round": snapshot.key[1], "kind": action.kind, "action": action.label,
                 "evidence": "verified_hand_change" if not action.ends_turn else "input_submitted",
                 "cast_outcome": "unknown" if action.kind == "cast" else None}
        if card:
            event.update(template_id=card["template_id"], enchantment=card["enchantment"], name=card["name"])
        if action.kind == "discard" and card:
            self.discards[(snapshot.state["player_id"], card["template_id"], card["enchantment"])] += 1
        self._append(event)

    def _graveyard(self, owner, cards, round_num):
        if cards is None:
            return
        counts = Counter((c.get("template_id"), c.get("enchantment")) for c in cards if c.get("template_id") is not None)
        previous = self.previous_graveyards.get(owner)
        additions = counts if previous is None else counts - previous
        evidence = "initial_graveyard_contents" if previous is None else "observed_graveyard_addition"
        for (template, enchantment), count in additions.items():
            discard_key = (owner, template, enchantment)
            known_discards = min(count, self.discards[discard_key])
            self.discards[discard_key] -= known_discards
            self._append({
                "round_observed": round_num, "owner_id": owner, **self.roster.get(owner, {}),
                "kind": "card_left_play", "template_id": template, "enchantment": enchantment,
                "spell_name": self.known_spells.get((template, enchantment)), "count": count,
                "known_bot_discards": known_discards, "evidence": evidence, "confirmed_cast": False,
                "note": "May include casts, discards, fizzles, or other removals; not a cast-event API.",
            })
        self.previous_graveyards[owner] = counts

    async def observe(self, client):
        """Poll graves during execution, even after our own turn was submitted."""
        if self.duel_id is None or await client.is_loading():
            return
        reader = Read(seconds=3)
        if await reader.get(client.duel, "duel_id_full") != self.duel_id:
            return
        round_num = await reader.get(client.duel, "round_num")
        participants = await reader.get(client.duel, "participant_list")
        for participant in reader.bounded(participants, "history.participants", 16):
            owner = await reader.get(participant, "owner_id_full")
            deck = await reader.get(participant, "play_deck")
            spells = await reader.get(deck, "graveyard_to_save")
            if owner is None or spells is None:
                continue
            cards = [await reader.fields(spell, ("template_id", "enchantment"), "graveyard")
                     for spell in reader.bounded(spells, "graveyard", 256)]
            if any(c["template_id"] is None or c["enchantment"] is None for c in cards):
                continue  # A partial read cannot establish a new baseline.
            self._graveyard(owner, cards, round_num)

    def as_dict(self):
        return {
            "duel_id": self.duel_id, "tracking_started_round": self.started_round,
            "complete_cast_log_available": False,
            "confirmed_player_casts": None, "confirmed_enemy_casts": None,
            "events": list(self.events), "older_events_omitted": self.dropped_events,
            "note": "The client exposes graveyards, not a reliable full cast log. Input submission does not prove a successful cast.",
        }
