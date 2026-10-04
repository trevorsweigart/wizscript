"""Main-quest tracking through the game's journal, without quest ID tables."""

import asyncio
import html
import re

from wizwalker.constants import Keycode

from quest_state import quest_entries, safe_read, visible_ui


def plain_text(text):
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", text)).split())


def main_quests(entries):
    return [quest for quest in entries if quest.mainline is True]


class MainQuestNavigator:
    def __init__(self, on_status):
        self.status = on_status
        self._selection = None
        self._seen_pages = set()

    async def ensure_tracked(self, client):
        """Return ready/missing/unavailable/changed; changes always require a new tick."""
        if await client.is_loading() or await client.in_battle():
            return "unavailable"
        entries = await quest_entries(client)
        if entries is None:
            self.status("Quest metadata unavailable — waiting")
            return "unavailable"
        mains = main_quests(entries)
        if not mains:
            return "missing"
        active = await safe_read(client.quest_id)
        selection = (active, tuple(sorted(quest.id for quest in mains)))
        if selection != self._selection:
            self._selection = selection
            self._seen_pages.clear()
        finder = await safe_read(client.stats.quest_finder_enabled)
        if finder is None:
            return "unavailable"
        if finder:
            await client.stats.write_quest_finder_enabled(False)
            self.status("Disabled Quest Finder while following an accepted main quest")
            return "changed"
        ui = await visible_ui(client)
        journal = next((node for node in ui.walk() if node.name == "wndQuestList"), None) if ui else None
        if ui is None:
            return "unavailable"
        if active in {quest.id for quest in mains}:
            if journal is not None:
                await client.send_key(Keycode.Q, 0.1)
                self.status("Closed the journal to continue the main quest")
                return "changed"
            return "ready"
        if journal is None:
            self._seen_pages.clear()
            await client.send_key(Keycode.Q, 0.1)
            self.status("Selecting an accepted main quest in the journal")
            return "changed"
        # Metadata identifies the same storyline quests represented by stars.
        # Select the visible quest card's Activate button, then verify tracking
        # on the next poll rather than writing registry IDs behind the game's UI.
        mains.sort(key=lambda quest: (not quest.ready, quest.id != active, quest.id))
        for quest in mains:
            for card in journal.children:
                nodes = list(card.walk())
                if any(node.name == "txtName" and plain_text(node.text).casefold() == quest.name.strip().casefold() for node in nodes):
                    activate = next((node for node in nodes if node.name == "btnActivate" and node.enabled), None)
                    if activate:
                        async with client.mouse_handler:
                            await client.mouse_handler.click_window(activate.window)
                            await asyncio.sleep(0.2)
                        await client.send_key(Keycode.Q, 0.1)
                        self.status(f"Tracking main quest: {quest.name}")
                        return "changed"
        # Search all pages, bounded by the number of current entries. A failed
        # search leaves the book open instead of navigating a stale arrow.
        next_page = next((node for node in journal.children if node.name == "btnNextPage" and node.enabled), None)
        page = tuple(plain_text(node.text) for node in journal.walk() if node.name == "txtName")
        if next_page and len(entries) > 4 and page not in self._seen_pages and len(self._seen_pages) <= (len(entries) + 3) // 4:
            self._seen_pages.add(page)
            async with client.mouse_handler:
                await client.mouse_handler.click_window(next_page.window)
                await asyncio.sleep(0.2)
            self.status("Looking for a main quest on the next journal page")
            return "changed"
        self.status("Main quest card unavailable — waiting for the journal")
        return "unavailable"
