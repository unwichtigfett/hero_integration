"""Two-connection intake test; enabled only for an explicitly named private site."""
import os
import hashlib
import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import frappe

from hero_integration.services.migration import _migrate_native_task


class _Plan:
    mapping_version = 'concurrency-fixture-v1'
    definitions = [SimpleNamespace(source_path=path) for path in ('id', 'title', 'comment')]

    def source_identity(self, row):
        return str(row['id'])


class TestNativeTaskConcurrency(unittest.TestCase):
    def test_task_lock_survives_until_commit_or_rollback_and_is_released(self):
        site = os.environ.get('HERO_TEST_PRIVATE_SITE')
        if not site or site != frappe.local.site:
            self.skipTest('Requires explicit isolated site identity')
        instance, source_id = 'fixture-task32a-lock', 982014
        source_key = f'HERO:{instance}:Task:{source_id}'
        subject = 'Synthetic Task 32A lock fixture'
        self.assertFalse(frappe.db.exists('HERO Integration Link', {'source_key': source_key}))
        self.assertFalse(frappe.db.exists('Task', {'subject': subject}))
        lock_key = json.dumps([site, 'native-task-intake:' + frappe.conf.db_name, 'Task', source_key],
                              ensure_ascii=False, separators=(',', ':'))
        lock_name = 'hero-map:' + hashlib.sha256(lock_key.encode()).hexdigest()[:52]

        def other_connection():
            values = []
            def query():
                frappe.init(site=site); frappe.connect()
                try:
                    values.append(int(frappe.db.sql('SELECT IS_FREE_LOCK(%s)', (lock_name,))[0][0]))
                finally:
                    frappe.destroy()
            thread = threading.Thread(target=query); thread.start(); thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(values), 1)
            return values[0]

        record = {'id': source_id, 'title': subject, '_hero_source_instance': instance}
        scope = {'confirmed': True, 'source_instance': instance, 'source_type': 'Task'}
        run = SimpleNamespace(mode='Execute', mapping_revision=1)
        try:
            original = frappe.get_doc
            def inject(*args, **kwargs):
                if args and isinstance(args[0], dict) and args[0].get('doctype') == 'HERO Integration Link':
                    raise RuntimeError('injected failure')
                return original(*args, **kwargs)
            with patch.object(frappe, 'get_doc', side_effect=inject):
                with self.assertRaisesRegex(RuntimeError, 'injected failure'):
                    _migrate_native_task(record, run, _Plan(), scope)
            self.assertEqual(other_connection(), 0)
            frappe.db.rollback()
            self.assertEqual(other_connection(), 1)
            _migrate_native_task(record, run, _Plan(), scope)
            self.assertEqual(other_connection(), 0)
            frappe.db.rollback()
            self.assertEqual(other_connection(), 1)
            _migrate_native_task(record, run, _Plan(), scope)
            self.assertEqual(other_connection(), 0)
            frappe.db.commit()
            self.assertEqual(other_connection(), 1)
            print('LOCK_RELEASE second_connection failure_rollback=free rollback=free commit=free', flush=True)
        finally:
            frappe.db.rollback()
            for row in frappe.get_all('HERO Integration Link', filters={'source_key': source_key}, fields=['name', 'target_name']):
                frappe.db.delete('HERO Integration Link', {'name': row.name})
                frappe.db.delete('Task', {'name': row.target_name})
            frappe.db.commit()

    def test_two_connections_keep_one_binding_and_one_task(self):
        site = os.environ.get('HERO_TEST_PRIVATE_SITE')
        if not site or site != frappe.local.site:
            self.skipTest('Requires explicit isolated site identity')
        key = 'HERO:fixture-task32a-race:Task:982013'
        subject = 'Synthetic Task 32A concurrency fixture'
        self.assertFalse(frappe.db.exists('HERO Integration Link', {'source_key': key}))
        self.assertFalse(frappe.db.exists('Task', {'subject': subject}))
        frappe.db.rollback()
        barrier = threading.Barrier(2)
        results = []

        def worker():
            frappe.init(site=site); frappe.connect(); frappe.set_user('Administrator')
            try:
                barrier.wait(timeout=10)
                result = _migrate_native_task(
                    {'id': 982013, 'title': subject, 'comment': 'Test', '_hero_source_instance': 'fixture-task32a-race'},
                    SimpleNamespace(mode='Execute', mapping_revision=1), _Plan(),
                    {'confirmed': True, 'source_instance': 'fixture-task32a-race', 'source_type': 'Task'},
                )
                frappe.db.commit(); results.append((result.code, result.target_name))
            except Exception as error:
                frappe.db.rollback(); results.append(('ERROR', type(error).__name__))
            finally:
                frappe.destroy()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=30)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        try:
            rows = frappe.get_all('HERO Integration Link', filters={'source_key': key},
                                  fields=['name', 'target_name', 'source_field_hashes', 'target_field_hashes'])
            tasks = frappe.get_all('Task', filters={'subject': subject}, pluck='name')
            self.assertEqual(sorted(result[0] for result in results), ['CREATED', 'SKIPPED'])
            self.assertEqual(len(rows), 1)
            self.assertEqual(tasks, [rows[0].target_name])
            self.assertEqual({result[1] for result in results}, {rows[0].target_name})
            self.assertTrue(rows[0].source_field_hashes and rows[0].target_field_hashes)
            print(f'INDEPENDENT_READBACK task={rows[0].target_name} links={len(rows)} tasks={len(tasks)} '
                  f'outcomes={sorted(result[0] for result in results)} confirmed_fields=present', flush=True)
        finally:
            # Only identities generated by this isolated test are removed.
            for row in frappe.get_all('HERO Integration Link', filters={'source_key': key}, fields=['name']):
                frappe.db.delete('HERO Integration Link', {'name': row.name})
            for name in frappe.get_all('Task', filters={'subject': subject}, pluck='name'):
                frappe.db.delete('Task', {'name': name})
            frappe.db.commit()
