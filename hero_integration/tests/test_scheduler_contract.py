from __future__ import annotations

import importlib
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch

try:
    import frappe  # noqa: F401
except ModuleNotFoundError:
    frappe = types.ModuleType("frappe")
    frappe.whitelist = lambda: lambda function: function
    sys.modules["frappe"] = frappe


class TestSchedulerContract(unittest.TestCase):
    def test_disabled_scheduler_does_not_start_a_sync(self):
        frappe_module = types.ModuleType("frappe")
        calls: list[str] = []
        def get_single(doctype):
            calls.append(doctype)
            return SimpleNamespace(scheduled_sync_enabled=0)

        frappe_module.get_single = get_single
        previous = sys.modules.get("frappe")
        sys.modules.pop("hero_integration.tasks", None)
        sys.modules["frappe"] = frappe_module
        try:
            tasks = importlib.import_module("hero_integration.tasks")
            tasks.run_scheduled_sync()
        finally:
            sys.modules.pop("hero_integration.tasks", None)
            if previous is None:
                sys.modules.pop("frappe", None)
            else:
                sys.modules["frappe"] = previous

        self.assertEqual(calls, ["HERO Integration Settings"])

    def test_public_rpc_cannot_supply_a_scheduled_permission_bypass(self):
        from hero_integration import api

        calls = []
        fake_frappe = SimpleNamespace(
            only_for=lambda role: calls.append(("only_for", role)),
            throw=lambda message: (_ for _ in ()).throw(ValueError(message)),
        )
        with patch.object(api, "frappe", fake_frappe), patch.object(api, "_require_active_mappings"), patch.object(
            api, "_has_active_run", return_value=False
        ), patch.object(api, "_start_migration", return_value={"status": "QUEUED"}) as start:
            api.start_migration("Sync", dry_run=0)

        self.assertEqual(calls, [("only_for", "System Manager")])
        self.assertEqual(start.call_args.kwargs, {"action": "Sync", "dry_run": 0, "scheduled": False})

    def test_scheduler_uses_the_internal_entrypoint_only(self):
        frappe_module = types.ModuleType("frappe")
        frappe_module.get_single = lambda _doctype: SimpleNamespace(scheduled_sync_enabled=1)
        previous = sys.modules.get("frappe")
        sys.modules.pop("hero_integration.tasks", None)
        sys.modules["frappe"] = frappe_module
        try:
            with patch("hero_integration.api.start_scheduled_sync", return_value={"status": "QUEUED"}) as start:
                tasks = importlib.import_module("hero_integration.tasks")
                tasks.run_scheduled_sync()
        finally:
            sys.modules.pop("hero_integration.tasks", None)
            if previous is None:
                sys.modules.pop("frappe", None)
            else:
                sys.modules["frappe"] = previous

        start.assert_called_once_with()
