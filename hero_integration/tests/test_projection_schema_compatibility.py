from __future__ import annotations

import json
from types import SimpleNamespace
import unittest

from graphql import build_schema, introspection_from_schema

from hero_integration.services import projection_contracts as pc

_GRAPHQL = {'int': 'Int', 'string': 'String', 'boolean': 'Boolean'}
_NESTED = {
    'resources': ('CompanyResource', 'id: Int name: String type: String type_id: Int'),
    'partners': ('Partner', 'id: Int full_name: String'),
}


def _sdl(*, drop=(), retype=None):
    """A synthetic schema that offers exactly what the reviewed projections read."""
    retype = retype or {}
    types, roots = [], []
    for name in pc.CONTRACTS:
        contract = pc.CONTRACTS[name]
        fields = []
        for path, source_type in contract['paths'].items():
            if (contract['type'], path) in drop:
                continue
            if path in _NESTED:
                fields.append(f'{path}: [{_NESTED[path][0]}]')
            else:
                fields.append(f"{path}: {retype.get((contract['type'], path), _GRAPHQL[source_type.split('|')[0]])}")
        types.append(f"type {contract['type']} {{ {' '.join(fields)} }}")
        roots.append(f"{contract['root']}(first: Int, offset: Int, orderBy: String): [{contract['type']}]")
    types += [f'type {name} {{ {fields} }}' for name, fields in _NESTED.values()]
    return 'type Query { ' + ' '.join(roots) + ' } ' + ' '.join(types)


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
