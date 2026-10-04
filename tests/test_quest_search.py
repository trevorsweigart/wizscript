import struct
import unittest

from wizwalker.utils import XYZ
from quest_search import parse_walkable_map, search_point


class WalkableSearchTests(unittest.TestCase):
    def data(self, records, edges):
        return (b'\0\0' + struct.pack('<i', len(records)) +
                b''.join(struct.pack('<fffh', *row) for row in records) +
                struct.pack('<i', len(edges)) +
                b''.join(struct.pack('<hh', *edge) for edge in edges))

    def test_ambiguous_labels_cannot_join_unrelated_paths(self):
        vertices, adjacency = parse_walkable_map(self.data(
            [(0, 0, 0, 0), (100, 0, 0, 1), (9000, 0, 0, 1), (1000, 0, 0, 2)],
            [(0, 1), (1, 2)]))
        self.assertNotIn(1, vertices)
        self.assertEqual(adjacency[0], set())
        self.assertIsNone(search_point(vertices, adjacency, XYZ(0, 0, 0), []))

    def test_search_stays_in_connected_walkable_paths_and_advances_coverage(self):
        vertices = {0: XYZ(0, 0, 20), 1: XYZ(1000, 0, 10),
                    2: XYZ(2000, 0, -100), 3: XYZ(0, 1000, 0)}
        adjacency = {0: {1}, 1: {0, 2}, 2: {1}, 3: set()}
        origin = vertices[0]
        self.assertIs(search_point(vertices, adjacency, origin, [origin]), vertices[1])
        self.assertIs(search_point(vertices, adjacency, origin, [origin, vertices[1]]), vertices[2])

    def test_malformed_maps_are_rejected(self):
        for data in (b'', self.data([(0, 0, 0, 0)], [])[:-1], self.data([(float('nan'), 0, 0, 0)], [])):
            with self.assertRaises(ValueError):
                parse_walkable_map(data)
