"""Verify authentication and a synthetic Choice without controlling the game."""

import asyncio
import json
from pathlib import Path

from combat_actions import CombatAction
from jev_client import JevClient


async def check():
    client = JevClient()
    try:
        result = await client.models()
        print("Available models:", [model["name"] for model in result["models"]])
        actions = [CombatAction("pass", "pass", {"ends_turn": True}),
                   CombatAction("use Fire Cat on enemy Imp", "cast", {"ends_turn": True})]
        decision = await client.choose({
            "scenario": "Synthetic connection test; no live game is being controlled",
            "player": {"health": 500, "pips": 1},
            "enemy": {"name": "Imp", "health": 50},
            "hand": [{"name": "Fire Cat", "damage": 100, "pip_cost": 1, "accuracy": 100}],
        }, actions)
        print("Live Jev Choice:", decision.choice)
        print("Model:", decision.model, "Confidence:", decision.confidence)
        return {"ok": True, "models": [model["name"] for model in result["models"]],
                "choice": decision.choice, "model": decision.model, "confidence": decision.confidence}
    finally:
        await client.close()


def run_report(path):
    try:
        report = asyncio.run(check())
    except Exception as error:
        report = {"ok": False, "error": str(error)}
    Path(path).write_text(json.dumps(report, indent=2), encoding="utf-8")
    if not report["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(check())
