from __future__ import annotations

import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from hero_integration import api


class _Mapping:
    def __init__(self, *, modified_by="Administrator", revision=3, status="Draft"):
        self.name = "HERO-GQL-MAP-00001"
        self.schema_snapshot = "SNAPSHOT"
        self.schema_fingerprint = "f" * 64
        self.source_instance = "isolated-source"
        self.operation_kind = "QUERY"
        self.operation_name = "readThings"
        self.direction = "QUERY_TO_FRAPPE"
        self.mapping_json = json.dumps({"record_path": "things"}, sort_keys=True)
        self.status = status
        self.mapping_revision = revision
        self.suggestion_reason = ""
        self.confidence = 0
        self.modified_by = modified_by
        self.save_calls = 0

    def is_new(self):
        return False

    def save(self):
        self.save_calls += 1


class _Frappe:
    PermissionError = PermissionError
    def __init__(self, *, roles=("System Manager",), user="Administrator", mapping=None):
        self._roles = roles
        self.session = SimpleNamespace(user=user)
        self.local = SimpleNamespace(site="b9-acceptance-41i.localhost")
        self.mapping = mapping or _Mapping()
        self.only_for_calls = []
        self.get_all_calls = []
        self.db = SimpleNamespace(count=lambda doctype, filters: 1 if doctype == "HERO Field Binding" else 0)

    def only_for(self, role):
        self.only_for_calls.append(role)
        if role not in self._roles:
            raise PermissionError("not permitted")

    def get_roles(self):
        return self._roles

    def throw(self, message, exception=ValueError):
        raise exception(message)

    def get_doc(self, doctype, name, **kwargs):
        assert (doctype, name) == ("HERO GraphQL Mapping", self.mapping.name)
        return self.mapping

    def new_doc(self, _doctype):
        raise AssertionError("this test only exercises existing mapping behavior")

    def get_all(self, doctype, **kwargs):
        self.get_all_calls.append((doctype, kwargs))
        if doctype == "HERO GraphQL Mapping":
            return [{
                "name": self.mapping.name,
                "schema_snapshot": self.mapping.schema_snapshot,
                "schema_fingerprint": self.mapping.schema_fingerprint,
                "source_instance": self.mapping.source_instance,
                "operation_kind": self.mapping.operation_kind,
                "operation_name": self.mapping.operation_name,
                "direction": self.mapping.direction,
                "status": self.mapping.status,
                "mapping_revision": self.mapping.mapping_revision,
                "mapping_json": json.dumps({
                    "record_path": "things",
                    "variables": {"api_token": "must-not-leak"},
                    "secret": "must-not-leak",
                }),
            }]
        if doctype == "HERO Frappe Mapping":
            return [{
                "name": "HERO-MAP-00001", "definition_id": "definition-1", "operation": "Things",
                "source_path": "id", "source_type": "ID", "target_doctype": "Customer",
                "target_field": "customer_name", "purpose": "Field", "transformation": "Identity",
                "relation": "", "priority": 5, "status": "Active", "mapping_revision": 2,
                "schema_snapshot": "SNAPSHOT", "schema_fingerprint": "f" * 64,
            }]
        if doctype == "HERO Field Binding":
            return [{
                "identity_key": "field-binding-1", "object_binding": "object-binding-1",
                "source_instance": "isolated-source", "source_field": "name", "target_app": "erpnext",
                "target_doctype": "Customer", "target_name": "CUST-0001", "target_field": "customer_name",
                "mapping_definition_id": "definition-1", "mapping_revision": 2,
                "direction": "HERO_TO_FRAPPE", "binding_revision": 3, "sync_state": "SYNCHRONIZED",
                "acknowledged_version": 1,
            }]
        if doctype == "HERO Object Binding":
            return [{
                "identity_key": "object-binding-1", "source_instance": "isolated-source", "object_type": "Contact",
                "external_object_id": "hero-contact-1", "target_app": "erpnext", "target_doctype": "Customer",
                "target_name": "CUST-0001", "mapping_definition_id": "definition-1", "mapping_revision": 2,
                "binding_revision": 3, "lifecycle": "VALID",
            }]
        raise AssertionError(doctype)


class _RuntimeService:
    def __init__(self, plan):
        self.plan = plan
        self.approve_calls = []

    def current(self, site_scope, source_instance):
        assert (site_scope, source_instance) == ("b9-acceptance-41i.localhost", "isolated-source")
        return self.plan

    def approve(self, plan_key, *, expected_revision, actor):
        self.approve_calls.append((plan_key, expected_revision, actor))
        return SimpleNamespace(
            plan_key=plan_key,
            state=SimpleNamespace(value="APPROVED"),
            revision=expected_revision + 1,
            generation=self.plan.generation + 1,
            approved_by=actor,
            selection=self.plan.selection,
        )


def _plan():
    return SimpleNamespace(
        plan_key="a" * 64,
        state=SimpleNamespace(value="DRAFT"),
        revision=4,
        generation=7,
        approved_by="",
        selection=SimpleNamespace(
            source_instance="isolated-source", graphql_service="HERO-GQL-00001", service_revision=3,
            operation_revision=3, graphql_mapping="HERO-GQL-MAP-00001", mapping_revision=2,
            schema_snapshot="SNAPSHOT", schema_fingerprint="f" * 64, operation_name="readThings",
            execution_mode="AUTO_SYNC",
        ),
    )


class TestFrontendApiBoundary(unittest.TestCase):
    def test_direction_filter_precedes_pagination_and_count(self):
        class Mixed(_Frappe):
            def get_all(self, doctype, **kwargs):
                self.get_all_calls.append((doctype, kwargs))
                if doctype == 'HERO Frappe Mapping':
                    rows = [{'name': 'rule-1', 'definition_id': 'rule', 'status': 'Active'}]
                else:
                    rows = [dict(name=f'map-{index}', definition_id=f'def-{index}', status='Active',
                                 direction='QUERY_TO_FRAPPE' if index % 2 == 0 else 'FRAPPE_TO_MUTATION',
                                 mapping_revision=1, mapping_json='{}') for index in range(6)]
                filters = kwargs.get('filters') or {}
                rows = [row for row in rows if all(row.get(k) == v for k, v in filters.items())]
                if kwargs.get('fields') == [{'COUNT': 'name', 'as': 'total'}]:
                    return [{'total': len(rows)}]
                start, limit = kwargs.get('limit_start', 0), kwargs.get('limit', 100)
                return rows[start:start + limit]
        frappe = Mixed(roles=('Employee',))
        with patch.object(api, 'frappe', frappe):
            q0 = api.frontend_mapping_list(start=0, page_size=1, direction='QUERY_TO_FRAPPE')
            q1 = api.frontend_mapping_list(start=1, page_size=1, direction='QUERY_TO_FRAPPE')
            m0 = api.frontend_mapping_list(start=0, page_size=1, direction='FRAPPE_TO_MUTATION')
            all_rows = api.frontend_mapping_list(start=0, page_size=10)
            with self.assertRaises(ValueError):
                api.frontend_mapping_list(direction='OTHER')
        self.assertEqual([q0['graphql_mappings'][0]['name'], q1['graphql_mappings'][0]['name']], ['map-0', 'map-2'])
        self.assertEqual((q0['pagination']['graphql_total'], q1['pagination']['graphql_total']), (3, 3))
        self.assertEqual(m0['graphql_mappings'][0]['name'], 'map-1')
        self.assertEqual((m0['pagination']['graphql_total'], m0['pagination']['frappe_total'], m0['frappe_mappings']), (3, 0, []))
        self.assertEqual(all_rows['pagination']['graphql_total'], 6)

    def test_employee_can_read_only_sanitized_mapping_status(self):
        frappe = _Frappe(roles=("Employee",), user="employee@example.invalid", mapping=_Mapping(status="Active"))
        with patch.object(api, "frappe", frappe):
            result = api.frontend_mapping_list()

        self.assertEqual(result["graphql_mappings"][0]["status"], "Active")
        self.assertEqual(result["frappe_mappings"][0]["status"], "Active")
        self.assertEqual(result["graphql_mappings"][0]["definition"], {"record_path": "things"})
        self.assertNotIn("must-not-leak", repr(result))
        self.assertNotIn("mapping_json", result["graphql_mappings"][0])

    def test_non_employee_cannot_read_mapping_status(self):
        frappe = _Frappe(roles=("Guest",), user="guest@example.invalid")
        with patch.object(api, "frappe", frappe):
            with self.assertRaises(PermissionError):
                api.frontend_mapping_list()
        self.assertEqual(frappe.get_all_calls, [])

    def test_active_projection_keeps_mutation_argument_and_input_path_but_not_values(self):
        projection = api._sanitized_mapping_definition({
            "source": {"app": "frappe", "doctype": "Customer", "field": "customer_name"},
            "target": {"operation": "updateCustomer", "argument": "input", "field_path": "name", "input_type": "CustomerInput"},
            "variables": {"value": "must-not-leak"}, "token": "must-not-leak",
        })
        self.assertEqual(projection["source"], {"app": "frappe", "doctype": "Customer", "field": "customer_name"})
        self.assertEqual(projection["target"], {"operation": "updateCustomer", "argument": "input", "field_path": "name", "input_type": "CustomerInput"})
        self.assertNotIn("must-not-leak", repr(projection))

    def test_binding_projection_is_system_manager_only_and_uses_existing_binding_identities(self):
        frappe = _Frappe()
        with patch.object(api, "frappe", frappe):
            result = api.frontend_mapping_bindings("definition-1")

        self.assertEqual(result["pagination"]["total"], 1)
        item = result["items"][0]
        self.assertEqual(item["identity_key"], "field-binding-1")
        self.assertEqual(item["object"]["identity_key"], "object-binding-1")
        self.assertNotIn("reconciliation", repr(result))
        self.assertEqual(frappe.get_all_calls[0][1]["filters"], {
            "site_scope": "b9-acceptance-41i.localhost", "mapping_definition_id": "definition-1",
        })

        employee = _Frappe(roles=("Employee",))
        with patch.object(api, "frappe", employee):
            with self.assertRaises(PermissionError):
                api.frontend_mapping_bindings("definition-1")
        self.assertEqual(employee.get_all_calls, [])

    def test_noop_save_preserves_revision_and_stale_edit_is_rejected(self):
        mapping = _Mapping(revision=3)
        frappe = _Frappe(mapping=mapping)
        with patch.object(api, "frappe", frappe), patch.object(api, '_lock_mapping_save_context', return_value={}):
            saved = api.graphql_mapping_save(
                "SNAPSHOT", "QUERY", "readThings", "QUERY_TO_FRAPPE", mapping.mapping_json,
                mapping_name=mapping.name, expected_revision=3,
            )
            with self.assertRaisesRegex(ValueError, "revision is stale"):
                api.graphql_mapping_save(
                    "SNAPSHOT", "QUERY", "readThings", "QUERY_TO_FRAPPE", mapping.mapping_json,
                    mapping_name=mapping.name, expected_revision=2,
                )

        self.assertTrue(saved["unchanged"])
        self.assertEqual(saved["revision"], 3)
        self.assertEqual(mapping.save_calls, 0)

    def test_other_administrator_cannot_activate_another_draft(self):
        mapping = _Mapping(modified_by="draft-owner@example.invalid")
        frappe = _Frappe(user="second-admin@example.invalid", mapping=mapping)
        with patch.object(api, "frappe", frappe), patch.object(api, '_lock_mapping_save_context', return_value={}):
            with self.assertRaisesRegex(ValueError, "last changed this Draft"):
                api.graphql_mapping_save(
                    "SNAPSHOT", "QUERY", "readThings", "QUERY_TO_FRAPPE", mapping.mapping_json,
                    mapping_name=mapping.name, expected_revision=3, activate=1,
                )
        self.assertEqual(mapping.status, "Draft")
        self.assertEqual(mapping.save_calls, 0)

    def test_runtime_plan_status_and_explicit_approval_are_administrative_and_revision_aware(self):
        frappe = _Frappe()
        service = _RuntimeService(_plan())
        with patch.object(api, "frappe", frappe), patch(
            "hero_integration.services.sync_runtime_adapter._runtime_plan_service", return_value=service
        ):
            status = api.runtime_plan_status("isolated-source")
            approved = api.runtime_plan_approve("a" * 64, expected_revision=4)

        self.assertEqual(status["state"], "DRAFT")
        self.assertEqual(status["generation"], 7)
        self.assertEqual(approved["state"], "APPROVED")
        self.assertEqual(approved["generation"], 8)
        self.assertEqual(service.approve_calls, [("a" * 64, 4, "Administrator")])

