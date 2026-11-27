"""Offline reservation ordering; this is not evidence of MariaDB concurrency."""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from hero_integration.services import migration, run_reservation
from hero_integration.services.sync_engine import FrappeSyncLedger, SyncConflict, SyncRun


class QueryDeadlockError(RuntimeError):
    pass


class TestSyncRunReservation(unittest.TestCase):
    def facade(self, *, legacy='', legacy_state='Running', b8=(), pending=(), acquired=True):
        events = []
        def sql(query, args=None, as_dict=False):
            events.append(query)
            if 'GET_LOCK' in query:
                return [SimpleNamespace(acquired=int(acquired))]
            if 'RELEASE_LOCK' in query:
                return []
            if 'SELECT `value`' in query:
                return [SimpleNamespace(value=legacy)]
            if 'WHERE state IN' in query:
                return [SimpleNamespace(name=name) for name in pending]
            if 'FROM `tabHERO Integration Run`' in query:
                return [SimpleNamespace(state=legacy_state)]
            if 'FROM `tabHERO Sync Run Ledger`' in query:
                return [SimpleNamespace(name=name, sync_run_id=name) for name in b8]
            raise AssertionError(query)
        doc = SimpleNamespace(generation=1, cursor=None, coverage_json='[]', status='IDLE', diagnostic='',
            configuration_json=json.dumps({'site_scope': 'site', 'source_instance': 'synthetic',
                'enabled': False, 'interval_seconds': 60, 'mapping_revision': 1, 'schema_fingerprint': 'a' * 64}),
            insert=Mock(side_effect=lambda: events.append('INSERT RUN')))
        frappe = SimpleNamespace(local=SimpleNamespace(site='site'), session=SimpleNamespace(user='Administrator'),
            QueryDeadlockError=QueryDeadlockError,
            get_roles=lambda: [], has_permission=Mock(),
            db=SimpleNamespace(sql=sql, table_exists=lambda _name: True, commit=Mock(), rollback=Mock()),
            get_doc=Mock(return_value=doc), get_all=Mock(side_effect=AssertionError('snapshot read forbidden during claim')))
        return frappe, events

    def test_claim_rechecks_under_shared_row_lock_and_does_not_commit(self):
        frappe, events = self.facade()
        FrappeSyncLedger(frappe).claim(SyncRun('one', 'site', 'Administrator', 1, False))
        self.assertIn('GET_LOCK', events[0])
        self.assertIn('tabSingles', events[1])
        self.assertIn('FOR UPDATE', events[1])
        self.assertIn('tabHERO Integration Run', events[2])
        self.assertIn('tabHERO Sync Run Ledger', events[3])
        self.assertIn('FOR UPDATE', events[2])
        self.assertEqual(events[4], 'INSERT RUN')
        self.assertIn('RELEASE_LOCK', events[5])
        frappe.db.commit.assert_not_called()
        frappe.db.rollback.assert_not_called()

    def test_current_active_run_blocks_insert_even_if_prior_snapshot_was_empty(self):
        frappe, events = self.facade(b8=('committed-while-waiting',))
        with self.assertRaises(SyncConflict):
            FrappeSyncLedger(frappe).claim(SyncRun('two', 'site', 'Administrator', 1, False))
        frappe.get_doc.assert_not_called()
        self.assertIn('RELEASE_LOCK', events[-1])

    def test_active_legacy_run_blocks_b8_but_completed_slot_does_not(self):
        frappe, events = self.facade(legacy='legacy')
        with self.assertRaises(SyncConflict):
            FrappeSyncLedger(frappe).claim(SyncRun('one', 'site', 'Administrator', 1, False))
        frappe.get_doc.assert_not_called()
        self.assertIn('RELEASE_LOCK', events[-1])
        frappe, _events = self.facade(legacy='finished', legacy_state='Completed')
        FrappeSyncLedger(frappe).claim(SyncRun('one', 'site', 'Administrator', 1, False))
        self.assertEqual(frappe.get_doc.call_count,2)

    def test_failed_setup_lock_does_not_claim_or_release_an_unowned_lock(self):
        frappe, events = self.facade(acquired=False)
        with self.assertRaises(SyncConflict):
            FrappeSyncLedger(frappe).claim(SyncRun('one', 'site', 'Administrator', 1, False))
        self.assertEqual(len(events), 1)
        frappe.get_doc.assert_not_called()

    def test_legacy_claim_checks_shared_b8_runs_before_mutating_slot(self):
        frappe, events = self.facade(b8=('b8-running',))
        run = SimpleNamespace(name='legacy', save=Mock())
        with patch.object(migration, 'frappe', frappe), patch.object(migration, '_block_run') as block:
            self.assertFalse(migration._claim_run(run))
        block.assert_called_once()
        run.save.assert_not_called()
        self.assertFalse(any(query.startswith('UPDATE') for query in events))
        self.assertIn('RELEASE_LOCK', events[-1])

    def test_unknown_legacy_slot_is_not_implicitly_reclaimed(self):
        frappe, _events = self.facade(legacy='orphan')
        with patch.object(frappe.db, 'sql', return_value=[]):
            self.assertTrue(run_reservation.legacy_slot_is_active(frappe, 'orphan'))

    def test_claim_insert_failure_releases_setup_lock_and_propagates(self):
        frappe, events = self.facade()
        frappe.get_doc.return_value.insert.side_effect = RuntimeError('insert rejected')
        with self.assertRaisesRegex(RuntimeError, 'insert rejected'):
            FrappeSyncLedger(frappe).claim(SyncRun('one', 'site', 'Administrator', 1, False))
        self.assertIn('RELEASE_LOCK', events[-1])
        frappe.db.commit.assert_not_called()

    def test_database_reservation_conflicts_block_without_taking_transaction_ownership(self):
        for stage in ('slot', 'active_runs', 'insert'):
            with self.subTest(stage=stage):
                frappe, events = self.facade()
                error = QueryDeadlockError('synthetic stale snapshot')
                target = {'slot': (run_reservation, 'locked_legacy_slot'),
                          'active_runs': (run_reservation, 'locked_b8_runs'),
                          'insert': (frappe.get_doc.return_value, 'insert')}[stage]
                with patch.object(*target, side_effect=error):
                    with self.assertRaises(SyncConflict) as caught:
                        FrappeSyncLedger(frappe).claim(SyncRun('one', 'site', 'Administrator', 1, False))
                self.assertIs(caught.exception.__cause__, error)
                self.assertIn('RELEASE_LOCK', events[-1])
                self.assertNotIn('INSERT RUN', events)
                if stage != 'insert':
                    frappe.get_doc.assert_not_called()
                frappe.db.commit.assert_not_called()
                frappe.db.rollback.assert_not_called()

    def test_committed_child_without_a_legacy_slot_blocks_sync(self):
        frappe, events = self.facade(pending=("queued-child",))
        with self.assertRaises(SyncConflict):
            FrappeSyncLedger(frappe).claim(SyncRun("other", "site", "Administrator", 1, False))
        self.assertNotIn("INSERT RUN",events)

    def test_admission_recovers_only_expired_native_owner_without_viable_job(self):
        import frappe
        from datetime import datetime, timedelta
        now = datetime(2026, 1, 1)
        for state, action, seconds, job, recover in (
            ('Running', 'Customer Migration', -1, None, True),
            ('Running', 'Customer Migration', -1, 'failed', True),
            ('Running', 'Customer Migration', -1, 'stopped', True),
            ('Running', 'Customer Migration', -1, 'started', False),
            ('Running', 'Customer Migration', -1, 'queued', False),
            ('Running', 'Customer Migration', -1, 'scheduled', False),
            ('Running', 'Customer Migration', 60, None, False),
            ('Queued', 'Customer Migration', -1, 'queued', False),
            ('Running', 'Full Migration', -1, None, False),
        ):
            with self.subTest(state=state, action=action, seconds=seconds, job=job):
                fake, _ = self.facade(legacy='' if job == 'failed' else 'old', legacy_state=state, pending=('old',))
                fake.ValidationError = frappe.ValidationError
                row = SimpleNamespace(state=state, action=action, started_on=now,
                                      lease_expires_on=now + timedelta(seconds=seconds))
                fake.db.get_value = Mock(return_value=row)
                with patch.object(migration, 'frappe', fake), patch.object(migration, '_interrupt_run') as stop, \
                     patch.object(migration, "_release_run"), patch.object(migration, "now_datetime", return_value=now), patch('frappe.utils.background_jobs.get_job_status', return_value=job):
                    if recover:
                        run_reservation.admit_migration(fake)
                        stop.assert_called_once()
                    else:
                        with self.assertRaises(frappe.ValidationError):
                            run_reservation.admit_migration(fake)
                        stop.assert_not_called()

    def test_full_parent_only_admits_recovery_of_its_stopped_child(self):
        import frappe
        for active_job in (True, False):
            fake, _ = self.facade(legacy='child', pending=('full', 'child'))
            fake.ValidationError = frappe.ValidationError
            fake.db.get_value = Mock(return_value='Full Migration')
            with patch.object(run_reservation, 'recover_stopped_owner', return_value=not active_job) as recovery:
                with self.assertRaises(frappe.ValidationError):
                    run_reservation.admit_migration(fake)
                recovery.assert_not_called()
                if active_job:
                    with self.assertRaises(frappe.ValidationError):
                        run_reservation.admit_migration(fake, parent='full', resume='child', recover_running=True)
                else:
                    run_reservation.admit_migration(fake, parent='full', resume='child', recover_running=True)
                recovery.assert_called_once_with(fake, 'child')
