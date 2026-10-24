from __future__ import annotations

import json

import frappe
from frappe.tests import IntegrationTestCase

from hero_integration.services.runtime_plan import (
    FrappeRuntimePlanAudit,
    FrappeRuntimePlanAuthority,
    FrappeRuntimePlanStorage,
    RuntimePlanConflict,
    RuntimePlanSelection,
    RuntimePlanService,
)


class _References:
    def validate_current(self, selection):
        return None


class TestRuntimePlanFrappeIntegration(IntegrationTestCase):
    fingerprint = "b" * 64
    source_instance = "b8a-test-source"

    def setUp(self):
        frappe.set_user("Administrator")
        self._remove_fixture_documents()
        snapshot = frappe.get_doc({
            "doctype": "HERO GraphQL Schema Snapshot",
            "snapshot_json": json.dumps({"__schema": {"queryType": {"name": "Query"}, "mutationType": {"name": "Mutation"}, "types": [
                {"kind": "OBJECT", "name": "Query", "fields": [{"name": "b8aTestRead", "args": [{"name": "first", "type": {"kind": "NON_NULL", "ofType": {"kind": "SCALAR", "name": "Int"}}}], "type": {"kind": "OBJECT", "name": "B8ARecord"}}]},
                {"kind": "OBJECT", "name": "Mutation", "fields": []},
                {"kind": "OBJECT", "name": "B8ARecord", "fields": [{"name": "id", "args": [], "type": {"kind": "SCALAR", "name": "ID"}}]},
            ]}}),
            "source_instance": self.source_instance,
        }).insert()
        mapping = frappe.get_doc({
            "doctype": "HERO GraphQL Mapping", "schema_snapshot": snapshot.name,
            "operation_kind": "QUERY", "operation_name": "b8aTestRead",
            "direction": "QUERY_TO_FRAPPE", "mapping_json": "{}", "status": "Draft",
        }).insert()
        service = frappe.get_doc({
            "doctype": "HERO GraphQL Service", "service_name": "B8A Test Service",
            "operation_name": "b8aTestRead",
            "query_document": "query B8A($first: Int!) { b8aTestRead(first: $first) { id } }",
            "schema_snapshot": snapshot.name, "read_mapping": mapping.name, "status": "Draft",
        }).insert()
        self.snapshot, self.mapping, self.graphql_service = snapshot, mapping, service

    def tearDown(self):
        self._remove_fixture_documents()

    def _remove_fixture_documents(self):
        for doctype in ("HERO Runtime Plan Audit", "HERO Runtime Plan"):
            frappe.db.delete(doctype, {"source_instance": self.source_instance})
        frappe.db.delete("HERO GraphQL Service", {"service_name": "B8A Test Service"})
        frappe.db.delete("HERO GraphQL Mapping", {"operation_name": "b8aTestRead"})
        frappe.db.delete("HERO GraphQL Schema Snapshot", {"source_instance": self.source_instance})

    def _selection(self, mapping_revision=1):
        return RuntimePlanSelection(
            site_scope=frappe.local.site,
            source_instance=self.source_instance,
            graphql_service=self.graphql_service.name,
            service_revision=1,
            operation_revision=1,
            graphql_mapping=self.mapping.name,
            mapping_revision=mapping_revision,
            schema_snapshot=self.snapshot.name,
            schema_fingerprint=self.snapshot.schema_fingerprint,
            operation_name="b8aTestRead",
            execution_mode="AUTO_SYNC",
        )

    def _service(self):
        return RuntimePlanService(
            FrappeRuntimePlanStorage(),
            _References(),
            FrappeRuntimePlanAudit(),
            FrappeRuntimePlanAuthority(),
        )

    def test_real_mariadb_lifecycle_cas_and_audit(self):
        service = self._service()
        draft = service.create_draft(self._selection(), actor="Administrator")
        self.assertEqual(frappe.db.count("HERO Runtime Plan", {"plan_key": draft.plan_key}), 1)
        self.assertEqual(frappe.db.count("HERO Runtime Plan Audit", {"runtime_plan": draft.plan_key}), 1)

        unchanged = service.update_draft(
            draft.plan_key, self._selection(), expected_revision=draft.revision, actor="Administrator"
        )
        self.assertEqual((unchanged.revision, unchanged.generation), (1, 1))
        changed = service.update_draft(
            draft.plan_key, self._selection(mapping_revision=2), expected_revision=1, actor="Administrator"
        )
        self.assertEqual((changed.revision, changed.generation), (2, 2))
        with self.assertRaises(RuntimePlanConflict):
            service.update_draft(draft.plan_key, self._selection(), expected_revision=1, actor="Administrator")

        approved = service.approve(changed.plan_key, expected_revision=2, actor="Administrator")
        stored = FrappeRuntimePlanStorage().read(approved.plan_key)
        self.assertEqual((stored.state.value, stored.revision, stored.generation), ("APPROVED", 3, 3))
        self.assertEqual(frappe.db.count("HERO Runtime Plan Audit", {"runtime_plan": approved.plan_key}), 3)
