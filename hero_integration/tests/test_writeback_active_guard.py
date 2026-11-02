"""Read/lock checks only; these tests never activate or update a mapping."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from hero_integration.hero_integration.doctype.hero_graphql_mapping import hero_graphql_mapping as controller
from hero_integration.hero_integration.doctype.hero_frappe_mapping import hero_frappe_mapping as locks


class TestWritebackActiveGuard(unittest.TestCase):
    def invoke(self, rows):
        sql_calls, lock_calls = [], []
        db = SimpleNamespace(sql=lambda query, values, as_dict: (sql_calls.append((query, values, as_dict)) or rows))
        fake = SimpleNamespace(_=lambda message: message, db=db, throw=lambda msg: (_ for _ in ()).throw(ValueError(msg)))
        lock = lambda *args: lock_calls.append(args)
        source = {"doctype": "Customer", "field": "customer_name"}
        with patch.object(controller, "frappe", fake), patch.object(locks, "_lock_active_target", side_effect=lock):
            controller._check_active_writeback_conflicts("SNAPSHOT", source, "MY-DRAFT")
        return lock_calls, sql_calls

    def test_shared_api_key_and_locking_read(self):
        locks_used, sql = self.invoke([])
        self.assertEqual(locks_used, [("writeback:SNAPSHOT", "Customer", "FRAPPE_TO_MUTATION:customer_name")])
        self.assertIn("FOR UPDATE", sql[0][0])
        self.assertEqual(sql[0][1], ("FRAPPE_TO_MUTATION", "Active", "SNAPSHOT", "MY-DRAFT"))

    def test_existing_source_metadata_is_not_ignored(self):
        mapping = {"source": {"doctype": "Customer", "field": "customer_name", "legacy": False}}
        with self.assertRaisesRegex(ValueError, "already owns"):
            self.invoke([SimpleNamespace(mapping_json=json.dumps(mapping))])

    def test_invalid_or_duplicate_source_fails_closed(self):
        duplicate = json.dumps({"source": {"doctype": "Customer", "field": "customer_name"}}).replace("Customer", "Customer\", \"doctype\": \"Other")
        for payload in ("{bad", "{}", duplicate):
            with self.subTest(payload=payload), self.assertRaisesRegex(ValueError, "cannot be safely inspected"):
                self.invoke([SimpleNamespace(mapping_json=payload)])

    def test_other_source_is_allowed(self):
        self.invoke([SimpleNamespace(mapping_json=json.dumps({"source": {"doctype": "Customer", "field": "other"}}))])
