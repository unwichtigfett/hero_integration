"""Polling receipts and live run outcomes, without network or site writes."""
import json
from pathlib import Path
import re
import sqlite3
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from hero_integration.services.polling import execution_summary, record_scheduler_outcome


class TestPollingReceipts(unittest.TestCase):
    def test_started_receipt_matches_installed_schema_and_does_not_save_settings(self):
        schema = json.loads((Path(__file__).parents[1] / 'hero_integration/doctype/hero_polling_event/hero_polling_event.json').read_text())
        statuses = next(f['options'].splitlines() for f in schema['fields'] if f['fieldname'] == 'status')
        receipts = []
        def doc(payload):
            self.assertIn(payload['status'], statuses)
            return SimpleNamespace(name='POLL-1', insert=lambda **kw: receipts.append(payload))
        settings = SimpleNamespace(hero_source_instance='source-a')
        written = []
        fake = SimpleNamespace(get_single=lambda dt: settings, get_doc=doc,
                               db=SimpleNamespace(set_single_value=lambda dt, values: written.append((dt, values))))
        with patch.dict(sys.modules, {'frappe': fake}):
            result = record_scheduler_outcome('AUTO_SYNC', 'Started', 'Queued.', run='RUN-1')
        self.assertEqual(result, 'POLL-1')
        self.assertEqual(written, [('HERO Integration Settings', {'sync_status': 'AUTO_SYNC started', 'sync_error': ''})])
        self.assertEqual(receipts[0]['run'], 'RUN-1')
        self.assertEqual(receipts[0]['source_instance'], 'source-a')


class TestPollingExecutionSummary(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            CREATE TABLE `tabHERO Polling Event` (name TEXT, run TEXT, status TEXT, source_instance TEXT, creation TEXT, manual_approval TEXT, sync_run TEXT, mode TEXT, detail TEXT);
            CREATE TABLE tabVersion (name TEXT PRIMARY KEY, ref_doctype TEXT, docname TEXT, creation TEXT, data TEXT);
            CREATE TABLE `tabHERO Sync Run Ledger` (name TEXT PRIMARY KEY, site_scope TEXT, source_instance TEXT,
                automatic INTEGER, scheduler_cycle TEXT, read_service TEXT, execution_scope TEXT,
                state TEXT, creation TEXT, modified TEXT);
            CREATE TABLE `tabHERO Integration Run` (name TEXT, state TEXT, source_instance TEXT, mode TEXT, finished_on TEXT);
        ''')
        self.fake = SimpleNamespace(db=SimpleNamespace(sql=self.sql), local=SimpleNamespace(site="site-a"))

    def sql(self, query, values, as_dict=False):
        query = re.sub(r'%\((\w+)\)s', r':\1', query)
        return [dict(row) for row in self.db.execute(query, values)]

    def run_row(self, name, state='Running', source='source-a', mode='Execute'):
        self.db.execute('INSERT INTO `tabHERO Integration Run` VALUES (?, ?, ?, ?, ?)',
                        (name, state, source, mode, '2026-11-03 02:00:00'))

    def event(self, name, run=None, status='Started', source='source-a', creation='2026-11-03 01:00:00'):
        self.db.execute('INSERT INTO `tabHERO Polling Event` (name,run,status,source_instance,creation) VALUES (?, ?, ?, ?, ?)',
                        (name, run, status, source, creation))

    def summary(self):
        return execution_summary(self.fake, 'source-a', '2026-11-01 00:00:00')

    def test_start_completion_and_duplicate_receipt_count_one_execution(self):
        self.run_row('R1')
        self.event('E1', 'R1')
        self.event('E2', 'R1')
        self.assertEqual((self.summary()['runs'], self.summary()['running']), (1, 1))
        self.db.execute("UPDATE `tabHERO Integration Run` SET state='Completed'")
        result = self.summary()
        self.assertEqual((result['runs'], result['running'], result['pass']), (1, 0, 1))
        self.assertEqual(result['last_status'], 'Succeeded')
        self.assertEqual(result['last_successful'], '2026-11-03 02:00:00')

    def test_conflicts_failures_cancelled_and_blocked_are_not_passes(self):
        for state in ['Completed With Conflicts', 'Failed', 'Cancelled', 'Blocked']:
            self.run_row(state, state)
            self.event(state, state)
        result = self.summary()
        self.assertEqual(result['runs'], 4)
        self.assertEqual([result[key] for key in ['conflicts', 'fail', 'cancelled', 'incomplete', 'pass']], [1, 1, 1, 1, 0])

    def test_manual_other_source_old_events_and_skips_do_not_inflate_executions(self):
        self.run_row('MANUAL', 'Completed')
        self.run_row('OTHER', 'Completed', source='source-b')
        self.event('OTHER', 'OTHER', source='source-b')
        self.event('OLD', status='Succeeded', creation='2026-10-01 00:00:00')
        self.event('SKIP', status='Skipped')
        self.event('MONITOR', status='Incomplete')
        self.event('MANUAL-MONITOR', status='Incomplete')
        self.db.execute("UPDATE `tabHERO Polling Event` SET manual_approval='explicit' WHERE name='MANUAL-MONITOR'")
        result = self.summary()
        self.assertEqual((result['runs'], result['incomplete'], result['skipped'], result['pass']), (1, 1, 1, 0))

    def test_missing_mismatched_or_preview_run_is_not_reported_successful(self):
        self.run_row('OTHER', 'Completed', source='source-b')
        self.run_row('PREVIEW', 'Completed', mode='Preview')
        for name in ['MISSING', 'OTHER', 'PREVIEW']:
            self.event(name, name)
        result = self.summary()
        self.assertEqual((result['runs'], result['incomplete'], result['pass']), (3, 3, 0))

    def inventory_receipt(self, status="Incomplete"):
        self.event("receipt", status=status)
        self.db.execute("UPDATE `tabHERO Polling Event` SET mode='AUTO_SYNC', detail='PARTIAL', sync_run='reader-a'")

    def inventory_reader(self, name, state, minute, diagnostic=None, checkpoint=None):
        prefix = '2026-11-03 00:' + minute
        self.db.execute("INSERT INTO `tabHERO Sync Run Ledger` VALUES (?, 'site-a', 'source-a', 1, 'cycle', "
                        "?, 'INVENTORY', ?, ?, ?)", (name, name, state, prefix + ':00', prefix + ':02'))
        if diagnostic:
            saved = {"source-a|" + name: {"status": state, "diagnostic": diagnostic,
                                          "inventory_checkpoint_json": json.dumps(checkpoint)}}
            data = {"changed": [["reader_states_json", "{}", json.dumps(saved)]]}
            self.db.execute("INSERT INTO tabVersion VALUES (?, 'HERO Sync State Ledger', 'site-a', ?, ?)",
                            (name, prefix + ':01', json.dumps(data)))

    def test_all_readers_must_confirm_progress_or_round_end(self):
        from hero_integration.services.sync_engine import INVENTORY_PAGE_PROCESSED, INVENTORY_PASS_ENDED
        self.inventory_receipt()
        self.inventory_reader("reader-a", "PARTIAL", "01", INVENTORY_PAGE_PROCESSED, {"next_offset": 91})
        self.inventory_reader("reader-b", "PARTIAL", "02", INVENTORY_PASS_ENDED)
        result = self.summary()
        self.assertEqual((result["runs"], result["pass"], result["partial_inventory"], result["incomplete"]), (1, 1, 1, 0))
        self.assertEqual(result["last_status"], "Succeeded")
        self.assertEqual(result["last_successful"], "2026-11-03 01:00:00")
        self.db.execute("DELETE FROM tabVersion WHERE name='reader-b'")
        self.assertEqual(self.summary()["incomplete"], 1)

    def test_unproven_partial_and_failed_or_conflicting_sibling_do_not_pass(self):
        from hero_integration.services.sync_engine import INVENTORY_PAGE_PROCESSED
        self.inventory_receipt()
        self.inventory_reader("reader-a", "PARTIAL", "01", INVENTORY_PAGE_PROCESSED, {"next_offset": 91})
        self.inventory_reader("reader-b", "PARTIAL", "02", "Inventory overlap moved; restarting the collection.")
        self.assertEqual(self.summary()["pass"], 0)
        for outcome in ("INCOMPLETE", "FAILED", "CONFLICT", "RUNNING"):
            self.db.execute("UPDATE `tabHERO Sync Run Ledger` SET state=? WHERE name='reader-b'", (outcome,))
            self.assertEqual(self.summary()["pass"], 0)

    def test_cycle_receipt_precedes_its_linked_reader(self):
        from hero_integration.services.sync_engine import INVENTORY_PAGE_PROCESSED
        self.inventory_receipt()
        self.inventory_reader("reader-a", "PARTIAL", "01", INVENTORY_PAGE_PROCESSED, {"next_offset": 91})
        for status, bucket in (("Failed", "fail"), ("Conflicts", "conflicts")):
            self.db.execute("UPDATE `tabHERO Polling Event` SET status=?", (status,))
            result = self.summary()
            self.assertEqual((result[bucket], result["partial_inventory"], result["pass"]), (1, 0, 0))

    def test_new_confirmed_partial_receipt_survives_version_retention(self):
        self.inventory_receipt("Succeeded")
        result = self.summary()
        self.assertEqual((result["pass"], result["partial_inventory"], result["incomplete"]), (1, 1, 0))

    def test_empty_period_is_not_started(self):
        result = self.summary()
        self.assertEqual(result['runs'], 0)
        self.assertEqual(result['last_status'], 'Not started')
        self.assertIsNone(result['last_successful'])


class TestPollingEventSchemaMigrates(unittest.TestCase):
    def test_changed_doctype_is_newer_than_its_previous_release(self):
        # Frappe syncs a DocType on migrate only when the JSON "modified" is newer than the site's.
        schema = json.loads((Path(__file__).parents[1] / 'hero_integration/doctype/hero_polling_event/hero_polling_event.json').read_text())
        self.assertGreater(schema['modified'], '2026-10-21 00:00:00.000000')
        self.assertIn('run', [field['fieldname'] for field in schema['fields']])


class TestFailureAfterAStartIsRecordedAgain(unittest.TestCase):
    """Failed → Started → Failed (same text) must give three receipts, not two."""

    def test_failure_start_failure(self):
        settings = SimpleNamespace(hero_source_instance='default', sync_status='', sync_error='',
                                   save=lambda **kw: None)
        receipts = []

        def doc(payload):
            return SimpleNamespace(name=f"POLL-{len(receipts) + 1}", insert=lambda **kw: receipts.append(payload))

        def set_single_value(doctype, values):
            for key, value in values.items():
                setattr(settings, key, value)

        fake = SimpleNamespace(get_single=lambda dt: settings, get_doc=doc,
                               db=SimpleNamespace(set_single_value=set_single_value))
        failure = 'Customers: the automatic run could not be started; see the error log.'
        with patch.dict(sys.modules, {'frappe': fake}):
            record_scheduler_outcome('AUTO_SYNC', 'Failed', failure)
            record_scheduler_outcome('AUTO_SYNC', 'Started', 'Customers: run RUN-1 queued.', run='RUN-1')
            self.assertEqual(settings.sync_error, '')
            record_scheduler_outcome('AUTO_SYNC', 'Failed', failure)
            record_scheduler_outcome('AUTO_SYNC', 'Failed', failure)  # an uninterrupted repeat stays deduplicated
        self.assertEqual([r['status'] for r in receipts], ['Failed', 'Started', 'Failed'])

