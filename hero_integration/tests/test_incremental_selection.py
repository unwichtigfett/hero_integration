"""Selection series use real InnoDB locks on the explicitly disposable test site."""
import os
from threading import Barrier
from concurrent.futures import ThreadPoolExecutor
from unittest import TestCase, skipUnless
from unittest.mock import patch
from uuid import uuid4

from hero_integration.services.execution_budget import ExecutionBudget

SITE = os.environ.get('HERO_EXECUTION_BUDGET_TEST_SITE')


@skipUnless(SITE, 'requires the disposable execution-budget site')
class TestIncrementalSelection(TestCase):
    def setUp(self):
        import frappe
        self.assertEqual(frappe.local.site, SITE)
        self.assertTrue(frappe.conf.allow_tests)
        self.environment = str(uuid4())
        self.budgets = []
        self.addCleanup(self.cleanup_rows)
        self.enterContext(patch('hero_integration.services.test_environment.require_environment', return_value=self.environment))
        settings = 'HERO Integration Settings'
        self.source = frappe.db.get_single_value(settings, 'hero_source_instance') or 'default'
        self.revision = int(frappe.db.get_single_value(settings, 'source_binding_revision') or 0)
        confirmed = frappe.db.get_single_value(settings, 'source_binding_confirmed_revision')
        if int(confirmed or 0) != self.revision:
            frappe.db.set_single_value(settings, 'source_binding_confirmed_revision', self.revision)
            frappe.db.commit()
            def restore_confirmation():
                frappe.db.rollback()
                frappe.db.set_single_value(settings, 'source_binding_confirmed_revision', confirmed)
                frappe.db.commit()
            self.addCleanup(restore_confirmation)

    def cleanup_rows(self):
        import frappe
        frappe.db.rollback()
        for budget in self.budgets:
            frappe.db.delete('HERO Budget Outcome', {'budget':budget.budget_id})
            frappe.db.delete('HERO Execution Budget', {'name':budget.budget_id})
        frappe.db.delete('HERO Test Cohort Series', {'environment_id':self.environment})
        frappe.db.commit()

    def budget(self, limit, *, revision=None):
        budget = ExecutionBudget('migration', str(uuid4()), self.source, limit, 3,
                                 self.revision if revision is None else revision)
        self.budgets.append(budget)
        return budget

    def test_followup_uses_new_principals_and_existing_runs_never_widen(self):
        rows = [list(range(1, 201))]
        before = self.budget(50)
        first = before.incremental_ids('HeroEmployees','partner',rows)
        before.freeze('partner', first, evidence={'edges':[['partner',i,'User','1001'] for i in first]})
        second = self.budget(50)
        next_ids = second.incremental_ids('HeroEmployees','partner',rows)
        second.freeze('partner', next_ids, evidence={'edges':[['partner',i,'User','1001'] for i in next_ids]})
        third = self.budget(10)
        tail = third.incremental_ids('HeroEmployees','partner',rows)
        self.assertEqual([int(i) for i in first], list(range(1,51)))
        self.assertEqual([int(i) for i in next_ids], list(range(51,101)))
        self.assertEqual([int(i) for i in tail], list(range(101,111)))
        self.assertEqual(before.incremental_ids('HeroEmployees','partner', iter(())), first)
        self.assertEqual(third.incremental_ids('HeroEmployees','partner', iter(())), tail)
        with self.assertRaisesRegex(ValueError, 'revision'):
            self.budget(1, revision=self.revision + 1).incremental_ids('HeroEmployees','partner',rows)
        self.assertEqual(before.report()['dependencies'], {'User':1})
        self.assertEqual(second.report()['dependencies'], {'User':1})

    def test_employee_worker_processes_two_distinct_batches_and_a_smaller_third(self):
        import frappe
        from hero_integration.services import direct_runtime, employee_migration
        from hero_integration.services.migration_testing_selection import prepare_direct
        from hero_integration.tests.test_employee_migration import TestEmployeeResume

        fixture = TestEmployeeResume()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.fake.db.commit = frappe.db.commit
        fixture.fake.db.rollback = frappe.db.rollback
        rows = [{'id': i, 'user_id': 1000 + i} for i in range(110, 0, -1)]
        fixture.client.query = lambda *args: {'partners': rows}
        raw_read = direct_runtime.builtin_read('HeroEmployees')
        budgets = [self.budget(limit) for limit in (50, 50, 10)]
        with patch.object(employee_migration, 'upsert', return_value='added') as write:
            for budget in budgets:
                read = prepare_direct(fixture.client, raw_read, budget, 'approved-employee-reader')
                with patch.object(employee_migration, 'current_budget', return_value=budget):
                    state = employee_migration.sync_employees(fixture.client, source_instance=self.source,
                        run_name=budget.run_id, record_limit=budget.limit, read=read)
                self.assertTrue(state['done'])
                report = budget.report()
                self.assertEqual((report['selected'], report['processed'], report['failed']),
                                 (budget.limit, budget.limit, 0))
                self.assertEqual(report['dependencies'], {'User':budget.limit})
            first = budgets[0]
            replay = prepare_direct(fixture.client, raw_read, first, 'approved-employee-reader')
            with patch.object(employee_migration, 'current_budget', return_value=first):
                employee_migration.sync_employees(fixture.client, source_instance=self.source,
                    run_name=first.run_id, record_limit=first.limit, read=replay)
        self.assertEqual([c.args[0]['id'] for c in write.call_args_list], list(range(1, 111)))
        digests = {budget.selection()['digest'] for budget in budgets}
        self.assertEqual(len(digests), 3)

    def test_concurrent_runs_get_disjoint_cohorts(self):
        import frappe
        budgets = [self.budget(10), self.budget(10)]
        barrier = Barrier(2)
        def select(budget):
            frappe.init(site=SITE)
            try:
                frappe.connect()
                barrier.wait(timeout=10)
                return budget.incremental_ids('HeroProducts','supply_product', [list(range(1,31))])
            finally:
                frappe.destroy()
        with ThreadPoolExecutor(2) as pool:
            a,b = pool.map(select,budgets)
        self.assertEqual(sorted(map(int,a+b)),list(range(1,21)))
        self.assertFalse(set(a) & set(b))
