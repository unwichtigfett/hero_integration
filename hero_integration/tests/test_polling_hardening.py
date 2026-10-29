"""Cyclic polling hardening: scheduler time handling, fail-closed configuration and visible outcomes."""
from __future__ import annotations

import importlib
import sys
import types
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from hero_integration.services import polling
from hero_integration.services.polling import runtime_plan, scheduler_due


def _settings(**values):
    return SimpleNamespace(**{"polling_mode": "MONITOR", "scheduled_sync_enabled": 0, "polling_interval_minutes": 60, **values})


class TestSchedulerTime(unittest.TestCase):
    def test_frappe_string_datetimes_from_singles_are_compared_safely(self):
        settings, now = _settings(), datetime(2026, 11, 10, 12, 0)
        self.assertFalse(scheduler_due(settings, now, "2026-11-10 11:30:00"))
        self.assertTrue(scheduler_due(settings, now, "2026-11-10 10:59:59.123456"))
        self.assertTrue(scheduler_due(settings, now, "not a timestamp"), "unparseable history means no known success")
        self.assertTrue(scheduler_due(settings, now, ""))

    def test_naive_now_is_read_in_the_system_timezone_not_the_polling_timezone(self):
        settings = _settings(polling_timezone="UTC", polling_execution_window="09:00-10:00")
        berlin_clock = datetime(2026, 11, 10, 10, 30)  # 09:30 UTC in winter time
        self.assertTrue(scheduler_due(settings, berlin_clock, None, system_timezone="Europe/Berlin"))
        self.assertFalse(scheduler_due(settings, datetime(2026, 11, 10, 11, 30), None, system_timezone="Europe/Berlin"))
        aware = datetime(2026, 11, 10, 9, 30, tzinfo=timezone.utc)
        self.assertTrue(scheduler_due(settings, aware, None, system_timezone="Europe/Berlin"))

    def test_interval_uses_the_same_clock_for_now_and_last_success(self):
        settings = _settings(polling_interval_minutes=60)
        now = datetime(2026, 11, 10, 12, 0)
        self.assertFalse(scheduler_due(settings, now, now - timedelta(minutes=59), system_timezone="Europe/Berlin"))
        self.assertTrue(scheduler_due(settings, now, now - timedelta(minutes=60), system_timezone="Europe/Berlin"))
        self.assertTrue(scheduler_due(settings, now, now + timedelta(hours=3), system_timezone="Europe/Berlin"),
                        "a success time in the future must not block polling indefinitely")
        self.assertFalse(scheduler_due(settings, now, None, system_timezone="Invalid/Zone"), "unknown system timezone fails closed")


class TestPollingConfiguration(unittest.TestCase):
    def test_invalid_or_unreviewed_filters_block_instead_of_running_unfiltered(self):
        cases = {
            "{not json": ["filters (invalid JSON object)"],
            "[1, 2]": ["filters (invalid JSON object)"],
            '{"HeroCustomers": 5}': ["filters (invalid JSON object)"],
            '{"HeroCustomers": {"showDeleted": "false"}}': ["filter HeroCustomers.showDeleted"],
            '{"HeroCustomers": {"search": "x"}}': ["filter HeroCustomers.search"],
            '{"HeroProjectMatches": {"status": 1}}': ["filter HeroProjectMatches.status"],
            '{"FutureQuery": {}}': ["filter FutureQuery"],
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                plan = runtime_plan(_settings(polling_filters_json=value))
                self.assertEqual(plan["unsupported_sources"], expected)
                self.assertFalse(scheduler_due(_settings(polling_filters_json=value), datetime(2026, 11, 10, 12, 0)))
        ok = runtime_plan(_settings(polling_filters_json='{"HeroCustomers": {"showDeleted": false}}'))
        self.assertEqual((ok["unsupported_sources"], ok["filters"]), ([], {"HeroCustomers": {"showDeleted": False}}))

    def test_unknown_data_source_line_is_reported(self):
        self.assertEqual(runtime_plan(_settings(polling_data_sources="{}"))["unsupported_sources"], ["{}"])


class _FakeFrappe(types.ModuleType):
    def __init__(self, settings):
        super().__init__("frappe")
        self.settings, self.events, self.logged = settings, [], []
        self.whitelist = lambda *args, **kwargs: (lambda function: function)
        self.utils = SimpleNamespace(now_datetime=lambda: datetime(2026, 11, 10, 12, 0), get_system_timezone=lambda: "Europe/Berlin")

    def get_single(self, doctype):
        assert doctype == "HERO Integration Settings", doctype
        return self.settings

    def get_doc(self, payload):
        doc = SimpleNamespace(**payload, name=f"EV-{len(self.events) + 1}")
        doc.insert = lambda ignore_permissions=False: self.events.append(payload)
        return doc

    def log_error(self, title=""):
        self.logged.append(title)


class TestScheduledEntry(unittest.TestCase):
    def run_tasks(self, settings, adapter=None, lock=None, lock_error=None):
        fake = _FakeFrappe(settings)
        settings.save = lambda ignore_permissions=False: None
        modules = {"frappe": fake}
        if adapter is not None:
            modules["hero_integration.services.sync_runtime_adapter"] = adapter
        previous = {name: sys.modules.get(name) for name in modules}
        sys.modules.pop("hero_integration.tasks", None)
        sys.modules.update(modules)
        try:
            tasks = importlib.import_module("hero_integration.tasks")
            errors = (lambda: lock_error) if lock_error else tasks._lock_timeout_error
            with patch.object(tasks, "_polling_lock", lock or tasks._polling_lock), patch.object(tasks, "_lock_timeout_error", errors):
                tasks.run_scheduled_sync()
            return fake, tasks
        finally:
            sys.modules.pop("hero_integration.tasks", None)
            for name, module in previous.items():
                if module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = module

    def test_unsupported_configuration_is_recorded_once_without_a_request(self):
        settings = _settings(polling_data_sources="{}", sync_status="", sync_error="")
        with patch.object(polling, "run_monitor") as run_monitor:
            fake, _tasks = self.run_tasks(settings)
            self.run_tasks(settings)
        run_monitor.assert_not_called()
        self.assertEqual(len(fake.events), 1)
        self.assertEqual((fake.events[0]["mode"], fake.events[0]["status"]), ("MONITOR", "Skipped"))
        self.assertIn("{}", settings.sync_error)
        self.assertEqual(settings.sync_status, "MONITOR skipped")

    def test_string_last_success_no_longer_crashes_the_hourly_run(self):
        settings = _settings(last_successful_sync="2026-11-10 11:30:00", sync_status="", sync_error="")
        with patch.object(polling, "run_monitor") as run_monitor:
            self.run_tasks(settings)
        run_monitor.assert_not_called()
        settings.last_successful_sync = "2026-11-10 10:00:00"
        with patch.object(polling, "run_monitor") as run_monitor:
            self.run_tasks(settings)
        run_monitor.assert_called_once()

    def test_a_running_poll_makes_the_next_tick_skip_visibly(self):
        class Busy(Exception):
            pass

        @contextmanager
        def busy_lock():
            raise Busy()
            yield

        settings = _settings(sync_status="", sync_error="")
        with patch.object(polling, "run_monitor") as run_monitor:
            fake, _ = self.run_tasks(settings, lock=busy_lock, lock_error=Busy)
        run_monitor.assert_not_called()
        self.assertEqual(fake.events[-1]["status"], "Skipped")
        self.assertIn("previous polling run is still active", fake.events[-1]["detail"])

    def test_blocked_and_failed_auto_sync_are_recorded_instead_of_crashing(self):
        class RuntimeBridgeError(ValueError):
            pass

        adapter = types.ModuleType("hero_integration.services.sync_runtime_adapter")
        adapter.RuntimeBridgeError = RuntimeBridgeError
        adapter.BLOCKED_NO_B7_MATERIALIZED_RUNTIME = "BLOCKED"
        settings = _settings(polling_mode="AUTO_SYNC", sync_status="", sync_error="")

        adapter.run_scheduled_auto_sync = MagicMock(side_effect=RuntimeBridgeError("No approved canonical runtime plan exists."))
        fake, _ = self.run_tasks(settings, adapter)
        self.assertEqual((fake.events[-1]["mode"], fake.events[-1]["status"]), ("AUTO_SYNC", "Skipped"))
        self.assertIn("No approved canonical runtime plan exists.", fake.events[-1]["detail"])

        adapter.run_scheduled_auto_sync = MagicMock(side_effect=KeyError("token=secret"))
        fake, _ = self.run_tasks(settings, adapter)
        self.assertEqual(fake.events[-1]["status"], "Failed")
        self.assertNotIn("secret", fake.events[-1]["detail"])
        self.assertEqual(fake.logged, ["HERO automatic sync failed"])

        adapter.run_scheduled_auto_sync = MagicMock(return_value=SimpleNamespace(status="BLOCKED"))
        fake, _ = self.run_tasks(settings, adapter)
        self.assertIn("no materialized runtime", fake.events[-1]["detail"])


if __name__ == "__main__":
    unittest.main()


class TestPollingSettingsSave(unittest.TestCase):
    def save(self, **values):
        try:
            import frappe  # noqa: F401
        except ModuleNotFoundError:
            stub = types.ModuleType("frappe")
            stub.whitelist = lambda *args, **kwargs: (lambda function: function)
            sys.modules["frappe"] = stub
        from hero_integration import api

        settings = SimpleNamespace(polling_pagination_strategy="OFFSET")
        defaults = {"timezone": None, "execution_window": None, "data_sources": None, "filters_json": None, "bootstrap": None,
                    "page_size": None, "pagination_strategy": None, "allow_backfill": None, "full_inventory": None,
                    "reconciliation": None, "retry_limit": None, "retry_backoff_seconds": None, "timeout_seconds": None,
                    "rate_limit": None, "concurrency": None, "notifications": None}
        fake = SimpleNamespace(throw=lambda message: (_ for _ in ()).throw(ValueError(message)))
        with patch.object(api, "frappe", fake):
            api._save_polling_configuration(settings, **{**defaults, **values})
        return settings

    def test_unknown_sources_and_unreviewed_filters_are_rejected_when_saving(self):
        with self.assertRaisesRegex(ValueError, "Unsupported polling data source: \\{\\}"):
            self.save(data_sources="{}")
        with self.assertRaisesRegex(ValueError, "Unsupported polling filters: filter HeroCustomers.search"):
            self.save(filters_json='{"HeroCustomers": {"search": "x"}}')
        with self.assertRaisesRegex(ValueError, "filter HeroCustomers.showDeleted"):
            self.save(filters_json='{"HeroCustomers": {"showDeleted": "no"}}')
        saved = self.save(data_sources="HeroCustomers\nHeroProjectMatches", filters_json='{"HeroCustomers": {"showDeleted": false}}')
        self.assertEqual(saved.polling_filters_json, '{"HeroCustomers": {"showDeleted": false}}')
