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
        self.plan = SimpleNamespace(generation=7)
        self.executable = SimpleNamespace(selection=self.selection, generation=7)
        self.service = SimpleNamespace(current_approved=lambda *a: self.plan,
                                       resolve_executable_plan=lambda *a: self.executable)
        self.client = SimpleNamespace(query=Mock(return_value={"things": [{"id": "1", "name": "test"}]}))
        self.config = SimpleNamespace(source_instance="b8-source", endpoint=object(), api_token="synthetic", timeout_seconds=30)

    def run_monitor(self):
        with patch.object(sync_runtime_adapter, "_runtime_plan_service", return_value=self.service), \
             patch("hero_integration.client.settings.get_configuration", return_value=self.config):
            return sync_runtime_adapter.run_materialized_monitor(frappe_module=self.frappe, client=self.client)

    def test_executes_exact_reconstructed_read_and_reports_bounded_incomplete(self):
        result = self.run_monitor()
        self.client.query.assert_called_once_with(self.request.document, dict(self.request.variables))
        self.assertEqual(result.project_records, 1)
        self.assertEqual(result.incomplete_sources, ("HeroProjectMatches",))
        self.assertEqual(result.project_next_offset, 0)

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

    def test_invalid_result_identity_is_not_reported_as_success(self):
        self.client.query.return_value = {"things": [{"id": None, "name": "test"}]}
        with self.assertRaises(ValueError): self.run_monitor()


class TestScheduledMonitor(unittest.TestCase):
    def test_scheduler_records_only_bounded_observation_and_no_sync_checkpoint(self):
        from hero_integration.services import polling
        settings = SimpleNamespace(polling_mode="MONITOR", sync_status="", sync_error="", save=Mock())
        settings.polling_notifications = False
        fake = types.ModuleType("frappe")
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
        fake.get_single.assert_called_once_with("HERO Integration Settings")

    def test_unapproved_plan_is_a_visible_skip_not_a_legacy_query(self):
        from hero_integration.services import polling
        settings = SimpleNamespace(polling_mode="MONITOR", sync_status="", sync_error="", save=Mock())
        fake = types.ModuleType("frappe")
        fake.get_single = Mock(return_value=settings)
        with patch.dict(sys.modules, {"frappe": fake}), \
             patch.object(sync_runtime_adapter, "run_materialized_monitor", side_effect=sync_runtime_adapter.RuntimeBridgeError("No approved plan.")), \
             patch.object(polling, "record_scheduler_outcome") as outcome:
            polling.run_monitor()
        outcome.assert_called_once_with("MONITOR", "Skipped", "Materialized monitoring is blocked: No approved plan.")
