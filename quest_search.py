"""Explore a zone's walkable map when an objective has no loaded objects."""

import asyncio
import math
import struct
from contextlib import suppress

from wizwalker.file_readers.wad import Wad
from wizwalker.utils import XYZ


def parse_walkable_map(data):
    """Read the documented nav records, tolerating duplicated vertex labels.

    Some current maps repeat a label. Those ambiguous vertices and their
    edges are excluded rather than silently connecting unrelated positions.
    """
    if len(data) < 10:
        raise ValueError("Truncated walkable map")
    count = struct.unpack_from("<i", data, 2)[0]
    if not 0 < count <= 32767 or 6 + count * 14 + 4 > len(data):
        raise ValueError("Invalid walkable-map vertex count")
    vertices, duplicates = {}, set()
    for index in range(count):
        x, y, z, label = struct.unpack_from("<fffh", data, 6 + index * 14)
        if label < 0 or not all(math.isfinite(v) for v in (x, y, z)):
            raise ValueError("Invalid walkable-map vertex")
        if label in vertices:
            duplicates.add(label)
        vertices[label] = XYZ(x, y, z)
    for label in duplicates:
        vertices.pop(label, None)
    offset = 6 + count * 14
    edges = struct.unpack_from("<i", data, offset)[0]
    if edges < 0 or offset + 4 + edges * 4 != len(data):
        raise ValueError("Invalid walkable-map edge count")
    adjacency = {label: set() for label in vertices}
    for index in range(edges):
        a, b = struct.unpack_from("<hh", data, offset + 4 + index * 4)
        if a in vertices and b in vertices:
            adjacency[a].add(b)
            adjacency[b].add(a)
    return vertices, adjacency


def distance_squared(a, b):
    return (a.x - b.x) ** 2 + (a.y - b.y) ** 2 + (a.z - b.z) ** 2


def search_point(vertices, adjacency, origin, visited, radius=900):
    """Choose the nearest uncovered point in the origin's connected paths."""
    if not vertices:
        return None
    start = min(vertices, key=lambda label: distance_squared(origin, vertices[label]))
    connected, pending = set(), [start]
    while pending:
        label = pending.pop()
        if label not in connected:
            connected.add(label)
            pending.extend(adjacency.get(label, ()) - connected)
    covered = visited or [origin]
    candidates = [vertices[label] for label in connected
                  if all(distance_squared(vertices[label], p) >= radius ** 2 for p in covered)]
    if not candidates:
        return None
    last = covered[-1]
    return min(candidates, key=lambda p: distance_squared(last, p))


class ZoneSearch:
    def __init__(self):
        self.maps = {}

    async def point(self, zone, origin, visited):
        if zone not in self.maps:
            wad = None
            try:
                wad = Wad.from_game_data(zone.replace("/", "-"))
                data = await asyncio.wait_for(wad.get_file("zone.nav"), 4)
                self.maps[zone] = parse_walkable_map(data)
            except (OSError, ValueError, TimeoutError, KeyError):
                self.maps[zone] = None
            finally:
                if wad is not None:
                    with suppress(AttributeError, OSError):
                        wad.close()
        geometry = self.maps[zone]
        return search_point(*geometry, origin, visited, radius=2200) if geometry else None
