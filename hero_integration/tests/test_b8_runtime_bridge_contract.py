from __future__ import annotations

import importlib
import sys
import types
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from hero_integration.services import sync_runtime_adapter


class TestB8RuntimeBridgeContract(unittest.TestCase):
    def test_auto_sync_hands_off_only_to_the_canonical_runtime_adapter(self):
        frappe_module = types.ModuleType("frappe")
        frappe_module.get_single = lambda _doctype: SimpleNamespace(
            polling_mode="AUTO_SYNC",
            scheduled_sync_enabled=1,
            polling_interval_minutes=60,
            last_successful_sync=None,
        )
        frappe_module.utils = SimpleNamespace(
            now_datetime=lambda: datetime(2026, 10, 25, 10, 0)
        )
        adapter = types.ModuleType("hero_integration.services.sync_runtime_adapter")
        calls: list[str] = []
        adapter.run_scheduled_auto_sync = lambda: calls.append("canonical")

        previous_frappe = sys.modules.get("frappe")
        previous_adapter = sys.modules.get("hero_integration.services.sync_runtime_adapter")
        sys.modules.pop("hero_integration.tasks", None)
        sys.modules["frappe"] = frappe_module
        sys.modules["hero_integration.services.sync_runtime_adapter"] = adapter
        try:
            tasks = importlib.import_module("hero_integration.tasks")
            tasks.run_scheduled_sync()
        finally:
            sys.modules.pop("hero_integration.tasks", None)
            if previous_frappe is None:
                sys.modules.pop("frappe", None)
            else:
                sys.modules["frappe"] = previous_frappe
            if previous_adapter is None:
                sys.modules.pop("hero_integration.services.sync_runtime_adapter", None)
            else:
                sys.modules["hero_integration.services.sync_runtime_adapter"] = previous_adapter

        self.assertEqual(calls, ["canonical"])

    def test_preflight_revalidates_the_current_plan_before_any_runtime_execution(self):
        selection = SimpleNamespace(
            source_instance="hero-one", execution_mode="AUTO_SYNC", operation_revision=3, service_revision=3
        )
        executable = SimpleNamespace(plan_key="a" * 64, generation=7, selection=selection)
        approved = SimpleNamespace(generation=7)
        calls = []

        class Service:
            def current_approved(self, site_scope, source_instance):
                calls.append(("current", site_scope, source_instance))
                return approved

            def resolve_executable_plan(self, *args):
                calls.append(("resolve", args))
                return executable

        frappe_module = SimpleNamespace(
            local=SimpleNamespace(site="b9-test.localhost"),
            session=SimpleNamespace(user="Administrator"),
            get_single=lambda _doctype: SimpleNamespace(polling_mode="AUTO_SYNC", scheduled_sync_enabled=1, hero_source_instance="hero-one"),
        )
        with patch.object(sync_runtime_adapter, "_runtime_plan_service", return_value=Service()):
            result = sync_runtime_adapter.run_scheduled_auto_sync(frappe_module=frappe_module)

        self.assertEqual(result.status, sync_runtime_adapter.BLOCKED_NO_B7_MATERIALIZED_RUNTIME)
        self.assertEqual(calls, [
            ("current", "b9-test.localhost", "hero-one"),
            ("resolve", ("b9-test.localhost", "hero-one", 7, True, "Administrator")),
        ])

    def test_preflight_rejects_missing_approved_plan_before_resolution(self):
        class Service:
            def current_approved(self, _site_scope, _source_instance):
                return None

            def resolve_executable_plan(self, *_args):
                raise AssertionError("must not resolve a missing plan")

        frappe_module = SimpleNamespace(
            local=SimpleNamespace(site="b9-test.localhost"),
            session=SimpleNamespace(user="Administrator"),
            get_single=lambda _doctype: SimpleNamespace(polling_mode="AUTO_SYNC", scheduled_sync_enabled=1, hero_source_instance="hero-one"),
        )
        with patch.object(sync_runtime_adapter, "_runtime_plan_service", return_value=Service()):
            with self.assertRaises(sync_runtime_adapter.RuntimeBridgeError):
                sync_runtime_adapter.run_scheduled_auto_sync(frappe_module=frappe_module)

    def test_preflight_rejects_an_unauthenticated_scheduler_before_plan_access(self):
        frappe_module = SimpleNamespace(
            local=SimpleNamespace(site="b9-test.localhost"),
            session=SimpleNamespace(user="Guest"),
            get_single=lambda _doctype: SimpleNamespace(polling_mode="AUTO_SYNC", scheduled_sync_enabled=1, hero_source_instance="hero-one"),
        )
        with patch.object(sync_runtime_adapter, "_runtime_plan_service") as service:
            with self.assertRaises(PermissionError):
                sync_runtime_adapter.run_scheduled_auto_sync(frappe_module=frappe_module)

        service.assert_not_called()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
