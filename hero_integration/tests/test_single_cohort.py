"""Execution sees only distinct records from the frozen standalone sample."""
import os
from uuid import uuid4
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from hero_integration.services import direct_runtime
from hero_integration.services.migration_testing_selection import prepare_direct, prepare_native
from hero_integration.sources.hero import SourcePage


class Budget:
    version = 2
    def __init__(self, limit):
        self.limit = limit
        self.frozen = None

    def selection(self):
        return self.frozen

    def freeze(self, main_type, ids, *, evidence):
        candidate = dict(main_type=main_type, ids=sorted(ids), evidence=evidence)
        if self.frozen is not None and self.frozen != candidate:
            raise ValueError('changed cohort')
        self.frozen = candidate

    def outcome(self, kind, identity, status):
        pass


class StandaloneCohort(unittest.TestCase):
    def test_source_faces_of_all_nine_areas_exclude_primary_after_configured_maximum(self):
        from hero_integration.services.migration_testing_selection import DIRECT_KINDS
        from hero_integration.services.sync_page import BY_OPERATION
        for limit in (1, 10, 50, 100):
            for operation in (*DIRECT_KINDS, *BY_OPERATION):
                with self.subTest(operation=operation, limit=limit):
                    budget = Budget(limit)
                    rows = [dict(id=i, product_id=str(i), category='images' if operation == 'HeroPictures' else 'document',
                                 customer_id=1100) for i in range(1, limit + 2)]
                    if operation in DIRECT_KINDS:
                        read = direct_runtime.builtin_read(operation)
                        with patch.object(direct_runtime, 'fetch_page', return_value=rows):
                            selected = prepare_direct(None, read, budget, 'frozen-checkpoint')
                        visible = selected.testing_rows
                    else:
                        plan = SimpleNamespace(source_identity=lambda r: str(r['id']), definitions=[
                            SimpleNamespace(transformation='HERO Customer Link',purpose='Customer Relation',source_path='customer_id')])
                        page = SourcePage(tuple(rows), None)
                        customers = SourcePage(({'id': 1100},), None) if operation == 'HeroProjectMatches' else page
                        source = SimpleNamespace(contract=SimpleNamespace(target_operation=operation),
                            customers=lambda off: customers, projects=lambda off: page, projection=lambda off: page)
                        selected = prepare_native(source,budget,'frozen-checkpoint',plan,plan)
                        reader = {'HeroCustomers':selected.customers,'HeroProjectMatches':selected.projects}.get(
                            operation,selected.projection)
                        visible = reader().records
                    self.assertEqual([r['id'] for r in visible], list(range(1,limit+1)))

    def test_single_type_limit_applies_after_file_category_and_duplicates(self):
        for maximum in (1, 10, 50, 100):
            with self.subTest(maximum=maximum):
                budget = Budget(maximum)
                read = direct_runtime.builtin_read('HeroDocuments')
                rows = [dict(id=1001, category='images'), dict(id=1, category='offer')] + [
                    dict(id=i, category='offer') for i in range(1, maximum + 2)]
                with patch.object(direct_runtime, 'fetch_page', return_value=rows):
                    selected = prepare_direct(None, read, budget, 'contract')
                page = direct_runtime.fetch_page(None, selected, 0)
                self.assertEqual([row['id'] for row in page], list(range(1, maximum + 1)))
                self.assertEqual(len(budget.selection()['ids']), maximum)

    def test_resume_keeps_original_ids_and_order_when_first_page_changes(self):
        budget = Budget(2)
        read = direct_runtime.builtin_read('HeroEmployees')
        for rows in ([{'id': 2}, {'id': 3}, {'id': 4}], [{'id': 1}, {'id': 3}, {'id': 2}]):
            with patch.object(direct_runtime, 'fetch_page', return_value=rows):
                selected = prepare_direct(None, read, budget, 'contract')
            self.assertEqual([row['id'] for row in direct_runtime.fetch_page(None, selected, 0)], [2, 3])

    def test_project_customer_phase_cannot_process_unrelated_contacts(self):
        budget = Budget(2)
        project = SimpleNamespace(source_identity=lambda row: str(row['id']), definitions=[
            SimpleNamespace(source_path='customer', transformation='HERO Customer Link', purpose='Customer Relation')])
        customer = SimpleNamespace(source_identity=lambda row: str(row['id']))
        source = SimpleNamespace(contract=SimpleNamespace(target_operation='HeroProjectMatches'),
            projects=Mock(return_value=SourcePage(tuple(dict(id=i, customer=c) for i,c in [(1,7),(2,7),(3,8)]), None)),
            customers=Mock(return_value=SourcePage(tuple(dict(id=i) for i in (6,7,8)),None)))
        result = prepare_native(source, budget, 'contract', project, customer)
        self.assertEqual([r['id'] for r in result.customers().records], [7])
        self.assertEqual([r['id'] for r in result.projects().records], [1,2])
        self.assertEqual(budget.selection()['evidence']['edges'], [
            ['project','1','customer','7'], ['project','2','customer','7']])

    def test_missing_selected_row_is_reported_and_never_replaced(self):
        budget = Budget(2)
        read = direct_runtime.builtin_read('HeroEmployees')
        with patch.object(direct_runtime, 'fetch_page', return_value=[{'id': 1}, {'id': 2}]):
            prepare_direct(None, read, budget, 'contract')
        with patch.object(direct_runtime, 'fetch_page', return_value=[{'id': 2}, {'id': 3}]), \
                patch('frappe.db', Mock()), patch.object(budget, 'outcome') as outcome:
            with self.assertRaisesRegex(ValueError, 'missing.*1'):
                prepare_direct(None, read, budget, 'contract')
        outcome.assert_called_once_with('partner', '1', 'missing')
        self.assertEqual(budget.selection()['ids'], ['1', '2'])

    def test_picture_and_document_membership_requires_the_file_category(self):
        for operation in ('HeroDocuments', 'HeroPictures'):
            budget = Budget(1)
            with patch.object(direct_runtime, 'fetch_page', return_value=[{'id': 1}]), \
                    self.assertRaisesRegex(ValueError, 'category'):
                prepare_direct(None, direct_runtime.builtin_read(operation), budget, 'contract')
            self.assertIsNone(budget.selection())

    def test_conflicting_duplicate_in_the_selected_page_blocks_before_freezing(self):
        budget = Budget(2)
        read = direct_runtime.builtin_read('HeroEmployees')
        rows = [{'id': 1, 'user_id': 2}, {'id': 1, 'user_id': 3}, {'id': 2}]
        with patch.object(direct_runtime, 'fetch_page', return_value=rows), self.assertRaisesRegex(ValueError, 'Conflicting'):
            prepare_direct(None, read, budget, 'contract')
        self.assertIsNone(budget.selection())


    def test_interrupted_file_page_reaches_the_last_frozen_identity_on_resume(self):
        from dataclasses import replace
        from hero_integration.services import file_transfer
        from hero_integration.tests.test_file_transfer import TestSourceAndTargetBinding

        fixture = TestSourceAndTargetBinding()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        rows = tuple(dict(id=i, uuid=str(i), category='offer', filename='offer.pdf') for i in (101, 102))
        read = replace(direct_runtime.builtin_read('HeroDocuments'), testing_rows=rows)
        state = dict(source_instance='source-a', mapping=read.mapping_version, offset=0,
                     counts={'skipped_deleted': 1}, testing_record_limit=2)
        budget = Mock(version=2)
        with patch.object(direct_runtime, 'run_progress', return_value=(state, Mock())), \
                patch('hero_integration.services.execution_budget.current_budget', return_value=budget), \
                patch.object(file_transfer, 'details', side_effect=lambda _, batch, *args, **kwargs: {
                    r['uuid']: {'is_deleted': True} for r in batch}), \
                patch('hero_integration.services.deferred_references.installed', return_value=False):
            state = file_transfer.transfer('documents', source_instance='source-a', run_name='fixture',
                                           read=read, record_limit=2, client=Mock(), downloader=Mock())
        self.assertTrue(state['done'])
        self.assertEqual([c.args[1] for c in budget.reserve_record.call_args_list], [101, 102])

    def test_project_history_is_exhausted_only_after_freeze_and_for_selected_parents(self):
        budget = Budget(2)
        plan = SimpleNamespace(source_identity=lambda row: str(row['id']), definitions=[
            SimpleNamespace(source_path='customer', transformation='HERO Customer Link', purpose='Customer Relation')])
        source = SimpleNamespace(contract=SimpleNamespace(target_operation='HeroProjectMatches'),
                                 query_adapter={'logbook': {'offset_variable': 'child_offset'}})
        expansions = []
        def read(offset):
            identity = offset + 1
            row = dict(id=identity, customer=7)
            if str(identity) in source.logbook_project_ids:
                self.assertIsNotNone(budget.selection())
                expansions.append(identity)
                row['_hero_logbook_complete'] = True
            return SourcePage((row,), offset + 1)
        source.projects = read
        source.customers = lambda offset: SourcePage(({'id': 7},), None)
        selected = prepare_native(source, budget, 'contract', plan, plan)
        self.assertEqual(expansions, [1,2])
        self.assertTrue(all(row['_hero_logbook_complete'] for row in selected.projects().records))


@unittest.skipUnless(os.environ.get('HERO_EXECUTION_BUDGET_TEST_SITE'), 'requires the disposable execution-budget database')
class TestEmployeeCohortMariaDB(unittest.TestCase):
    def test_configured_employee_cohort_and_its_users_survive_retry(self):
        import frappe
        from hero_integration.services import employee_migration as employees
        from hero_integration.services.execution_budget import ExecutionBudget
        from hero_integration.tests.test_employee_migration import TestEmployeeResume
        self.assertEqual(frappe.local.site, os.environ['HERO_EXECUTION_BUDGET_TEST_SITE'])
        self.assertTrue(frappe.conf.allow_tests)
        for limit in (1, 10, 50, 100):
            with self.subTest(limit=limit):
                fixture = TestEmployeeResume()
                fixture.setUp()
                budget = ExecutionBudget('migration', 'single-test-' + uuid4().hex, 'synthetic', limit, 2)
                rows = [{'id': i, 'user_id': 1000 + i} for i in range(1, limit + 2)]
                fixture.client.query = Mock(return_value={'partners': rows})
                # Use the real outcome owner on the same transaction as the
                # worker's checkpoint. Native target writes use synthetic I/O.
                fixture.fake.db.commit = frappe.db.commit
                fixture.fake.db.rollback = frappe.db.rollback
                try:
                    read = direct_runtime.builtin_read('HeroEmployees')
                    selected = prepare_direct(fixture.client, read, budget, 'synthetic-contract')
                    with patch.object(employees, 'current_budget', return_value=budget), \
                            patch.object(employees, 'upsert', return_value='added') as write:
                        state = fixture.run_import(record_limit=limit, read=selected)
                        self.assertTrue(state['done'])
                        self.assertEqual([call.args[0]['id'] for call in write.call_args_list],
                                         list(range(1, limit + 1)))
                        original = budget.report()
                        self.assertEqual((original['limit'],original['selected'],original['processed'],original['failed']),
                                         (limit, limit, limit, 0))
                        self.assertEqual(original['dependencies'], {'User': limit})
                        self.assertEqual(original['conflicts'], 0)
                        self.assertEqual(original['missing'], 0)
                        fixture.client.query.return_value = {'partners': [*reversed(rows)]}
                        retry = prepare_direct(fixture.client, read, budget, 'synthetic-contract')
                        fixture.run_import(record_limit=limit, read=retry)
                        self.assertEqual(write.call_count, limit)
                        self.assertEqual(budget.report(), original)
                finally:
                    frappe.db.rollback()
                    frappe.db.delete('HERO Budget Outcome', {'budget': budget.budget_id})
                    frappe.db.delete('HERO Execution Budget', {'name': budget.budget_id})
                    frappe.db.commit()
                    fixture.doCleanups()
