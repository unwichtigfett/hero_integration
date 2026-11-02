"""Automatic sync runs the Synchronisation page areas one by one (2026-11-02)."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from hero_integration import api, tasks

NOW = datetime(2026, 11, 2, 3, 0)


def settings(**values):
    base = dict(polling_mode="AUTO_SYNC", polling_interval_minutes=60, polling_execution_window="", polling_timezone="UTC",
                polling_pagination_strategy="OFFSET", sync_service_paused=0, hero_source_instance="default",
                sync_area_contacts=1, sync_area_projects=1, sync_area_events=1, sync_area_tasks=1)
    base.update(values)
    return SimpleNamespace(**base)


class _Frappe:
    def __init__(self, runs, active=False):
        self.runs, self.active, self.committed = runs, active, 0
        self.db = SimpleNamespace(exists=lambda *args, **kwargs: self.active, commit=self._commit)
        self.utils = SimpleNamespace(now_datetime=lambda: NOW, get_system_timezone=lambda: "UTC")
        self.local = SimpleNamespace(site="")
        self.logged = []

    def _commit(self):
        self.committed += 1

    def get_all(self, doctype, filters=None, **kwargs):
        last = self.runs.get(filters["action"])
        return [last] if last else []

    def log_error(self, title=""):
        self.logged.append(title)


class TestAutoSyncAreas(unittest.TestCase):
    def tick(self, config, runs, active=False):
        fake, started, outcomes = _Frappe(runs, active), [], []
        start = lambda **kwargs: started.append(kwargs["action"]) or {"status": "QUEUED", "run": "RUN-1"}
        services = {item: [{"service": "SVC"}] for item in ("HeroCustomers", "HeroProjectMatches", "HeroCalendarEvents", "HeroTasks")}
        with patch.object(tasks, "frappe", fake), patch.object(api, "_start_migration", side_effect=start), \
                patch.object(api, "_sync_area_read_services", return_value=services):
            tasks._auto_sync_tick(config, lambda *args: outcomes.append(args))
        return started, outcomes

    def test_the_first_due_area_is_started_and_only_one_per_tick(self):
        started, outcomes = self.tick(settings(), {})
        self.assertEqual(started, ["Customer Migration"])
        self.assertEqual(outcomes[-1][:2], ("AUTO_SYNC", "Started"))

    def test_areas_run_once_per_interval(self):
        recent = NOW - timedelta(minutes=10)
        runs = {"Customer Migration": recent, "Project Migration": recent}
        self.assertEqual(self.tick(settings(), runs)[0], ["Schedule Migration"])
        runs.update({"Schedule Migration": recent, "Projection Migration": recent})
        self.assertEqual(self.tick(settings(), runs)[0], [])
        runs["Customer Migration"] = NOW - timedelta(minutes=61)
        self.assertEqual(self.tick(settings(), runs)[0], ["Customer Migration"])

    def test_nothing_starts_while_a_run_is_active_paused_disabled_or_outside_the_window(self):
        self.assertEqual(self.tick(settings(), {}, active=True)[0], [])
        self.assertEqual(self.tick(settings(sync_service_paused=1), {})[0], [])
        self.assertEqual(self.tick(settings(sync_area_contacts=0, sync_area_projects=0, sync_area_events=0,
                                            sync_area_tasks=0), {})[0], [])
        self.assertEqual(self.tick(settings(polling_execution_window="22:00-23:00"), {})[0], [])

    def test_a_disabled_area_is_skipped(self):
        self.assertEqual(self.tick(settings(sync_area_contacts=0), {})[0], ["Project Migration"])


if __name__ == "__main__":
    unittest.main()
