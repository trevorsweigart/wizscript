"""Advance story dialog and accept only visibly starred main quest offers."""

import asyncio

from quest_navigation import plain_text
from quest_state import quest_entries, visible_ui


def dialog_action(ui, *, main_titles=(), inspected_titles=()):
    """Choose from observed controls, leaving unknown screens untouched."""
    nodes = list(ui.walk()) if ui else []
    services = next((node for node in nodes if node.name == "NPCServicesWin"), None)
    if services is not None:
        options = [node for node in services.walk()
                   if node.name == "OptionButton" and node.enabled and plain_text(node.text)]
        names = {title.casefold() for title in main_titles}
        inspected = {title.casefold() for title in inspected_titles}
        options = [node for node in options if plain_text(node.text).casefold() not in inspected]
        current = [node for node in options if plain_text(node.text).casefold() in names]
        if len(current) == 1:
            return current[0], f"Opening main quest: {plain_text(current[0].text)}"
        if options:
            return options[0], f"Inspecting NPC offer: {plain_text(options[0].text)}"
        cancel = next((node for node in services.walk() if node.enabled and
                       plain_text(node.text).casefold() in ("cancel", "exit", "back")), None)
        return cancel, "Finished checking this NPC's offers"
    dialog = next((node for node in nodes if node.name == "wndDialogMain"), None)
    if dialog is None:
        return None, "Waiting for NPC dialog controls"
    right = next((node for node in dialog.walk()
                  if node.name == "btnRight" and node.enabled), None)
    if right is None:
        return None, "Waiting for an enabled dialog action"
    label = plain_text(right.text).casefold()
    if label == "accept":
        offers = [node for node in nodes if node.kind == "QuestInfoWindow"]
        mains = [offer for offer in offers
                 if any(child.name in ("LeftMainline", "RightMainline")
                        for child in offer.walk())]
        if len(offers) != 1 or len(mains) != 1:
            # Only decline an unambiguous side quest. Never accept it just to
            # escape a menu, and never guess which of multiple offers is active.
            left = next((node for node in dialog.walk() if node.name == "btnLeft"
                         and node.enabled and plain_text(node.text).casefold() == "decline"), None)
            return (left if len(offers) == 1 and not mains else None), "Skipping an unstarred or ambiguous quest offer"
        title = next((plain_text(node.text) for node in mains[0].walk()
                      if node.name == "txtName"), "main quest")
        return right, f"Accepting main quest: {title}"
    if label in ("more", "continue", "done", "complete", "finish"):
        return right, "Advancing story dialog"
    return None, f"Waiting for a recognized dialog action: {label or 'unknown'}"


class DialogNavigator:
    """Inspect each choice once during an NPC conversation, in any story area."""

    def __init__(self):
        self.inspected = set()

    def begin(self):
        self.inspected.clear()

    def choose(self, ui, main_titles=()):
        action, status = dialog_action(ui, main_titles=main_titles,
                                       inspected_titles=self.inspected)
        if action is not None and action.name == "OptionButton":
            self.inspected.add(plain_text(action.text))
        return action, status


async def advance_dialog(client, on_status, navigator=None):
    ui = await visible_ui(client)
    entries = await quest_entries(client)
    names = [quest.name for quest in entries or [] if quest.mainline is True]
    action, status = (navigator.choose(ui, names) if navigator else
                      dialog_action(ui, main_titles=names))
    on_status(status)
    if action is None:
        return False
    async with client.mouse_handler:
        await client.mouse_handler.click_window(action.window)
        await asyncio.sleep(0.2)
    return True
