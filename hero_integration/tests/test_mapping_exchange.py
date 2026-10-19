from __future__ import annotations

import json
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from hero_integration.services import mapping_exchange
from hero_integration.services import mapping_runtime
from hero_integration import api
from hero_integration.services.direct_mapping import SOURCE_CONTRACTS
from hero_integration.services.graphql_graph import analyze_snapshot
from hero_integration.tests.mapping_fixture import fixture_payload


SCHEMA = {"__schema": {"types": [{"kind": "OBJECT", "name": "Query", "fields": []}]}}
FINGERPRINT = analyze_snapshot(SCHEMA).fingerprint


class TestMappingExchange(unittest.TestCase):
    def setUp(self):
        self.frappe = _FakeFrappe(FINGERPRINT)
        self.patch = patch.object(mapping_exchange, "frappe", self.frappe)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_fresh_install_and_repeated_schema_setup_have_zero_mappings(self):
        self.assertEqual(self.frappe.count(), 0)
        self.assertEqual(self.frappe.count(), 0)

    def test_migration_is_blocked_before_enqueue_when_active_mapping_is_missing(self):
        calls = []
        fake_api_frappe = SimpleNamespace(
            only_for=lambda role: calls.append(("only_for", role)),
            enqueue=lambda *args, **kwargs: calls.append(("enqueue", args, kwargs)),
            throw=lambda message: (_ for _ in ()).throw(ValueError(message)),
        )
        with patch.object(api, "frappe", fake_api_frappe), patch.object(
            api, "_require_active_mappings", side_effect=ValueError("No active mapping definitions exist.")
        ):
            with self.assertRaisesRegex(ValueError, "No active mapping"):
                api.start_migration("Customer Migration")
        self.assertEqual(calls, [("only_for", "System Manager")])

    def test_export_import_export_is_structurally_equivalent_and_draft_only(self):
        payload = json.dumps(fixture_payload(FINGERPRINT))

        preview = mapping_exchange.preview_import("SYNTHETIC", payload)
        imported = mapping_exchange.import_json("SYNTHETIC", payload)
        exported = json.loads(mapping_exchange.export_json("SYNTHETIC"))

        self.assertEqual(preview["counts"]["New"], len(fixture_payload()["mappings"]))
        self.assertEqual(imported, {"created": 32, "changed": 0, "unchanged": 0, "status": "DRAFT_ONLY"})
        self.assertEqual({row["status"] for row in self.frappe.rows.values()}, {"Draft"})
        self.assertEqual(_normalized(exported), _normalized(fixture_payload(FINGERPRINT)))
        export_text = json.dumps(exported).lower()
        self.assertTrue(all(marker not in export_text for marker in ("token", "endpoint", "password", "source_payload_hash", "migration_log")))

    def test_identical_import_is_idempotent(self):
        payload = json.dumps(fixture_payload(FINGERPRINT))
        mapping_exchange.import_json("SYNTHETIC", payload)

        repeated = mapping_exchange.import_json("SYNTHETIC", payload)

        self.assertEqual(repeated, {"created": 0, "changed": 0, "unchanged": 32, "status": "DRAFT_ONLY"})
        self.assertEqual(self.frappe.count(), 32)

    def test_imported_fixture_requires_activation_before_runtime_uses_it(self):
        payload = json.dumps(fixture_payload(FINGERPRINT))
        mapping_exchange.import_json("SYNTHETIC", payload)
        self.assertEqual({row["status"] for row in self.frappe.rows.values()}, {"Draft"})
        for row in self.frappe.rows.values():
            row["status"] = "Active"

        with patch.dict(sys.modules, {"frappe": self.frappe}):
            customer = mapping_runtime.active_mapping_plan("HeroCustomers")
            project = mapping_runtime.active_mapping_plan("HeroProjectMatches")

        self.assertEqual(customer.values({"id": 1, "company_name": "", "full_name": "Synthetic Ada"})["customer_name"], "Synthetic Ada")
        self.assertEqual(project.values({"id": 2, "customer_id": 1, "project_title": "Synthetic Project"}, lambda value: "CUST-1", lambda: "Synthetic Company")["customer"], "CUST-1")

    def test_schema_mismatch_and_invalid_transformation_are_rejected(self):
        wrong_schema = fixture_payload("wrong")
        with self.assertRaisesRegex(mapping_exchange.MappingExchangeError, "fingerprint"):
            mapping_exchange.preview_import("SYNTHETIC", json.dumps(wrong_schema))

        invalid = fixture_payload(FINGERPRINT)
        invalid["mappings"][0]["transformation"] = "Invented Conversion"
        preview = mapping_exchange.preview_import("SYNTHETIC", json.dumps(invalid))
        self.assertEqual(preview["counts"]["Invalid"], 1)
        with self.assertRaisesRegex(mapping_exchange.MappingExchangeError, "conflicts or invalid"):
            mapping_exchange.import_json("SYNTHETIC", json.dumps(invalid))
        self.assertEqual(self.frappe.count(), 0)

    def test_oversized_import_is_rejected_before_preview(self):
        with self.assertRaisesRegex(mapping_exchange.MappingExchangeError, "permitted size"):
            mapping_exchange.preview_import("SYNTHETIC", "x" * (mapping_exchange.MAX_IMPORT_BYTES + 1))

    def test_active_local_change_is_a_conflict_without_mutation(self):
        payload = fixture_payload(FINGERPRINT)
        mapping_exchange.import_json("SYNTHETIC", json.dumps(payload))
        self.frappe.rows["customer-number"]["status"] = "Active"
        changed = fixture_payload(FINGERPRINT)
        next(item for item in changed["mappings"] if item["definition_id"] == "customer-number")["priority"] = 77

        preview = mapping_exchange.preview_import("SYNTHETIC", json.dumps(changed))

        item = next(item for item in preview["items"] if item["definition_id"] == "customer-number")
        self.assertEqual(item["state"], "Conflict")
        with self.assertRaisesRegex(mapping_exchange.MappingExchangeError, "conflicts"):
            mapping_exchange.import_json("SYNTHETIC", json.dumps(changed))
        self.assertEqual(self.frappe.rows["customer-number"]["priority"], 100)

    def test_duplicate_import_target_is_reported_before_any_draft_is_written(self):
        duplicate = fixture_payload(FINGERPRINT)
        copied = dict(next(item for item in duplicate["mappings"] if item["definition_id"] == "customer-number"))
        copied["definition_id"] = "customer-number-duplicate"
        duplicate["mappings"].append(copied)

        preview = mapping_exchange.preview_import("SYNTHETIC", json.dumps(duplicate))

        self.assertEqual(preview["counts"]["Conflict"], 1)
        with self.assertRaisesRegex(mapping_exchange.MappingExchangeError, "conflicts"):
            mapping_exchange.import_json("SYNTHETIC", json.dumps(duplicate))
        self.assertEqual(self.frappe.count(), 0)

    def test_fixture_accounts_for_every_selected_source_path(self):
        paths = {operation: set() for operation in SOURCE_CONTRACTS}
        for item in fixture_payload()["mappings"]:
            paths[item["operation"]].add(item["source_path"])
        self.assertEqual(paths, {operation: set(contract["paths"]) for operation, contract in SOURCE_CONTRACTS.items()})


class _FakeFrappe:
    def __init__(self, fingerprint):
        self.snapshot = SimpleNamespace(
            analysis_status="Validated", snapshot_json=json.dumps(SCHEMA), schema_fingerprint=fingerprint,
        )
        self.rows: dict[str, _Row] = {}
        self.db = SimpleNamespace(savepoint=lambda name: None, rollback=lambda save_point: None)

    def get_doc(self, *args):
        if args == ("HERO GraphQL Schema Snapshot", "SYNTHETIC"):
            return self.snapshot
        if len(args) == 2 and args[0] == "HERO Frappe Mapping":
            return _Document(self, self.rows[args[1]])
        if len(args) == 1 and isinstance(args[0], dict):
            return _Document(self, _Row(args[0]))
        raise AssertionError(f"Unexpected get_doc call: {args}")

    def get_all(self, doctype, filters=None, fields=None, order_by=None):
        if doctype != "HERO Frappe Mapping":
            raise AssertionError(f"Unexpected query: {doctype}")
        rows = list(self.rows.values())
        for field, expected in (filters or {}).items():
            rows = [row for row in rows if row.get(field) == expected]
        return [_Row({field: row.get(field, "") for field in fields}) for row in rows]

    @staticmethod
    def has_permission(doctype, permission, throw=False):
        return True

    @staticmethod
    def get_meta(doctype):
        return _Meta(doctype)

    def count(self):
        return len(self.rows)


class _Document:
    def __init__(self, frappe_api, row):
        self.frappe_api = frappe_api
        self.row = row

    def update(self, values):
        self.row.update(values)

    def save(self):
        self.row["schema_fingerprint"] = FINGERPRINT
        self.frappe_api.rows[self.row["definition_id"]] = self.row

    def insert(self):
        self.row["name"] = self.row["definition_id"]
        self.row["schema_fingerprint"] = FINGERPRINT
        self.frappe_api.rows[self.row["definition_id"]] = self.row

    def __setattr__(self, name, value):
        if name in {"frappe_api", "row"}:
            object.__setattr__(self, name, value)
        else:
            self.row[name] = value


class _Row(dict):
    __getattr__ = dict.__getitem__


class _Meta:
    def __init__(self, doctype):
        self.doctype = doctype

    def get_field(self, fieldname):
        if fieldname.startswith("custom_hero_"):
            return SimpleNamespace(fieldtype="Data", read_only=0, is_custom_field=1)
        return None


def _normalized(payload):
    return {
        **payload,
        "mappings": sorted(payload["mappings"], key=lambda item: (item["operation"], item["priority"], item["definition_id"])),
    }
