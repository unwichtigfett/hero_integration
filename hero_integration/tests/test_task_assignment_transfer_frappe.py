"""Private Frappe/MariaDB tests for managed native ToDo transfers."""
import json
import os
import unittest
import threading
import time
from uuid import uuid4
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from hero_integration.api import task_assignment_policy_create, task_assignment_policy_approve, task_assignment_policy_revoke
from hero_integration.services.binding_registry import BindingRegistryService, FrappeBindingStorage
from hero_integration.services.migration import _migrate_native_task, _record_task_assignment_pending


class _Plan:
    mapping_version = 'transfer-fixture'
    definitions = (SimpleNamespace(source_path='id'), SimpleNamespace(source_path='title'))
    def source_identity(self, row): return str(row['id'])


class TestTaskAssignmentTransfer(unittest.TestCase):
    def setUp(self):
        if not os.environ.get('HERO_TEST_PRIVATE_SITE') or os.environ['HERO_TEST_PRIVATE_SITE'] != frappe.local.site:
            self.skipTest('Requires explicit isolated site identity')
        frappe.set_user('Administrator')
        company = frappe.get_all('Company', pluck='name', limit=1)[0]
        gender = frappe.get_all('Gender', pluck='name', limit=1)[0]
        nonce = uuid4().hex[:8]
        self.source = 'fixture-' + self._testMethodName + '-' + nonce
        self.users = []
        bindings = []
        for index in (1, 2, 3):
            user = frappe.get_doc({'doctype': 'User', 'email': f'transfer-{nonce}-{index}@example.invalid',
                'first_name': 'Synthetic', 'enabled': 1, 'send_welcome_email': 0}).insert()
            frappe.get_doc({'doctype': 'Employee', 'first_name': 'Synthetic', 'gender': gender,
                'date_of_birth': '1990-01-01', 'date_of_joining': '2020-01-01', 'status': 'Active',
                'company': company, 'user_id': user.name}).insert()
            user.reload(); user.add_roles('Projects User', *(['System Manager'] if index == 1 else []))
            self.users.append(user.name)
            binding = BindingRegistryService(FrappeBindingStorage()).create_or_resolve_object_binding(
                site_scope=frappe.local.site, source_instance=self.source, object_type='User', external_object_id=str(991000 + index),
                target_app='frappe', target_doctype='User', target_name=user.name,
                mapping_definition_id=f'transfer-{self._testMethodName}-{index}', mapping_revision=1)
            bindings.append(binding.identity_key)
        self.bindings = bindings
        payload = {'source_instance': self.source, 'issuer': self.users[0], 'company': company,
                   'user_bindings': bindings, 'allow_initial': True, 'allow_transfer': self._testMethodName != 'test_transfer_requires_policy',
                   'native_effects': {'date_rule': 'LOCAL_TODAY', 'allow_notifications': True, 'allow_follow': True}}
        if self._testMethodName in {'test_competing_silent_initial_assignment', 'test_silent_initial_against_manual_assignment'}:
            payload.update(silent_initial=True, allow_transfer=False, native_effects={
                'date_rule': 'LOCAL_TODAY', 'allow_notifications': False, 'allow_follow': False})
        self.policy = task_assignment_policy_create(json.dumps(payload))['name']
        task_assignment_policy_approve(self.policy, 1)
        settings = frappe.get_single('System Settings'); settings.disable_document_sharing = 1; settings.save()
        frappe.set_user(self.users[0])
        self.run = SimpleNamespace(mode='Execute', mapping_revision=1, name=None)
        self.scope = {'confirmed': True, 'source_instance': self.source, 'source_type': 'Task'}
        self.record = {'id': 991010, 'title': 'Synthetic transfer task', 'target_user_id': 991001, 'author_user_id': None,
                       '_hero_source_instance': self.source}
        self.result = _migrate_native_task(self.record, self.run, _Plan(), self.scope)
        if not self.__class__.__name__ == 'TestTaskAssignmentScope':
            frappe.set_user('Administrator')
            project = frappe.get_doc({'doctype': 'Project', 'project_name': 'transfer-' + self._testMethodName + '-' + nonce,
                                      'company': company}).insert()
            task = frappe.get_doc('Task', self.result.target_name); task.project = project.name; task.save()
            frappe.set_user(self.users[0])
        _record_task_assignment_pending(self.run, self.record, self.result.target_name, self.source)
        self.task = self.result.target_name

    def tearDown(self):
        frappe.set_user('Administrator')
        frappe.db.rollback()

    def state(self):
        return frappe.get_doc('HERO Task Assignment State', {'task_name': self.task})

    def transfer(self):
        _record_task_assignment_pending(self.run, {**self.record, 'target_user_id': 991002}, self.task, self.source)

    def test_unordered_transfer_is_held_and_repeat_preserves_assignment(self):
        prior = self.state(); old_name = prior.todo
        self.transfer()
        state = self.state(); self.assertEqual((state.state, state.state_revision, state.user_binding), ('SYSTEM', 1, self.bindings[0]))
        todo = frappe.get_doc('ToDo', old_name)
        self.assertEqual((todo.status, todo.allocated_to), ('Open', self.users[0]))
        self.transfer()
        self.assertEqual(frappe.db.count('ToDo', {'reference_type': 'Task', 'reference_name': self.task, 'status': 'Open'}), 1)
        self.assertEqual(self.state().state_revision, 1)
        self.assertFalse(frappe.db.exists('DocShare', {'share_doctype': 'Task', 'share_name': self.task, 'user': self.users[1]}))
        _record_task_assignment_pending(self.run, self.record, self.task, self.source)
        self.assertEqual((self.state().todo, self.state().state_revision), (state.todo, 1))
        self.assertTrue(frappe.db.exists('HERO Integration Record', {'technical_action': 'task_assignment_review', 'target_name': self.task}))
        details = frappe.get_all('HERO Integration Record', filters={'technical_action': 'task_assignment_review',
                                 'target_name': self.task}, pluck='detail')
        self.assertTrue(any('PENDING_SOURCE_ORDER' in detail for detail in details))

    def test_local_save_same_recipient_blocks_transfer(self):
        state = self.state(); old_name = state.todo
        todo = frappe.get_doc('ToDo', old_name); todo.description = 'Local instruction to same recipient'; todo.save()
        self.assertEqual(self.state().state, 'LOCAL')
        forged = self.state(); forged.state = 'SYSTEM'
        with self.assertRaises(frappe.PermissionError): forged.save()
        self.transfer()
        self.assertEqual((self.state().state, self.state().todo), ('LOCAL', old_name))
        self.assertEqual(frappe.get_doc('ToDo', old_name).status, 'Open')
        self.assertTrue(frappe.db.exists('HERO Integration Record', {'technical_action': 'task_assignment_review', 'target_name': self.task}))

    def test_direct_same_recipient_blocks_transfer(self):
        from frappe.desk.form import assign_to
        old = self.state()
        result = assign_to._add({'doctype': 'Task', 'name': self.task, 'assign_to': [self.users[0]]})
        self.assertTrue(result)
        self.assertEqual((self.state().state, self.state().state_revision), ('LOCAL', 2))
        assign_to._add({'doctype': 'Task', 'name': self.task, 'assign_to': [self.users[0]]})
        self.assertEqual(self.state().state_revision, 3)
        self.transfer()
        self.assertEqual((self.state().todo, self.state().state), (old.todo, 'LOCAL'))

    def test_authenticated_rpc_and_unauthorized_rpc(self):
        from frappe.utils.password import set_encrypted_password
        frappe.set_user('Administrator')
        nonce = uuid4().hex[:12]
        denied = frappe.get_doc({'doctype': 'User', 'email': f'rpc-denied-{nonce}@example.invalid',
            'first_name': 'Denied', 'enabled': 1, 'send_welcome_email': 0}).insert()
        good_key, good_secret = f'good-{nonce}', f'good-secret-{nonce}'
        denied_key, denied_secret = f'denied-{nonce}', f'denied-secret-{nonce}'
        for user, key, secret in ((self.users[0], good_key, good_secret),
                                  (denied.name, denied_key, denied_secret)):
            frappe.db.set_value('User', user, 'api_key', key)
            set_encrypted_password('User', user, secret, 'api_secret')
        old = self.state().todo
        frappe.db.commit()
        results = []
        def request(key, secret):
            from werkzeug.test import Client
            from werkzeug.wrappers import Response
            from frappe.app import application
            client = Client(application, Response)
            response = client.post('/api/method/frappe.desk.form.assign_to.add',
                base_url='http://' + os.environ['HERO_TEST_PRIVATE_SITE'],
                json={'args': {'doctype': 'Task', 'name': self.task, 'assign_to': [self.users[0]]}},
                headers={'Authorization': 'token ' + key + ':' + secret})
            if response.status_code not in (200, 403):
                print('RPC_UNEXPECTED_STATUS', response.status_code, response.get_data(as_text=True)[:1000], flush=True)
            results.append(response.status_code)
        thread = threading.Thread(target=request, args=(denied_key, denied_secret))
        thread.start(); thread.join(timeout=30); self.assertFalse(thread.is_alive())
        frappe.db.rollback()
        self.assertEqual(results, [403])
        self.assertEqual((self.state().state, self.state().state_revision), ('SYSTEM', 1))
        thread = threading.Thread(target=request, args=(good_key, good_secret))
        thread.start(); thread.join(timeout=30); self.assertFalse(thread.is_alive())
        frappe.db.rollback()
        self.assertEqual(results, [403, 200])
        self.assertEqual((self.state().state, self.state().state_revision, self.state().todo), ('LOCAL', 2, old))
        frappe.set_user(self.users[0]); self.transfer()
        self.assertEqual((self.state().state, self.state().todo), ('LOCAL', old))

    def test_unpatched_boundary_holds_transfer(self):
        old = self.state()
        with patch('hero_integration.services.task_assignment_state.assignment_boundary_available', return_value=False):
            self.transfer()
        self.assertEqual((self.state().todo, self.state().state_revision), (old.todo, 1))

    def test_missing_boundary_also_holds_initial_assignment(self):
        row = {**self.record, 'id': 991011, 'title': 'Synthetic unpatched intake'}
        result = _migrate_native_task(row, self.run, _Plan(), self.scope)
        task = frappe.get_doc('Task', result.target_name)
        task.project = frappe.get_doc('Task', self.task).project
        task.save()
        with patch('hero_integration.services.task_assignment_state.assignment_boundary_available', return_value=False):
            _record_task_assignment_pending(self.run, row, task.name, self.source)
        self.assertFalse(frappe.db.exists('ToDo', {'reference_type': 'Task', 'reference_name': task.name, 'status': 'Open'}))
        self.assertFalse(frappe.db.exists('HERO Task Assignment State', {'task_name': task.name}))

    def test_real_unpatched_boundary_holds_initial_assignment(self):
        from hero_integration.services.task_assignment_state import assignment_boundary_available
        if assignment_boundary_available():
            self.skipTest('Requires the actual unpatched private framework')
        self.assertFalse(frappe.db.exists('HERO Task Assignment State', {'task_name': self.task}))
        self.assertFalse(frappe.db.exists('ToDo', {'reference_type': 'Task', 'reference_name': self.task, 'status': 'Open'}))
        details = frappe.get_all('HERO Integration Record', filters={'technical_action': 'task_assignment_review',
                                 'target_name': self.task}, pluck='detail')
        self.assertTrue(any('native assignment boundary is unavailable' in detail for detail in details))

    def test_caught_native_failure_does_not_confirm_local_instruction(self):
        from frappe.desk.form import assign_to
        old = self.state()
        frappe.set_user('Administrator')
        user = frappe.get_doc('User', self.users[1]); user.remove_roles('Projects User')
        frappe.set_user(self.users[0]); frappe.db.commit()
        with self.assertRaises(frappe.ValidationError):
            assign_to.add({'doctype': 'Task', 'name': self.task, 'assign_to': [self.users[1]]})
        frappe.db.commit()
        readback = []
        def independent():
            frappe.init(site=os.environ['HERO_TEST_PRIVATE_SITE']); frappe.connect(); frappe.set_user('Administrator')
            try:
                state = frappe.get_doc('HERO Task Assignment State', {'task_name': self.task})
                readback.append((state.state, state.state_revision, state.todo,
                    frappe.db.count('ToDo', {'reference_type': 'Task', 'reference_name': self.task, 'status': 'Open'})))
            finally: frappe.destroy()
        thread = threading.Thread(target=independent); thread.start(); thread.join(timeout=20)
        self.assertFalse(thread.is_alive()); self.assertEqual(readback, [('SYSTEM', 1, old.todo, 1)])

    def test_local_extra_assignment_then_removal_keeps_local_ownership(self):
        from frappe.desk.form import assign_to
        original = self.state().todo
        assign_to.add({'doctype': 'Task', 'name': self.task, 'assign_to': [self.users[1]]})
        self.assertEqual(self.state().state, 'LOCAL')
        assign_to.remove('Task', self.task, self.users[1])
        self.assertEqual(self.state().state, 'LOCAL')
        self.transfer()
        self.assertEqual((self.state().state, self.state().todo), ('LOCAL', original))
        self.assertEqual(frappe.get_doc('ToDo', original).status, 'Open')

    def test_local_todo_deletion_is_preserved_and_blocks_transfer(self):
        original = self.state().todo
        frappe.delete_doc('ToDo', original)
        self.assertEqual(self.state().state, 'LOCAL')
        self.transfer()
        self.assertFalse(frappe.db.exists('ToDo', original))
        self.assertEqual(self.state().state, 'LOCAL')
        self.assertEqual(frappe.db.count('ToDo', {'reference_type': 'Task', 'reference_name': self.task, 'status': 'Open'}), 0)

    def test_transfer_requires_policy(self):
        old = self.state(); self.transfer()
        self.assertEqual((self.state().todo, self.state().state_revision), (old.todo, 1))
        self.assertEqual(frappe.get_doc('ToDo', old.todo).status, 'Open')
        self.assertTrue(frappe.db.exists('HERO Integration Record', {'technical_action': 'task_assignment_review', 'target_name': self.task}))

    def test_revoked_policy_does_not_transfer(self):
        old = self.state()
        frappe.set_user('Administrator'); task_assignment_policy_revoke(self.policy, 1); frappe.set_user(self.users[0])
        self.transfer()
        self.assertEqual((self.state().todo, self.state().state_revision), (old.todo, 1))
        self.assertEqual(frappe.get_doc('ToDo', old.todo).status, 'Open')

    def test_stale_policy_approval_blocks_transfer(self):
        old = self.state()
        frappe.db.set_value('HERO Task Assignment Policy', self.policy, 'approved_revision', 0)
        self.transfer()
        self.assertEqual((self.state().todo, self.state().state_revision), (old.todo, 1))
        self.assertEqual(frappe.get_doc('ToDo', old.todo).status, 'Open')

    def test_stale_binding_revision_blocks_transfer(self):
        old = self.state()
        frappe.db.set_value('HERO Object Binding', old.user_binding, 'binding_revision', 2)
        self.transfer()
        self.assertEqual((self.state().todo, self.state().state_revision), (old.todo, 1))
        self.assertEqual(frappe.get_doc('ToDo', old.todo).status, 'Open')

    def test_recipient_without_task_access_blocks_transfer(self):
        old = self.state()
        frappe.set_user('Administrator')
        user = frappe.get_doc('User', self.users[1]); user.remove_roles('Projects User')
        frappe.set_user(self.users[0]); self.transfer()
        self.assertEqual((self.state().todo, self.state().state_revision), (old.todo, 1))
        self.assertEqual(frappe.get_doc('ToDo', old.todo).status, 'Open')
        self.assertFalse(frappe.db.exists('DocShare', {'share_doctype': 'Task', 'share_name': self.task, 'user': self.users[1]}))

    def test_wrong_project_company_blocks_transfer(self):
        old = self.state()
        frappe.set_user('Administrator')
        company = frappe.get_doc('Company', frappe.get_doc('Project', frappe.get_doc('Task', self.task).project).company)
        other = frappe.get_doc({'doctype': 'Company', 'company_name': 'Other scope ' + self._testMethodName,
            'abbr': 'O32B', 'default_currency': company.default_currency, 'country': company.country}).insert()
        project = frappe.get_doc('Project', frappe.get_doc('Task', self.task).project)
        project.company = other.name; project.save()
        frappe.set_user(self.users[0]); self.transfer()
        self.assertEqual((self.state().todo, self.state().state_revision), (old.todo, 1))
        self.assertEqual(frappe.get_doc('ToDo', old.todo).status, 'Open')

    def test_foreign_doctype_keeps_native_assignment_behavior(self):
        from frappe.desk.form import assign_to
        old = self.state()
        project = frappe.get_doc('Task', self.task).project
        args = {'doctype': 'Project', 'name': project, 'assign_to': [self.users[0]]}
        first = assign_to.add(args)
        second = assign_to.add(args)
        self.assertEqual(first, second)
        self.assertEqual(frappe.db.count('ToDo', {'reference_type': 'Project', 'reference_name': project, 'status': 'Open'}), 1)
        self.assertEqual((self.state().todo, self.state().state_revision, self.state().state), (old.todo, 1, 'SYSTEM'))

    def test_unverified_author_does_not_transfer(self):
        old = self.state()
        _record_task_assignment_pending(self.run, {**self.record, 'target_user_id': 991002, 'author_user_id': 123}, self.task, self.source)
        self.assertEqual((self.state().todo, self.state().state_revision), (old.todo, 1))
        self.assertEqual(frappe.get_doc('ToDo', old.todo).status, 'Open')

    def test_explicit_null_preserves_system_assignment(self):
        old = self.state()
        _record_task_assignment_pending(self.run, {**self.record, 'target_user_id': None}, self.task, self.source)
        self.assertEqual((self.state().todo, self.state().user_binding, self.state().state_revision), (old.todo, old.user_binding, 1))
        self.assertEqual(frappe.get_doc('ToDo', old.todo).status, 'Open')

    def test_injected_transfer_failure_rolls_back_todo_and_state(self):
        old = self.state(); old_name = old.todo
        frappe.db.commit()
        with patch('hero_integration.services.migration.record_output', side_effect=RuntimeError('injected transfer failure')):
            with self.assertRaisesRegex(RuntimeError, 'injected transfer failure'):
                self.transfer()
        restored = self.state()
        self.assertEqual((restored.todo, restored.user_binding, restored.state_revision), (old_name, self.bindings[0], 1))
        self.assertEqual(frappe.get_doc('ToDo', old_name).status, 'Open')
        self.assertEqual(frappe.db.count('ToDo', {'reference_type': 'Task', 'reference_name': self.task, 'status': 'Open'}), 1)
        self.assertEqual(len(frappe.db.after_commit), 0)
        readback = []
        def independent():
            frappe.init(site=os.environ['HERO_TEST_PRIVATE_SITE']); frappe.connect(); frappe.set_user('Administrator')
            try:
                state = frappe.get_doc('HERO Task Assignment State', {'task_name': self.task})
                todo = frappe.get_doc('ToDo', old_name)
                readback.append((state.todo, state.user_binding, state.state_revision, todo.status,
                    frappe.db.count('ToDo', {'reference_type': 'Task', 'reference_name': self.task, 'status': 'Open'})))
            finally: frappe.destroy()
        thread = threading.Thread(target=independent); thread.start(); thread.join(timeout=20)
        self.assertFalse(thread.is_alive())
        self.assertEqual(readback, [(old_name, self.bindings[0], 1, 'Open', 1)])

    def test_local_same_recipient_wins_against_waiting_transfer(self):
        from frappe.desk.form import assign_to
        old = self.state().todo
        frappe.db.commit()
        assign_to.add({'doctype': 'Task', 'name': self.task, 'assign_to': [self.users[0]]})
        ready = threading.Event(); results = []
        def worker():
            frappe.init(site=os.environ['HERO_TEST_PRIVATE_SITE']); frappe.connect(); frappe.set_user(self.users[0])
            try:
                ready.set()
                self.transfer()
                frappe.db.commit(); results.append('OK')
            except Exception as error:
                frappe.db.rollback(); results.append(f'{type(error).__name__}: {error}')
            finally: frappe.destroy()
        thread = threading.Thread(target=worker); thread.start(); self.assertTrue(ready.wait(10))
        waiting = False
        for _ in range(100):
            if frappe.db.sql('SELECT COUNT(*) FROM information_schema.INNODB_LOCK_WAITS')[0][0]:
                waiting = True
                break
            if not thread.is_alive():
                break
            time.sleep(.05)
        frappe.db.commit(); thread.join(timeout=30)
        self.assertFalse(thread.is_alive())
        self.assertTrue(waiting or (len(results) == 1 and results[0].startswith('QueryDeadlockError:')),
                        f'No InnoDB lock wait or deadlock observed: {results}')
        self.assertTrue(results == ['OK'] or results[0].startswith('QueryDeadlockError:'))
        print(f'LOCAL_TRANSFER_RACE lock_wait={int(waiting)} results={results}', flush=True)
        frappe.db.rollback()
        self.assertEqual((self.state().state, self.state().todo), ('LOCAL', old))
        self.assertEqual(frappe.get_doc('ToDo', old).status, 'Open')
        readback = []
        def independent():
            frappe.init(site=os.environ['HERO_TEST_PRIVATE_SITE']); frappe.connect(); frappe.set_user('Administrator')
            try:
                state = frappe.get_doc('HERO Task Assignment State', {'task_name': self.task})
                readback.append((state.state, state.todo, frappe.get_doc('ToDo', old).status))
            finally: frappe.destroy()
        thread = threading.Thread(target=independent); thread.start(); thread.join(timeout=20)
        self.assertFalse(thread.is_alive()); self.assertEqual(readback, [('LOCAL', old, 'Open')])

    def test_competing_transfers_with_stale_snapshots(self):
        initial_state = self.state()
        old_name, state_name = initial_state.todo, initial_state.name
        frappe.db.commit()
        gate = threading.Barrier(2); results = []
        site, actor, source, task, scope = frappe.local.site, self.users[0], self.source, self.task, self.scope
        def worker(target):
            frappe.init(site=site); frappe.connect(); frappe.set_user(actor)
            try:
                frappe.db.sql('SELECT state_revision FROM `tabHERO Task Assignment State` WHERE name=%s', (state_name,))
                gate.wait(timeout=10)
                run = SimpleNamespace(mode='Execute', mapping_revision=1, name=None)
                row = {**self.record, 'target_user_id': target}
                _migrate_native_task(row, run, _Plan(), scope)
                _record_task_assignment_pending(run, row, task, source)
                frappe.db.commit(); results.append('OK')
            except Exception as error:
                frappe.db.rollback(); results.append(type(error).__name__)
            finally: frappe.destroy()
        threads = [threading.Thread(target=worker, args=(target,)) for target in (991002, 991003)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=30)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        frappe.db.rollback()
        self.assertIn(sorted(results), (['OK', 'OK'], ['OK', 'QueryDeadlockError']))
        state = self.state()
        self.assertEqual((state.state, state.state_revision, state.user_binding), ('SYSTEM', 1, self.bindings[0]))
        self.assertEqual(frappe.get_doc('ToDo', old_name).status, 'Open')
        self.assertEqual(frappe.db.count('ToDo', {'reference_type': 'Task', 'reference_name': task, 'status': 'Open'}), 1)
        self.assertEqual(frappe.db.count('HERO Integration Record', {'technical_action': 'task_assignment_transferred', 'target_name': task}), 0)
        reviews = frappe.db.count('HERO Integration Record', {'technical_action': 'task_assignment_review', 'target_name': task})
        self.assertEqual(reviews, results.count('OK'))
        print(f'CONCURRENT_TRANSFER independent_connections=2 distinct_recipients=2 transferred=0 review={reviews} results={sorted(results)} open_todos=1', flush=True)

    def test_competing_initial_assignment(self):
        row = {**self.record, 'id': 991011}
        result = _migrate_native_task(row, self.run, _Plan(), self.scope)
        frappe.set_user('Administrator')
        task = frappe.get_doc('Task', result.target_name)
        task.project = frappe.get_doc('Task', self.task).project; task.save()
        frappe.db.commit()
        gate = threading.Barrier(2); results = []
        def worker():
            frappe.init(site=os.environ['HERO_TEST_PRIVATE_SITE']); frappe.connect(); frappe.set_user(self.users[0])
            try:
                gate.wait(timeout=10)
                _record_task_assignment_pending(self.run, row, result.target_name, self.source)
                frappe.db.commit(); results.append('OK')
            except Exception as error:
                frappe.db.rollback(); results.append(type(error).__name__)
            finally: frappe.destroy()
        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=30)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertIn(sorted(results), (['OK', 'OK'], ['OK', 'QueryDeadlockError']))
        readback = []
        def independent():
            frappe.init(site=os.environ['HERO_TEST_PRIVATE_SITE']); frappe.connect(); frappe.set_user('Administrator')
            try:
                readback.append((frappe.db.count('ToDo', {'reference_type': 'Task', 'reference_name': result.target_name, 'status': 'Open'}),
                    frappe.db.count('HERO Task Assignment State', {'task_name': result.target_name, 'state': 'SYSTEM'})))
            finally: frappe.destroy()
        thread = threading.Thread(target=independent); thread.start(); thread.join(timeout=20)
        self.assertFalse(thread.is_alive()); self.assertEqual(readback, [(1, 1)])
        print(f'CONCURRENT_INITIAL independent_connections=2 results={sorted(results)} readback=1', flush=True)

    def test_competing_silent_initial_assignment(self):
        self.test_competing_initial_assignment()

    def test_silent_initial_against_manual_assignment(self):
        row = {**self.record, 'id': 991012}
        task = _migrate_native_task(row, self.run, _Plan(), self.scope).target_name
        frappe.db.commit()
        gate = threading.Barrier(2); results = []
        def worker(manual):
            frappe.init(site=os.environ['HERO_TEST_PRIVATE_SITE']); frappe.connect(); frappe.set_user(self.users[0])
            try:
                gate.wait(timeout=10)
                if manual:
                    from frappe.desk.form.assign_to import add
                    add({'doctype': 'Task', 'name': task, 'assign_to': [self.users[0]]})
                else:
                    _record_task_assignment_pending(self.run, row, task, self.source)
                frappe.db.commit(); results.append('OK')
            except Exception as error:
                frappe.db.rollback(); results.append(type(error).__name__)
            finally: frappe.destroy()
        with patch('frappe.desk.form.assign_to.notify_assignment'):
            threads = [threading.Thread(target=worker, args=(manual,)) for manual in (False, True)]
            for thread in threads: thread.start()
            for thread in threads: thread.join(timeout=30)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertIn(sorted(results), (['OK', 'OK'], ['OK', 'QueryDeadlockError']))
        frappe.db.rollback()
        self.assertEqual(frappe.db.count('ToDo', {'reference_type': 'Task', 'reference_name': task,
                                                 'allocated_to': self.users[0], 'status': 'Open'}), 1)
