from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

from hero_integration.services.polling import PaginationProgressError, effective_mode, monitor, runtime_plan, scheduler_due
from hero_integration.sources.hero import SourcePage


class _Source:
    source_instance = "synthetic"

    @staticmethod
    def customers(offset):
        return SourcePage(({"id": 1}, {"id": 2}) if offset == 0 else ({"id": 3},), 2 if offset == 0 else None)

    @staticmethod
    def projects(offset):
        return SourcePage(({"id": 4},), None)


class TestPollingContract(unittest.TestCase):
    def test_fresh_mode_is_off_and_legacy_boolean_is_explicitly_compatible(self):
        self.assertEqual(effective_mode(SimpleNamespace(polling_mode="OFF", scheduled_sync_enabled=1)), "OFF")
        self.assertEqual(effective_mode(SimpleNamespace(polling_mode="", scheduled_sync_enabled=0)), "OFF")
        self.assertEqual(effective_mode(SimpleNamespace(polling_mode="", scheduled_sync_enabled=1)), "AUTO_SYNC")

    def test_monitor_reads_pages_without_claiming_watermarks_or_business_writes(self):
        result = monitor(_Source())
        self.assertEqual((result.customer_records, result.project_records, result.source_instance), (3, 1, "synthetic"))

    def test_runtime_plan_labels_unimplemented_schema_operations_without_enabling_auto_sync(self):
        settings = SimpleNamespace(polling_mode="AUTO_SYNC", scheduled_sync_enabled=0, polling_data_sources="HeroCustomers\nFutureQuery")
        plan = runtime_plan(settings)
        self.assertEqual(plan["unsupported_sources"], ["FutureQuery"])
        self.assertFalse(plan["can_auto_sync"])
        self.assertIn("does not claim", plan["watermarks"])

    def test_scheduler_window_and_interval_are_side_effect_free_gates(self):
        settings = SimpleNamespace(polling_mode="MONITOR", scheduled_sync_enabled=0, polling_execution_window="09:00-17:00", polling_interval_minutes=60)
        now = datetime(2026, 10, 21, 10, 0)
        self.assertTrue(scheduler_due(settings, now, None))
        self.assertFalse(scheduler_due(settings, now, now - timedelta(minutes=30)))
        self.assertFalse(scheduler_due(settings, datetime(2026, 10, 21, 18, 0), None))

    def test_monitor_stops_at_the_persisted_inventory_bound_and_records_resume_offset(self):
        plan = runtime_plan(SimpleNamespace(
            polling_mode="MONITOR", scheduled_sync_enabled=0, polling_data_sources="HeroCustomers",
            polling_full_inventory=False, polling_rate_limit=10000,
        ))
        result = monitor(_Source(), plan)

        self.assertEqual(result.customer_records, 2)
        self.assertEqual(result.incomplete_sources, ("HeroCustomers",))
        self.assertEqual(result.customer_next_offset, 2)

    def test_repeated_or_non_advancing_offset_stops_before_an_unbounded_loop(self):
        class RepeatingSource:
            source_instance = "synthetic"

            @staticmethod
            def customers(offset):
                return SourcePage(({"id": 1},), offset)

        with self.assertRaises(PaginationProgressError):
            monitor(RepeatingSource(), {"sources": ["HeroCustomers"], "max_pages": 3, "rate_limit": 10000})

    def test_runtime_plan_exposes_the_effective_reader_controls(self):
        settings = SimpleNamespace(
            polling_mode="MONITOR", scheduled_sync_enabled=0, polling_data_sources="HeroCustomers",
            polling_filters_json='{"HeroCustomers":{"showDeleted":false}}', polling_timezone="Europe/Berlin",
            polling_page_size=25, polling_pagination_strategy="OFFSET", polling_bootstrap=True,
            polling_allow_backfill=True, polling_full_inventory=True, polling_reconciliation=True,
            polling_retry_limit=2, polling_retry_backoff_seconds=15, polling_timeout_seconds=45,
            polling_rate_limit=20, polling_concurrency=1, polling_notifications=True,
        )

        plan = runtime_plan(settings)

        self.assertEqual(plan["filters"], {"HeroCustomers": {"showDeleted": False}})
        self.assertEqual((plan["page_size"], plan["max_pages"], plan["timeout_seconds"]), (25, 100, 45))
        self.assertTrue(all(plan[key] for key in ("bootstrap", "allow_backfill", "reconciliation", "notifications")))
