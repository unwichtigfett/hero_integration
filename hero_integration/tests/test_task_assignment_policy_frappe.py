"""Policy lifecycle acceptance, limited to an explicitly named private site."""
import json
import os
import unittest
import threading
from types import SimpleNamespace
from uuid import uuid4

import frappe

from hero_integration.services.binding_registry import BindingRegistryService, FrappeBindingStorage
from hero_integration.services.task_assignment_policy import PolicyError
from hero_integration.api import task_assignment_policy_create, task_assignment_policy_approve, task_assignment_policy_revoke


class TestTaskAssignmentPolicyFrappe(unittest.TestCase):
    def setUp(self):
        if not os.environ.get('HERO_TEST_PRIVATE_SITE') or os.environ['HERO_TEST_PRIVATE_SITE'] != frappe.local.site:
            self.skipTest('Requires explicit isolated site identity')
        self.company = frappe.get_all('Company', pluck='name', limit=1)[0]
        gender = frappe.get_all('Gender', pluck='name', limit=1)[0]
        self.nonce = uuid4().hex[:12]
        self.user = frappe.get_doc({'doctype': 'User', 'email': f'task32b-policy-{self.nonce}@example.invalid', 'first_name': 'Synthetic',
                                    'enabled': 1, 'send_welcome_email': 0}).insert()
        frappe.get_doc({'doctype': 'Employee', 'first_name': 'Synthetic', 'gender': gender,
                        'date_of_birth': '1990-01-01', 'date_of_joining': '2020-01-01',
                        'status': 'Active', 'company': self.company, 'user_id': self.user.name}).insert()
        binding = BindingRegistryService(FrappeBindingStorage()).create_or_resolve_object_binding(
            site_scope=frappe.local.site, source_instance='fixture-policy32b', object_type='User',
            external_object_id='990001', target_app='frappe', target_doctype='User', target_name=self.user.name,
            mapping_definition_id='fixture-user-32b', mapping_revision=1)
        self.fixture_binding = binding.identity_key
        self.payload = {'source_instance': 'fixture-policy32b', 'issuer': 'Administrator', 'company': self.company,
                        'user_bindings': [binding.identity_key], 'allow_initial': True, 'allow_transfer': False}

    def tearDown(self):
        frappe.set_user('Administrator')
        frappe.db.rollback()
        fixture = getattr(self, '_committed_fixture', None)
        if fixture:
            task = fixture['task']
            for name in frappe.get_all('HERO Pending Reference', filters={'origin_run': fixture['run']}, pluck='name'):
                frappe.db.delete('HERO Pending Dependency', {'parent': name})
                frappe.db.delete('HERO Pending Reference', {'name': name})
            frappe.db.delete('HERO Integration Run', {'name': fixture['run']})
            frappe.db.delete('HERO Integration Record', {'target_doctype': 'Task', 'target_name': task})
            frappe.db.delete('HERO Task Assignment State', {'task_name': task})
            frappe.db.delete('ToDo', {'reference_type': 'Task', 'reference_name': task})
            frappe.db.delete('HERO Integration Link', {'source_key': 'HERO:default:Task:990003', 'target_name': task})
            frappe.db.delete('Task', {'name': task})
            frappe.db.delete('Project', {'name': fixture['project']})
            frappe.db.delete('HERO Task Assignment Policy', {'name': fixture['policy']})
            for binding in fixture['bindings']:
                frappe.db.delete('HERO Object Binding', {'name': binding})
            for doctype in ('HERO GraphQL Service', 'HERO GraphQL Mapping', 'HERO Frappe Mapping'):
                frappe.db.delete(doctype, {'schema_snapshot': fixture['snapshot']})
            frappe.db.delete('HERO GraphQL Schema Snapshot', {'name': fixture['snapshot']})
            for user in fixture['users']:
                frappe.db.delete('Employee', {'user_id': user})
                frappe.db.delete('User', {'name': user})
            frappe.db.set_single_value('System Settings', 'disable_document_sharing', fixture['sharing'])
            frappe.db.commit()

    def test_managed_create_approve_revoke_and_tampering(self):
        name = task_assignment_policy_create(json.dumps(self.payload))['name']
        policy = frappe.get_doc('HERO Task Assignment Policy', name)
        self.assertEqual(policy.state, 'DRAFT')
        policy.state = 'APPROVED'
        with self.assertRaises(frappe.PermissionError): policy.save()
        with self.assertRaises(PolicyError): task_assignment_policy_approve(name, 2)
        frappe.set_user('Guest')
        with self.assertRaises(frappe.PermissionError): task_assignment_policy_approve(name, 1)
        frappe.set_user('Administrator')
        task_assignment_policy_approve(name, 1)
        policy.reload()
        self.assertEqual((policy.state, policy.approved_revision, policy.approved_by), ('APPROVED', 1, 'Administrator'))
        from hero_integration.services.task_assignment_policy import approved_initial_policy
        frappe.db.set_value('HERO Object Binding', self.payload['user_bindings'][0], 'target_name', 'Administrator')
        with self.assertRaises(PolicyError):
            approved_initial_policy('fixture-policy32b', self.payload['user_bindings'][0])
        frappe.db.set_value('HERO Object Binding', self.payload['user_bindings'][0], 'target_name', self.user.name)
        task_assignment_policy_revoke(name, 1)
        policy.reload()
        self.assertEqual((policy.state, policy.policy_revision, policy.revoked_by), ('REVOKED', 2, 'Administrator'))
        self.assertIsNone(approved_initial_policy('fixture-policy32b', self.payload['user_bindings'][0]))
        with self.assertRaises(PolicyError): task_assignment_policy_approve(name, 1)

    def test_approved_policy_creates_native_todo_without_sharing(self):
        from hero_integration.services.migration import _migrate_native_task, _record_task_assignment_pending
        self.user.reload()
        self.user.add_roles('System Manager', 'Projects User')
        self.payload['issuer'] = self.user.name
        self.payload['native_effects'] = {'date_rule': 'LOCAL_TODAY', 'allow_notifications': False, 'allow_follow': False}
        name = task_assignment_policy_create(json.dumps(self.payload))['name']
        task_assignment_policy_approve(name, 1)
        settings = frappe.get_single('System Settings')
        settings.disable_document_sharing = 1
        settings.save()
        frappe.set_user(self.user.name)
        class Plan:
            mapping_version = 'policy-test'
            definitions = (SimpleNamespace(source_path='id'), SimpleNamespace(source_path='title'))
            def source_identity(self, record): return str(record['id'])
        scope = {'confirmed': True, 'source_instance': 'fixture-policy32b', 'source_type': 'Task'}
        run = SimpleNamespace(mode='Execute', mapping_revision=1, name=None)
        record = {'id': 990002, 'title': 'Synthetic policy task', 'target_user_id': 990001, 'author_user_id': None,
                  '_hero_source_instance': 'fixture-policy32b'}
        result = _migrate_native_task(record, run, Plan(), scope)
        frappe.set_user('Administrator')
        project = frappe.get_doc({'doctype': 'Project', 'project_name': 'policy32b-created', 'company': self.company}).insert()
        task = frappe.get_doc('Task', result.target_name); task.project = project.name; task.save()
        frappe.set_user(self.user.name)
        _record_task_assignment_pending(run, record, result.target_name, 'fixture-policy32b')
        todos = frappe.get_all('ToDo', filters={'reference_type': 'Task', 'reference_name': result.target_name,
                              'status': 'Open'}, fields=['name', 'allocated_to', 'assigned_by'])
        self.assertEqual(len(todos), 1)
        self.assertEqual((todos[0].allocated_to, todos[0].assigned_by), (self.user.name, self.user.name))
        self.assertFalse(frappe.db.exists('DocShare', {'share_doctype': 'Task', 'share_name': result.target_name, 'user': self.user.name}))
        self.assertTrue(frappe.db.exists('HERO Integration Record', {'technical_action': 'task_assignment_created',
                                                                    'target_name': result.target_name, 'correlation_id': todos[0].name}))
        _record_task_assignment_pending(run, record, result.target_name, 'fixture-policy32b')
        self.assertEqual(frappe.db.count('ToDo', {'reference_type': 'Task', 'reference_name': result.target_name, 'status': 'Open'}), 1)
        review = frappe.get_all('HERO Integration Record', filters={'technical_action': 'task_assignment_review',
                                'target_name': result.target_name}, fields=['result', 'detail'])
        self.assertEqual(len(review), 1)
        self.assertEqual((review[0].result, json.loads(review[0].detail)['decision']), ('SKIPPED', 'KEEP'))
        local = frappe.get_doc('ToDo', todos[0].name)
        local.description = 'Local instruction to the same recipient'
        local.save()
        _record_task_assignment_pending(run, record, result.target_name, 'fixture-policy32b')
        self.assertTrue(frappe.db.exists('HERO Integration Record', {'technical_action': 'task_assignment_review',
                                                                    'target_name': result.target_name, 'result': 'CONFLICT'}))

    def test_policy_does_not_grant_recipient_task_access(self):
        from hero_integration.services.migration import _migrate_native_task, _record_task_assignment_pending
        name = task_assignment_policy_create(json.dumps(self.payload))['name']
        task_assignment_policy_approve(name, 1)
        class Plan:
            mapping_version = 'policy-test'
            definitions = (SimpleNamespace(source_path='id'), SimpleNamespace(source_path='title'))
            def source_identity(self, record): return str(record['id'])
        scope = {'confirmed': True, 'source_instance': 'fixture-policy32b', 'source_type': 'Task'}
        run = SimpleNamespace(mode='Execute', mapping_revision=1, name=None)
        record = {'id': 990004, 'title': 'Synthetic restricted policy task', 'target_user_id': 990001, 'author_user_id': None,
                  '_hero_source_instance': 'fixture-policy32b'}
        task = _migrate_native_task(record, run, Plan(), scope)
        project = frappe.get_doc({'doctype': 'Project', 'project_name': 'policy32b-restricted', 'company': self.company}).insert()
        task_doc = frappe.get_doc('Task', task.target_name); task_doc.project = project.name; task_doc.save()
        _record_task_assignment_pending(run, record, task.target_name, 'fixture-policy32b')
        event = frappe.get_all('HERO Integration Record', filters={'target_name': task.target_name,
            'technical_action': 'task_assignment_review'}, fields=['detail'])
        self.assertEqual(len(event), 1)
        self.assertIn('BLOCKED_PERMISSION', event[0].detail)
        self.assertFalse(frappe.db.exists('ToDo', {'reference_type': 'Task', 'reference_name': task.target_name}))
        self.assertFalse(frappe.db.exists('DocShare', {'share_doctype': 'Task', 'share_name': task.target_name, 'user': self.user.name}))

    def test_z_registered_intake_committed_independent_readback(self):
        from hero_integration.api import graphql_mapping_save, workspace_service_save, workspace_service_validate, approve_read_service
        from hero_integration.services.migration import _process_projection
        from hero_integration.services.projection_contracts import approved_query
        from hero_integration.tests.projection_schema_fixture import insert_projection_snapshot
        self.user.reload(); self.user.add_roles('System Manager', 'Projects User')
        user_binding = BindingRegistryService(FrappeBindingStorage()).create_or_resolve_object_binding(
            site_scope=frappe.local.site, source_instance='default', object_type='User', external_object_id='990001',
            target_app='frappe', target_doctype='User', target_name=self.user.name,
            mapping_definition_id='fixture-user-default32b', mapping_revision=1)
        recipient = frappe.get_doc({'doctype': 'User', 'email': f'task32b-recipient-{self.nonce}@example.invalid',
            'first_name': 'Synthetic', 'enabled': 1, 'send_welcome_email': 0}).insert()
        gender = frappe.get_all('Gender', pluck='name', limit=1)[0]
        frappe.get_doc({'doctype': 'Employee', 'first_name': 'Synthetic', 'gender': gender,
            'date_of_birth': '1990-01-01', 'date_of_joining': '2020-01-01', 'status': 'Active',
            'company': self.company, 'user_id': recipient.name}).insert()
        recipient.reload(); recipient.add_roles('Projects User')
        second_binding = BindingRegistryService(FrappeBindingStorage()).create_or_resolve_object_binding(
            site_scope=frappe.local.site, source_instance='default', object_type='User', external_object_id='990002',
            target_app='frappe', target_doctype='User', target_name=recipient.name,
            mapping_definition_id='fixture-user-default32b-2', mapping_revision=1)
        self.payload['source_instance'] = 'default'
        self.payload['user_bindings'] = [user_binding.identity_key, second_binding.identity_key]
        self.payload['allow_transfer'] = True
        self.payload['issuer'] = self.user.name
        self.payload['native_effects'] = {'date_rule': 'LOCAL_TODAY', 'allow_notifications': True, 'allow_follow': True}
        policy = task_assignment_policy_create(json.dumps(self.payload))['name']
        task_assignment_policy_approve(policy, 1)
        settings = frappe.get_single('System Settings'); old_sharing = settings.disable_document_sharing
        settings.disable_document_sharing = 1; settings.save()
        snapshot = insert_projection_snapshot()
        rules = [
            ('id', 'int', 'HERO Integration Link', 'source_id', 'Source Identity', 'Positive Int To Text'),
            ('title', 'string|null', 'Task', 'subject', 'Record Name', 'Trim'),
            ('target_user_id', 'int|null', 'ToDo', 'allocated_to', 'Native Binding', 'Native Adapter'),
        ]
        ids = []
        for index, (path, kind, doctype, field, purpose, transform) in enumerate(rules):
            doc = frappe.get_doc({'doctype': 'HERO Frappe Mapping', 'schema_snapshot': snapshot.name,
                'operation': 'HeroTasks', 'source_path': path, 'source_type': kind, 'target_doctype': doctype,
                'target_field': field, 'purpose': purpose, 'transformation': transform,
                'definition_id': f'task32b-system-{index}', 'status': 'Active'}).insert()
            ids.append(doc.definition_id)
        config = {'target_operation': 'HeroTasks', 'records_path': 'tasks', 'identity_path': 'id', 'mapping_definition_ids': ids,
                  'variables': {'first': '$page_size', 'offset': '$offset'}, 'page_size': 2,
                  'native_task_onboarding': {'confirmed': True, 'source_instance': 'default', 'source_type': 'Task'}}
        mapping = frappe.get_doc('HERO GraphQL Mapping', graphql_mapping_save(snapshot.name, 'QUERY', 'tasks',
            'QUERY_TO_FRAPPE', json.dumps(config), activate=1)['name'])
        payload = json.dumps({'name': 'task32b policy fixture', 'operation_name': 'tasks', 'query': approved_query('HeroTasks')})
        info = workspace_service_save(snapshot.name, payload)['query']
        workspace_service_validate(snapshot.name, query_id=info['query_id'], expected_revision=info['revision'])
        service = frappe.get_doc('HERO GraphQL Service', info['query_id']); service.read_mapping = mapping.name; service.save()
        approve_read_service(service.name, mapping.name, service.service_revision, mapping.mapping_revision)

        class Run(SimpleNamespace):
            def save(self): pass
            def reload(self): pass
        class Source:
            contract = SimpleNamespace(target_operation='HeroTasks')
            source_instance = 'default'
            target = 990001
            def projection(self, offset):
                return SimpleNamespace(records=({'id': 990003, 'title': 'Synthetic registered policy task',
                    'target_user_id': self.target, 'author_user_id': None, '_hero_source_instance': 'default'},), next_offset=None)
        run_doc = frappe.get_doc({'doctype': 'HERO Integration Run', 'action': 'Projection Migration',
                                  'mode': 'Execute', 'source_instance': 'default'}).insert(ignore_permissions=True)
        run = Run(graphql_mapping=mapping.name, graphql_service=service.name, service_revision=service.service_revision,
                  mapping_revision=mapping.mapping_revision, source_instance='default', mode='Execute',
                  resume_offset=0, resume_record_index=0, state='Running', name=run_doc.name, processed=0,
                  created_records=0, conflict_records=0, skipped_records=0)
        frappe.set_user(self.user.name)
        source = Source()
        _process_projection(run, source)
        link = frappe.get_doc('HERO Integration Link', {'source_key': 'HERO:default:Task:990003'})
        frappe.set_user('Administrator')
        project = frappe.get_doc({'doctype': 'Project', 'project_name': 'task32b-registered-' + self.nonce, 'company': self.company}).insert()
        task = frappe.get_doc('Task', link.target_name); task.project = project.name; task.save()
        frappe.set_user(self.user.name)
        run.resume_record_index = 0
        _process_projection(run, source)
        task_name, recipient, binding_name = link.target_name, self.user.name, self.payload['user_bindings'][0]
        self._committed_fixture = {'task': task_name, 'project': project.name, 'policy': policy, 'run': run_doc.name,
            'bindings': [self.fixture_binding, user_binding.identity_key, second_binding.identity_key], 'snapshot': snapshot.name,
            'users': [self.user.name, second_binding.target_name], 'sharing': old_sharing}
        frappe.db.commit()
        initial = []
        def initial_readback():
            frappe.init(site=os.environ['HERO_TEST_PRIVATE_SITE']); frappe.connect(); frappe.set_user('Administrator')
            try:
                state = frappe.get_doc('HERO Task Assignment State', {'task_name': task_name})
                todo = frappe.get_doc('ToDo', state.todo)
                initial.append((state.state, state.user_binding, todo.allocated_to, todo.status))
            finally: frappe.destroy()
        thread = threading.Thread(target=initial_readback); thread.start(); thread.join(timeout=30)
        self.assertFalse(thread.is_alive()); self.assertEqual(initial, [('SYSTEM', binding_name, recipient, 'Open')])
        source.target = 990002
        run.resume_record_index = 0
        _process_projection(run, source)
        run.resume_record_index = 0
        _process_projection(run, source)
        recipient, binding_name = self.user.name, user_binding.identity_key
        frappe.db.commit()
        readback = []
        def independent():
            frappe.init(site=os.environ['HERO_TEST_PRIVATE_SITE']); frappe.connect(); frappe.set_user('Administrator')
            try:
                link2 = frappe.get_doc('HERO Integration Link', {'source_key': 'HERO:default:Task:990003'})
                todos = frappe.get_all('ToDo', filters={'reference_type': 'Task', 'reference_name': task_name, 'status': 'Open'}, fields=['name', 'allocated_to'])
                records = frappe.get_all('HERO Integration Record', filters={'target_name': task_name, 'technical_action': 'task_assignment_review'}, fields=['detail'])
                states = frappe.get_all('HERO Task Assignment State', filters={'task_name': task_name}, fields=['todo', 'user_binding', 'state'])
                readback.append((link2.target_name == task_name, frappe.db.exists('Task', task_name) is not None,
                    len(todos) == 1 and todos[0].allocated_to == recipient, any('PENDING_SOURCE_ORDER' in row.detail for row in records),
                    frappe.get_doc('HERO Task Assignment Policy', policy).state == 'APPROVED',
                    frappe.db.exists('HERO Object Binding', binding_name) is not None,
                    not frappe.db.exists('DocShare', {'share_doctype': 'Task', 'share_name': task_name, 'user': recipient}),
                    len(states) == 1 and states[0].todo == todos[0].name and states[0].user_binding == binding_name and states[0].state == 'SYSTEM'))
            finally: frappe.destroy()
        thread = threading.Thread(target=independent); thread.start(); thread.join(timeout=30)
        self.assertFalse(thread.is_alive()); self.assertEqual(readback, [(True, True, True, True, True, True, True, True)])
        print('REGISTERED_COMMITTED_READBACK task=1 todo=1 policy=approved binding=1 state=system share=0', flush=True)
