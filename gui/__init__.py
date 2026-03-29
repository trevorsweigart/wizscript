"""GUI package — re-exports panel components."""

from gui.collapsible import CollapsiblePanel
from gui.client_panel import ClientPanel
from gui.info_panel import InfoPanel
from gui.teleport_panel import TeleportPanel
from gui.auto_quest_panel import AutoQuestPanel
from gui.auto_combat_panel import AutoCombatPanel
from gui.theme import apply_theme

__all__ = [
    "CollapsiblePanel",
    "ClientPanel",
    "InfoPanel",
    "TeleportPanel",
    "AutoQuestPanel",
    "AutoCombatPanel",
    "apply_theme",
]
