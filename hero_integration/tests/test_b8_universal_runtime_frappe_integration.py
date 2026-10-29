from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from hero_integration import api, tasks
from hero_integration.services import b8_runtime
from hero_integration.services.binding_registry import BindingRegistryService, FrappeBindingStorage
from hero_integration.services.mapping_contract_v1 import QueryAdapter, QueryDraft, SnapshotAdapter, StoredSnapshot
from hero_integration.services.mapping_registry_model import MappingDirection
from hero_integration.services.runtime_plan import RuntimePlanSelection
from hero_integration.services.sync_engine import ChangePage, FrappeSyncLedger, SyncStatus
from hero_integration.services.sync_provider_capabilities import ProviderCapability, ProviderContract, guarded_provider
from hero_integration.services.sync_runtime_adapter import _runtime_plan_service


class TestB8UniversalRuntimeFrappeIntegration(IntegrationTestCase):
    """The B8 seam is tasks/API -> approved plan -> B2/B6/B7 persistence."""

    source = "b9-universal-delta-source"

    def setUp(self):
        frappe.set_user("Administrator")
        self._remove()
        self.settings = frappe.get_single("HERO Integration Settings")
        self.original_settings = (
            self.settings.hero_source_instance, self.settings.polling_mode,
            self.settings.allow_graphql_mutation_execution,
        )
        self.settings.hero_source_instance, self.settings.polling_mode = self.source, "AUTO_SYNC"
        self.settings.save()
        self._create_approved_universal_plan()
        token = frappe.generate_hash(length=10)
        self.bound_old, self.bound_new = f"B9Universal{token}Old", f"B9Universal{token}New"
        self.bound_pending, self.bound_changed = f"B9Universal{token}Pending", f"B9Universal{token}Changed"
        self.unbound_old = f"B9Universal{token}Unbound"
        self.bound = frappe.get_doc({"doctype": "Customer", "customer_name": self.bound_old, "customer_type": "Company"}).insert()
        self.unbound = frappe.get_doc({"doctype": "Customer", "customer_name": self.unbound_old, "customer_type": "Company"}).insert()
        self.bindings = BindingRegistryService(FrappeBindingStorage())
        parent = self.bindings.create_or_resolve_object_binding(
            site_scope=frappe.local.site, source_instance=self.source, object_type="ProjectMatch",
            external_object_id="1", target_app="erpnext", target_doctype="Customer",
            target_name=self.bound.name, mapping_definition_id="b9-universal-name", mapping_revision=1,
        )
        self.binding = self.bindings.create_or_resolve_field_binding(
            parent, source_field="name", target_field="customer_name", mapping_definition_id="b9-universal-name",
            mapping_revision=1, direction=MappingDirection.HERO_TO_FRAPPE,
        )

    def tearDown(self):
        (
            self.settings.hero_source_instance, self.settings.polling_mode,
            self.settings.allow_graphql_mutation_execution,
        ) = self.original_settings
        self.settings.save()
        self._remove()
        for name in (self.bound.name, self.unbound.name):
            frappe.db.delete("Customer", name)
        frappe.db.commit()

    def _remove(self):
        frappe.db.delete("HERO Sync Run Ledger", {"site_scope": frappe.local.site})
        frappe.db.delete("HERO Sync State Ledger", {"site_scope": frappe.local.site})
        frappe.db.delete("HERO Field Binding", {"source_instance": self.source})
        frappe.db.delete("HERO Object Binding", {"source_instance": self.source})
        frappe.db.delete("HERO Runtime Plan Audit", {"source_instance": self.source})
        frappe.db.delete("HERO Runtime Plan", {"source_instance": self.source})
        frappe.db.delete("HERO Mutation Audit", {"site": frappe.local.site})
        frappe.db.delete("HERO Mutation Contract", {"graphql_service": ["in", ["B9 Universal Mutation"]]})
        frappe.db.delete("HERO GraphQL Service", {"service_name": ["in", ["B9 Universal Delta", "B9 Universal Mutation"]]})
        frappe.db.delete("HERO GraphQL Mapping", {"source_instance": self.source})
        frappe.db.delete("HERO GraphQL Schema Snapshot", {"source_instance": self.source})
        frappe.db.commit()

    def _create_approved_universal_plan(self):
        schema = {"__schema": {"queryType": {"name": "Query"}, "mutationType": {"name": "Mutation"}, "types": [
            {"kind": "OBJECT", "name": "Query", "fields": [{"name": "project_matches", "args": [
                {"name": name, "type": {"kind": "NON_NULL", "ofType": {"kind": "SCALAR", "name": kind}}}
                for name, kind in (("first", "Int"), ("offset", "Int"), ("orderBy", "String"))
            ], "type": {"kind": "LIST", "ofType": {"kind": "OBJECT", "name": "ProjectMatch"}}}]},
            {"kind": "OBJECT", "name": "ProjectMatch", "fields": [
                {"name": "id", "args": [], "type": {"kind": "SCALAR", "name": "ID"}},
                {"name": "name", "args": [], "type": {"kind": "SCALAR", "name": "String"}},
            ]},
            {"kind": "OBJECT", "name": "Mutation", "fields": [{"name": "update_contact", "args": [
                {"name": "contact", "type": {"kind": "NON_NULL", "ofType": {"kind": "INPUT_OBJECT", "name": "CustomerInput"}}}
            ], "type": {"kind": "OBJECT", "name": "Contact"}}]},
            {"kind": "OBJECT", "name": "Contact", "fields": [{"name": "id", "args": [], "type": {"kind": "SCALAR", "name": "ID"}}]},
            {"kind": "INPUT_OBJECT", "name": "CustomerInput", "inputFields": [
                {"name": "id", "type": {"kind": "NON_NULL", "ofType": {"kind": "SCALAR", "name": "Int"}}},
                {"name": "full_name", "type": {"kind": "SCALAR", "name": "String"}},
            ]},
            *[{"kind": "SCALAR", "name": name} for name in ("ID", "Int", "String")],
        ]}}
        snapshot = frappe.get_doc({"doctype": "HERO GraphQL Schema Snapshot", "source_instance": self.source, "snapshot_json": json.dumps(schema)}).insert()
        draft = QueryDraft("project_matches", "project_matches", "project_matches", ("id", "name"), {"first": 1, "offset": 0, "orderBy": "id"})
        request = QueryAdapter().materialize(
            SnapshotAdapter().adapt(StoredSnapshot(snapshot.name, snapshot.snapshot_json, snapshot.schema_fingerprint, self.source)), draft
        )
        provisional = RuntimePlanSelection(
            frappe.local.site, self.source, "B8-SERVICE", 1, 1, "B8-MAPPING", 1,
            snapshot.name, snapshot.schema_fingerprint, "project_matches", "AUTO_SYNC",
        )
        payload = {
            "target_operation": "HeroProjectMatches", "records_path": "project_matches", "identity_path": "id",
            "mapping_definition_ids": ["b9-universal-name"],
            "variables": {"first": "$page_size", "offset": "$offset", "orderBy": "id"}, "page_size": 1,
            "b8_compilation": {"version": 1, "operation_name": "project_matches", "alias": "project_matches", "field": "project_matches", "selected_paths": ["id", "name"], "variables": {"first": 1, "offset": 0, "orderBy": "id"}},
            "b8_canonical_operation": b8_runtime.canonical_operation_contract(provisional, request),
        }
        mapping = frappe.get_doc({
            "doctype": "HERO GraphQL Mapping", "schema_snapshot": snapshot.name, "operation_kind": "QUERY",
            "operation_name": "project_matches", "direction": "QUERY_TO_FRAPPE", "mapping_json": json.dumps(payload), "status": "Active",
        }).insert()
        service = frappe.get_doc({
            "doctype": "HERO GraphQL Service", "service_name": "B9 Universal Delta", "operation_name": "project_matches",
            "query_document": request.document, "schema_snapshot": snapshot.name, "read_mapping": mapping.name, "status": "Validated",
        }).insert()
        frappe.db.set_value("HERO GraphQL Service", service.name, {
            "execution_status": "Approved", "approved_service_revision": service.service_revision,
            "approved_mapping_revision": mapping.mapping_revision,
        })
        service.reload()
        self.selection = RuntimePlanSelection(
            frappe.local.site, self.source, service.name, int(service.service_revision), int(service.service_revision),
            mapping.name, int(mapping.mapping_revision), snapshot.name, snapshot.schema_fingerprint, "project_matches", "AUTO_SYNC",
        )
        self.assertEqual(b8_runtime.canonical_operation_contract(self.selection, request), payload["b8_canonical_operation"])
        self.snapshot = snapshot
        plans = _runtime_plan_service(frappe)
        draft_plan = plans.create_draft(self.selection, actor="Administrator")
        self.plan = plans.approve(draft_plan.plan_key, expected_revision=draft_plan.revision, actor="Administrator")
        self.assertEqual(b8_runtime.reconstruct_materialized_read_plan(self.selection).request, request)

    def test_scheduler_and_pending_sync_use_the_approved_universal_contract(self):
        calls: list[str | None] = []
        phase = {"name": "scheduler"}

        def read_page(cursor):
            calls.append(cursor)
            if phase["name"] == "failure":
                raise RuntimeError("synthetic provider failure")
            if phase["name"] == "pending":
                return ChangePage(({"id": "1", "name": self.bound_pending},), None, True, "wm-2")
            return ChangePage((
                {"id": "1", "name": self.bound_new},
                {"id": "unbound-1", "name": "MUST-NOT-CREATE"},
            ), None, True, "wm-1")

        provider = guarded_provider(ProviderContract(ProviderCapability.COMPLETE_DELTA_CURSOR), read_page)
        with patch("hero_integration.services.sync_runtime_adapter._UnprovenProvider", return_value=provider):
            tasks.run_scheduled_sync()
        self.assertEqual(calls, [None])
        self.assertEqual(frappe.get_doc("Customer", self.bound.name).customer_name, self.bound_old)
        self.assertEqual(frappe.get_doc("Customer", self.unbound.name).customer_name, self.unbound_old)
        self.assertFalse(frappe.db.exists("Customer", {"customer_name": "MUST-NOT-CREATE"}))
        self.assertEqual((FrappeSyncLedger().state(frappe.local.site).cursor, FrappeSyncLedger().state(frappe.local.site).status), (None, SyncStatus.PARTIAL))
        self.assertEqual(FrappeBindingStorage().field(frappe.local.site, self.binding.identity_key).revision, self.binding.revision)

        with patch("hero_integration.services.sync_runtime_adapter._UnprovenProvider", return_value=provider):
            tasks.run_scheduled_sync()
        self.assertEqual(calls, [None, None])
        self.assertIsNone(FrappeSyncLedger().state(frappe.local.site).cursor)

        phase["name"] = "failure"
        with patch("hero_integration.services.sync_runtime_adapter._UnprovenProvider", return_value=provider):
            tasks.run_scheduled_sync()
        failed = FrappeSyncLedger().state(frappe.local.site)
        self.assertEqual((failed.cursor, failed.status), (None, SyncStatus.FAILED))

        self.settings.polling_mode = "OFF"
        self.settings.save()
        phase["name"] = "pending"
        with patch("hero_integration.services.sync_runtime_adapter._UnprovenProvider", return_value=provider):
            result = api.pending_sync(self.plan.generation)
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(frappe.get_doc("Customer", self.bound.name).customer_name, self.bound_old)
        self.assertIsNone(FrappeSyncLedger().state(frappe.local.site).cursor)

        before = list(calls)
        with patch("hero_integration.services.sync_runtime_adapter._UnprovenProvider", return_value=provider):
            with self.assertRaises(frappe.ValidationError):
                api.pending_sync(self.plan.generation - 1)
        self.assertEqual(calls, before)

    def test_sync_hero_uses_one_shot_synthetic_transport_and_restores_the_release_gate(self):
        mutation_service = frappe.get_doc({
            "doctype": "HERO GraphQL Service", "service_name": "B9 Universal Mutation", "operation_name": "update_contact",
            "query_document": "mutation UpdateContact($contact: CustomerInput!) { update_contact(contact: $contact) { id } }",
            "schema_snapshot": self.snapshot.name, "status": "Draft",
        }).insert()
        api.workspace_mutation_contract_save(mutation_service.name, json.dumps({"mode": "ADMIN_INPUT"}), activate=1)
        outbound = frappe.get_doc({
            "doctype": "HERO GraphQL Mapping", "schema_snapshot": self.snapshot.name, "operation_kind": "MUTATION",
            "operation_name": "update_contact", "direction": "FRAPPE_TO_MUTATION", "status": "Active",
            "mapping_json": json.dumps({
                "source": {"doctype": "Customer", "field": "customer_name"},
                "target": {"operation": "update_contact", "argument": "contact", "input_type": "CustomerInput", "field": "full_name"},
                "transformation": "Identity",
            }),
        }).insert()
        self.settings.allow_graphql_mutation_execution = 1
        self.settings.save()
        calls: list[tuple[str, dict[str, object]]] = []

        class SyntheticMutationClient:
            def __init__(self, *_args):
                pass

            def execute_once(self, document, variables):
                calls.append((document, variables))
                if len(calls) == 2:
                    raise TimeoutError("synthetic uncertain acknowledgement")
                return {"data": {"update_contact": {"id": "1"}}}

        configuration = SimpleNamespace(endpoint="https://synthetic.invalid/graphql", api_token="synthetic", timeout_seconds=1)
        with patch("hero_integration.client.graphql_mutation.HeroMutationClient", SyntheticMutationClient), patch(
            "hero_integration.client.settings.get_configuration", return_value=configuration
        ):
            preview = api.sync_hero_preview(self.binding.identity_key, outbound.name, self.plan.generation)
            self.assertTrue(preview["eligible"])
            result = api.sync_hero_execute(
                self.binding.identity_key, outbound.name, self.plan.generation, preview["binding_revision"],
                preview["request_fingerprint"], 1,
            )
            self.assertEqual(result["status"], "SUCCEEDED")
            acknowledged = FrappeBindingStorage().field(frappe.local.site, self.binding.identity_key)
            self.assertEqual(acknowledged.acknowledged_version, 1)
            self.assertEqual(len(calls), 1)
            replay = api.sync_hero_preview(self.binding.identity_key, outbound.name, self.plan.generation)
            self.assertFalse(replay["eligible"])
            with self.assertRaises(frappe.ValidationError):
                api.sync_hero_execute(
                    self.binding.identity_key, outbound.name, self.plan.generation, preview["binding_revision"],
                    preview["request_fingerprint"], 1,
                )
            self.assertEqual(len(calls), 1)

            customer = frappe.get_doc("Customer", self.bound.name)
            customer.customer_name = self.bound_changed
            customer.save()
            changed = api.sync_hero_preview(self.binding.identity_key, outbound.name, self.plan.generation)
            self.assertTrue(changed["eligible"])
            with self.assertRaises(frappe.ValidationError):
                api.sync_hero_execute(
                    self.binding.identity_key, outbound.name, self.plan.generation, changed["binding_revision"],
                    changed["request_fingerprint"], 1,
                )
            self.assertEqual(len(calls), 2)
            uncertain = FrappeBindingStorage().field(frappe.local.site, self.binding.identity_key)
            self.assertEqual(uncertain.acknowledged_version, 1)
            audit = frappe.get_all("HERO Mutation Audit", filters={"site": frappe.local.site}, fields=["status"], order_by="creation asc")
            self.assertEqual([row.status for row in audit], ["Succeeded", "Timeout"])
        self.settings.allow_graphql_mutation_execution = 0
        self.settings.save()
        self.assertEqual(frappe.get_single("HERO Integration Settings").allow_graphql_mutation_execution, 0)
