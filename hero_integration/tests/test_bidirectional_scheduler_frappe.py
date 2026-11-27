"""Exercise both directions with native persistence, changing only the HERO transport."""
import json
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from hero_integration import api, tasks
from hero_integration.client.graphql import HeroGraphQLClient, validate_endpoint
from hero_integration.client.graphql_mutation import HeroMutationClient
from hero_integration.client.settings import HeroConfiguration
from hero_integration.services import automatic_writeback
from hero_integration.tests.test_sync_native_events import NativeCustomerSyncFixture


class TestBidirectionalScheduler(NativeCustomerSyncFixture, IntegrationTestCase):
    source_field = "full_name"
    native_field = "customer_name"
    purpose = "Record Name"

    def setUp(self):
        super().setUp()
        _, self.approved, revision, config = self.install_runtime_plan("AUTO_SYNC")
        self.original_switch = frappe.db.get_single_value("HERO Integration Settings", "allow_graphql_mutation_execution")
        self.last_tick = frappe.db.get_single_value("HERO Integration Sync State", "last_poll_started_on")
        self.service = frappe.get_doc({"doctype": "HERO GraphQL Service", "service_name": "Write-" + self.identity,
            "operation_name": "update_contact", "schema_snapshot": self.snapshot.name, "status": "Draft",
            "query_document": "mutation Update($contact:CustomerInput!){update_contact(contact:$contact){id}}"}).insert()
        self.addCleanup(self.cleanup_writeback)
        api.workspace_mutation_contract_save(self.service.name, json.dumps({"mode": "ADMIN_INPUT"}), activate=1)
        self.outbound = frappe.get_doc({"doctype": "HERO GraphQL Mapping", "schema_snapshot": self.snapshot.name,
            "direction": "FRAPPE_TO_MUTATION", "operation_kind": "MUTATION", "operation_name": "update_contact",
            "status": "Active", "mapping_json": json.dumps({"source": {"doctype": "Customer", "field": self.native_field},
                "target": {"operation": "update_contact", "argument": "contact", "input_type": "CustomerInput",
                           "field": self.source_field}, "transformation": "Trim"})}).insert()
        frappe.db.set_single_value("HERO Integration Settings", "allow_graphql_mutation_execution", 1)
        frappe.db.commit()
        settings = frappe.get_single("HERO Integration Settings")
        endpoint = validate_endpoint(settings.hero_api_url, {settings.hero_api_url.split("/")[2]})
        fixture_config = HeroConfiguration(endpoint, "synthetic", 1, "hero", revision, True)
        self.enterContext(patch("hero_integration.client.settings.get_configuration", return_value=fixture_config))
        self.enterContext(patch("frappe.utils.scheduler.is_scheduler_inactive", return_value=False))
        self.queries = self.enterContext(patch.object(HeroGraphQLClient, "query", side_effect=self.query))
        self.mutations = self.enterContext(patch.object(HeroMutationClient, "execute_once", side_effect=self.mutate))

    def cleanup_writeback(self):
        frappe.db.rollback()
        frappe.db.set_single_value("HERO Integration Settings", "allow_graphql_mutation_execution", self.original_switch)
        frappe.db.set_single_value("HERO Integration Sync State", "last_poll_started_on", self.last_tick)
        frappe.db.delete("HERO Polling Event", {"sync_run": ["in", frappe.get_all("HERO Sync Run Ledger",
            filters={"plan_key": self.approved.plan_key}, pluck="name")]})
        if hasattr(self, "outbound"):
            frappe.db.delete("HERO Mutation Audit", {"origin_event": ["in", frappe.get_all("HERO Integration Record",
                filters={"correlation_id": self.parent.identity_key}, pluck="name")]})
            frappe.db.delete("HERO GraphQL Mapping", self.outbound.name)
        frappe.db.delete("HERO Mutation Contract", {"graphql_service": self.service.name})
        frappe.db.delete("HERO GraphQL Service", self.service.name)
        frappe.db.commit()

    def query(self, document, variables):
        self.assertIn("ids:", document)
        return {"contacts": [dict(self.payload)]}

    def mutate(self, document, variables):
        self.assertIn("update_contact", document)
        target = variables["contact"]
        self.assertEqual(set(target), {"id", self.source_field})
        self.assertEqual(str(target["id"]), self.identity)
        self.payload[self.source_field] = target[self.source_field]
        return {"update_contact": {"id": self.identity}}

    def tick(self):
        frappe.db.set_single_value("HERO Integration Sync State", "last_poll_started_on", None)
        frappe.db.commit()
        # Run the enqueued worker inline so the two transport mocks share state.
        with patch.object(frappe, "enqueue") as queue:
            tasks.run_scheduled_sync()
            frappe.db.commit()
        job, = [call for call in queue.call_args_list if call.args and call.args[0] == automatic_writeback.process_pending]
        job.args[0](job.kwargs["source"], job.kwargs["generation"])

    def test_native_save_and_scheduler_round_trip_and_idle_replay(self):
        self.tick()
        self.assertEqual(self.doc.reload().customer_name, "revised")
        self.assertEqual(self.mutations.call_count, 0)
        self.doc.customer_name = self.identity + " local"
        self.doc.save()
        frappe.db.commit()
        self.tick()
        self.assertEqual(self.payload[self.source_field], self.doc.customer_name)
        self.assertEqual(self.mutations.call_count, 1)
        self.tick()
        self.assertEqual(self.mutations.call_count, 1)
        self.assertEqual(frappe.db.get_value("HERO Integration Record", {"correlation_id": self.parent.identity_key},
                                            "outbound_status"), "Delivered")

    def save_name(self, suffix):
        self.doc.reload()
        self.doc.customer_name = self.identity + suffix
        self.doc.save()
        frappe.db.commit()
        return self.doc.customer_name

    def observations(self):
        return frappe.get_all("HERO Integration Record", filters={"correlation_id": self.parent.identity_key,
            "technical_action": "frappe_change_observed"}, fields=["name", "outbound_status", "outbound_detail"],
            order_by="creation asc")

    def test_newer_committed_save_supersedes_older_pending_value(self):
        self.tick()
        self.save_name(" old")
        final = self.save_name(" current")
        self.tick()
        self.assertEqual([r.outbound_status for r in self.observations()], ["Superseded", "Delivered"])
        self.assertEqual(self.payload[self.source_field], final)
        self.assertEqual(self.mutations.call_count, 1)

    def test_lost_response_is_verified_on_next_tick_without_redispatch(self):
        self.tick()
        final = self.save_name(" timed out")
        def lost_response(document, variables):
            self.mutate(document, variables)
            raise TimeoutError("Response lost after remote success")
        self.mutations.side_effect = lost_response
        self.tick()
        event, = self.observations()
        self.assertEqual(event.outbound_status, "Deferred")
        audit = frappe.get_doc("HERO Mutation Audit", {"origin_event": event.name})
        self.assertEqual(audit.delivery_stage, "Uncertain")
        frappe.db.set_value("HERO Integration Record", event.name, "outbound_retry_on", None)
        frappe.db.commit()
        self.tick()
        self.assertEqual(audit.reload().delivery_stage, "Confirmed")
        self.assertEqual(self.observations()[0].outbound_status, "Delivered")
        self.assertEqual(self.payload[self.source_field], final)
        self.assertEqual(self.mutations.call_count, 1)
        self.tick()
        self.assertEqual(self.mutations.call_count, 1)

    def test_conflicting_remote_edit_preserves_both_values_and_shows_field_review(self):
        self.tick()
        final = self.save_name(" local")
        remote = self.payload[self.source_field] = self.identity + " independently edited"
        self.tick()
        self.assertEqual(self.doc.reload().customer_name, final)
        self.assertEqual(self.payload[self.source_field], remote)
        self.assertEqual(self.mutations.call_count, 0)
        self.assertTrue(frappe.db.exists("HERO Sync Change Event", {"open_field": self.binding.identity_key,
                                                                   "outcome": "FIELD_CONFLICT"}))
        self.assertEqual(self.observations()[0].outbound_status, "Deferred")

    def test_worker_rechecks_mutation_switch_after_enqueue(self):
        self.tick()
        self.save_name(" pending")
        frappe.db.set_single_value("HERO Integration Settings", "allow_graphql_mutation_execution", 0)
        frappe.db.commit()
        automatic_writeback.process_pending("hero", self.approved.generation)
        self.assertFalse(self.observations()[0].outbound_status)
        self.assertEqual(self.mutations.call_count, 0)
        frappe.db.set_single_value("HERO Integration Settings", "allow_graphql_mutation_execution", 1)
        frappe.db.commit()
        self.tick()
        self.assertEqual(self.mutations.call_count, 1)

    def test_scheduler_dispatches_after_commit_to_a_real_rq_worker(self):
        import multiprocessing
        from frappe.utils import background_jobs
        from rq import Queue, SimpleWorker

        self.tick()
        final = self.save_name(" queued")
        channel = Queue("hero-test-" + self.identity, connection=background_jobs.get_redis_conn())
        job_id = "hero-auto-write-" + self.approved.plan_key
        self.assertIsNone(background_jobs.get_job(job_id))
        self.addCleanup(channel.delete)
        frappe.db.set_single_value("HERO Integration Sync State", "last_poll_started_on", None)
        frappe.db.commit()
        # Only the queue namespace is isolated. Frappe serializes the original worker,
        # registers the after-commit callback and the child opens a fresh DB session.
        with patch.object(background_jobs, "get_queue", return_value=channel) as route:
            tasks.run_scheduled_sync()
            frappe.db.commit()
            route.assert_called_with("long", is_async=True)
        job = background_jobs.get_job(job_id)
        self.assertIsNotNone(job)
        self.addCleanup(job.delete)
        self.assertEqual(channel.job_ids, [job.id])
        self.assertEqual(job.kwargs["method"], automatic_writeback.process_pending)
        ctx = multiprocessing.get_context("fork")
        receiver, sender = ctx.Pipe(duplex=False)
        def work():
            SimpleWorker([channel], connection=channel.connection).work(burst=True, max_jobs=1, logging_level="WARNING")
            sender.send((self.mutations.call_count, self.payload[self.source_field]))
            sender.close()

        frappe.db.close()
        worker = ctx.Process(target=work)
        worker.start()
        sender.close()
        try:
            worker.join(30)
            self.assertFalse(worker.is_alive(), "RQ worker did not finish the isolated job")
            self.assertEqual(worker.exitcode, 0)
            self.assertEqual(job.get_status(), "finished")
            self.assertTrue(receiver.poll(2))
            self.assertEqual(receiver.recv(), (1, final))
        finally:
            if worker.is_alive():
                worker.terminate()
                worker.join(5)
            receiver.close()
        self.assertEqual(self.observations()[0].outbound_status, "Delivered")
