from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

from graphql import build_schema, introspection_from_schema

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
        self.original_binding = (self.settings.source_binding_revision, self.settings.source_binding_confirmed_revision)
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
        self.settings.reload()
        (
            self.settings.hero_source_instance, self.settings.polling_mode,
            self.settings.allow_graphql_mutation_execution,
        ) = self.original_settings
        self.settings.save()
        frappe.db.set_single_value("HERO Integration Settings", "source_binding_revision", self.original_binding[0])
        frappe.db.set_single_value("HERO Integration Settings", "source_binding_confirmed_revision", self.original_binding[1])
        self._remove()
        for name in (self.bound.name, self.unbound.name):
            frappe.db.delete("Customer", name)
        frappe.db.commit()

    def _remove(self):
        frappe.db.delete("HERO Polling Event", {"source_instance": self.source})
        frappe.db.delete("HERO Sync Run Ledger", {"site_scope": frappe.local.site})
        frappe.db.delete("HERO Sync State Ledger", {"site_scope": frappe.local.site})
        frappe.db.delete("HERO Frappe Mapping", {"name": "B9-UNIVERSAL-RULE"})
        frappe.db.delete("HERO Sync Change Event", {"source_instance": self.source})
        frappe.db.delete("HERO Execution Budget", {"source_instance": self.source})
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
        schema = introspection_from_schema(build_schema("""
            type Query { project_matches(first: Int!, offset: Int!, orderBy: String!): [ProjectMatch] project_match(project_match_id: Int!): ProjectMatch }
            type ProjectMatch { id: ID name: String }
            type Mutation { update_contact(contact: CustomerInput!): Contact }
            type Contact { id: ID }
            input CustomerInput { id: Int! full_name: String }
        """))
        from hero_integration.services.introspection import endpoint_fingerprint
        endpoint = self.settings.hero_api_url
        snapshot = frappe.get_doc({"doctype": "HERO GraphQL Schema Snapshot", "source_instance": self.source, "endpoint_fingerprint": endpoint_fingerprint(endpoint, self.source), "snapshot_json": json.dumps(schema)}).insert()
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
        # Persist the synthetic field revision named in the sealed B8 reader.
        frappe.get_doc({"doctype": "HERO Frappe Mapping", "name": "B9-UNIVERSAL-RULE",
            "definition_id": "b9-universal-name", "operation": "HeroProjectMatches", "source_path": "name",
            "source_type": "String", "target_doctype": "Customer", "target_field": "customer_name",
            "purpose": "Field", "transformation": "Identity", "schema_snapshot": snapshot.name,
            "schema_fingerprint": snapshot.schema_fingerprint, "mapping_revision": 1,
            "status": "Active", "priority": 5}).db_insert()
        self.snapshot = snapshot
        plans = _runtime_plan_service(frappe)
        draft_plan = plans.create_draft(self.selection, actor="Administrator")
        self.plan = plans.approve(draft_plan.plan_key, expected_revision=draft_plan.revision, actor="Administrator")
        self.assertEqual(b8_runtime.reconstruct_materialized_read_plan(self.selection).request, request)

    def test_scheduler_dispatch_and_pending_sync_use_the_approved_contract(self):
        calls: list[str | None] = []
        phase = {"name": "scheduler"}

        def read_page(cursor):
            calls.append(cursor)
            if phase["name"] == "failure":
                raise RuntimeError("synthetic provider failure")
            if phase["name"] == "pending":
                return ChangePage(({"id": "unbound-1", "name": self.bound_pending},), None, True, "wm-2")
            return ChangePage((
                {"id": "1", "name": self.bound_new},
                {"id": "unbound-1", "name": "MUST-NOT-CREATE"},
            ), None, True, "wm-1")

        provider = guarded_provider(ProviderContract(ProviderCapability.COMPLETE_DELTA_CURSOR), read_page)
        with patch.object(tasks, "_run") as dispatch, patch("frappe.utils.scheduler.is_scheduler_inactive", return_value=True):
            tasks.run_scheduled_sync()
        dispatch.assert_not_called()
        self.assertEqual(calls, [])
        self.settings.reload()
        self.settings.polling_mode = "OFF"
        self.settings.save()
        frappe.db.set_single_value("HERO Integration Settings", "source_binding_confirmed_revision", self.settings.source_binding_revision)
        with patch("hero_integration.services.sync_runtime_adapter._UnprovenProvider", return_value=provider):
            api.pending_sync(self.plan.generation)
        self.assertEqual(calls, [None])
        self.assertEqual(frappe.get_doc("Customer", self.bound.name).customer_name, self.bound_old)
        self.assertEqual(frappe.get_doc("Customer", self.unbound.name).customer_name, self.unbound_old)
        self.assertFalse(frappe.db.exists("Customer", {"customer_name": "MUST-NOT-CREATE"}))
        self.assertEqual((FrappeSyncLedger().state(frappe.local.site).cursor, FrappeSyncLedger().state(frappe.local.site).status), (None, SyncStatus.INCOMPLETE))
        self.assertEqual(FrappeBindingStorage().field(frappe.local.site, self.binding.identity_key).revision, self.binding.revision)

        with patch("hero_integration.services.sync_runtime_adapter._UnprovenProvider", return_value=provider):
            api.pending_sync(self.plan.generation)
        self.assertEqual(calls, [None, None])
        self.assertIsNone(FrappeSyncLedger().state(frappe.local.site).cursor)

        phase["name"] = "failure"
        with patch("hero_integration.services.sync_runtime_adapter._UnprovenProvider", return_value=provider):
            api.pending_sync(self.plan.generation)
        failed = FrappeSyncLedger().state(frappe.local.site)
        self.assertEqual((failed.cursor, failed.status), (None, SyncStatus.FAILED))

        phase["name"] = "pending"
        with patch("hero_integration.services.sync_runtime_adapter._UnprovenProvider", return_value=provider):
            result = api.pending_sync(self.plan.generation)
        self.assertEqual(result["status"], "SUCCEEDED")
        self.assertEqual(frappe.get_doc("Customer", self.bound.name).customer_name, self.bound_old)
        self.assertEqual(FrappeSyncLedger().state(frappe.local.site).cursor, "wm-2")

        before = list(calls)
        with patch("hero_integration.services.sync_runtime_adapter._UnprovenProvider", return_value=provider):
            with self.assertRaises(frappe.ValidationError):
                api.pending_sync(self.plan.generation - 1)
        self.assertEqual(calls, before)

    def test_scheduled_unproved_delta_references_its_own_durable_run(self):
        from hero_integration.client.graphql import HeroGraphQLClient
        from hero_integration.services.polling import record_scheduler_outcome

        frappe.db.set_single_value("HERO Integration Settings", "source_binding_confirmed_revision",
                                   self.settings.source_binding_revision)
        with patch.object(HeroGraphQLClient, "query") as transport:
            tasks._run("AUTO_SYNC", record_scheduler_outcome)
            transport.assert_not_called()
        event = frappe.db.get_value("HERO Polling Event", {"source_instance": self.source},
                                    ["status", "sync_run"], as_dict=True)
        self.assertEqual(event.status, "Incomplete")
        receipt = frappe.get_doc("HERO Sync Run Ledger", event.sync_run)
        self.assertEqual((receipt.source_instance, receipt.plan_key, receipt.state),
                         (self.source, self.plan.plan_key, "INCOMPLETE"))
        self.assertEqual(frappe.db.count("HERO Integration Run", {"source_instance": self.source}), 0)

    def test_one_shot_requires_approval_and_replays_its_durable_off_mode_receipt(self):
        from uuid import uuid4
        from hero_integration.client.graphql import HeroGraphQLClient
        from hero_integration.services.sync_runtime_adapter import RuntimeBridgeError

        self.settings.reload()
        self.settings.polling_mode = "OFF"
        self.settings.save()
        revision = self.settings.source_binding_revision
        frappe.db.set_single_value("HERO Integration Settings", "source_binding_confirmed_revision", revision)
        frappe.db.commit()
        args = dict(request_id=uuid4().hex, expected_source_revision=revision, plan_key=self.plan.plan_key,
                    expected_plan_generation=self.plan.generation, scope="DELTA", confirmed=1)
        with patch.object(HeroGraphQLClient, "query", side_effect=AssertionError) as transport:
            with self.assertRaises(RuntimeBridgeError):
                api.start_sync_once(**(args | {"confirmed": 0}))
            self.assertFalse(frappe.db.exists("HERO Sync Run Ledger", args["request_id"]))
            receipt = api.start_sync_once(**args)
            self.assertEqual((receipt["run"], receipt["status"], receipt["reserved"]),
                             (args["request_id"], "INCOMPLETE", 0))
            self.assertEqual(api.start_sync_once(**args), receipt)
            with self.assertRaises(RuntimeBridgeError):
                api.start_sync_once(**(args | {"scope": "BOUND_OBJECTS"}))
            frappe.set_user("Guest")
            try:
                with self.assertRaises(frappe.PermissionError):
                    api.start_sync_once(**args)
            finally:
                frappe.set_user("Administrator")
            transport.assert_not_called()
        self.assertEqual(frappe.get_single("HERO Integration Settings").polling_mode, "OFF")
        self.assertFalse(FrappeSyncLedger().active_run(frappe.local.site))

    def test_sync_hero_rejects_a_read_contract_for_a_different_object_type(self):
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
        # A ProjectMatch read cannot verify this Customer field.
        # Reject before audit reservation or external I/O.
        from hero_integration.services.frappe_writeback import WritebackContractError
        before = FrappeBindingStorage().field(frappe.local.site, self.binding.identity_key)
        with patch("hero_integration.client.graphql_mutation.HeroMutationClient") as transport, patch.object(
            api, "_reserve_mutation"
        ) as reserve:
            with self.assertRaises(WritebackContractError):
                api.sync_hero_preview(self.binding.identity_key, outbound.name, self.plan.generation)
            with self.assertRaises(WritebackContractError):
                api.sync_hero_execute(self.binding.identity_key, outbound.name, self.plan.generation,
                                      self.binding.revision, "unapproved", 1)
        transport.assert_not_called()
        reserve.assert_not_called()
        self.assertEqual(FrappeBindingStorage().field(frappe.local.site, self.binding.identity_key), before)
        self.settings.allow_graphql_mutation_execution = 0
        self.settings.save()
        self.assertEqual(frappe.get_single("HERO Integration Settings").allow_graphql_mutation_execution, 0)
