"""Two-connection reservation checks on an explicitly authorized disposable site.

No HERO transport is constructed. Do not run on an operational site: the test
requires an idle reservation boundary and creates only test-owned run rows.
"""
import json
import os
from queue import Queue
from threading import Barrier, Thread
import unittest
from uuid import uuid4

import frappe
from frappe.tests import IntegrationTestCase

from hero_integration.services.sync_engine import FrappeSyncLedger, SyncConflict, SyncRun

SITE = os.environ.get('HERO_RUN_RESERVATION_TEST_SITE')


@unittest.skipUnless(SITE, 'requires HERO_RUN_RESERVATION_TEST_SITE set to a disposable MariaDB site')
class TestSyncRunReservationMariaDB(IntegrationTestCase):
    def setUp(self):
        super().setUp()
        self.assertEqual(frappe.local.site, SITE)
        self.original_user = frappe.session.user
        frappe.set_user('Administrator')
        self.assertFalse(frappe.db.get_single_value('HERO Integration Sync State', 'active_run'))
        self.assertFalse(frappe.db.exists('HERO Sync Run Ledger', {'site_scope': SITE, 'state': 'RUNNING'}))
        self.names = ['reservation-test-' + uuid4().hex for _ in range(2)]
        self.legacy_names = []
        self.created_state = not frappe.db.exists('HERO Sync State Ledger', SITE)
        if self.created_state:
            frappe.get_doc({'doctype': 'HERO Sync State Ledger', 'site_scope': SITE,
                'configuration_json': json.dumps({'site_scope': SITE, 'enabled': False, 'interval_seconds': 60,
                    'source_instance': 'reservation-test', 'mapping_revision': 1, 'snapshot_fingerprint': 'a' * 64}),
                'coverage_json': '[]', 'status': 'IDLE', 'generation': 1}).insert()
        frappe.db.commit()

    def tearDown(self):
        frappe.db.rollback()
        for name in self.names:
            frappe.db.delete('HERO Sync Run Ledger', {'sync_run_id': name})
        for name in self.legacy_names:
            frappe.db.sql("UPDATE `tabSingles` SET value='' WHERE doctype=%s AND field=%s AND value=%s",
                ('HERO Integration Sync State', 'active_run', name))
            frappe.db.delete('HERO Integration Run', {'name': name})
            frappe.db.delete('HERO Integration Record', {'run': name})
        if self.created_state:
            frappe.db.delete('HERO Sync State Ledger', {'site_scope': SITE})
        frappe.db.commit()
        frappe.set_user(self.original_user)
        super().tearDown()

    def test_two_distinct_run_ids_get_only_one_site_reservation(self):
        start, results = Barrier(2), Queue()
        def claim(name):
            try:
                frappe.init(site=SITE)
                frappe.connect()
                frappe.set_user('Administrator')
                # Establish an old REPEATABLE READ snapshot before the race.
                frappe.db.sql('SELECT COUNT(*) FROM `tabHERO Sync Run Ledger`')
                start.wait(timeout=10)
                try:
                    FrappeSyncLedger(frappe).claim(SyncRun(name, SITE, 'Administrator', 1, False))
                    frappe.db.commit()
                    results.put('claimed')
                except SyncConflict:
                    frappe.db.rollback()
                    results.put('blocked')
            except Exception as error:
                results.put(type(error).__name__)
            finally:
                frappe.destroy()
        threads = [Thread(target=claim, args=(name,)) for name in self.names]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=25)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(sorted([results.get(timeout=2), results.get(timeout=2)]), ['blocked', 'claimed'])
        frappe.db.rollback()
        self.assertEqual(frappe.db.count('HERO Sync Run Ledger', {'site_scope': SITE, 'state': 'RUNNING'}), 1)

    def test_legacy_migration_and_b8_cannot_claim_together(self):
        from hero_integration.services.migration import _claim_run
        legacy = frappe.get_doc({'doctype': 'HERO Integration Run', 'action': 'Sync',
            'mode': 'Execute', 'state': 'Queued'}).insert()
        self.legacy_names.append(legacy.name)
        frappe.db.commit()
        start, results = Barrier(2), Queue()
        def claim(kind):
            try:
                frappe.init(site=SITE)
                frappe.connect()
                frappe.set_user('Administrator')
                # Both calls start with a snapshot which predates either claim.
                frappe.db.sql('SELECT COUNT(*) FROM `tabHERO Sync Run Ledger`')
                start.wait(timeout=10)
                try:
                    if kind == 'legacy':
                        accepted = _claim_run(frappe.get_doc('HERO Integration Run', legacy.name))
                    else:
                        FrappeSyncLedger(frappe).claim(SyncRun(self.names[0], SITE, 'Administrator', 1, False))
                        accepted = True
                    frappe.db.commit()
                    results.put('claimed' if accepted else 'blocked')
                except SyncConflict:
                    frappe.db.rollback()
                    results.put('blocked')
            except Exception as error:
                results.put(type(error).__name__)
            finally:
                frappe.destroy()
        threads = [Thread(target=claim, args=(kind,)) for kind in ('legacy', 'b8')]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=25)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(sorted([results.get(timeout=2), results.get(timeout=2)]), ['blocked', 'claimed'])
        frappe.db.rollback()
        active_b8 = frappe.db.count('HERO Sync Run Ledger', {'site_scope': SITE, 'state': 'RUNNING'})
        active_legacy = frappe.db.get_value('HERO Integration Run', legacy.name, 'state') == 'Running'
        self.assertEqual(active_b8 + int(active_legacy), 1)
