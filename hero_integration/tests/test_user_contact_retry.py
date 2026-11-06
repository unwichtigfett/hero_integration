"""Retries preserve the original failure and apply only to the known Frappe job."""
from types import SimpleNamespace
import os
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe
from frappe.tests import IntegrationTestCase

from hero_integration.services import user_contact_retry as retry


class TestUserContactRetry(TestCase):
    def test_deadlock_schedules_one_deduplicated_contact_retry(self):
        fake = SimpleNamespace(QueryDeadlockError=frappe.QueryDeadlockError,
                               local=SimpleNamespace(site='site-a'), enqueue=Mock())
        with patch.object(retry, 'frappe', fake), \
             patch.object(retry.sys, 'exc_info', return_value=(None, frappe.QueryDeadlockError('retry'), None)):
            retry.after_job(retry.CORE_JOB, {'user': {'name': 'synthetic@example.invalid'}})
        self.assertEqual(fake.enqueue.call_count, 1)
        self.assertEqual(fake.enqueue.call_args.args[0], retry.RETRY_JOB)
        self.assertTrue(fake.enqueue.call_args.kwargs['deduplicate'])
        self.assertNotIn('synthetic@example.invalid', fake.enqueue.call_args.kwargs['job_id'])

    def test_other_jobs_and_other_errors_do_not_schedule_retry(self):
        fake = SimpleNamespace(QueryDeadlockError=frappe.QueryDeadlockError, enqueue=Mock())
        with patch.object(retry, 'frappe', fake), \
             patch.object(retry.sys, 'exc_info', return_value=(None, ValueError('other'), None)):
            retry.after_job(retry.CORE_JOB, {'user': {'name': 'synthetic@example.invalid'}})
            retry.after_job('another.job', {'user': {'name': 'synthetic@example.invalid'}})
        fake.enqueue.assert_not_called()

    def test_retry_converts_deadlock_to_frappe_bounded_retry(self):
        fake = SimpleNamespace(QueryDeadlockError=frappe.QueryDeadlockError,
            RetryBackgroundJobError=frappe.RetryBackgroundJobError,
            db=SimpleNamespace(exists=lambda *args: True), get_doc=lambda *args: object())
        with patch.object(retry, 'frappe', fake), \
             patch('frappe.core.doctype.user.user.create_contact', side_effect=frappe.QueryDeadlockError('retry')):
            with self.assertRaises(frappe.RetryBackgroundJobError):
                retry.retry_contact('synthetic@example.invalid')


class TestUserContactRetryFrappe(IntegrationTestCase):
    def test_framework_failure_hook_enqueues_recovery_without_masking_error(self):
        if not os.environ.get('HERO_TEST_PRIVATE_SITE') or os.environ['HERO_TEST_PRIVATE_SITE'] != frappe.local.site:
            self.skipTest('Requires explicitly selected synthetic Frappe site')
        from frappe.utils.background_jobs import execute_job
        frappe.clear_cache()
        with patch('frappe.core.doctype.user.user.create_contact', side_effect=frappe.QueryDeadlockError('synthetic')), \
             patch.object(frappe, 'enqueue') as enqueue:
            with self.assertRaises(frappe.QueryDeadlockError):
                execute_job(frappe.local.site, retry.CORE_JOB, None, 'synthetic-contact-test',
                            {'user': {'name': 'synthetic@example.invalid'}, 'ignore_mandatory': True}, is_async=False)
        self.assertEqual(enqueue.call_count, 1)
        self.assertEqual(enqueue.call_args.args[0], retry.RETRY_JOB)
        self.assertTrue(enqueue.call_args.kwargs['ignore_mandatory'])
