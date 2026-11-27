"""MONITOR uses the existing approved materializer without business writes."""
import os
import unittest
import sys
import types
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

from hero_integration.services import sync_runtime_adapter
from hero_integration.tests.test_b8_runtime_reconstruction import _context


class TestMaterializedMonitor(unittest.TestCase):
    def setUp(self):
        self.selection, self.request, self.mapping, self.frappe = _context()
        self.frappe.session = SimpleNamespace(user="Administrator")
        self.settings = SimpleNamespace(polling_mode="MONITOR", hero_source_instance="b8-source",
                                        hero_api_url="https://hero.example/graphql", allowed_hero_hosts="hero.example")
        self.frappe.get_single = lambda _: self.settings
        self.frappe.get_all = lambda *a, **kw: [{"name": self.selection.schema_snapshot,
                                                 "schema_fingerprint": self.selection.schema_fingerprint}]
        self.rule = dict(definition_id="b8-definition", status="Active", personal_graphql_mapping="B8-MAPPING",
                         source_path="name", target_field="custom_hero_display_id")
        original_get_all = self.frappe.get_all
        self.frappe.get_all = lambda doctype, *a, **kw: [self.rule] if doctype == "HERO Frappe Mapping" else original_get_all(doctype, *a, **kw)
        self.frappe.db = SimpleNamespace(rollback=Mock(), sql=lambda query, *a, **kw:
            [self.rule] if "tabHERO Frappe Mapping" in query else [(7,)])
        self.plan = SimpleNamespace(generation=7, plan_key="synthetic-plan")
        self.executable = SimpleNamespace(selection=self.selection, plan_key=self.plan.plan_key, generation=7)
        def resolve(site, source, generation, automatic, actor, *, reader=None):
            self.assertIn(reader, (None, self.selection.graphql_service))
            return self.executable

        self.service = SimpleNamespace(current_approved=lambda *a: self.plan, resolve_executable_plan=resolve)
        self.client = SimpleNamespace(query=Mock(return_value={"things": [{"id": "1", "name": "test"}]}))
        self.config = SimpleNamespace(source_instance="b8-source", endpoint=object(), api_token="synthetic", timeout_seconds=30)

    def run_monitor(self):
        with patch("hero_integration.hero_integration.doctype.hero_graphql_mapping.hero_graphql_mapping.lock_exchange_context"), \
             patch.object(sync_runtime_adapter, "_runtime_plan_service", return_value=self.service), \
             patch("hero_integration.client.settings.get_configuration", return_value=self.config):
            return sync_runtime_adapter.run_materialized_monitor(frappe_module=self.frappe, client=self.client)

    def test_executes_exact_reconstructed_read_and_reports_bounded_incomplete(self):
        result = self.run_monitor()
        self.client.query.assert_called_once_with(self.request.document, dict(self.request.variables))
        self.assertEqual(result.project_records, 1)
        self.assertEqual(result.incomplete_sources, ("HeroProjectMatches",))
        self.assertEqual(result.project_next_offset, 0)

    def test_unconfirmed_source_binding_never_queries(self):
        self.settings.source_binding_revision, self.settings.source_binding_confirmed_revision = 2, 1
        with self.assertRaisesRegex(sync_runtime_adapter.RuntimeBridgeError, "confirm the source binding"):
            self.run_monitor()
        self.client.query.assert_not_called()
        self.settings.source_binding_confirmed_revision = 2
        self.run_monitor()
        self.client.query.assert_called_once()

    def test_missing_plan_or_newer_snapshot_never_queries(self):
        self.service.current_approved = lambda *a: None
        with self.assertRaises(sync_runtime_adapter.RuntimeBridgeError): self.run_monitor()
        self.service.current_approved = lambda *a: self.plan
        self.frappe.get_all = lambda *a, **kw: [{"name": "new", "schema_fingerprint": "new"}]
        with self.assertRaises(sync_runtime_adapter.RuntimeBridgeError): self.run_monitor()
        self.client.query.assert_not_called()

    def test_changed_seal_and_wrong_source_never_query(self):
        self.mapping.mapping_json = "{}"
        with self.assertRaises(ValueError): self.run_monitor()
        self.client.query.assert_not_called()

    def test_reconciliation_is_not_silently_ignored(self):
        for attribute, value in (("polling_reconciliation", 1),):
            setattr(self.settings, attribute, value)
            with self.subTest(attribute=attribute), self.assertRaises(sync_runtime_adapter.RuntimeBridgeError):
                self.run_monitor()
            delattr(self.settings, attribute)
        self.client.query.assert_not_called()

    def test_activation_during_http_cannot_be_acknowledged(self):
        def reply(*args):
            self.service.resolve_executable_plan = Mock(side_effect=sync_runtime_adapter.RuntimeBridgeError("generation changed"))
            return {"things": [{"id": "1", "name": "test"}]}
        self.client.query.side_effect = reply
        with self.assertRaises(sync_runtime_adapter.RuntimeBridgeError):
            self.run_monitor()

    def test_changed_persisted_generation_after_http_fails_before_settings_lock(self):
        original_sql = self.frappe.db.sql
        post_read_queries = []

        def reply(*args):
            def changed_generation(query, *params, **kwargs):
                post_read_queries.append(query)
                if 'tabHERO Runtime Plan' in query:
                    return [(8,)]
                return original_sql(query, *params, **kwargs)
            self.frappe.db.sql = changed_generation
            return {"things": [{"id": "1", "name": "test"}]}

        self.client.query.side_effect = reply
        with self.assertRaisesRegex(sync_runtime_adapter.RuntimeBridgeError, "generation changed during transport"):
            self.run_monitor()
        self.assertEqual(len(post_read_queries), 1)
        self.assertIn('tabHERO Runtime Plan', post_read_queries[0])

    def test_field_rule_change_during_http_is_rejected_without_root_revision_change(self):
        def reply(*args):
            self.rule['source_path'] = 'other'
            return {"things": [{"id": "1", "name": "test"}]}
        self.client.query.side_effect = reply
        with self.assertRaises(sync_runtime_adapter.RuntimeBridgeError): self.run_monitor()

    def test_confirmed_source_rebinding_during_http_is_still_stale(self):
        def reply(*args):
            self.settings.source_binding_revision = self.settings.source_binding_confirmed_revision = 2
            return {"things": [{"id": "1", "name": "test"}]}
        self.client.query.side_effect = reply
        with self.assertRaises(sync_runtime_adapter.RuntimeBridgeError): self.run_monitor()

    def test_area_disabled_during_http_cannot_be_acknowledged(self):
        def reply(*args):
            self.settings.sync_area_projects = 0
            return {"things": [{"id": "1", "name": "test"}]}
        self.client.query.side_effect = reply
        with self.assertRaisesRegex(sync_runtime_adapter.RuntimeBridgeError, "source/configuration changed"):
            self.run_monitor()
        self.client.query.assert_called_once()

    def test_transport_boundary_has_released_prior_transactions(self):
        def reply(*args):
            self.assertGreaterEqual(self.frappe.db.rollback.call_count, 2)
            return {"things": [{"id": "1", "name": "test"}]}
        self.client.query.side_effect = reply
        self.run_monitor()

    def test_invalid_result_identity_is_not_reported_as_success(self):
        self.client.query.return_value = {"things": [{"id": None, "name": "test"}]}
        with self.assertRaises(ValueError): self.run_monitor()

    def test_oversized_materialized_page_is_not_reported_as_success(self):
        self.client.query.return_value = {"things": [{"id": "1", "name": "one"},
                                                       {"id": "2", "name": "two"}]}
        with self.assertRaisesRegex(sync_runtime_adapter.RuntimeBridgeError, "bounded read contract"):
            self.run_monitor()
        self.client.query.assert_called_once()


    def test_manual_observation_freezes_distinct_main_ids_at_configured_limits(self):
        import json
        from hero_integration.services import b8_runtime
        from hero_integration.services.mapping_contract_v1 import QueryAdapter, QueryDraft, SnapshotAdapter, StoredSnapshot

        snapshot = self.frappe.get_doc("HERO GraphQL Schema Snapshot", self.selection.schema_snapshot)
        draft = QueryDraft("SearchThings", "things", "things", ("id", "name"), {"first": 104})
        request = QueryAdapter().materialize(SnapshotAdapter().adapt(StoredSnapshot(
            snapshot.name, snapshot.snapshot_json, snapshot.schema_fingerprint, snapshot.source_instance)), draft)
        payload = json.loads(self.mapping.mapping_json)
        payload["page_size"] = 104
        payload["b8_compilation"]["variables"] = draft.variables
        payload["b8_canonical_operation"] = b8_runtime.canonical_operation_contract(self.selection, request)
        self.mapping.mapping_json = json.dumps(payload)
        self.frappe.get_doc("HERO GraphQL Service", self.selection.graphql_service).query_document = request.document
        self.settings.polling_page_size = 104
        rows = [{"id": str(i), "name": "observed"} for i in range(1, 102)]
        rows += [dict(rows[0]), dict(rows[0], name="changed")]
        self.client.query.return_value = {"things": list(reversed(rows))}
        with patch("hero_integration.hero_integration.doctype.hero_graphql_mapping.hero_graphql_mapping.lock_exchange_context"), \
             patch.object(sync_runtime_adapter, "_runtime_plan_service", return_value=self.service), \
             patch("hero_integration.client.settings.get_configuration", return_value=self.config):
            for limit in (1, 10, 50, 100):
                with self.subTest(limit=limit):
                    sample = sync_runtime_adapter.ManualMonitorSample("synthetic-plan", 7, 0, "HeroProjectMatches", limit)
                    result = sync_runtime_adapter.run_materialized_monitor(frappe_module=self.frappe, client=self.client, sample=sample)
                    self.assertEqual(result.observed_ids, tuple(str(i) for i in range(1, limit + 1)))
                    self.assertEqual(result.conflicting_ids, ("1",))
                    self.assertEqual(result.project_records, limit)
                    self.assertEqual(result.rows_read, 103)

    def test_incremental_reader_uses_only_approved_paging_variables(self):
        import json
        from hero_integration.graphql_protocol.graphql_operation_materializer import GraphQLDocumentRequest
        from hero_integration.sources.hero import CUSTOMER_QUERY_V1
        request = GraphQLDocumentRequest(CUSTOMER_QUERY_V1, "schema", self.request.operation_kind, "seal", None,
            json.dumps({"first": 100, "offset": 0, "orderBy": "id", "showDeleted": True}))
        contract = SimpleNamespace(records_path="contacts", identity_path="id", page_size=100,
            variables={"first": "$page_size", "offset": "$offset", "orderBy": "id", "showDeleted": True})
        materialized = SimpleNamespace(sealed=SimpleNamespace(contract=contract), request=request)
        root = sync_runtime_adapter._require_bounded_monitor_query(materialized)
        sample = sync_runtime_adapter.ManualMonitorSample("approved", 7, 0, "HeroCustomers", 10, offset=30)
        variables, size = sync_runtime_adapter._monitor_page_variables(materialized, root, sample)
        self.assertEqual((variables, size), ({"first": 10, "offset": 30, "orderBy": "id", "showDeleted": True}, 10))
        self.assertEqual(request.variables["offset"], 0)
        for field, value in (("orderBy", "name"), ("offset", 0), ("first", 100)):
            with self.subTest(field=field):
                original = contract.variables[field]
                contract.variables[field] = value
                with self.assertRaisesRegex(sync_runtime_adapter.RuntimeBridgeError, "identity-ordered"):
                    sync_runtime_adapter._monitor_page_variables(materialized, root, sample)
                contract.variables[field] = original

    def test_incremental_scan_ceiling_and_unstable_pages_fail_before_claim(self):
        from hero_integration.services import polling
        from hero_integration.services.polling import MonitorResult

        budget = Mock(limit=1, source_instance="source")
        budget.monitor_checkpoint.return_value = None
        budget.unobserved.return_value = []
        sample = sync_runtime_adapter.ManualMonitorSample("plan", 1, 1, "HeroCustomers", 1)
        def page(key):
            return MonitorResult(1, 0, "source", observed_ids=(key,), rows_read=1, page_size=1,
                                 reader_digest="approved", fingerprints={key:key})
        with patch("hero_integration.services.test_environment.require_environment", return_value="sandbox"), \
             patch.object(polling, "MAX_FULL_INVENTORY_PAGES", 2):
            for pages, error in (([page("1"), page("2")], "page ceiling"), ([page("1"), page("1")], "order")):
                with self.subTest(error=error):
                    reader = Mock(side_effect=pages)
                    with self.assertRaisesRegex(sync_runtime_adapter.RuntimeBridgeError, error):
                        polling._incremental_monitor_selection(self.frappe, budget, sample, reader, "approval")
                    self.assertEqual([c.kwargs["sample"].offset for c in reader.call_args_list], [0, 1])
                    budget.incremental_group.assert_not_called()

    def test_manual_stream_mismatch_refuses_transport(self):
        with patch.object(sync_runtime_adapter, "_runtime_plan_service", return_value=self.service):
            sample = sync_runtime_adapter.ManualMonitorSample("synthetic-plan", 7, 0, "HeroCustomers", 10)
            with self.assertRaisesRegex(sync_runtime_adapter.RuntimeBridgeError, "stream"):
                sync_runtime_adapter.run_materialized_monitor(frappe_module=self.frappe, client=self.client, sample=sample)
        self.client.query.assert_not_called()


    def test_manual_observation_blocks_an_unpaginated_approved_query(self):
        from hero_integration.services.b8_runtime import reconstruct_materialized_read_plan
        read = reconstruct_materialized_read_plan(self.selection, frappe_module=self.frappe)
        unpaged = SimpleNamespace(document="query SearchThings { things { id name } }", variables={})
        with patch.object(sync_runtime_adapter, "_runtime_plan_service", return_value=self.service), \
             patch("hero_integration.services.b8_runtime.reconstruct_materialized_read_plan",
                   return_value=SimpleNamespace(sealed=read.sealed, request=unpaged)):
            sample = sync_runtime_adapter.ManualMonitorSample("synthetic-plan", 7, 0, "HeroProjectMatches", 100)
            with self.assertRaisesRegex(sync_runtime_adapter.RuntimeBridgeError, "proven server-side limit"):
                sync_runtime_adapter.run_materialized_monitor(frappe_module=self.frappe, client=self.client, sample=sample)
        self.client.query.assert_not_called()


@unittest.skipUnless(os.environ.get("HERO_EXECUTION_BUDGET_TEST_SITE"), "requires an explicitly selected disposable site")
class TestManualMonitorMariaDB(unittest.TestCase):
    def test_legacy_v2_manual_approval_survives_interruption_and_settings_change(self):
        import hashlib
        import json
        self.enterContext(patch("hero_integration.services.test_environment.require_environment", return_value="test-sandbox"))
        from uuid import uuid4
        import frappe
        from hero_integration import api
        from hero_integration.services.execution_budget import ExecutionBudget
        from hero_integration.services.polling import MonitorResult

        self.assertEqual(frappe.local.site, os.environ["HERO_EXECUTION_BUDGET_TEST_SITE"])
        self.assertTrue(frappe.conf.allow_tests)
        run = uuid4().hex
        source = "synthetic-monitor"
        settings = SimpleNamespace(hero_source_instance=source, testing_enabled=1, testing_max_actions=10,
                                   polling_mode="MONITOR", source_binding_revision=7)
        real_get_single = frappe.get_single
        def single(doctype):
            return settings if doctype == "HERO Integration Settings" else real_get_single(doctype)
        call = dict(request_id=run, plan_key="approved", expected_plan_generation=2, expected_source_revision=7,
                    operation="HeroProjectMatches", confirmed=1)
        ids = tuple(str(i) for i in range(1, 11))
        sample = MonitorResult(0, 10, source, rows_read=12, observed_ids=ids, conflicting_ids=("10",),
                               reader_digest="synthetic-reader")
        key = ExecutionBudget("monitor", run, source, 10, version=2).budget_id
        # Simulate a v2 receipt persisted before the version field existed.
        approval = hashlib.sha256(json.dumps([2, frappe.local.site, frappe.session.user, source, "approved",
                                             2, 7, "HeroProjectMatches"]).encode()).hexdigest()
        frappe.get_doc({"doctype": "HERO Polling Event", "mode": "MONITOR", "status": "Started",
                        "source_instance": source, "manual_approval": approval, "testing_max_actions": 10}
                       ).insert(ignore_permissions=True, set_name=run)
        frappe.db.commit()
        try:
            with patch.object(frappe, "get_single", side_effect=single), \
                 patch.object(sync_runtime_adapter, "run_materialized_monitor", return_value=sample) as reader:
                with patch.object(ExecutionBudget, "reserve_record", side_effect=ValueError("worker interrupted")):
                    with self.assertRaisesRegex(ValueError, "worker interrupted"):
                        api.start_monitor_test(**call)
                frozen = ExecutionBudget("monitor", run, source, 10, version=2).selection()
                self.assertEqual(reader.call_args.kwargs["sample"].limit, 10)
                self.assertEqual(set(frozen["ids"]), set(ids))
                settings.testing_max_actions = 1
                receipt = api.start_monitor_test(**call)
                self.assertEqual(receipt["limit"], 10)
                self.assertEqual(receipt["selected"], 10)
                self.assertEqual(receipt["processed"], 9)
                self.assertEqual(receipt["failed"], 0)
                self.assertEqual(receipt["missing"], 0)
                self.assertEqual(receipt["conflicts"], 1)
                self.assertEqual(receipt["rows_read"], 12)
                self.assertEqual(receipt["dependencies"], {})
                self.assertEqual(receipt["cohort_digest"], frozen["digest"])
                self.assertEqual(api.start_monitor_test(**call), receipt)
                with self.assertRaisesRegex(sync_runtime_adapter.RuntimeBridgeError, "different approval"):
                    api.start_monitor_test(**dict(call, operation="HeroCustomers"))
                reader.assert_called_once()
        finally:
            frappe.db.rollback()
            frappe.db.delete("HERO Polling Event", {"name": run})
            frappe.db.delete("HERO Budget Outcome", {"budget": key})
            frappe.db.delete("HERO Execution Budget", {"name": key})
            frappe.db.commit()

    def test_new_runs_advance_and_replay_after_claim_before_freeze(self):
        from uuid import uuid4
        import frappe
        from hero_integration import api
        from hero_integration.services.execution_budget import ExecutionBudget
        from hero_integration.services.polling import MonitorResult

        self.assertEqual(frappe.local.site, os.environ["HERO_EXECUTION_BUDGET_TEST_SITE"])
        self.assertTrue(frappe.conf.allow_tests)
        environment = uuid4().hex
        self.enterContext(patch("hero_integration.services.test_environment.require_environment", return_value=environment))
        settings = frappe.get_single("HERO Integration Settings")
        source = settings.hero_source_instance or "default"
        revision = int(settings.source_binding_revision or 0)
        confirmed = settings.source_binding_confirmed_revision
        if revision != int(confirmed or 0):
            frappe.db.set_single_value(settings.doctype, "source_binding_confirmed_revision", revision)
            frappe.db.commit()
            def restore_binding():
                frappe.db.set_single_value(settings.doctype, "source_binding_confirmed_revision", confirmed)
                frappe.db.commit()
            self.addCleanup(restore_binding)
        settings.testing_enabled, settings.testing_max_actions, settings.polling_mode = 1, 2, "MONITOR"
        get_single = frappe.get_single
        self.enterContext(patch.object(frappe, "get_single", side_effect=lambda dt: settings if dt == settings.doctype else get_single(dt)))
        requests = []
        def reader(*, frappe_module, sample):
            ids = ("1","2","3","4","5")[sample.offset:sample.offset+sample.limit]
            requests.append((sample.offset, sample.limit))
            return MonitorResult(len(ids), 0, source, rows_read=len(ids), observed_ids=ids, reader_digest="reader",
                                 page_size=sample.limit, fingerprints={key: key for key in ids})

        runs = []
        def call(run=None, expected_limit=2):
            if not run:
                run = uuid4().hex
                runs.append(run)
            return api.start_monitor_test(run, "plan", 3, revision, "HeroCustomers", 1, expected_limit)
        def budget(run, limit):
            return ExecutionBudget("monitor", run, source, limit, version=3, source_revision=revision)
        try:
            with patch.object(sync_runtime_adapter, "run_materialized_monitor", side_effect=reader):
                with self.assertRaisesRegex(ValueError, "limit changed"):
                    call(expected_limit=1)
                self.assertFalse(requests)
                first = call()
                self.assertEqual(budget(first["run"], 2).selection()["ids"], ["1","2"])
                with patch.object(ExecutionBudget, "freeze", side_effect=ValueError("crash after claim")):
                    with self.assertRaisesRegex(ValueError, "crash after claim"):
                        call()
                last = runs[-1]
                before = list(requests)
                settings.testing_max_actions = 1
                second = call(last)
                self.assertEqual(before, [(0,2),(0,2),(2,2)])
                self.assertEqual(requests, before)
                self.assertEqual(call(last), second)
                self.assertEqual((second["selected"], second["limit"], second["processed"]), (2,2,2))
                self.assertEqual(budget(last, 2).selection()["ids"], ["3","4"])
                third = call(expected_limit=1)
                self.assertEqual(budget(third["run"], 1).selection()["ids"], ["5"])
                self.assertEqual(call(expected_limit=1)["selected"], 0)
                self.assertEqual(frappe.db.count("HERO Test Cohort Series",
                                 {"environment_id": environment, "migration_type":"monitor:HeroCustomers"}), 1)
        finally:
            frappe.db.rollback()
            for run in runs:
                key = budget(run, 2).budget_id
                frappe.db.delete("HERO Polling Event", {"name":run})
                frappe.db.delete("HERO Budget Outcome", {"budget":key})
                frappe.db.delete("HERO Execution Budget", {"name":key})
            frappe.db.delete("HERO Test Cohort Series", {"environment_id":environment})
            frappe.db.commit()


class TestScheduledMonitor(unittest.TestCase):
    def test_scheduler_records_only_bounded_observation_and_no_sync_checkpoint(self):
        from hero_integration.services import polling
        settings = SimpleNamespace(polling_mode="MONITOR", sync_status="", sync_error="", save=Mock())
        settings.last_successful_sync = datetime(2025, 12, 1)
        settings.polling_notifications = False
        fake = types.ModuleType("frappe")
        fake.db = Mock()
        fake.get_single = Mock(return_value=settings)
        fake.utils = SimpleNamespace(now_datetime=lambda: datetime(2026, 1, 1))
        result = polling.MonitorResult(1, 0, "source", ("HeroCustomers",))
        with patch.dict(sys.modules, {"frappe": fake}), \
             patch.object(sync_runtime_adapter, "run_materialized_monitor", return_value=result) as run, \
             patch.object(polling, "_record_event", return_value="event") as event:
            polling.run_monitor()
        run.assert_called_once_with(frappe_module=fake)
        event.assert_called_once()
        self.assertEqual(event.call_args.args[:2], ("MONITOR", "Started"))
        fake.db.commit.assert_called_once()
        self.assertEqual(fake.db.set_value.call_args.args[1:], ("event", {
            "status": "Incomplete", "detail": fake.db.set_value.call_args.args[2]["detail"],
            "customer_records": 1, "project_records": 0, "rows_read": 1}))
        self.assertEqual(settings.sync_status, "MONITOR incomplete")
        self.assertEqual(settings.last_successful_sync, datetime(2025, 12, 1))
        self.assertEqual(fake.get_single.call_count, 2)

    def test_unapproved_plan_is_a_visible_skip_not_a_legacy_query(self):
        from hero_integration.services import polling
        settings = SimpleNamespace(polling_mode="MONITOR", sync_status="", sync_error="", save=Mock())
        fake = types.ModuleType("frappe")
        fake.db = Mock()
        fake.get_single = Mock(return_value=settings)
        with patch.dict(sys.modules, {"frappe": fake}), \
             patch.object(sync_runtime_adapter, "run_materialized_monitor", side_effect=sync_runtime_adapter.RuntimeBridgeError("No approved plan.")), \
             patch.object(polling, "_record_event", return_value="event"):
            polling.run_monitor()
        self.assertEqual(fake.db.set_value.call_args.args[:2], ("HERO Polling Event", "event"))
        self.assertEqual(fake.db.set_value.call_args.args[2]["status"], "Skipped")

    def test_monitor_failure_reloads_settings_instead_of_saving_a_stale_source(self):
        from hero_integration.services import polling
        old = SimpleNamespace(polling_mode='MONITOR', save=Mock())
        current = SimpleNamespace(save=Mock())
        fake = types.ModuleType('frappe')
        fake.db = Mock()
        fake.get_single = Mock(side_effect=[old, current])
        fake.log_error = Mock()
        with patch.dict(sys.modules, {'frappe': fake}), \
             patch.object(sync_runtime_adapter, 'run_materialized_monitor', side_effect=ValueError('changed rules')), \
             patch.object(polling, '_record_event'):
            polling.run_monitor()
        old.save.assert_not_called()
        current.save.assert_called_once()
        fake.db.rollback.assert_called_once()

    def test_unexpected_failure_rolls_back_and_does_not_publish_exception_text(self):
        from hero_integration.services import polling
        sensitive = 'synthetic-secret-from-exception'
        settings = SimpleNamespace(polling_mode='MONITOR', save=Mock())
        fake = types.ModuleType('frappe')
        fake.db = Mock()
        fake.get_single = Mock(return_value=settings)
        fake.log_error = Mock()

        def record_event(*args):
            return "event"

        with patch.dict(sys.modules, {'frappe': fake}), \
             patch.object(sync_runtime_adapter, 'run_materialized_monitor', side_effect=ValueError(sensitive)), \
             patch.object(polling, '_record_event', side_effect=record_event) as event:
            polling.run_monitor()

        event.assert_called_once()
        fake.db.rollback.assert_called_once()
        self.assertEqual(fake.db.set_value.call_args.args[2]["status"], "Failed")
        self.assertNotIn(sensitive, str(fake.db.set_value.call_args))
        self.assertNotIn(sensitive, settings.sync_error)
        self.assertNotIn(sensitive, str(fake.log_error.call_args))
