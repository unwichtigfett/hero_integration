from __future__ import annotations

import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from hero_integration.services import mapping_draft_exchange as exchange


class _Frappe:
    def __init__(self):
        self.local = SimpleNamespace(site="draft-exchange.test")
        self.session = SimpleNamespace(user="owner@example.invalid")
        self.db = SimpleNamespace(savepoint=lambda _name: None, rollback=lambda **_kwargs: None)
        self.active = [
            self._active("query-one", "QUERY", "readThings", "QUERY_TO_FRAPPE"),
            self._active("mutation-one", "MUTATION", "writeThings", "FRAPPE_TO_MUTATION"),
        ]
        self.drafts = [self._draft("query-one", "owner@example.invalid"), self._draft("mutation-one", "other@example.invalid")]

    @staticmethod
    def _active(definition_id, operation_kind, operation_name, direction):
        content = {"source": {"field": definition_id}, "value": 9007199254740993}
        return {
            "name": "MAP-" + definition_id, "definition_id": definition_id, "schema_fingerprint": "a" * 64,
            "source_instance": "source-1", "schema_snapshot": "SNAPSHOT", "status": "Active",
            "operation_kind": operation_kind, "operation_name": operation_name, "direction": direction,
            "mapping_revision": 3, "mapping_json": json.dumps(content, sort_keys=True),
        }

    def _draft(self, definition_id, owner):
        row = next(item for item in self.active if item["definition_id"] == definition_id)
        content = {"source": {"field": definition_id}, "value": 9007199254740993}
        return {
            "definition_id": definition_id, "owner_user": owner, "site_scope": self.local.site, "state": "OPEN",
            "content_json": json.dumps(content, sort_keys=True), "content_hash": exchange._hash(content),
            "generation": 1, "content_revision": 2, "base_active_mapping": row["name"],
            "base_active_revision": row["mapping_revision"], "base_active_hash": exchange._hash(content),
            "base_schema_fingerprint": row["schema_fingerprint"], "reconciliation_revision": 0,
        }

    def get_all(self, doctype, filters=None, fields=None, **_kwargs):
        filters = filters or {}
        if doctype == "HERO GraphQL Mapping":
            return [dict(row) for row in self.active if all(row.get(key) == value for key, value in filters.items())]
        if doctype == "HERO Mapping Draft":
            ids = set(filters["definition_id"][1])
            return [dict(row) for row in self.drafts if row["definition_id"] in ids and row["owner_user"] == filters["owner_user"]
                    and row["site_scope"] == filters["site_scope"] and row["state"] == filters["state"]]
        raise AssertionError(doctype)


def _schema_preview(_schema):
    return {"items": [], "counts": {state: 0 for state in ("New", "Unchanged", "Changed", "Conflict", "Invalid")}}


class TestMappingDraftExchange(unittest.TestCase):
    def setUp(self):
        self.frappe = _Frappe()
        self.patch = patch.object(exchange, "frappe", self.frappe)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.schema = patch.object(exchange, "preview_schema", side_effect=_schema_preview)
        self.schema_mock = self.schema.start()
        self.addCleanup(self.schema.stop)

    def package(self):
        return exchange.export_package("source-1", "SNAPSHOT", "QUERY_TO_FRAPPE")

    def test_export_and_preview_are_owner_scoped_directional_and_read_only(self):
        package = json.loads(self.package())
        self.assertEqual(["query-one"], [item["definition_id"] for item in package["mappings"]])
        self.assertEqual("QUERY_TO_FRAPPE", package["direction"])
        preview = exchange.preview_import(self.package(), "source-1", "SNAPSHOT", "QUERY_TO_FRAPPE")
        self.assertFalse(preview["persisted"])
        self.assertEqual(["UNCHANGED"], [item["state"] for item in preview["items"]])
        self.assertEqual(9007199254740993, package["mappings"][0]["content"]["value"])

    def test_additional_assignment_and_large_number_survive_export_and_preview(self):
        row = self.frappe.drafts[0]
        value = json.loads(row['content_json'])
        value['additional_mappings'] = [{'id': '8a6c58ba-08fb-4da4-87e6-684bb1b16e19',
            'source': {'field': 'nr'}, 'target': {'doctype': 'Customer', 'field': 'alias'},
            'transformation': 'Identity'}]
        row['content_json'] = json.dumps(value)
        row['content_hash'] = exchange._hash(value)
        exported = self.package()
        self.assertEqual(value, json.loads(exported)['mappings'][0]['content'])
        preview = exchange.preview_import(exported, 'source-1', 'SNAPSHOT', 'QUERY_TO_FRAPPE')
        self.assertEqual(['UNCHANGED'], [item['state'] for item in preview['items']])

    def test_only_selected_native_deadline_carries_the_fixed_schema(self):
        rule = {'source_path': 'due_date', 'target_doctype': 'Task', 'target_field': 'custom_hero_due_date',
                'purpose': 'Native Binding', 'transformation': 'Native Adapter'}
        item = {'content': {'target_operation': 'HeroTasks', 'native_task_onboarding': {'confirmed': True},
                            'mapping_definition_ids': ['due'], 'field_rules': {'due': rule}}}
        schema = exchange._export_schema([item], 'QUERY_TO_FRAPPE')
        self.assertEqual(schema['custom_fields'][0]['fieldname'], 'custom_hero_due_date')
        self.assertEqual(schema['custom_fields'][0]['read_only'], 1)
        item['content']['mapping_definition_ids'] = []
        self.assertEqual(exchange._export_schema([item], 'QUERY_TO_FRAPPE')['custom_fields'], [])
        item['content']['mapping_definition_ids'] = ['due']
        self.assertEqual(exchange._export_schema([item], 'FRAPPE_TO_MUTATION')['custom_fields'], [])

    def test_preview_reports_omitted_own_drafts_without_touching_other_direction_or_owner(self):
        package = json.loads(self.package())
        package["mappings"] = []
        preview = exchange.preview_import(json.dumps(package), "source-1", "SNAPSHOT", "QUERY_TO_FRAPPE")
        self.assertEqual(["REMOVE"], [item["state"] for item in preview["items"]])
        self.assertEqual("query-one", preview["items"][0]["definition_id"])

    def test_mixed_or_wrong_direction_is_rejected_before_schema_preview(self):
        package = json.loads(self.package())
        package["mappings"][0]["direction"] = "FRAPPE_TO_MUTATION"
        with self.assertRaisesRegex(exchange.MappingDraftExchangeError, "mixed or wrong"):
            exchange.preview_import(json.dumps(package), "source-1", "SNAPSHOT", "QUERY_TO_FRAPPE")
        self.schema_mock.assert_not_called()

    def test_save_uses_expected_states_and_discards_only_omitted_own_draft(self):
        package = json.loads(self.package())
        package["mappings"] = []
        preview = exchange.preview_import(json.dumps(package), "source-1", "SNAPSHOT", "QUERY_TO_FRAPPE")
        expected = [{"definition_id": item["definition_id"], **item["expected"]} for item in preview["items"]]
        calls = []

        class Service:
            def discard(self, *args, **kwargs):
                calls.append(("discard", args, kwargs))

        class Storage:
            def __init__(self, *_args):
                pass

            def service(self):
                return Service()

        with patch.object(exchange, "FrappeMappingDraftStorage", Storage), patch.object(
            exchange, "provision_schema", return_value={"status": "UNCHANGED", "created": {"custom_doctypes": [], "custom_fields": []}}
        ):
            result = exchange.save_import(json.dumps(package), "source-1", "SNAPSHOT", "QUERY_TO_FRAPPE", expected, confirmed=True)
        self.assertEqual(["query-one"], result["drafts"]["discarded"])
        self.assertEqual("discard", calls[0][0])
        self.assertEqual("query-one", calls[0][1][0])

    def test_stale_expected_state_is_rejected_before_schema_provisioning(self):
        preview = exchange.preview_import(self.package(), "source-1", "SNAPSHOT", "QUERY_TO_FRAPPE")
        expected = [{"definition_id": item["definition_id"], **item["expected"]} for item in preview["items"]]
        expected[0]["draft_revision"] += 1
        with patch.object(exchange, "provision_schema") as provision:
            with self.assertRaisesRegex(exchange.MappingDraftExchangeError, "state changed"):
                exchange.save_import(self.package(), "source-1", "SNAPSHOT", "QUERY_TO_FRAPPE", expected, confirmed=True)
        provision.assert_not_called()
