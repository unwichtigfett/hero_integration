"""Bounded execution uses a durable, site-local reservation outside business transactions."""
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import unittest
from uuid import uuid4
from unittest.mock import Mock, patch

from hero_integration.services.execution_budget import BudgetReached, ExecutionBudget

SITE = os.environ.get('HERO_EXECUTION_BUDGET_TEST_SITE')


@unittest.skipUnless(SITE, 'requires an explicitly selected disposable MariaDB site')
class TestExecutionBudgetMariaDB(unittest.TestCase):
    def setUp(self):
        import frappe
        self.assertEqual(frappe.local.site, SITE)
        self.budget = ExecutionBudget('migration', 'budget-test-' + uuid4().hex, 'synthetic')

    def tearDown(self):
        import frappe
        frappe.db.rollback()
        frappe.db.delete('HERO Execution Budget', {'name': self.budget.budget_id})
        frappe.db.commit()

    def test_concurrent_50th_identity_and_relationship_retry(self):
        import frappe
        self.budget.reserve_many(('project', i) for i in range(1, 50))
        barrier = Barrier(2)
        def attempt(kind):
            frappe.init(site=SITE)
            try:
                frappe.connect()
                barrier.wait(timeout=10)
                try:
                    self.budget.reserve(kind, 50)
                except BudgetReached:
                    return None
                return kind
            finally:
                frappe.destroy()
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(attempt, ['partner', 'project']))
        self.assertEqual(results.count(None), 1)
        self.assertEqual(self.budget.used(), 50)
        winner = next(kind for kind in results if kind)
        self.assertFalse(self.budget.reserve(winner, '50'))
        with self.assertRaises(BudgetReached):
            self.budget.reserve_many([('project', '1'), ('User', '51')])
        self.assertEqual(self.budget.used(), 50)

    def test_outer_rollback_does_not_lose_reservation_or_commit_business_writes(self):
        import frappe
        marker = self.budget.run_id
        frappe.db.sql('INSERT INTO `tabSingles` (doctype, field, value) VALUES (%s,%s,%s)',
                      (marker, 'uncommitted_marker', '1'))
        self.budget.reserve_many([('partner', 7), ('User', 22)])
        self.assertEqual(self.budget.used(), 2)
        frappe.db.rollback()
        self.assertFalse(frappe.db.sql('SELECT value FROM `tabSingles` WHERE doctype=%s', marker))
        self.assertFalse(self.budget.reserve_many([('partner', '7'), ('User', 22)]))
        with self.assertRaisesRegex(ValueError, 'another run contract'):
            ExecutionBudget('migration', marker, 'other-source').reserve('User', 3)
        with self.assertRaises(BudgetReached):
            self.budget.reserve_many(('project', i) for i in range(49))
        self.assertEqual(self.budget.used(), 2)

    def test_employee_outside_first_sample_cannot_exceed_the_root_budget(self):
        from hero_integration.services import employee_migration
        from hero_integration.tests.test_employee_migration import TestEmployeeResume
        fixture = TestEmployeeResume()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.client.query = Mock(return_value={'partners': [
            {'id': i, 'user_id': 100+i} for i in range(1, 7)]})
        self.budget.reserve_many(('project', i) for i in range(47))
        with patch.object(employee_migration, 'current_budget', return_value=self.budget), \
                patch.object(employee_migration, 'upsert', return_value='added') as write:
            with self.assertRaises(BudgetReached):
                fixture.run_import(record_limit=1, cohort_user_ids=['106'], cohort_partner_ids=[])
        self.assertEqual([call.args[0]['id'] for call in write.call_args_list], [1])
        self.assertEqual(self.budget.used(), 49)

    def test_file_admission_precedes_detail_blob_and_native_storage(self):
        from hero_integration.services import file_transfer
        from hero_integration.tests.test_file_transfer import TestSourceAndTargetBinding
        fixture = TestSourceAndTargetBinding()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.budget.reserve_many(('project', i) for i in range(50))
        downloader = Mock()
        with patch('hero_integration.services.execution_budget.current_budget', return_value=self.budget), \
                patch.object(file_transfer, 'details') as detail, patch.object(file_transfer, 'commit_file') as write:
            with self.assertRaises(BudgetReached):
                fixture.run_transfer([{'id': 72, 'uuid':'f72','filename':'offer.pdf', 'category':'offer'}],
                                     {}, downloader=downloader)
        detail.assert_not_called()
        downloader.fetch.assert_not_called()
        write.assert_not_called()
        fixture.observations.assert_not_called()
        self.assertEqual(self.budget.used(), 50)

    def test_project_relation_replay_cannot_create_a_51st_identity(self):
        from hero_integration.tests.test_mapped_relations import ADDRESS, TestNativeRelations
        fixture = TestNativeRelations()
        fixture.setUp()
        self.budget.reserve_many(('project', i) for i in range(50))
        with patch('hero_integration.services.execution_budget.current_budget', return_value=self.budget):
            with self.assertRaises(BudgetReached):
                fixture.sync(ADDRESS, fixture.address_record)
        self.assertFalse(fixture.site.inserted)
        self.assertEqual(fixture.docs('Address'), [])
        self.assertIsNone(fixture.site.docs[('Project', 'P1')].custom_hero_project_address)
