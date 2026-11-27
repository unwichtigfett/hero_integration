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
        self.assertTrue(frappe.conf.allow_tests)
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

    def test_changed_employee_resume_uses_original_partner_and_user_slots(self):
        from hero_integration.services import employee_migration as employees
        from hero_integration.tests.test_employee_migration import TestEmployeeResume

        fixture = TestEmployeeResume()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        rows = [{"id": 1, "user_id": 101, "first_name": "Ada"}, {"id": 2, "user_id": 102}]
        fixture.client.query = lambda *args: {"partners": rows}
        with patch.object(employees, "current_budget", return_value=self.budget):
            with patch.object(employees, "upsert", side_effect=["added", employees.EmployeeMigrationBlocked("Review")]):
                first = fixture.run_import()
            self.assertFalse(first["done"])
            self.assertEqual(self.budget.used(), 4)
            rows[0]["first_name"] = "Ida"
            with patch.object(employees, "upsert", side_effect=["updated", "added"]) as write:
                final = fixture.run_import()
            self.assertEqual([c.args[0]["id"] for c in write.call_args_list], [1, 2])
            self.assertTrue(final["done"])
            self.assertEqual(final["counts"]["unique_processed"], 2)
            self.assertEqual(self.budget.used(), 4)

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
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        self.budget.reserve_many(('project', i) for i in range(50))
        with patch('hero_integration.services.execution_budget.current_budget', return_value=self.budget):
            with self.assertRaises(BudgetReached):
                fixture.sync(ADDRESS, fixture.address_record)
        self.assertFalse(fixture.site.inserted)
        self.assertEqual(fixture.docs('Address'), [])
        self.assertIsNone(fixture.site.docs[('Project', 'P1')].custom_hero_project_address)

    def test_configured_cohort_limits_and_independent_relationships(self):
        import frappe
        for limit in (1, 10, 50, 100):
            with self.subTest(limit=limit):
                budget = ExecutionBudget('migration', uuid4().hex, 'synthetic', limit, version=2)
                self.addCleanup(self.remove_contract, budget)
                ids = list(range(1, limit + 1))
                budget.freeze('partner', [*ids, ids[0]], evidence={'revision': 4})
                for identity in ids:
                    budget.reserve_record('partner', identity, [('User', identity + 1000), ('address', 'shared')])
                    budget.outcome('partner', identity, 'processed')
                frappe.db.commit()
                with self.assertRaises(BudgetReached):
                    budget.reserve_record('partner', limit + 1)
                report = budget.report()
                self.assertEqual((report['selected'], report['processed'], report['failed'], report['limit']),
                                 (limit, limit, 0, limit))
                self.assertEqual(report['dependencies'], {'User': limit, 'address': 1})
                retry = ExecutionBudget('migration', budget.run_id, 'synthetic', limit, version=2)
                self.assertEqual(retry.freeze('partner', reversed(ids), evidence={'revision': 4})['digest'],
                                 report['cohort_digest'])
                self.assertFalse(retry.reserve_record('partner', 1, [('address', 'shared'), ('User', 1001)]))
                with self.assertRaisesRegex(ValueError, 'dependency set changed'):
                    retry.reserve_record('partner', 1, [('User', 2001)])
                with self.assertRaises(ValueError):
                    retry.freeze('partner', ['replacement'])
                with self.assertRaises(ValueError):
                    ExecutionBudget('migration', budget.run_id, 'synthetic', 2, version=2).selection()

    @staticmethod
    def remove_contract(budget):
        import frappe
        frappe.db.rollback()
        frappe.db.delete('HERO Budget Outcome', {'budget':budget.budget_id})
        frappe.db.delete('HERO Execution Budget', {'name':budget.budget_id})
        frappe.db.commit()

    def test_shared_project_children_serialize_edges_and_atomic_outcomes(self):
        import frappe
        budget = ExecutionBudget('migration', uuid4().hex, 'synthetic', 1, version=2)
        self.addCleanup(self.remove_contract, budget)
        budget.freeze('project', ['last'], evidence={
            'members': {'Task': ['a','b']},
            'edges': [['project','last','Task','a'], ['project','last','Task','b']]})
        barrier = Barrier(2)
        def child(identity):
            frappe.init(site=SITE)
            try:
                frappe.connect()
                worker = ExecutionBudget('migration', budget.run_id, 'synthetic', 1, version=2)
                barrier.wait(timeout=10)
                worker.reserve_record('Task', identity, [('User','shared')])
                worker.outcome('Task', identity, 'processed')
                frappe.db.commit()
            finally:
                frappe.destroy()
        with ThreadPoolExecutor(max_workers=2) as workers:
            list(workers.map(child, ['a','b']))
        self.assertEqual(budget.used(), 0)
        budget.reserve_record('project', 'last')
        budget.outcome('project', 'last', 'processed')
        frappe.db.rollback()
        self.assertEqual(budget.report()['processed'], 0)
        budget.reserve_record('project', 'last')
        budget.outcome('project', 'last', 'processed')
        frappe.db.commit()
        budget.outcome('project', 'last', 'missing', phase='relationships')
        self.assertEqual(budget.report()['processed'], 0)
        budget.outcome('project', 'last', 'processed', phase='relationships')
        report = budget.report()
        self.assertEqual((report['processed'],report['failed'],report['missing']), (1,0,0))
        self.assertEqual(report['dependencies'], {'Task':2,'User':1})
        with self.assertRaises(BudgetReached):
            budget.reserve_record('Task','unrelated')
        with self.assertRaises(BudgetReached):
            budget.reserve_record('project','next')

    def test_persisted_budget_cannot_be_reinterpreted_and_setting_changes_are_ignored(self):
        from types import SimpleNamespace
        from hero_integration.services.execution_budget import for_run
        self.budget.reserve('partner', 1)
        newer = ExecutionBudget('migration', self.budget.run_id, 'synthetic', self.budget.limit, version=2)
        with self.assertRaisesRegex(ValueError, 'another run contract'):
            newer.freeze('partner', [1])
        root = SimpleNamespace(name=self.budget.run_id, execution_budget_run=self.budget.run_id,
            source_instance='synthetic', source_binding_revision=3, testing_enabled=1,
            testing_max_actions=100, action='Full Migration')
        self.assertEqual((for_run(root).limit, for_run(root).version), (50,1))
        root.budget_contract_version = 2
        with patch('frappe.get_single', side_effect=AssertionError('must use the recorded run contract')):
            self.assertEqual((for_run(root).limit, for_run(root).version), (100,2))
        child = SimpleNamespace(**{**vars(root), 'name':'child', 'action':'Customer Migration'})
        with patch('frappe.get_doc', return_value=root), patch(
                'hero_integration.services.file_migration._parent_run',return_value='another-root'):
            with self.assertRaisesRegex(ValueError, 'no recorded link'):
                for_run(child)

    def test_standalone_finalization_marks_missing_relation_and_rechecks_after_resume(self):
        import frappe
        from types import SimpleNamespace
        from hero_integration.services import deferred_references as refs
        budget = ExecutionBudget('migration',uuid4().hex,'synthetic',1,version=2)
        self.addCleanup(self.remove_contract, budget)
        budget.freeze('Task',['t1'])
        budget.reserve_record('Task','t1',[('project','missing')])
        budget.outcome('Task','t1','processed')
        frappe.db.commit()
        run = SimpleNamespace(name=budget.run_id, execution_budget_run=budget.run_id, budget_contract_version=2,
                              action='Projection Migration', mode='Execute',source_instance='synthetic')
        pending = SimpleNamespace(name='ref', operation='HeroTasks',source_id='t1',status='Waiting')
        real_get_all, real_get_value = frappe.get_all, frappe.db.get_value
        def rows(doctype, **params):
            return [pending] if doctype == refs.MAIN else real_get_all(doctype, **params)
        def get_value(doctype, *args, **kwargs):
            return pending.status if doctype == refs.MAIN else real_get_value(doctype,*args,**kwargs)
        with patch.object(refs.execution_budget,'for_run',return_value=budget), patch.object(frappe,'get_all',rows), \
                patch.object(frappe.db,'get_value',get_value), patch.object(refs,'resolve_one'):
            self.assertTrue(refs.finish_standalone_cohort(run))
            self.assertEqual((budget.report()['processed'],budget.report()['missing']), (0,1))
            pending.status = 'Resolved'
            self.assertFalse(refs.finish_standalone_cohort(run))
            self.assertEqual((budget.report()['processed'],budget.report()['missing']), (1,0))
