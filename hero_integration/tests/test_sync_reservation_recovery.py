"""Native field review transactions with an isolated, observable mutation boundary."""
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from hero_integration import api
from hero_integration.services import conflict_report as review
from hero_integration.services.binding_registry import BindingRegistryService
from hero_integration.services.sync_engine import ChangePage, SyncConflict, SyncStatus
from hero_integration.tests import test_sync_native_events as fixtures


class WorkerExit(BaseException):
    """Simulate process exit after a committed dispatch marker."""


class TestSyncReservationRecovery(fixtures.NativeCustomerSyncFixture, IntegrationTestCase):
    run_sync = fixtures.TestNativeSyncEvents.run_sync

    def setUp(self):
        super().setUp()
        self.doc.customer_details = "local"
        self.doc.save()
        frappe.db.commit()
        self.assertEqual(self.run_sync().status, SyncStatus.CONFLICT)
        self.event = frappe.get_doc("HERO Sync Change Event", {"open_field": self.binding.identity_key})
        self.version = str(self.event.modified)
        self.chosen = "chosen correction"
        self.mapping_name = "review-test-" + self.identity
        self.context = {"mapping": {"target": {"operation": self.mapping_name, "field": "category", "argument": "data"}}, "fingerprint": self.identity,
            "executable": SimpleNamespace(selection=SimpleNamespace(schema_fingerprint=self.snapshot.schema_fingerprint)),
            "intent": SimpleNamespace(document="synthetic", variables={"data": {"category": self.chosen, "id": int(self.identity)}})}
        self.dispatches = []
        self.addCleanup(self.clean_audits)
        self.enterContext(patch.object(review, "_read_reviewed_record", side_effect=lambda *_:
            (self.payload, self.processor._approved_plan(frappe, "hero"), 1, "id")))
        self.enterContext(patch.object(api, "_sync_hero_context", side_effect=lambda *a, **k: self.context.copy()))
        self.enterContext(patch.object(api, "_assert_mutation_execution_released"))
        self.enterContext(patch.object(api._SyncHeroTransport, "execute_once", autospec=True, side_effect=self.wire))

    def clean_audits(self):
        frappe.db.rollback()
        frappe.db.delete("HERO Mutation Audit", {"operation_name": self.mapping_name})
        frappe.db.commit()

    def wire(self, adapter, document, variables):
        adapter._context["before_mutation"]()
        self.dispatches.append(dict(variables))
        if probe := getattr(self, "at_dispatch", None):
            probe()
        if getattr(self, "crash", False):
            raise WorkerExit
        self.payload["category"] = variables["data"]["category"]

    def confirm(self):
        args = {"value": self.chosen, "mutation_mapping": self.mapping_name}
        preview = review.preview_field(self.event.name, self.version, "CUSTOM", **args)
        return review.resolve_field(self.event.name, self.version, "CUSTOM", **args,
                                    confirmation_fingerprint=preview["confirmation_fingerprint"], confirmed=True)

    def audit(self):
        return frappe.get_doc("HERO Mutation Audit", {"operation_name": self.mapping_name})

    def assert_independent_slot(self):
        from hero_integration.client import graphql_mutation, settings
        from hero_integration.services import mapping_contract_v1

        fingerprint = self.identity + "-independent"
        contract = SimpleNamespace(status="Active", contract_revision=1)
        service = SimpleNamespace(operation_name=self.mapping_name, query_document="synthetic")
        request = SimpleNamespace(document="synthetic", variables={"id": 1})
        config = SimpleNamespace(endpoint="synthetic", timeout_seconds=1, api_token="synthetic")
        with patch.object(api, "_validated_mutation_request",
                          return_value=(contract, service, self.snapshot, request.variables, fingerprint)), \
             patch.object(mapping_contract_v1, "materialize_saved_mutation", return_value=request), \
             patch.object(settings, "get_configuration", return_value=config), \
             patch.object(graphql_mutation, "HeroMutationClient") as client:
            receipt = api.execute_graphql_service("independent", confirmation_fingerprint=fingerprint, confirmed=True)
        client.return_value.execute_once.assert_called_once_with(request.document, request.variables)
        self.assertEqual(receipt["status"], "SUCCEEDED")

    def test_native_ack_failure_releases_global_slot_and_recovers_without_dispatch(self):
        with patch.object(BindingRegistryService, "record_verified_acknowledgment", side_effect=RuntimeError("native ack failed")):
            with self.assertRaisesRegex(RuntimeError, "native ack failed"):
                self.confirm()
        frappe.db.rollback()
        audit = self.audit()
        self.assertEqual(audit.reservation_key, audit.correlation_id)
        self.assertEqual(self.doc.reload().customer_details, "local")
        self.assertEqual(self.event.reload().outcome, "FIELD_CONFLICT")
        self.assert_independent_slot()
        self.assertEqual(self.confirm()["status"], "RESOLVED")
        self.assertEqual(len(self.dispatches), 1)
        self.assertEqual(self.doc.reload().customer_details, self.chosen)
        self.assertEqual(audit.reload().status, "Succeeded")

    def test_stale_after_reservation_cancels_only_unsent_decision(self):
        original = api._reserve_mutation

        def change_remote(**kwargs):
            audit = original(**kwargs)
            self.payload["category"] = "changed after approval"
            return audit

        with patch.object(api, "_reserve_mutation", side_effect=change_remote):
            with self.assertRaises(frappe.ValidationError):
                self.confirm()
        frappe.db.rollback()
        self.assertFalse(self.dispatches)
        self.assertFalse(self.event.reload().resolution_json)
        self.assertEqual(self.audit().status, "Failed")
        self.assert_independent_slot()
        self.pages["w1"] = ChangePage((self.payload,), None, True, "w2")
        self.assertEqual(self.run_sync().status, SyncStatus.CONFLICT)
        current = frappe.get_doc("HERO Sync Change Event", {"open_field": self.binding.identity_key})
        self.assertEqual(json.loads(current.field_changes)["source"], self.payload["category"])
        self.event, self.version = current, str(current.modified)
        self.assertEqual(self.confirm()["status"], "RESOLVED")
        self.assertEqual(len(self.dispatches), 1)

    def test_late_readback_keeps_a_more_recent_local_edit_for_a_new_review(self):
        self.crash = True
        with self.assertRaises(WorkerExit):
            self.confirm()
        self.doc.reload()
        self.doc.customer_details = "subsequent local decision"
        self.doc.save()
        frappe.db.commit()
        self.payload["category"] = self.chosen
        args = dict(value=self.chosen, mutation_mapping=self.mapping_name)
        preview = review.preview_field(self.event.name, self.version, "CUSTOM", **args)
        self.assertTrue(preview["requires_new_review"])
        self.assertFalse(preview["updates_local"])
        result = self.confirm()
        self.assertEqual(result["status"], "FIELD_CONFLICT")
        current = frappe.get_doc("HERO Sync Change Event", result["review"])
        evidence = json.loads(current.field_changes)
        self.assertEqual((evidence["source"], evidence["local"]), (self.chosen, "subsequent local decision"))
        self.assertEqual(self.event.reload().outcome, "SUPERSEDED")
        self.assertEqual(self.doc.reload().customer_details, "subsequent local decision")
        self.assertEqual(self.registry.get_field_bindings(self.site, self.parent.identity_key)[0].acknowledged_version, 1)
        self.assertEqual(self.audit().status, "Succeeded")
        self.assertEqual(len(self.dispatches), 1)

    def test_interrupted_before_dispatch_can_be_cancelled_by_confirmed_preview(self):
        reserve = api._reserve_mutation

        def interrupt_after_commit(**kwargs):
            reserve(**kwargs)
            raise WorkerExit

        with patch.object(api, "_reserve_mutation", side_effect=interrupt_after_commit):
            with self.assertRaises(WorkerExit):
                self.confirm()
        self.assertEqual(json.loads(self.event.reload().resolution_json)["phase"], "PREPARED")
        self.assertEqual(self.confirm()["status"], "NOT_SENT")
        self.assertEqual(self.dispatches, [])
        self.assertEqual(self.audit().status, "Failed")
        self.assertFalse(self.event.reload().resolution_json)
        self.assert_independent_slot()

    def test_abandoned_slot_can_be_released_with_unavailable_reader_without_confirming_business_data(self):
        self.crash = True
        with self.assertRaises(WorkerExit):
            self.confirm()
        event = self.event.reload()
        decision = event.resolution_json
        self.doc.reload()
        self.doc.customer_details = "edited since dispatch"
        self.doc.save()
        frappe.db.commit()
        args = dict(event=event.name, expected_modified=self.version, choice="RELEASE_RESERVATION")
        with patch.object(review, "_read_reviewed_record", side_effect=TimeoutError) as reader:
            preview = api.preview_sync_field(**args)
            with self.assertRaises(frappe.ValidationError):
                api.resolve_sync_field(**args, confirmation_fingerprint="stale", confirmed=1)
            self.assertEqual(self.audit().status, "Approved")
            with self.assertRaises(SyncConflict):
                from hero_integration.services.sync_engine import FrappeSyncLedger
                with FrappeSyncLedger().execution_lease("field-review:" + event.name):
                    api.resolve_sync_field(**args, confirmation_fingerprint=preview["confirmation_fingerprint"], confirmed=1)
            result = api.resolve_sync_field(**args, confirmation_fingerprint=preview["confirmation_fingerprint"], confirmed=1)
            self.assertEqual(result["status"], "READBACK_REQUIRED")
            reader.assert_not_called()
        self.assertEqual(self.event.reload().resolution_json, decision)
        self.assertEqual(self.event.outcome, "FIELD_CONFLICT")
        self.assertEqual(self.doc.reload().customer_details, "edited since dispatch")
        self.assertEqual(self.registry.get_field_bindings(self.site, self.parent.identity_key)[0].acknowledged_version, 1)
        self.assertEqual(self.audit().status, "Timeout")
        self.assert_independent_slot()
        self.assertEqual(len(self.dispatches), 1)

    def test_unconfirmed_recovery_preserves_the_review_and_its_audit(self):
        self.crash = True
        with self.assertRaises(WorkerExit):
            self.confirm()
        event = self.event.reload()
        decision = event.resolution_json
        with self.assertRaises(frappe.ValidationError):
            review.resolve_field(event.name, self.version, "CUSTOM", value=self.chosen,
                                 mutation_mapping=self.mapping_name, confirmed=False)
        self.assertEqual(event.reload().resolution_json, decision)
        self.assertEqual((self.audit().status, self.audit().reservation_key),
                         ("Approved", api.ACTIVE_MUTATION_RESERVATION))
        self.assertEqual(len(self.dispatches), 1)

    def test_interrupted_dispatch_uses_readback_only_and_releases_its_abandoned_slot(self):
        self.crash = True
        with self.assertRaises(WorkerExit):
            self.confirm()
        frappe.db.rollback()
        self.assertTrue(self.event.reload().resolution_json)
        with self.assertRaises(frappe.ValidationError):
            self.confirm()
        frappe.db.rollback()
        audit = self.audit()
        self.assertEqual(audit.reservation_key, audit.correlation_id)
        other = api._reserve_mutation(operation_name=self.mapping_name, schema_fingerprint=self.snapshot.schema_fingerprint,
                                      mapping_revision=1, request_fingerprint=self.identity + "-still-in-flight")
        self.payload["category"] = self.chosen
        self.assertEqual(self.confirm()["status"], "RESOLVED")
        self.assertEqual(len(self.dispatches), 1)
        self.assertEqual(other.reload().reservation_key, api.ACTIVE_MUTATION_RESERVATION)
        api._finalize_mutation(other, "Succeeded", "Independent synthetic operation.")

    def test_live_dispatch_cannot_be_reclaimed_by_another_or_reentrant_session(self):
        sites_path = frappe.local.sites_path

        def try_while_active():
            with self.assertRaises(SyncConflict):
                review.resolve_field(self.event.name, self.version, "CUSTOM", value=self.chosen,
                                     mutation_mapping=self.mapping_name)

        def other_session():
            frappe.init(site=self.site, sites_path=sites_path)
            try:
                frappe.connect()
                frappe.set_user("Administrator")
                try_while_active()
            finally:
                frappe.destroy()

        def probe():
            with ThreadPoolExecutor(max_workers=1) as workers:
                workers.submit(other_session).result(timeout=5)
            try_while_active()
            self.assertEqual(self.audit().reservation_key, api.ACTIVE_MUTATION_RESERVATION)
        self.at_dispatch = probe
        self.assertEqual(self.confirm()["status"], "RESOLVED")
        self.assertEqual(len(self.dispatches), 1)
