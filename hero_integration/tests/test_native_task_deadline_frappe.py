"""Run only against an explicitly named private Frappe site."""
import os
import unittest
from types import SimpleNamespace

import frappe
from hero_integration.services.migration import _migrate_native_task
from hero_integration.services.change_detection import decode


class _Plan:
    mapping_version = 'deadline37-fixture'
    definitions = tuple(SimpleNamespace(source_path=path) for path in ('id', 'title', 'due_date', 'target_project_match_id'))

    def source_identity(self, row):
        return str(row['id'])


class TestNativeTaskDeadline(unittest.TestCase):
    def setUp(self):
        if os.environ.get('HERO_TEST_PRIVATE_SITE') != frappe.local.site:
            self.skipTest('Requires explicit isolated site identity')
        self.run = SimpleNamespace(mapping_revision=1, mode='Execute', name=None)
        self.scope = {'confirmed': True, 'source_instance': 'deadline37-fixture', 'source_type': 'Task'}
        self.row = {'id': 839371, 'title': 'Synthetic deadline', 'due_date': '2026-10-29T09:30:00+00:00',
                    '_hero_source_instance': 'deadline37-fixture'}

    def tearDown(self):
        frappe.db.rollback()

    def apply(self, row):
        return _migrate_native_task(row, self.run, _Plan(), self.scope)

    def test_deadline_is_separate_guarded_and_null_never_clears(self):
        created = self.apply(self.row)
        self.assertEqual(created.code, 'CREATED')
        task = frappe.get_doc('Task', created.target_name)
        self.assertTrue(task.get('custom_hero_due_date'))
        # The legacy lifecycle intentionally also projects the due *day* to
        # ERPNext's end date; the separate field retains the precise instant.
        self.assertEqual(str(task.exp_end_date)[:10], '2026-10-29')
        self.assertEqual(str(task.get('custom_hero_due_date'))[:19], '2026-10-29 09:30:00')
        link = frappe.get_doc('HERO Integration Link', {'source_key': 'HERO:deadline37-fixture:Task:839371'})
        baseline = decode(link.source_field_hashes)['custom_hero_due_date']
        old = task.get('custom_hero_due_date')
        task.custom_hero_due_date = '2026-01-01 00:00:00'
        with self.assertRaises(frappe.PermissionError):
            task.save()
        task.reload()
        self.assertEqual(self.apply({**self.row, 'due_date': None}).code, 'CONFLICT')
        task.reload(); link.reload()
        self.assertEqual(task.get('custom_hero_due_date'), old)
        self.assertEqual(decode(link.source_field_hashes)['custom_hero_due_date'], baseline)

    def test_naive_time_is_held_without_preventing_task_creation(self):
        result = self.apply({**self.row, 'due_date': '2026-10-29T09:30:00'})
        self.assertEqual(result.code, 'CREATED')
        task = frappe.get_doc('Task', result.target_name)
        self.assertFalse(task.get('custom_hero_due_date'))
        link = frappe.get_doc('HERO Integration Link', {'source_key': 'HERO:deadline37-fixture:Task:839371'})
        self.assertEqual(link.sync_status, 'Conflict')

    def test_project_uses_existing_binding_and_null_preserves_it(self):
        project = frappe.get_doc({'doctype': 'Project', 'project_name': 'Synthetic Task Project 37',
                                  'company': frappe.db.get_single_value('Global Defaults', 'default_company')}).insert()
        frappe.get_doc({'doctype': 'HERO Integration Link', 'source_system': 'HERO', 'source_type': 'project',
            'source_id': '839372', 'source_instance': 'deadline37-fixture', 'target_doctype': 'Project',
            'target_name': project.name, 'sync_status': 'Synchronized', 'source_payload_hash': 'fixture',
            'target_payload_hash': 'fixture', 'mapping_version': 'fixture'}).insert()
        row = {**self.row, 'target_project_match_id': 839372}
        result = self.apply(row)
        self.assertEqual(result.code, 'CREATED')
        task = frappe.get_doc('Task', result.target_name)
        self.assertEqual(task.project, project.name)
        self.assertEqual(self.apply({**row, 'target_project_match_id': None}).code, 'CONFLICT')
        task.reload()
        self.assertEqual(task.project, project.name)
        self.assertEqual(self.apply({**row, 'id': 839373, 'target_project_match_id': 839999}).code, 'BLOCKED_RECORD')
