from __future__ import annotations

import json
import unittest

import frappe
from frappe.tests import IntegrationTestCase

from hero_integration import api
from hero_integration.services.runtime_plan import RuntimePlanSelection
from hero_integration.services.sync_runtime_adapter import _runtime_plan_service


class TestFrontendApiBoundaryFrappeIntegration(IntegrationTestCase):
    source_instance = "b11-frontend-boundary"
    employee = "b11-employee@example.invalid"
    second_admin = "b11-second-admin@example.invalid"

    def setUp(self):
        frappe.set_user("Administrator")
        self._remove_fixtures()
        self._create_employee_user(self.employee)
        self._create_user(self.second_admin, "System Manager")
        self.snapshot = frappe.get_doc({
            "doctype": "HERO GraphQL Schema Snapshot",
            "source_instance": self.source_instance,
            "snapshot_json": json.dumps({"__schema": {"queryType": {"name": "Query"}, "types": [
                {"kind": "OBJECT", "name": "Query", "fields": [
                    {"name": "b11Read", "args": [], "type": {"kind": "OBJECT", "name": "B11Record"}},
                ]},
                {"kind": "OBJECT", "name": "B11Record", "fields": [
                    {"name": "id", "args": [], "type": {"kind": "SCALAR", "name": "ID"}},
                ]},
                {"kind": "SCALAR", "name": "ID"},
            ]}}),
        }).insert()
        self.mapping = frappe.get_doc({
            "doctype": "HERO GraphQL Mapping", "schema_snapshot": self.snapshot.name,
            "operation_kind": "QUERY", "operation_name": "b11Read", "direction": "QUERY_TO_FRAPPE",
            "mapping_json": json.dumps({"record_path": "b11Read", "api_token": "must-not-leak"}), "status": "Draft",
        }).insert()
        self.service = frappe.get_doc({
            "doctype": "HERO GraphQL Service", "service_name": "B11 Frontend Service",
            "operation_name": "b11Read", "query_document": "query B11Read { b11Read { id } }",
            "schema_snapshot": self.snapshot.name, "read_mapping": self.mapping.name, "status": "Validated",
        }).insert()

    def tearDown(self):
        frappe.set_user("Administrator")
        self._remove_fixtures()

    def _create_user(self, email: str, role: str) -> None:
        user = frappe.get_doc({
            "doctype": "User", "email": email, "first_name": "B11", "send_welcome_email": 0,
            "user_type": "System User",
        }).insert(ignore_permissions=True)
        user.add_roles(role)
        frappe.clear_cache(user=email)
        frappe.cache.hdel("roles", email)
        frappe.db.commit()

    def _create_employee_user(self, email: str) -> None:
        self._create_user(email, "Desk User")
        frappe.get_doc({
            "doctype": "Has Role", "parent": email, "parenttype": "User", "parentfield": "roles", "role": "Employee",
        }).insert(ignore_permissions=True)
        frappe.clear_cache(user=email)
        frappe.cache.hdel("roles", email)
        frappe.db.commit()

    def _remove_fixtures(self):
        for doctype in ("HERO Runtime Plan Audit", "HERO Runtime Plan"):
            frappe.db.delete(doctype, {"source_instance": self.source_instance})
        frappe.db.delete("HERO GraphQL Service", {"service_name": "B11 Frontend Service"})
        frappe.db.delete("HERO GraphQL Mapping", {"operation_name": "b11Read"})
        frappe.db.delete("HERO GraphQL Schema Snapshot", {"source_instance": self.source_instance})
        for email in (self.employee, self.second_admin):
            frappe.db.delete("Has Role", {"parent": email, "parenttype": "User"})
            if frappe.db.exists("User", email):
                frappe.delete_doc("User", email, ignore_permissions=True, force=True)
        frappe.db.commit()

    def _selection(self):
        return RuntimePlanSelection(
            site_scope=frappe.local.site,
            source_instance=self.source_instance,
            graphql_service=self.service.name,
            service_revision=int(self.service.service_revision),
            operation_revision=int(self.service.service_revision),
            graphql_mapping=self.mapping.name,
            mapping_revision=int(self.mapping.mapping_revision),
            schema_snapshot=self.snapshot.name,
            schema_fingerprint=self.snapshot.schema_fingerprint,
            operation_name="b11Read",
            execution_mode="AUTO_SYNC",
        )

    def test_employee_read_admin_cas_activation_and_runtime_plan_approval(self):
        frappe.set_user(self.employee)
        employee_view = api.frontend_mapping_list()
        row = next(item for item in employee_view["graphql_mappings"] if item["name"] == self.mapping.name)
        self.assertEqual(row["status"], "Draft")
        self.assertNotIn("must-not-leak", repr(row))
        with self.assertRaises(frappe.PermissionError):
            api.graphql_mapping_save(
                self.snapshot.name, "QUERY", "b11Read", "QUERY_TO_FRAPPE", self.mapping.mapping_json,
                mapping_name=self.mapping.name, expected_revision=self.mapping.mapping_revision,
            )

        frappe.set_user("Administrator")
        unchanged = api.graphql_mapping_save(
            self.snapshot.name, "QUERY", "b11Read", "QUERY_TO_FRAPPE", self.mapping.mapping_json,
            mapping_name=self.mapping.name, expected_revision=self.mapping.mapping_revision,
        )
        self.assertTrue(unchanged["unchanged"])
        self.assertEqual(unchanged["revision"], 1)
        changed = api.graphql_mapping_save(
            self.snapshot.name, "QUERY", "b11Read", "QUERY_TO_FRAPPE", json.dumps({"record_path": "b11ReadChanged"}),
            mapping_name=self.mapping.name, expected_revision=1,
        )
        self.assertEqual((changed["status"], changed["revision"]), ("Draft", 2))
        with self.assertRaises(frappe.ValidationError):
            api.graphql_mapping_save(
                self.snapshot.name, "QUERY", "b11Read", "QUERY_TO_FRAPPE", json.dumps({"record_path": "again"}),
                mapping_name=self.mapping.name, expected_revision=1,
            )

        frappe.set_user(self.second_admin)
        with self.assertRaises(frappe.ValidationError):
            api.graphql_mapping_save(
                self.snapshot.name, "QUERY", "b11Read", "QUERY_TO_FRAPPE", json.dumps({"record_path": "b11ReadChanged"}),
                mapping_name=self.mapping.name, expected_revision=2, activate=1,
            )
        frappe.set_user("Administrator")
        activated = api.graphql_mapping_save(
            self.snapshot.name, "QUERY", "b11Read", "QUERY_TO_FRAPPE", json.dumps({"record_path": "b11ReadChanged"}),
            mapping_name=self.mapping.name, expected_revision=2, activate=1,
        )
        self.assertEqual((activated["status"], activated["revision"]), ("Active", 2))

        frappe.db.set_value("HERO GraphQL Service", self.service.name, {
            "execution_status": "Approved", "approved_service_revision": self.service.service_revision,
            "approved_mapping_revision": 2,
        })
        self.service.reload()
        self.mapping.reload()
        draft = _runtime_plan_service(frappe).create_draft(self._selection(), actor="Administrator")
        status = api.runtime_plan_status(self.source_instance)
        self.assertEqual((status["state"], status["revision"], status["generation"]), ("DRAFT", 1, 1))
        approved = api.runtime_plan_approve(draft.plan_key, expected_revision=1)
        self.assertEqual((approved["state"], approved["revision"], approved["generation"]), ("APPROVED", 2, 2))
        with self.assertRaises(frappe.ValidationError):
            api.runtime_plan_approve(draft.plan_key, expected_revision=1)

        with self.assertRaises(frappe.ValidationError):
            frappe.get_doc({
                "doctype": "HERO Runtime Plan", "plan_key": "e" * 64, "site_scope": frappe.local.site,
                "source_instance": "b11-raw-bypass", "state": "DRAFT", "plan_revision": 1, "generation": 1,
                "semantic_hash": "e" * 64, "graphql_service": self.service.name, "service_revision": 1,
                "operation_revision": 1, "graphql_mapping": self.mapping.name, "mapping_revision": 2,
                "schema_snapshot": self.snapshot.name, "schema_fingerprint": self.snapshot.schema_fingerprint,
                "operation_name": "b11Read", "execution_mode": "AUTO_SYNC",
            }).insert()

