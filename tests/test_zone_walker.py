import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from wizwalker.utils import XYZ
from zone_walker import walk_zone_path


class ObservedRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_intro_variant_reaching_final_zone_finishes_and_records_actual_door(self):
        client = SimpleNamespace(zone_name=AsyncMock(side_effect=["Hub", "School"]),
            teleport=AsyncMock(), is_loading=AsyncMock(side_effect=[True, False]),
            body=SimpleNamespace(position=AsyncMock(return_value=XYZ(4, 5, 6))))
        record = Mock()
        route = [dict(to_zone="IntroSchool", from_pos=[1, 2, 3]),
                 dict(to_zone="School", from_pos=[9, 9, 9])]
        with patch("zone_walker.asyncio.sleep", AsyncMock()):
            self.assertTrue(await walk_zone_path(client, route, on_transition=record))
        self.assertEqual(client.teleport.await_count, 1)
        record.assert_called_once_with("Hub", (1, 2, 3), "School", (4, 5, 6))

    async def test_unexpected_zone_is_recorded_but_not_followed_blindly(self):
        client = SimpleNamespace(zone_name=AsyncMock(side_effect=["Hub", "Unexpected"]),
            teleport=AsyncMock(), is_loading=AsyncMock(side_effect=[True, False]),
            body=SimpleNamespace(position=AsyncMock(return_value=XYZ(4, 5, 6))))
        record = Mock()
        with patch("zone_walker.asyncio.sleep", AsyncMock()):
            self.assertFalse(await walk_zone_path(client, [dict(to_zone="School", from_pos=[1, 2, 3])], on_transition=record))
        record.assert_called_once_with("Hub", (1, 2, 3), "Unexpected", (4, 5, 6))
