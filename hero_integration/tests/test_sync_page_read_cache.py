"""Status rendering reuses validated readers without caching migration authority."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from hero_integration import api
from hero_integration.services import read_contract as contracts


class TestSyncPageReadCache(unittest.TestCase):
    def setUp(self):
        self.documents = [SimpleNamespace(name=name, revision=1) for name in ('service', 'mapping', 'snapshot')]

    def test_identical_documents_validate_once_only_inside_one_page(self):
        with patch.object(contracts, '_from_documents', return_value='checked') as validate:
            with contracts.reuse_page_reads():
                for _ in range(3):
                    self.assertEqual(contracts.from_documents(*self.documents), 'checked')
            self.assertEqual(validate.call_count, 1)
            with contracts.reuse_page_reads():
                contracts.from_documents(*self.documents)
            self.assertEqual(validate.call_count, 2)
            contracts.from_documents(*self.documents)
            contracts.from_documents(*self.documents)
            self.assertEqual(validate.call_count, 4)

    def test_document_changes_are_revalidated_even_with_the_same_names(self):
        with patch.object(contracts, '_from_documents', return_value='checked') as validate:
            with contracts.reuse_page_reads():
                contracts.from_documents(*self.documents)
                for doc in self.documents:
                    doc.revision += 1
                    contracts.from_documents(*self.documents)
            self.assertEqual(validate.call_count, 4)

    def test_failures_are_not_cached_and_exception_cleans_up_scope(self):
        with patch.object(contracts, '_from_documents', side_effect=contracts.ReadContractError('revoked')) as validate:
            with self.assertRaises(contracts.ReadContractError):
                with contracts.reuse_page_reads():
                    with self.assertRaises(contracts.ReadContractError):
                        contracts.from_documents(*self.documents)
                    contracts.from_documents(*self.documents)
            self.assertEqual(validate.call_count, 2)
            self.assertIsNone(contracts.page_read_cache())

    def test_reader_discovery_reuses_source_scoped_results_only_for_the_page(self):
        fake = SimpleNamespace(get_all=Mock(return_value=[]))
        with patch.object(api, 'frappe', fake):
            with contracts.reuse_page_reads():
                api._sync_area_read_services('source-a')
                api._sync_area_read_services('source-a')
                api._sync_area_read_services('source-b')
            self.assertEqual(fake.get_all.call_count, 2)
            api._sync_area_read_services('source-a')
            api._sync_area_read_services('source-a')
            self.assertEqual(fake.get_all.call_count, 4)

    def test_endpoint_scope_ends_before_the_next_status_or_migration_request(self):
        fake = SimpleNamespace(only_for=Mock(), get_single=Mock(side_effect=RuntimeError('status interrupted')))
        with patch.object(api, 'frappe', fake):
            with self.assertRaisesRegex(RuntimeError, 'status interrupted'):
                api.sync_page_state()
        fake.only_for.assert_called_once_with('System Manager')
        self.assertIsNone(contracts.page_read_cache())

    def test_status_reuses_plans_but_execution_and_locking_reads_do_not(self):
        from hero_integration.services import mapping_runtime
        with patch.object(mapping_runtime, '_active_mapping_plan', return_value=object()) as build:
            with contracts.reuse_page_reads():
                mapping_runtime.active_mapping_plan('HeroProducts', definition_ids=('one',), mapping_name='map')
                mapping_runtime.active_mapping_plan('HeroProducts', definition_ids=('one',), mapping_name='map')
                self.assertEqual(build.call_count, 1)
                for _ in range(2):
                    mapping_runtime.active_mapping_plan('HeroTasks', native_task=True, lock_rules=True)
                self.assertEqual(build.call_count, 3)
            mapping_runtime.active_mapping_plan('HeroProducts', definition_ids=('one',), mapping_name='map')
            self.assertEqual(build.call_count, 4)
