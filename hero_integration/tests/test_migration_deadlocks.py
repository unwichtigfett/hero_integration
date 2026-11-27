"""Concurrency regressions for the migration writer and dependency workers."""
from contextlib import contextmanager
from types import SimpleNamespace as NS
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe
from hero_integration.services import deferred_references as refs, migration


class RecordRetry(TestCase):
    def setUp(self):
        self.db = {}
        self.run = NS(checkpoint_contract_json="bound", processed=0, resume_record_index=0)
        self.run.save = Mock()
        self.run.reload = lambda: vars(self.run).update(self.db)
        self.db.update(processed=0, resume_record_index=0, checkpoint_contract_json="bound")
        self.page = NS(records=[{"id": 7}])
        self.enterContext(patch.object(migration, 'frappe', frappe))
        self.enterContext(patch.object(migration.time, 'sleep'))
        self.result = self.enterContext(patch.object(migration, '_record_result', side_effect=self.count))

    def count(self, run, *_):
        run.processed += 1

    @contextmanager
    def checkpoint(self):
        yield
        self.db.update(processed=self.run.processed, resume_record_index=self.run.resume_record_index)

    def execute(self, write, checkpoint=None):
        migration._write_checkpoint_record(self.run, checkpoint or self.checkpoint, self.page,
            0, 0, 'HeroCalendarEvents', 'calendar_event', self.page.records[0], write)

    def test_rollback_discards_counter_and_cursor_before_repeating_whole_record(self):
        self.run.save.side_effect = [frappe.QueryDeadlockError(), None]
        writer = Mock()
        self.execute(writer)
        self.assertEqual(writer.call_count, 2)
        self.assertEqual((self.db['processed'], self.db['resume_record_index']), (1, 1))

    def test_schedule_worker_retries_only_current_event_without_rereading_page(self):
        op = 'HeroCalendarEvents'
        contract = NS(target_operation=op, service_revision=1, mapping_revision=2)
        self.run.resume_offset = 0
        self.run.graphql_service = 'service'
        self.run.graphql_mapping = 'mapping'
        self.run.service_revision, self.run.mapping_revision = 1, 2
        self.run.state = 'Running'
        page = NS(records=[{'id': 71}, {'id': 72}], next_offset=None)
        source = NS(contract=contract, source_instance='fixture', projection=Mock(return_value=page))
        fake = NS(QueryDeadlockError=frappe.QueryDeadlockError, get_doc=Mock(side_effect=[
            NS(schema_snapshot='schema'), NS(mapping_json='{}'), NS(source_instance='fixture')]))
        attempts = []
        def apply(row, *_):
            attempts.append(row['id'])
            if attempts == [71, 72]:
                raise frappe.QueryDeadlockError()
            return migration.Result('CREATED', '', 'Event', str(row['id']))
        with (patch.object(migration, 'frappe', fake),
              patch.object(migration, '_mapping_plan_for_run', return_value=NS(target_operation=op)),
              patch('hero_integration.services.read_contract.from_documents', return_value=contract),
              patch.object(migration, '_migrate_with_binding', side_effect=apply)):
            migration._process_projection(self.run, source, checkpoint=self.checkpoint)
        self.assertEqual(attempts, [71, 72, 72])
        self.assertEqual((self.db['processed'], self.db['resume_record_index']), (2, 2))
        source.projection.assert_called_once_with(0)

    def test_persistent_deadlock_exhausts_three_attempts_without_checkpoint(self):
        write = Mock(side_effect=frappe.QueryDeadlockError)
        with self.assertRaises(frappe.QueryDeadlockError):
            self.execute(write)
        self.assertEqual(write.call_count, 3)
        self.assertEqual((self.db['processed'], self.db['resume_record_index']), (0, 0))

    def test_replaced_contract_blocks_retry(self):
        self.db['checkpoint_contract_json'] = 'replacement'
        write = Mock(side_effect=frappe.QueryDeadlockError)
        with self.assertRaises(migration.MappingPlanError):
            self.execute(write)
        write.assert_called_once()

    def test_postcommit_error_never_replays_writer(self):
        @contextmanager
        def checkpoint():
            with self.checkpoint():
                yield
            raise frappe.QueryDeadlockError()
        write = Mock()
        with self.assertRaises(frappe.QueryDeadlockError):
            self.execute(write, checkpoint)
        write.assert_called_once()
        self.assertEqual(self.db['processed'], 1)


class PendingRetry(TestCase):
    def setUp(self):
        self.db = Mock()
        self.db.sql.return_value = [NS(name='ref')]
        self.actor = NS(user='worker')
        self.fake = NS(db=self.db, session=self.actor, set_user=lambda user: setattr(self.actor, 'user', user),
                       QueryDeadlockError=frappe.QueryDeadlockError, RetryBackgroundJobError=frappe.RetryBackgroundJobError)
        self.enterContext(patch.object(refs, 'installed', return_value=True))
        self.enterContext(patch.object(refs, 'frappe', self.fake))
        self.enterContext(patch.object(refs.time, 'sleep'))

    def test_dependency_worker_restores_actor_and_commits_once(self):
        users = []
        def realize(*args, **kwargs):
            users.append(self.actor.user)
            self.actor.user = 'origin'
            if len(users) == 1:
                raise frappe.QueryDeadlockError()
        with patch.object(refs, 'resolve_one', side_effect=realize):
            refs.resolve_dependency('source', 'User', 7, test_revision=2)
        self.assertEqual(users, ['worker', 'worker'])
        self.assertEqual(self.actor.user, 'worker')
        self.db.commit.assert_called_once()
        self.db.set_value.assert_not_called()

    def test_full_reconciliation_keeps_waiting_and_does_not_advance_on_exhaustion(self):
        state = {'reconciliation_stage': 0, 'reconciliation_after': 'previous'}
        self.db.get_value.return_value = 'Waiting'
        self.fake.log_error = Mock()
        with patch.object(refs, 'resolve_one', side_effect=frappe.QueryDeadlockError) as resolve:
            with self.assertRaises(refs.ReferenceRetryExhausted) as raised:
                refs.reconcile_full_page('source', ['child'], state)
        self.assertIsInstance(raised.exception.__cause__, frappe.QueryDeadlockError)
        self.assertEqual(resolve.call_count, 3)
        self.assertEqual(state, {'reconciliation_stage': 0, 'reconciliation_after': 'previous'})
        self.db.commit.assert_not_called()
        self.db.set_value.assert_not_called()
