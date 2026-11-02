"""Offline reservation ordering; this is not evidence of MariaDB concurrency."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from hero_integration.services import migration, run_reservation
from hero_integration.services.sync_engine import FrappeSyncLedger, SyncConflict, SyncRun


class TestSyncRunReservation(unittest.TestCase):
    def facade(self, *, legacy='', legacy_state='Running', b8=(), acquired=True):
        events = []
        def sql(query, args=None, as_dict=False):
            events.append(query)
            if 'GET_LOCK' in query:
                return [SimpleNamespace(acquired=int(acquired))]
            if 'RELEASE_LOCK' in query:
                return []
            if 'SELECT `value`' in query:
                return [SimpleNamespace(value=legacy)]
            if 'FROM `tabHERO Integration Run`' in query:
                return [SimpleNamespace(state=legacy_state)]
            if 'FROM `tabHERO Sync Run Ledger`' in query:
                return [SimpleNamespace(name=name, sync_run_id=name) for name in b8]
            raise AssertionError(query)
        doc = SimpleNamespace(insert=Mock(side_effect=lambda: events.append('INSERT RUN')))
        frappe = SimpleNamespace(local=SimpleNamespace(site='site'), session=SimpleNamespace(user='Administrator'),
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
        self.assertIn('tabHERO Sync Run Ledger', events[2])
        self.assertIn('FOR UPDATE', events[2])
        self.assertEqual(events[3], 'INSERT RUN')
        self.assertIn('RELEASE_LOCK', events[4])
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
        frappe.get_doc.assert_called_once()

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
