"""The same native run retains distinct outcomes across mutable source re-enumeration."""
import json
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from hero_integration.services import file_migration, migration_replay as replay
from hero_integration.services.migration import Result


class TestMigrationReplay(TestCase):
    def setUp(self):
        self.run = SimpleNamespace(name='test-run', action='Project Migration', mode='Execute',
            source_instance='test', source_binding_revision=2, customer_phase_state='Completed',
            project_phase_state='Failed', resume_phase='project', resume_offset=7, resume_record_index=1,
            progress_counts_json='{"rows_read":9}')

    def test_replay_rebuilds_distinct_counters_and_retains_the_completed_prerequisite(self):
        prior = {'customer': {'1': ('CREATED', 'C1'), '2': ('PENDING_REVIEW', None)},
                 'project': {'3': ('CREATED', 'P3'), '4': ('BLOCKED_RECORD', None), '5': ('DEFERRED', None)}}
        with patch.object(replay, 'prior_receipts', side_effect=lambda run, phase: prior[phase]):
            replay.begin(self.run)
        r = self.run
        self.assertEqual((r.processed, r.created_records, r.conflict_records, r.skipped_records), (4, 2, 2, 0))
        self.assertEqual((r.project_phase_processed, r.project_phase_blocked, r.project_phase_skipped), (3, 1, 1))
        self.assertEqual((r.customer_phase_state, r.project_phase_state), ('Completed', 'Queued'))
        self.assertEqual((r.resume_offset, r.resume_record_index), (0, 0))
        self.assertEqual(json.loads(r.progress_counts_json), {'rows_read': 9, 'native_replay': True})
        for old, new in [
            (prior['project']['3'], Result('SKIPPED', '', 'Project', 'P3')),
            (prior['project']['4'], Result('CREATED', '', 'Project', 'P4')),
            (prior['project']['5'], Result('CREATED', '', 'Project', 'P5')),
            (None, Result('CONFLICT', '')),
        ]:
            replay.count_result(r, new, old)
            replay.count_result(r, new, old, phase='project')
        self.assertEqual((r.processed, r.created_records, r.conflict_records, r.skipped_records), (6, 4, 2, 0))
        self.assertEqual((r.project_phase_processed, r.project_phase_created, r.project_phase_blocked), (4, 3, 1))

    def test_incomplete_historical_customer_receipts_trigger_a_fresh_customer_scan(self):
        from hero_integration.services.migration import _completed_customer_sibling
        self.run.customer_phase_processed = 3
        self.run.customer_service = 'frozen-reader'
        self.run.checkpoint_contract_json = '{}'
        with patch.object(replay, 'prior_receipts', side_effect=[{'1': ('CREATED', 'C1')}, {}]):
            replay.begin(self.run)
        self.assertEqual(self.run.customer_phase_state, 'Queued')
        with patch.object(file_migration, '_parent_run', side_effect=AssertionError('unexpected sibling lookup')):
            self.assertIsNone(_completed_customer_sibling(self.run))

    def test_unchanged_rebound_target_is_never_counted_as_the_original_creation(self):
        self.assertEqual(replay.receipt('SKIPPED','other',('CONFLICT','original')), ('SKIPPED','original'))
        self.assertEqual(replay.receipt('OBSERVED','original',('CONFLICT','original')), ('CREATED','original'))
        self.assertEqual(replay.receipt('CONFLICT','original',('CREATED','original')), ('CONFLICT','original'))

    def test_bounded_or_preview_replay_preserves_original_budget_accounting(self):
        for field, value in [('testing_enabled', 1), ('execution_budget_run', 'root'), ('mode', 'Preview')]:
            with self.subTest(field=field):
                run = SimpleNamespace(**vars(self.run))
                setattr(run, field, value)
                run.processed = 7
                with patch.object(replay, 'prior_receipts', side_effect=AssertionError('traversed the budget')):
                    replay.begin(run)
                self.assertEqual(run.progress_counts_json, self.run.progress_counts_json)
                self.assertEqual((run.processed, run.customer_phase_state), (7, 'Queued'))

    def test_full_run_reuses_only_its_matching_sealed_native_child(self):
        state = {'steps': list(file_migration.NEW_FULL_STEPS), 'step': 2, 'child': 'child'}
        row = SimpleNamespace(action='Project Migration', mode='Execute', source_instance='test',
            source_binding_revision=2, checkpoint_contract_json='frozen', testing_enabled=0)
        with patch.object(file_migration,'frappe',SimpleNamespace(db=SimpleNamespace(get_value=Mock(return_value=row)))), \
                patch.object(file_migration,'_parent_run',return_value=self.run.name):
            self.assertTrue(file_migration._sealed_native_child(self.run, state, 'child'))
            row.source_binding_revision = 3
            with self.assertRaises(file_migration.SourceBindingError):
                file_migration._sealed_native_child(self.run, state, 'child')
            row.checkpoint_contract_json = ''
            self.assertFalse(file_migration._sealed_native_child(self.run, state, 'child'))
            self.assertFalse(file_migration._sealed_native_child(self.run, dict(state, step=0), 'child'))
