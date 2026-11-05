"""Fail-closed organization and local-authority checks on a private site."""
import os
import threading
import unittest

import frappe

from hero_integration.tests import test_task_assignment_transfer_frappe as fixtures
from hero_integration.services.migration import _record_task_assignment_pending


class TestTaskAssignmentScope(unittest.TestCase):
    setUp = fixtures.TestTaskAssignmentTransfer.setUp
    tearDown = fixtures.TestTaskAssignmentTransfer.tearDown
    state = fixtures.TestTaskAssignmentTransfer.state

    def test_projectless_task_stays_pending(self):
        self.assertFalse(frappe.db.exists('ToDo', {'reference_type': 'Task', 'reference_name': self.task}))
        event = frappe.get_all('HERO Integration Record', filters={'target_name': self.task,
            'technical_action': 'task_assignment_review'}, fields=['detail'])
        self.assertTrue(any('no verified project' in row.detail for row in event))

    def test_project_company_allows_initial_assignment_but_holds_unordered_transfer(self):
        company = frappe.get_all('Company', pluck='name', limit=1)[0]
        frappe.set_user('Administrator')
        project = frappe.get_doc({'doctype': 'Project', 'project_name': 'scope-' + self._testMethodName,
                                  'company': company}).insert()
        task = frappe.get_doc('Task', self.task); task.project = project.name; task.save()
        frappe.set_user(self.users[0])
        row = {**self.record, 'target_user_id': 991002}
        _record_task_assignment_pending(self.run, row, self.task, self.source)
        state = self.state()
        todo = frappe.get_doc('ToDo', state.todo)
        self.assertEqual((todo.allocated_to, todo.assigned_by), (self.users[1], self.users[0]))
        frappe.db.commit()
        readback = []
        def independent():
            frappe.init(site=os.environ['HERO_TEST_PRIVATE_SITE']); frappe.connect(); frappe.set_user('Administrator')
            try:
                task = frappe.get_doc('Task', self.task)
                saved = frappe.get_doc('HERO Task Assignment State', {'task_name': self.task})
                native = frappe.get_doc('ToDo', saved.todo)
                readback.append((task.project == project.name, native.allocated_to == self.users[1],
                    native.assigned_by == self.users[0], saved.state == 'SYSTEM',
                    not frappe.db.exists('DocShare', {'share_doctype': 'Task', 'share_name': self.task, 'user': self.users[1]})))
            finally:
                frappe.destroy()
        thread = threading.Thread(target=independent); thread.start(); thread.join(timeout=20)
        self.assertFalse(thread.is_alive())
        self.assertEqual(readback, [(True, True, True, True, True)])
        print('COMMITTED_READBACK distinct_issuer_recipient=1 task_project=1 todo=1 system_state=1 new_share=0', flush=True)
        _record_task_assignment_pending(self.run, row, self.task, self.source)
        self.assertEqual(frappe.db.count('ToDo', {'reference_type': 'Task', 'reference_name': self.task, 'status': 'Open'}), 1)
        _record_task_assignment_pending(self.run, {**self.record, 'target_user_id': 991003}, self.task, self.source)
        self.assertEqual(self.state().state_revision, 1)
        self.assertEqual(frappe.get_doc('ToDo', state.todo).status, 'Open')
        self.assertEqual(frappe.get_doc('ToDo', self.state().todo).allocated_to, self.users[1])
        details = frappe.get_all('HERO Integration Record', filters={'technical_action': 'task_assignment_review',
                                 'target_name': self.task}, pluck='detail')
        self.assertTrue(any('PENDING_SOURCE_ORDER' in detail for detail in details))
