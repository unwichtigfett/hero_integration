import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from hero_integration.patches.v0_1 import backfill_graphql_definition_ids as backfill
from hero_integration.patches.v0_1 import normalize_empty_graphql_definition_ids as pre
from hero_integration.services.mapping_registry_model import FORMAT_ID
from hero_integration.services.mapping_registry_storage import _integrity
from hero_integration.tests.mapping_registry.test_mapping_registry_service import _definition
from hero_integration.tests.test_graphql_definition_id_backfill import FakeDB, TestIdentityBackfill


class PreDB:
    def __init__(self, rows, column=True):
        self.rows, self.column = rows, column
    def table_exists(self, name): return name == "HERO GraphQL Mapping"
    def has_column(self, name, column): return self.column
    def sql(self, query):
        if query.startswith("SELECT name FROM `tabHERO GraphQL Mapping`"):
            return [("row",)] if any(row["definition_id"] == "" for row in self.rows) else []
        assert query == "UPDATE `tabHERO GraphQL Mapping` SET definition_id = NULL WHERE definition_id = ''"
        for row in self.rows:
            if row["definition_id"] == "": row["definition_id"] = None


class RegistryDB(FakeDB):
    def __init__(self, versions, binding=True):
        super().__init__([dict(name="legacy", definition_id=None)], binding=binding)
        self.versions = versions
    def table_exists(self, table):
        return table == "HERO Mapping Registry Version" or super().table_exists(table)
    def sql(self, query, values=None, as_dict=False):
        if query.startswith("SELECT mapping_payload_json, integrity_hash"):
            return [SimpleNamespace(mapping_payload_json=raw, integrity_hash=digest) for raw, digest in self.versions]
        if query.startswith("SELECT mapping_definition_id FROM `tabHERO"):
            return [SimpleNamespace(mapping_definition_id="registry-only")] if self.binding else []
        return super().sql(query, values, as_dict)


def version(definitions):
    raw = json.dumps({"format": FORMAT_ID, "schema_fingerprint": "a" * 64, "snapshot_name": "snapshot",
                      "definitions": [item.to_payload() for item in definitions], "availability": {}}, sort_keys=True, separators=(",", ":"))
    return raw, _integrity(raw)


class TestIdentityUpgradeGuards(unittest.TestCase):
    def test_presync_only_normalizes_empty_strings_and_is_idempotent(self):
        rows = [{"definition_id": ""}, {"definition_id": ""}, {"definition_id": None}, {"definition_id": "persisted"}]
        db = PreDB(rows)
        with patch.object(pre, "frappe", SimpleNamespace(db=db)):
            pre.execute(); pre.execute()
        self.assertEqual([row["definition_id"] for row in rows], [None, None, None, "persisted"])
    def test_invalid_registry_blocks_presync_before_empty_normalization(self):
        rows = [{"definition_id": ""}, {"definition_id": ""}]
        with patch.object(pre, "frappe", SimpleNamespace(db=PreDB(rows))), \
             patch.object(backfill, "_registry_definition_ids", side_effect=ValueError("invalid registry")):
            with self.assertRaisesRegex(ValueError, "invalid registry"): pre.execute()
        self.assertEqual([row["definition_id"] for row in rows], ["", ""])

    def test_presync_without_old_column_is_noop(self):
        rows = [{"definition_id": ""}]
        with patch.object(pre, "frappe", SimpleNamespace(db=PreDB(rows, column=False))): pre.execute()
        self.assertEqual(rows[0]["definition_id"], "")
    def test_registry_only_binding_is_accepted_and_duplicate_version_is_same_identity(self):
        entry = version([_definition("registry-only")])
        db = RegistryDB([entry, entry])
        TestIdentityBackfill().run_patch(db)
        self.assertEqual(len(db.updates), 1)
    def test_registry_identity_is_in_generated_collision_set(self):
        db = RegistryDB([version([_definition("f" * 32)])], binding=False)
        with patch.object(backfill, "uuid4", return_value=SimpleNamespace(hex="f" * 32)):
            with self.assertRaisesRegex(ValueError, "collided"): TestIdentityBackfill().run_patch(db)
        self.assertEqual(db.updates, [])
    def test_invalid_registry_or_duplicate_ids_fail_before_updates(self):
        bad = [('{', _integrity('{')), version([_definition("same"), _definition("same")]),
               ('{"format":"x","format":"x"}', _integrity('{"format":"x","format":"x"}')),
               (version([_definition("one")])[0], "bad")]
        for entry in bad:
            db = RegistryDB([entry], binding=False)
            with self.subTest(entry=entry[0][:1]), self.assertRaisesRegex(ValueError, "registry identity"):
                TestIdentityBackfill().run_patch(db)
            self.assertEqual(db.updates, [])
