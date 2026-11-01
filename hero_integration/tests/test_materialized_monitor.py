"""MONITOR uses the existing approved materializer without business writes."""
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
        self.executable = SimpleNamespace(selection=self.selection, generation=7)
        self.service = SimpleNamespace(current_approved=lambda *a: self.plan,
                                       resolve_executable_plan=lambda *a: self.executable)
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

    def test_transport_boundary_has_released_prior_transactions(self):
        def reply(*args):
            self.assertGreaterEqual(self.frappe.db.rollback.call_count, 2)
            return {"things": [{"id": "1", "name": "test"}]}
        self.client.query.side_effect = reply
        self.run_monitor()

    def test_invalid_result_identity_is_not_reported_as_success(self):
        self.client.query.return_value = {"things": [{"id": None, "name": "test"}]}
        with self.assertRaises(ValueError): self.run_monitor()


class TestScheduledMonitor(unittest.TestCase):
    def test_scheduler_records_only_bounded_observation_and_no_sync_checkpoint(self):
        from hero_integration.services import polling
        settings = SimpleNamespace(polling_mode="MONITOR", sync_status="", sync_error="", save=Mock())
        settings.polling_notifications = False
        fake = types.ModuleType("frappe")
        fake.db = SimpleNamespace(rollback=Mock())
        fake.get_single = Mock(return_value=settings)
        fake.utils = SimpleNamespace(now_datetime=lambda: datetime(2026, 1, 1))
        result = polling.MonitorResult(1, 0, "source", ("HeroCustomers",))
        with patch.dict(sys.modules, {"frappe": fake}), \
             patch.object(sync_runtime_adapter, "run_materialized_monitor", return_value=result) as run, \
             patch.object(polling, "_record_event", return_value="event") as event:
            polling.run_monitor()
        run.assert_called_once_with(frappe_module=fake)
        event.assert_called_once()
        self.assertEqual(event.call_args.args[:2], ("MONITOR", "Incomplete"))
        self.assertEqual(settings.sync_status, "MONITOR incomplete")
        self.assertEqual(fake.get_single.call_count, 2)

    def test_unapproved_plan_is_a_visible_skip_not_a_legacy_query(self):
        from hero_integration.services import polling
        settings = SimpleNamespace(polling_mode="MONITOR", sync_status="", sync_error="", save=Mock())
        fake = types.ModuleType("frappe")
        fake.db = SimpleNamespace(rollback=Mock())
        fake.get_single = Mock(return_value=settings)
        with patch.dict(sys.modules, {"frappe": fake}), \
             patch.object(sync_runtime_adapter, "run_materialized_monitor", side_effect=sync_runtime_adapter.RuntimeBridgeError("No approved plan.")), \
             patch.object(polling, "record_scheduler_outcome") as outcome:
            polling.run_monitor()
        outcome.assert_called_once_with("MONITOR", "Skipped", "Materialized monitoring is blocked: No approved plan.")

    def test_monitor_failure_reloads_settings_instead_of_saving_a_stale_source(self):
        from hero_integration.services import polling
        old = SimpleNamespace(polling_mode='MONITOR', save=Mock())
        current = SimpleNamespace(save=Mock())
        fake = types.ModuleType('frappe')
        fake.db = SimpleNamespace(rollback=Mock())
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
        fake.db = SimpleNamespace(rollback=Mock())
        fake.get_single = Mock(return_value=settings)
        fake.log_error = Mock()

        def record_event(*args):
            fake.db.rollback.assert_called_once()

        with patch.dict(sys.modules, {'frappe': fake}), \
             patch.object(sync_runtime_adapter, 'run_materialized_monitor', side_effect=ValueError(sensitive)), \
             patch.object(polling, '_record_event', side_effect=record_event) as event:
            polling.run_monitor()

        event.assert_called_once()
        self.assertNotIn(sensitive, str(event.call_args))
        self.assertNotIn(sensitive, settings.sync_error)
        self.assertNotIn(sensitive, str(fake.log_error.call_args))
