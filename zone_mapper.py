"""
Zone Mapper  --  builds a persistent graph of Wizard101 zones as the
auto-quester moves through the world.

Records:
  - Every zone we've ever been in
  - Each observed transition: (from_zone, from_pos, to_zone, to_pos)
    where from_pos is the teleport target inside the source zone that
    triggered the load screen, and to_pos is where we ended up after.
  - For each visited zone, whether we've seen health and/or mana wisps.

Persists to JSON on disk (`zone_map.json` by default) after every write.

Use `find_path(from_zone, to_zone)` to get an ordered list of
transitions to traverse, even across several intermediate zones.
"""

import json
import logging
import os
import threading
from collections import deque
from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple

log = logging.getLogger("zone_mapper")

ZoneCoord = Tuple[float, float, float]

DEFAULT_FILE = "zone_map.json"
SCHEMA_VERSION = 1

# Two transitions with from/to positions within this distance are
# considered the "same door" -- their count is bumped instead of
# inserting a duplicate row.
MERGE_RADIUS = 50.0


def _now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


def _dist_sq(a, b) -> float:
    return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2


def _close(a, b, radius: float = MERGE_RADIUS) -> bool:
    return _dist_sq(a, b) < (radius * radius)


class ZoneMapper:
    """Thread-safe in-memory zone graph, persisted to JSON on every change."""

    def __init__(self, file_path: str = DEFAULT_FILE):
        self._file_path = file_path
        self._lock = threading.Lock()
        self._data: dict = self._load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> dict:
        if not os.path.exists(self._file_path):
            return self._empty()
        try:
            with open(self._file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            data.setdefault("version", SCHEMA_VERSION)
            data.setdefault("zones", {})
            data.setdefault("transitions", [])
            log.info(
                f"Loaded zone map: {len(data['zones'])} zones, "
                f"{len(data['transitions'])} transitions"
            )
            return data
        except Exception as e:
            log.warning(f"Failed to load zone map ({e})  --  starting fresh")
            return self._empty()

    @staticmethod
    def _empty() -> dict:
        return {"version": SCHEMA_VERSION, "zones": {}, "transitions": []}

    def _save(self):
        try:
            tmp_path = self._file_path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2)
            os.replace(tmp_path, self._file_path)
        except Exception as e:
            log.warning(f"Failed to save zone map: {e}")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_zone(self, zone: str) -> dict:
        z = self._data["zones"].get(zone)
        now = _now_iso()
        if z is None:
            z = {
                "first_seen": now,
                "last_seen": now,
                "visit_count": 0,
                "has_health_wisps": False,
                "has_mana_wisps": False,
            }
            self._data["zones"][zone] = z
        else:
            z["last_seen"] = now
        return z

    # ------------------------------------------------------------------
    # Public mutators
    # ------------------------------------------------------------------

    def record_visit(self, zone: str):
        if not zone:
            return
        with self._lock:
            z = self._ensure_zone(zone)
            z["visit_count"] = z.get("visit_count", 0) + 1
            self._save()

    def record_transition(
        self,
        from_zone: str,
        from_pos: ZoneCoord,
        to_zone: str,
        to_pos: ZoneCoord,
    ):
        if not from_zone or not to_zone:
            return
        with self._lock:
            self._ensure_zone(from_zone)
            self._ensure_zone(to_zone)
            from_pos = tuple(float(x) for x in from_pos)
            to_pos = tuple(float(x) for x in to_pos)

            for t in self._data["transitions"]:
                if (
                    t["from_zone"] == from_zone
                    and t["to_zone"] == to_zone
                    and _close(t["from_pos"], from_pos)
                    and _close(t["to_pos"], to_pos)
                ):
                    t["count"] = t.get("count", 0) + 1
                    t["last_seen"] = _now_iso()
                    self._save()
                    log.info(
                        f"Transition (existing, n={t['count']}): "
                        f"{from_zone} -> {to_zone}"
                    )
                    return

            self._data["transitions"].append({
                "from_zone": from_zone,
                "to_zone": to_zone,
                "from_pos": list(from_pos),
                "to_pos": list(to_pos),
                "count": 1,
                "last_seen": _now_iso(),
            })
            self._save()
            log.info(
                f"Transition (new): {from_zone}@({from_pos[0]:.0f},{from_pos[1]:.0f},{from_pos[2]:.0f})"
                f"  ->  {to_zone}@({to_pos[0]:.0f},{to_pos[1]:.0f},{to_pos[2]:.0f})"
            )

    def mark_wisps(self, zone: str, has_health: bool, has_mana: bool):
        """Record observed wisp availability. Sticky: once True, stays True."""
        if not zone:
            return
        with self._lock:
            z = self._ensure_zone(zone)
            new_h = z.get("has_health_wisps", False) or bool(has_health)
            new_m = z.get("has_mana_wisps", False) or bool(has_mana)
            changed = (new_h != z.get("has_health_wisps")) or (
                new_m != z.get("has_mana_wisps")
            )
            z["has_health_wisps"] = new_h
            z["has_mana_wisps"] = new_m
            if changed:
                self._save()
                log.info(f"Wisp flags {zone}: health={new_h} mana={new_m}")

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def find_path(
        self, from_zone: str, to_zone: str
    ) -> Optional[List[dict]]:
        """Return a list of transition dicts to walk from from_zone to
        to_zone (BFS, shortest in hop count). None if unreachable.
        Empty list if from_zone == to_zone.
        """
        with self._lock:
            if from_zone == to_zone:
                return []

            adj: Dict[str, List[dict]] = {}
            for t in self._data["transitions"]:
                adj.setdefault(t["from_zone"], []).append(t)

            visited = {from_zone}
            queue: deque = deque([(from_zone, [])])
            while queue:
                current, path = queue.popleft()
                for t in adj.get(current, []):
                    nxt = t["to_zone"]
                    if nxt in visited:
                        continue
                    new_path = path + [t]
                    if nxt == to_zone:
                        return new_path
                    visited.add(nxt)
                    queue.append((nxt, new_path))
            return None

    def find_nearest(
        self,
        from_zone: str,
        predicate: Callable[[str, dict], bool],
        exclude_self: bool = True,
    ) -> Optional[Tuple[str, List[dict]]]:
        """BFS outward from `from_zone`, returning the first zone whose
        `(name, zone_data)` satisfies `predicate`, plus the path to it.

        Args:
            from_zone: starting zone name
            predicate: called with (zone_name, zone_data_dict); return
                True to select that zone.
            exclude_self: if True, `from_zone` itself never matches even
                if the predicate would accept it.

        Returns:
            (target_zone, path) where `path` is a list of transition
            dicts in traversal order (empty if target == from_zone), or
            None if no reachable zone matches.
        """
        with self._lock:
            zones = self._data["zones"]

            if not exclude_self and from_zone in zones:
                if predicate(from_zone, zones[from_zone]):
                    return (from_zone, [])

            adj: Dict[str, List[dict]] = {}
            for t in self._data["transitions"]:
                adj.setdefault(t["from_zone"], []).append(t)

            visited = {from_zone}
            queue: deque = deque([(from_zone, [])])
            while queue:
                current, path = queue.popleft()
                for t in adj.get(current, []):
                    nxt = t["to_zone"]
                    if nxt in visited:
                        continue
                    visited.add(nxt)
                    new_path = path + [t]
                    if nxt in zones and predicate(nxt, zones[nxt]):
                        return (nxt, new_path)
                    queue.append((nxt, new_path))
            return None

    def find_nearest_with_health(
        self, from_zone: str, exclude: Optional[set] = None
    ) -> Optional[Tuple[str, List[dict]]]:
        """Nearest zone (by transition-hops) flagged as containing health wisps."""
        ex = exclude or set()
        return self.find_nearest(
            from_zone,
            lambda name, z: name not in ex and z.get("has_health_wisps", False),
        )

    def find_nearest_with_mana(
        self, from_zone: str, exclude: Optional[set] = None
    ) -> Optional[Tuple[str, List[dict]]]:
        """Nearest zone (by transition-hops) flagged as containing mana wisps."""
        ex = exclude or set()
        return self.find_nearest(
            from_zone,
            lambda name, z: name not in ex and z.get("has_mana_wisps", False),
        )

    def neighbors(self, zone: str) -> List[str]:
        """All zones directly reachable via a recorded transition from `zone`."""
        with self._lock:
            out = []
            for t in self._data["transitions"]:
                if t["from_zone"] == zone and t["to_zone"] not in out:
                    out.append(t["to_zone"])
            return out

    def summary(self) -> dict:
        with self._lock:
            zones = self._data["zones"]
            return {
                "zone_count": len(zones),
                "transition_count": len(self._data["transitions"]),
                "zones_with_health_wisps": sum(
                    1 for z in zones.values() if z.get("has_health_wisps")
                ),
                "zones_with_mana_wisps": sum(
                    1 for z in zones.values() if z.get("has_mana_wisps")
                ),
            }

    def dump(self) -> dict:
        """Deep copy of the full data structure (safe to log / pass around)."""
        with self._lock:
            return json.loads(json.dumps(self._data))
