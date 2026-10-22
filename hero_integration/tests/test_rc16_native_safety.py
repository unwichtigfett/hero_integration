from __future__ import annotations

from queue import Queue
from threading import Barrier, Thread
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.handler import is_valid_http_method
from frappe.tests import IntegrationTestCase

from hero_integration import api
from hero_integration.services.change_detection import encode, field_hashes, observe_frappe_target_change
from hero_integration.services.migration import CustomerAddressError, _claim_run, _release_run, _upsert


def _claim_in_separate_connection(site: str, run_name: str, start: Barrier, results: Queue) -> None:
    """Exercise the durable run slot from an independent Frappe DB connection."""
    try:
        frappe.init(site=site)
        frappe.connect()
        frappe.set_user("Administrator")
        run = frappe.get_doc("HERO Integration Run", run_name)
        start.wait(timeout=10)
        claimed = _claim_run(run)
        frappe.db.commit()
        results.put((run_name, claimed, run.state, ""))
    except Exception as error:  # pragma: no cover - assertion is made in the parent test
        results.put((run_name, False, "ERROR", f"{error.__class__.__name__}: {error}"))
    finally:
        frappe.destroy()


def _reserve_in_separate_connection(site: str, request_fingerprint: str, start: Barrier, results: Queue) -> None:
    """Reserve without any transport call; the unique key is the concurrency boundary."""
    try:
        frappe.init(site=site)
        frappe.connect()
        frappe.set_user("Administrator")
        start.wait(timeout=10)
        audit = api._reserve_mutation(
            operation_name="RC16 native reservation proof",
            schema_fingerprint="rc16-native-test",
            mapping_revision=1,
            request_fingerprint=request_fingerprint,
        )
        results.put((True, audit.name, ""))
    except Exception as error:
        results.put((False, "", f"{error.__class__.__name__}: {error}"))
    finally:
        frappe.destroy()


class TestRC16NativeSafety(IntegrationTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.original_user = frappe.session.user
        frappe.set_user("Administrator")
        self.addCleanup(frappe.set_user, self.original_user)
        self.token = f"rc16-{frappe.generate_hash(length=12)}"
        self.run_names: list[str] = []

    def tearDown(self) -> None:
        for run_name in self.run_names:
            _release_run(run_name)
        if self.run_names:
            frappe.db.delete("HERO Integration Run", {"name": ["in", self.run_names]})
        frappe.db.delete("HERO Mutation Audit", {"request_fingerprint": self.token})
        frappe.db.delete("HERO Integration Link", {"source_id": self.token})
        frappe.db.delete("HERO Integration Record", {"source_id": self.token})
        frappe.db.delete("Customer", {"customer_name": ["like", f"{self.token}%"]})
        frappe.db.commit()
        super().tearDown()

    def _run(self):
        run = frappe.get_doc({"doctype": "HERO Integration Run", "action": "Sync", "mode": "Preview"}).insert(
            ignore_permissions=True
        )
        self.run_names.append(run.name)
        return run

    def test_frappe_dispatcher_rejects_get_for_every_write_rpc(self) -> None:
        previous_request = getattr(frappe.local, "request", None)
        try:
            frappe.local.request = SimpleNamespace(method="GET")
            for method in (
                api.start_migration,
                api.cancel_run,
                api.resume_run,
                api.workspace_schema_refresh,
                api.workspace_settings_save,
                api.workspace_mutation_execution_set,
                api.graphql_mapping_save,
                api.workspace_service_save,
                api.workspace_service_validate,
                api.workspace_service_delete,
                api.adopt_mapping_package,
                api.resume_mapping_adoption,
                api.workspace_mutation_contract_save,
                api.execute_graphql_service,
            ):
                with self.assertRaises(frappe.PermissionError):
                    is_valid_http_method(method)
            frappe.local.request = SimpleNamespace(method="POST")
            is_valid_http_method(api.start_migration)
        finally:
            frappe.local.request = previous_request

    def test_two_connections_can_claim_only_one_active_run(self) -> None:
        first, second = self._run(), self._run()
        frappe.db.commit()
        start, results = Barrier(2), Queue()
        workers = [
            Thread(target=_claim_in_separate_connection, args=(frappe.local.site, run.name, start, results))
            for run in (first, second)
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=20)
            self.assertFalse(worker.is_alive())
        observed = [results.get_nowait() for _ in workers]
        self.assertTrue(all(not detail for _, _, _, detail in observed), observed)
        self.assertEqual(sorted(claimed for _, claimed, _, _ in observed), [False, True])
        self.assertEqual(sorted(state for _, claimed, state, _ in observed if not claimed), ["Blocked"])
        active = frappe.db.get_single_value("HERO Integration Sync State", "active_run", cache=False)
        self.assertIn(active, self.run_names)

    def test_two_connections_can_reserve_only_one_mutation_without_transport(self) -> None:
        start, results = Barrier(2), Queue()
        workers = [
            Thread(target=_reserve_in_separate_connection, args=(frappe.local.site, self.token, start, results))
            for _ in range(2)
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=20)
            self.assertFalse(worker.is_alive())
        observed = [results.get_nowait() for _ in workers]
        self.assertEqual(sum(success for success, _, _ in observed), 1, observed)
        self.assertEqual(
            frappe.db.count("HERO Mutation Audit", {"reservation_key": api.ACTIVE_MUTATION_RESERVATION}), 1
        )

    def test_relation_failure_rolls_back_real_customer_and_provenance_link(self) -> None:
        values = {
            "customer_name": self.token,
            "customer_type": "Individual",
        }
        result = _upsert(
            "customer",
            self.token,
            {"id": self.token},
            values,
            "Customer",
            "customer_name",
            "rc16-native-test",
            SimpleNamespace(mode="Execute"),
            after_insert=lambda customer: (_ for _ in ()).throw(CustomerAddressError("synthetic relation conflict")),
        )
        self.assertEqual(result.code, "CONFLICT")
        self.assertFalse(frappe.db.exists("Customer", {"customer_name": self.token}))
        self.assertFalse(
            frappe.db.exists("HERO Integration Link", {"source_type": "customer", "source_id": self.token})
        )

    def test_customer_on_update_hook_records_a_mapped_change_without_outbound_work(self) -> None:
        customer = frappe.get_doc(
            {
                "doctype": "Customer",
                "customer_name": self.token,
                "customer_type": "Individual",
            }
        ).insert(ignore_permissions=True)
        baseline = encode(field_hashes({"customer_name": self.token}))
        link = frappe.get_doc(
            {
                "doctype": "HERO Integration Link",
                "source_type": "customer",
                "source_id": self.token,
                "source_instance": "default",
                "target_doctype": "Customer",
                "target_name": customer.name,
                "sync_status": "Synchronized",
                "source_payload_hash": "source-baseline",
                "target_payload_hash": "target-baseline",
                "source_field_hashes": baseline,
                "target_field_hashes": baseline,
                "mapping_version": "rc16-native-test",
            }
        ).insert(ignore_permissions=True)
        customer.customer_name = f"{self.token}-changed"
        customer.save(ignore_permissions=True)
        link.reload()
        self.assertTrue(link.observed_target_hash)
        self.assertEqual(link.source_field_hashes, baseline)
        self.assertEqual(link.target_field_hashes, baseline)
        self.assertTrue(
            frappe.db.exists(
                "HERO Integration Record",
                {
                    "source_id": self.token,
                    "technical_action": "frappe_change_observed",
                    "target_name": customer.name,
                },
            )
        )

    def test_change_observer_does_not_query_link_fields_during_migration(self) -> None:
        """The migration guard runs before any Link query can need new columns."""

        previous = frappe.flags.in_migrate
        frappe.flags.in_migrate = True
        self.addCleanup(setattr, frappe.flags, "in_migrate", previous)
        with patch.object(
            frappe, "get_all", side_effect=AssertionError("Link query must not run during migration")
        ) as get_all:
            observe_frappe_target_change(SimpleNamespace(doctype="Customer", name=self.token))
        get_all.assert_not_called()

    def test_change_observer_propagates_normal_query_errors_outside_migration(self) -> None:
        """Only the framework migration phase is a no-op boundary."""

        previous = frappe.flags.in_migrate
        frappe.flags.in_migrate = False
        self.addCleanup(setattr, frappe.flags, "in_migrate", previous)
        with patch.object(frappe, "get_all", side_effect=RuntimeError("synthetic query failure")):
            with self.assertRaisesRegex(RuntimeError, "synthetic query failure"):
                observe_frappe_target_change(SimpleNamespace(doctype="Customer", name=self.token))
