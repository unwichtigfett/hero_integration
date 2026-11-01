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

    def test_overnight_window_uses_configured_local_time(self):
        settings = _settings(polling_timezone="Europe/Berlin", polling_execution_window="22:00-02:00")
        for hour, minute, expected in ((20, 59, False), (21, 0, True), (0, 59, True), (1, 0, True), (1, 1, False)):
            with self.subTest(utc=(hour, minute)):
                now = datetime(2026, 11, 10, hour, minute, tzinfo=timezone.utc)
                self.assertEqual(scheduler_due(settings, now, None), expected)


class TestPollingConfiguration(unittest.TestCase):
    def test_left_over_sources_and_filters_from_older_versions_no_longer_block_polling(self):
        for sources, filters in (("{}", "{}"), ("FutureQuery", "{not json"), ("HeroCustomers", '{"HeroCustomers": {"search": "x"}}')):
            with self.subTest(sources=sources, filters=filters):
                settings = _settings(polling_data_sources=sources, polling_filters_json=filters)
                plan = runtime_plan(settings)
                self.assertEqual((plan["sources"], plan["unsupported_sources"]), (["HeroCustomers", "HeroProjectMatches"], []))
                self.assertTrue(scheduler_due(settings, datetime(2026, 11, 10, 12, 0)))


class _FakeDb:
    def __init__(self, state=None):
        self.state, self.commits = dict(state or {}), 0

    def get_single_value(self, doctype, field):
        assert doctype == "HERO Integration Sync State", doctype
        return self.state.get(field)

    def set_single_value(self, doctype, field, value):
        assert doctype == "HERO Integration Sync State", doctype
        self.state[field] = value

    def commit(self):
        self.commits += 1


class _FakeFrappe(types.ModuleType):
    def __init__(self, settings, state=None):
        super().__init__("frappe")
        self.settings, self.events, self.logged = settings, [], []
        self.db = _FakeDb(state)
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
    def run_tasks(self, settings, adapter=None, lock=None, lock_error=None, state=None):
        fake = _FakeFrappe(settings, state)
        settings.save = lambda ignore_permissions=False: None
        modules = {"frappe": fake}
        if adapter is not None:
            modules["hero_integration.services.sync_runtime_adapter"] = adapter
        previous = {name: sys.modules.get(name) for name in modules}
        package = importlib.import_module("hero_integration")
        missing = object()
        previous_alias = getattr(package, "tasks", missing)
        previous_tasks = sys.modules.get("hero_integration.tasks")
        sys.modules.pop("hero_integration.tasks", None)
        sys.modules.update(modules)
        try:
            tasks = importlib.import_module("hero_integration.tasks")
            errors = (lambda: lock_error) if lock_error else tasks._lock_timeout_error
            with patch.object(tasks, "_polling_lock", lock or tasks._polling_lock), patch.object(tasks, "_lock_timeout_error", errors):
                tasks.run_scheduled_sync()
            return fake, tasks
        finally:
            if previous_tasks is None:
                sys.modules.pop("hero_integration.tasks", None)
            else:
                sys.modules["hero_integration.tasks"] = previous_tasks
            if previous_alias is missing:
                if hasattr(package, "tasks"):
                    delattr(package, "tasks")
            else:
                package.tasks = previous_alias
            for name, module in previous.items():
                if module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = module

    def test_unsupported_configuration_is_recorded_once_without_a_request(self):
        settings = _settings(polling_timezone="Invalid/Zone", sync_status="", sync_error="")
        with patch.object(polling, "run_monitor") as run_monitor:
            fake, _tasks = self.run_tasks(settings)
            self.run_tasks(settings)
        run_monitor.assert_not_called()
        self.assertEqual(len(fake.events), 1)
        self.assertEqual((fake.events[0]["mode"], fake.events[0]["status"]), ("MONITOR", "Skipped"))
        self.assertIn("timezone Invalid/Zone", settings.sync_error)
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

    def test_off_paused_and_invalid_window_never_start_a_poll(self):
        for overrides in ({"polling_mode": "OFF"}, {"sync_service_paused": 1},
                          {"polling_execution_window": "not-a-window"}):
            with self.subTest(settings=overrides):
                settings = _settings(sync_status="", sync_error="", **overrides)
                with patch.object(polling, "run_monitor") as run_monitor:
                    fake, _ = self.run_tasks(settings)
                run_monitor.assert_not_called()
                self.assertNotIn("last_poll_started_on", fake.db.state)
                self.assertEqual(fake.db.commits, 0)

    def test_interval_counts_from_the_last_started_poll_so_failures_are_not_retried_every_tick(self):
        settings = _settings(sync_status="", sync_error="")
        with patch.object(polling, "run_monitor") as run_monitor:
            fake, _ = self.run_tasks(settings, state={"last_poll_started_on": "2026-11-10 11:50:00"})
        run_monitor.assert_not_called()
        with patch.object(polling, "run_monitor") as run_monitor:
            fake, _ = self.run_tasks(settings, state={"last_poll_started_on": "2026-11-10 10:50:00"})
        run_monitor.assert_called_once()
        self.assertEqual(fake.db.state["last_poll_started_on"], datetime(2026, 11, 10, 12, 0))
        self.assertEqual(fake.db.commits, 1)

    def test_a_live_migration_run_makes_polling_wait(self):
        settings = _settings(sync_status="", sync_error="")
        with patch.object(polling, "run_monitor") as run_monitor, \
             patch.dict(sys.modules, {"hero_integration.services.migration": SimpleNamespace(run_lease_alive=lambda name: True)}):
            fake, _ = self.run_tasks(settings, state={"active_run": "RUN-1"})
        run_monitor.assert_not_called()
        self.assertEqual(fake.events[-1]["status"], "Skipped")
        self.assertIn("HERO run is active", fake.events[-1]["detail"])
        with patch.object(polling, "run_monitor") as run_monitor, \
             patch.dict(sys.modules, {"hero_integration.services.migration": SimpleNamespace(run_lease_alive=lambda name: False)}):
            self.run_tasks(settings, state={"active_run": "RUN-1"})
        run_monitor.assert_called_once()

    def test_out_of_range_numeric_settings_fail_closed(self):
        for field, value in (("polling_interval_minutes", 5000), ("polling_page_size", 0.5), ("polling_retry_limit", 9),
                             ("polling_rate_limit", -1), ("polling_timeout_seconds", "x")):
            with self.subTest(field=field):
                plan = runtime_plan(_settings(**{field: value}))
                self.assertIn("setting " + field, plan["unsupported_sources"])
        self.assertEqual(runtime_plan(_settings(polling_page_size=0, polling_rate_limit=None))["unsupported_sources"], [])

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
        defaults = {"timezone": None, "execution_window": None, "bootstrap": None,
                    "page_size": None, "pagination_strategy": None, "allow_backfill": None, "full_inventory": None,
                    "reconciliation": None, "retry_limit": None, "retry_backoff_seconds": None, "timeout_seconds": None,
                    "rate_limit": None, "concurrency": None, "notifications": None}
        fake = SimpleNamespace(throw=lambda message: (_ for _ in ()).throw(ValueError(message)))
        with patch.object(api, "frappe", fake):
            api._save_polling_configuration(settings, **{**defaults, **values})
        return settings

    def test_only_reviewed_reader_controls_are_saved(self):
        with self.assertRaisesRegex(ValueError, "must be OFFSET"):
            self.save(pagination_strategy="CURSOR")
        saved = self.save(page_size=50, timezone=" Europe/Berlin ")
        self.assertEqual((saved.polling_page_size, saved.polling_timezone), (50, "Europe/Berlin"))
        self.assertFalse(hasattr(saved, "polling_data_sources") or hasattr(saved, "polling_filters_json"))
