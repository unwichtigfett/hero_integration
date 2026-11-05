from __future__ import annotations

import json
from types import SimpleNamespace
import unittest

from graphql import build_schema, introspection_from_schema

from hero_integration.services import projection_contracts as pc
from hero_integration.tests.projection_schema_fixture import projection_sdl as _sdl


def _snapshot(sdl, fingerprint=''):
    return SimpleNamespace(schema_fingerprint=fingerprint,
                           snapshot_json=json.dumps(introspection_from_schema(build_schema(sdl))))


class TestProjectionSchemaCompatibility(unittest.TestCase):
    def setUp(self):
        pc._COMPATIBILITY.clear()

    def test_any_snapshot_that_offers_the_reviewed_fields_is_compatible(self):
        for operation in pc.CONTRACTS:
            with self.subTest(operation=operation):
                self.assertEqual(pc.schema_incompatibility(operation, _snapshot(_sdl())), '')

    def test_a_missing_or_retyped_field_blocks_the_projection(self):
        missing = pc.schema_incompatibility('HeroCalendarEvents', _snapshot(_sdl(drop={('CalendarEvent', 'color')})))
        self.assertIn('color', missing)
        retyped = pc.schema_incompatibility('HeroTasks', _snapshot(_sdl(retype={('Task', 'is_deleted'): 'String'})))
        self.assertIn('is_deleted', retyped)
        listed = pc.schema_incompatibility('HeroTasks', _snapshot(_sdl(retype={('Task', 'id'): '[Int]'})))
        self.assertTrue(listed)

    def test_an_unreadable_snapshot_never_authorizes(self):
        self.assertEqual(pc.schema_incompatibility('HeroTasks', SimpleNamespace(schema_fingerprint='', snapshot_json='{}')),
                         'the snapshot schema cannot be read')

    def test_the_reviewed_schema_stays_compatible(self):
        reviewed = SimpleNamespace(schema_fingerprint=pc.CONTRACTS['HeroTasks']['fingerprint'], snapshot_json=None)
        self.assertEqual(pc.schema_incompatibility('HeroTasks', reviewed), '')

    def test_the_result_is_cached_per_snapshot_fingerprint(self):
        snapshot = _snapshot(_sdl(), fingerprint='a' * 64)
        self.assertEqual(pc.schema_incompatibility('HeroTasks', snapshot), '')
        snapshot.snapshot_json = '{}'
        self.assertEqual(pc.schema_incompatibility('HeroTasks', snapshot), '')


if __name__ == '__main__':
    unittest.main()
