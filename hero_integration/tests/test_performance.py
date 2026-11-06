"""Aggregate phase timings are opt-in, scoped and independent of business data."""
from unittest import TestCase
from unittest.mock import Mock, patch

from hero_integration.services import performance as perf


class TestPerformance(TestCase):
    def test_disabled_measurement_does_not_read_clock(self):
        with patch.object(perf.time, 'perf_counter', side_effect=AssertionError('disabled')):
            with perf.measure('phase'):
                pass

    def test_nested_collectors_aggregate_calls_errors_and_reset_after_failure(self):
        with perf.collect() as stats:
            with patch.object(perf.time, 'perf_counter', side_effect=[1, 1.002, 2, 2.003]):
                with perf.measure('phase'):
                    pass
                with self.assertRaises(ValueError), perf.collect() as nested, perf.measure('phase'):
                    self.assertIs(nested, stats)
                    raise ValueError('private request detail')
        self.assertEqual(stats['phase']['calls'], 2)
        self.assertEqual(stats['phase']['errors'], 1)
        self.assertAlmostEqual(stats['phase']['total_ms'], 5)
        self.assertIsNone(perf._active.get())
        self.assertNotIn('private', str(stats))

    def test_profile_logs_once_and_never_logs_arguments_or_error_text(self):
        logger = Mock()
        @perf.profiled('outer')
        def outer(secret):
            return inner(secret)
        @perf.profiled('inner')
        def inner(secret):
            raise ValueError(secret)
        with patch.object(perf, '_enabled', return_value=True), \
                patch('frappe.logger', return_value=logger), self.assertRaises(ValueError):
            outer('private-token-and-record')
        logger.info.assert_called_once()
        text = logger.info.call_args.args[0]
        self.assertNotIn('private', text)
        self.assertIn('outer', text)
        self.assertIn('inner', text)
        self.assertIsNone(perf._active.get())

    def test_logging_failure_cannot_change_business_result(self):
        @perf.profiled('operation')
        def operation():
            return 42
        with patch.object(perf, '_enabled', return_value=True), \
                patch('frappe.logger', side_effect=RuntimeError('unavailable')):
            self.assertEqual(operation(), 42)
