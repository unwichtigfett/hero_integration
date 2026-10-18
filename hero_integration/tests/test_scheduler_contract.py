from __future__ import annotations

import importlib
import sys
import types
import unittest
from types import SimpleNamespace


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
