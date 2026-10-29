import unittest
from types import SimpleNamespace
from unittest.mock import patch

from hero_integration.patches.v0_1 import backfill_graphql_definition_ids as migration


class FakeDB:
    def __init__(self, rows, draft=False, binding=False, resolved=False, revision=False):
        self.rows = [SimpleNamespace(**row) for row in rows]
        self.draft, self.binding, self.resolved, self.revision = draft, binding, resolved, revision
        self.updates = []
    def has_column(self, table, column):
        return True
    def table_exists(self, table):
        return table in {"HERO Mapping Draft", "HERO Field Binding", "HERO Object Binding", "HERO Frappe Mapping", "HERO Mapping Revision"}
    def sql(self, query, values=None, as_dict=False):
        if query.startswith("SELECT name, definition_id"):
            return self.rows
        if query.startswith("SELECT definition_id FROM `tabHERO Frappe Mapping`"):
            return []
        if query.startswith("SELECT base_active_mapping, definition_id FROM `tabHERO Mapping Draft`"):
            return [SimpleNamespace(base_active_mapping="stable" if self.resolved else "legacy", definition_id="stable-id")] if self.draft else []
        if query.startswith("SELECT base_active_mapping FROM `tabHERO Mapping Revision`"):
            return [SimpleNamespace(base_active_mapping="stable" if self.resolved else "legacy")] if self.revision else []
        if query.startswith("SELECT mapping_definition_id FROM `tabHERO"):
            return [SimpleNamespace(mapping_definition_id="stable-id" if self.resolved else "unknown-id")] if self.binding else []
        if query.startswith("UPDATE `tabHERO GraphQL Mapping`"):
            identity, name = values
            row = next(row for row in self.rows if row.name == name)
            if row.definition_id is None or row.definition_id == "":
                row.definition_id = identity
                self.updates.append((name, identity))
            return []
        raise AssertionError(query)


class TestIdentityBackfill(unittest.TestCase):
    def run_patch(self, db):
        fake = SimpleNamespace(db=db, throw=lambda msg: (_ for _ in ()).throw(ValueError(msg)))
        with patch.object(migration, "frappe", fake):
            migration.execute()
    def test_backfills_only_missing_and_is_idempotent(self):
        db = FakeDB([dict(name="legacy-A", definition_id=None, status="Active", mapping_revision=3, modified="old", mapping_json="{}"),
                     dict(name="legacy-B", definition_id="", status="Disabled", mapping_revision=2, modified="older", mapping_json="{ }"),
                     dict(name="different-name", definition_id="persisted-canonical", status="Active", mapping_revision=7, modified="oldest", mapping_json="{}")])
        self.run_patch(db)
        ids = [r.definition_id for r in db.rows]
        self.assertEqual(len(set(ids)), 3)
        self.assertTrue(all(len(i) == 32 for i in ids[:2]))
        self.assertEqual(ids[2], "persisted-canonical")
        self.assertEqual([(r.status, r.mapping_revision, r.modified, r.mapping_json) for r in db.rows], [("Active", 3, "old", "{}"), ("Disabled", 2, "older", "{ }"), ("Active", 7, "oldest", "{}")])
        self.run_patch(db)
        self.assertEqual([r.definition_id for r in db.rows], ids)
        self.assertEqual(len(db.updates), 2)
    def test_ambiguous_draft_and_bindings_fail_before_any_update(self):
        for draft, binding in ((True, False), (False, True)):
            db = FakeDB([dict(name="legacy", definition_id=None)], draft=draft, binding=binding)
            with self.subTest(draft=draft, binding=binding), self.assertRaises(ValueError):
                self.run_patch(db)
            self.assertEqual(db.updates, [])
            self.assertIsNone(db.rows[0].definition_id)
    def test_independent_resolved_draft_and_binding_do_not_block(self):
        db = FakeDB([dict(name="legacy", definition_id=None), dict(name="stable", definition_id="stable-id")], draft=True, binding=True, resolved=True)
        self.run_patch(db)
        self.assertEqual(len(db.updates), 1)
        self.assertEqual(db.rows[1].definition_id, "stable-id")

    def test_ambiguous_revision_blocks_before_any_update(self):
        db = FakeDB([dict(name="legacy", definition_id=None), dict(name="stable", definition_id="stable-id")], revision=True)
        with self.assertRaisesRegex(ValueError, "revision references"):
            self.run_patch(db)
        self.assertEqual(db.updates, [])
        self.assertIsNone(db.rows[0].definition_id)
        self.assertEqual(db.rows[1].definition_id, "stable-id")

    def test_independent_revision_is_preserved(self):
        db = FakeDB([dict(name="legacy", definition_id=None), dict(name="stable", definition_id="stable-id")], revision=True, resolved=True)
        self.run_patch(db)
        self.assertEqual(len(db.updates), 1)
        self.assertEqual(db.rows[1].definition_id, "stable-id")
        self.assertEqual(db.sql("SELECT base_active_mapping FROM `tabHERO Mapping Revision` FOR UPDATE", as_dict=True)[0].base_active_mapping, "stable")

    def test_collision_fails_without_aliasing(self):
        db = FakeDB([dict(name="legacy", definition_id=None), dict(name="other", definition_id="x" * 32)])
        with patch.object(migration, "uuid4", return_value=SimpleNamespace(hex="x" * 32)):
            with self.assertRaisesRegex(ValueError, "collided"):
                self.run_patch(db)
        self.assertEqual(db.updates, [])
