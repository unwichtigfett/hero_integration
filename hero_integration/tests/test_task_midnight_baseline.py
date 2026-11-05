"""Legacy day acknowledgements remain valid for native midnight storage only."""
from datetime import datetime, timezone
from unittest import TestCase
from hero_integration.services.change_detection import field_hashes
from hero_integration.services.migration import _Comparison, _task_midnight_baseline

FIELD = 'exp_start_date'


class TestTaskMidnightBaseline(TestCase):
    def outcome(self, current, *, source='2026-01-01 12:00:00', old_source='2026-01-01'):
        previous = field_hashes({FIELD: '2026-01-01'})
        source_base = field_hashes({FIELD: old_source})
        target = {FIELD: current}
        normalized = _task_midnight_baseline(source_base, previous, target)
        self.assertEqual(previous, field_hashes({FIELD: '2026-01-01'}))
        return _Comparison({FIELD: source}).reconcile(source_base, normalized, target)

    def test_remote_datetime_change_applies_to_unchanged_proven_legacy_day(self):
        outcome = self.outcome(datetime(2026, 1, 1))
        self.assertEqual(outcome.apply, {FIELD})
        self.assertFalse(outcome.conflicts)

    def test_identical_midnight_advances_once_to_the_current_datetime_contract(self):
        outcome = self.outcome(datetime(2026, 1, 1), source='2026-01-01 00:00:00')
        self.assertEqual(outcome.apply, {FIELD})
        self.assertFalse(outcome.conflicts)

    def test_real_local_day_or_time_and_unproven_source_remain_conflicts(self):
        for current in (datetime(2026, 1, 2), datetime(2026, 1, 1, 9), datetime(2026, 1, 1, tzinfo=timezone.utc)):
            with self.subTest(current=current):
                self.assertEqual(self.outcome(current).conflicts, {FIELD})
        self.assertEqual(self.outcome(datetime(2026, 1, 1), old_source='2025-12-30').conflicts, {FIELD})
