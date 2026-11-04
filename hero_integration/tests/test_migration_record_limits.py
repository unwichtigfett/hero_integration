"""Per-area test imports: bounded records, resumable slices and isolated cursors."""
import copy
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from hero_integration.services import employee_migration as employees
from hero_integration.services import migration_testing as testing
from hero_integration.services import product_migration as products
from hero_integration.services import service_migration as services
from hero_integration.sources.hero import SourcePage
from hero_integration.tests import test_employee_migration as employee_fixtures
from hero_integration.tests import test_service_migration as service_fixtures


class TestMigrationRecordLimits(unittest.TestCase):
    def employee_fixture(self):
        fixture = employee_fixtures.TestEmployeeResume()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.client.query = Mock(return_value={'partners': [{'id': i} for i in range(1, 27)]})
        return fixture

    def test_customers_and_projects_each_receive_five_records(self):
        page = SourcePage(tuple({'id': i} for i in range(26)), 26)
        source = SimpleNamespace(customers=Mock(return_value=page), projects=Mock(return_value=page))
        bounded = testing.RecordLimitedSource(source, 5)
        self.assertEqual(len(bounded.customers().records), 5)
        self.assertEqual(len(bounded.projects().records), 5)
        self.assertEqual(bounded.customers(26), SourcePage((), None))
        self.assertEqual(bounded.projects(26), SourcePage((), None))
        source.customers.assert_called_once_with(0)
        source.projects.assert_called_once_with(0)
        self.assertEqual({key: value['processed'] for key, value in bounded.results.items()},
                         {'HeroCustomers': 5, 'HeroProjectMatches': 5})

    def test_projection_budget_spans_multiple_pages(self):
        source = SimpleNamespace(contract=SimpleNamespace(target_operation='HeroTasks'), projection=Mock(
            side_effect=[SourcePage(({'id': 1}, {'id': 2}), 2),
                         SourcePage(tuple({'id': i} for i in range(3, 9)), 8)]))
        bounded = testing.RecordLimitedSource(source, 5)
        self.assertEqual(len(bounded.projection().records), 2)
        second = bounded.projection(2)
        self.assertEqual([row['id'] for row in second.records], [3, 4, 5])
        self.assertIsNone(second.next_offset)
        self.assertEqual(bounded.projection(8), SourcePage((), None))
        self.assertEqual(source.projection.call_count, 2)

    def test_employee_conflict_counts_toward_five_and_duplicate_job_does_not_repeat(self):
        fixture = self.employee_fixture()
        with patch.object(employees, 'upsert', side_effect=[employees.EmployeeMigrationBlocked('Review'),
                                                          'updated', 'unchanged', 'added', 'updated']) as write:
            first = fixture.run_import(record_limit=5, continue_on_conflict=True)
            second = fixture.run_import(record_limit=5, continue_on_conflict=True)
        self.assertEqual(write.call_count, 5)
        self.assertEqual([call.args[0]['id'] for call in write.call_args_list], [1, 2, 3, 4, 5])
        self.assertTrue(first['done'])
        self.assertTrue(second['done'])
        self.assertEqual(second['counts']['conflict'], 1)
        self.assertEqual(testing.processed(second['counts']), 5)
        self.assertNotIn('last_error', second)
        self.assertEqual(second['testing_records_received'], 26)

    def test_employee_slices_share_frozen_selection_and_remaining_budget(self):
        fixture = self.employee_fixture()
        with patch.object(employees, 'upsert', return_value='updated') as write:
            first = fixture.run_import(record_limit=5, continue_on_conflict=True,
                                       should_stop=Mock(side_effect=[False, False, True]))
            self.assertEqual(write.call_count, 2)
            self.assertFalse(first['done'])
            final = fixture.run_import(record_limit=5, continue_on_conflict=True)
        self.assertTrue(final['done'])
        self.assertEqual(write.call_count, 5)
        self.assertEqual([call.args[0]['id'] for call in write.call_args_list], [1, 2, 3, 4, 5])

    def test_service_first_run_limits_detail_ids_and_collects_conflicts(self):
        fixture = service_fixtures.TestServiceRun()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.rows = [{'id': i, 'company_id': 9, 'modified': 'now'} for i in range(1, 27)]
        fixture.client.query = Mock(wraps=fixture.client.query)
        with patch.object(services, 'upsert', side_effect=[services.ServiceBlocked('Review')] + ['updated'] * 4) as write:
            first = fixture.run_sync(record_limit=5, continue_on_conflict=True)
            second = fixture.run_sync(record_limit=5, continue_on_conflict=True)
        self.assertTrue(first['done'])
        self.assertTrue(second['done'])
        self.assertEqual(write.call_count, 5)
        fixture.client.query.assert_called_once()
        self.assertEqual(fixture.client.query.call_args.args[1], {'ids': [1, 2, 3, 4, 5], 'first': 5})
        self.assertEqual(second['counts'], {'conflict': 1, 'updated': 4})
        self.assertNotIn('last_error', second)

    def test_product_finished_test_checkpoint_is_reused_without_production_cursor(self):
        read = SimpleNamespace(mapping_version='mapping-v1', origin='approved', page_size=100)
        saved = {}

        def save(run, operation, state):
            saved[(run, operation)] = copy.deepcopy(state)

        fake = SimpleNamespace(db=SimpleNamespace(commit=Mock(), rollback=Mock()), log_error=Mock())
        with patch.object(products, 'frappe', fake), \
             patch.object(testing, 'test_state', side_effect=lambda run, operation: copy.deepcopy(saved.get((run, operation), {}))), \
             patch.object(testing, 'save_test_state', side_effect=save), \
             patch.object(products, '_state', side_effect=AssertionError('production cursor read')), \
             patch.object(products, '_save', side_effect=AssertionError('production cursor write')), \
             patch.object(products, '_links', return_value={}), patch.object(products, '_Masters'), \
             patch.object(products, 'fetch_page', return_value=[{'product_id': i} for i in range(1, 27)]) as fetch, \
             patch.object(products, 'upsert', return_value='updated') as write:
            first = products.sync_products(Mock(), source_instance='source', read=read, record_limit=5, run_name='test-run')
            second = products.sync_products(Mock(), source_instance='source', read=read, record_limit=5, run_name='test-run')
        self.assertTrue(first['done'])
        self.assertEqual(second, first)
        self.assertEqual(write.call_count, 5)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(set(saved), {('test-run', 'HeroProducts')})

    def test_child_inherits_parent_limit_without_rereading_changed_settings(self):
        owner = SimpleNamespace(get_single=Mock(side_effect=AssertionError('child must use frozen parent')))
        parent = SimpleNamespace(testing_enabled=1, testing_max_actions=5)
        self.assertEqual(testing.configuration(parent, frappe_module=owner),
                         {'testing_enabled': 1, 'testing_max_actions': 5})
        parent.testing_enabled = 0
        self.assertEqual(testing.configuration(parent, frappe_module=owner),
                         {'testing_enabled': 0, 'testing_max_actions': 0})

    def test_budget_counts_record_outcomes_and_ignores_transport_counters(self):
        state = {'counts': {'added': 1, 'updated': 1, 'conflict': 1, 'skipped_no_id': 1,
                            'requests': 100, 'bytes': 50000, 'bytes_planned': 50000,
                            'relinked': 1, 'target_project': 1}}
        self.assertEqual(testing.processed(state['counts']), 4)
        self.assertEqual(testing.bounded_rows(state, list(range(26)), 5), [0])
        state['counts']['failed'] = 1
        self.assertEqual(testing.bounded_rows(state, list(range(26)), 5), [])

    def test_changed_limit_or_context_does_not_restart_frozen_checkpoint(self):
        state = {'testing_record_limit': 5, 'source_instance': 'source', 'mapping': 'v1'}
        with self.assertRaises(ValueError):
            testing.bounded_rows(state, [1, 2], 6)
        with self.assertRaises(ValueError):
            testing.validate_checkpoint(state, 5, source_instance='source', mapping='v2')
        with self.assertRaises(ValueError):
            testing.validate_checkpoint(state, 5, source_instance='other-source', mapping='v1')
        self.assertEqual(state, {'testing_record_limit': 5, 'source_instance': 'source', 'mapping': 'v1'})

    def test_testing_limit_status_is_not_complete_for_partial_slice(self):
        fake = SimpleNamespace(db=SimpleNamespace(set_value=Mock()))
        state = {'testing_record_limit': 5, 'testing_limit_reached': True,
                 'testing_records_received': 26, 'done': False, 'counts': {'updated': 2}}
        with patch.object(testing, 'frappe', fake):
            testing.persist('test-run', 'HeroEmployees', state)
            self.assertEqual(fake.db.set_value.call_args.args[2]['testing_status'], '')
            state.update(done=True, counts={'updated': 5})
            testing.persist('test-run', 'HeroEmployees', state)
            self.assertEqual(fake.db.set_value.call_args.args[2]['testing_status'], 'LIMIT_REACHED')


if __name__ == '__main__':
    unittest.main()


class TestFullMigrationSerialization(unittest.TestCase):
    def test_duplicate_parent_jobs_create_only_one_child(self):
        from hero_integration.services import file_migration as full
        from hero_integration.tests.test_file_migration import _DB
        db = _DB()
        db.runs['FULL'] = {'state': 'Running'}
        mutex = threading.Lock()
        barrier = threading.Barrier(2)
        errors = []

        def start(action, parent):
            time.sleep(0.02)  # Leave the old checkpoint visible to a competing job.
            db.runs['CHILD'] = {'state': 'Queued'}
            return 'CHILD'

        def worker():
            try:
                barrier.wait(timeout=2)
                full.advance_full('FULL')
            except Exception as error:
                errors.append(error)

        fake = SimpleNamespace(db=db)
        with patch.object(full, 'frappe', fake), patch.object(full, '_start_step', side_effect=start) as create, \
             patch('frappe.utils.synchronization.filelock', return_value=mutex) as lock:
            threads = [threading.Thread(target=worker) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=3)
            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
            create.assert_called_once()
            self.assertEqual(lock.call_count, 2)
            self.assertEqual(full._full('FULL')['child'], 'CHILD')

    def test_finished_file_test_job_does_not_reopen_production_cursor(self):
        from hero_integration.services import file_transfer as files
        state = {'done': True, 'offset': 26, 'source_instance': 'source', 'mapping': 'v1',
                 'testing_record_limit': 5, 'counts': {'added': 5}}
        with patch.object(files.direct_runtime, 'resolve_read', return_value=SimpleNamespace(mapping_version='v1')), \
             patch.object(testing, 'test_state', return_value=state), \
             patch.object(files, '_state', side_effect=AssertionError('production cursor')), \
             patch.object(files, '_save_state', side_effect=AssertionError('production cursor')), \
             patch.object(files.direct_runtime, 'fetch_page', side_effect=AssertionError('finished run must not read')):
            self.assertEqual(files.transfer('documents', client=Mock(), downloader=Mock(), source_instance='source',
                                            record_limit=5, run_name='test-run'), state)
