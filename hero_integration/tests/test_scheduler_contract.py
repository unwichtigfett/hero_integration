from __future__ import annotations

import importlib
import sys
import types
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

try:
    import frappe  # noqa: F401
except ModuleNotFoundError:
    frappe = types.ModuleType("frappe")
    frappe.whitelist = lambda *args, **kwargs: lambda function: function
    sys.modules["frappe"] = frappe


class TestSchedulerContract(unittest.TestCase):
    def setUp(self):
        package = importlib.import_module("hero_integration")
        missing = object()
        previous_module = sys.modules.get("hero_integration.tasks")
        previous_alias = getattr(package, "tasks", missing)

        def restore():
            if previous_module is None:
                sys.modules.pop("hero_integration.tasks", None)
            else:
                sys.modules["hero_integration.tasks"] = previous_module
            if previous_alias is missing:
                if hasattr(package, "tasks"):
                    delattr(package, "tasks")
            else:
                package.tasks = previous_alias

        self.addCleanup(restore)

    def test_disabled_scheduler_does_not_start_a_sync(self):
        frappe_module = types.ModuleType("frappe")
        frappe_module.whitelist = lambda *args, **kwargs: lambda function: function
        calls: list[str] = []
        def get_single(doctype):
            calls.append(doctype)
            return SimpleNamespace(scheduled_sync_enabled=0)

        frappe_module.get_single = get_single
        frappe_module.utils = SimpleNamespace(now_datetime=lambda: datetime(2026, 10, 21, 10, 0))
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
        with patch.object(api, "frappe", fake_frappe), patch.object(
            api, "_has_active_run", return_value=False
        ), patch.object(api, "_start_migration", return_value={"status": "QUEUED"}) as start:
            api.start_migration("Sync", dry_run=0)

        self.assertEqual(calls, [("only_for", "System Manager")])
        self.assertEqual(start.call_args.kwargs, {"action": "Sync", "dry_run": 0, "scheduled": False, "graphql_service": ""})

class TestSchedulerCadence(unittest.TestCase):
    def test_scheduler_ticks_every_minute_so_short_intervals_are_honoured(self):
        import ast
        from pathlib import Path

        tree = ast.parse((Path(__file__).resolve().parents[1] / "hooks.py").read_text())
        events = next(ast.literal_eval(node.value) for node in tree.body
                      if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "scheduler_events")
        self.assertEqual(events, {"cron": {"* * * * *": [
            "hero_integration.tasks.run_scheduled_sync", "hero_integration.tasks.run_scheduled_schema_refresh",
        ]}, "daily": ["hero_integration.integrations.hero_people.sync_daily"]})
