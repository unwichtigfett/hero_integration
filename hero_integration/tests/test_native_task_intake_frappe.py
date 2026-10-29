"""Isolated-site tests: run only with an explicitly selected private Frappe site."""
from types import SimpleNamespace
from contextlib import nullcontext
import json
import os
import threading
from unittest import TestCase
from unittest.mock import patch

import frappe

from hero_integration.services.migration import _migrate_native_task, _process_projection
from hero_integration.services.projection_contracts import CONTRACTS, approved_query
from hero_integration.services.direct_mapping import MappingSpec


class _Plan:
    mapping_version = 'fixture-v1'
    definitions = [SimpleNamespace(source_path=path) for path in ('id', 'title', 'comment')]

    def source_identity(self, record):
        return str(record['id'])


class TestNativeTaskEmptyTitleOffline(TestCase):
    def test_empty_title_keeps_subject_and_updates_independent_description(self):
        from hero_integration.services import migration
        from hero_integration.services.change_detection import encode, decode, field_hashes
        from hero_integration.hero_integration.doctype.hero_frappe_mapping import hero_frappe_mapping

        values = {'subject': 'Local title', 'description': '<p>Old</p>'}
        baseline = field_hashes(values)
        class Task:
            name = 'TASK-SYNTHETIC'
            def get(self, field): return values.get(field)
            def set(self, field, value): values[field] = value
            def save(self): pass
        link = SimpleNamespace(name='LINK', target_doctype='Task', target_name='TASK-SYNTHETIC',
            mapping_version='native-task-v1:1:fixture-v1', source_field_hashes=encode(baseline),
            target_field_hashes=encode(baseline), sync_status='Synchronized', conflict_detail='', save=lambda: None)
        fake_db = SimpleNamespace(sql=lambda query, *args, **kwargs: [SimpleNamespace(name='LINK')]
                                  if 'HERO Integration Link' in query else [], exists=lambda *args: True)
        fake = SimpleNamespace(only_for=lambda role: None, conf=SimpleNamespace(db_name='synthetic'), db=fake_db,
            get_doc=lambda doctype, *args, **kwargs: link if doctype == 'HERO Integration Link' else Task())
        record = {'id': 7, '_hero_source_instance': 'fixture-task32a', 'title': '  ', 'comment': 'New'}
        scope = {'confirmed': True, 'source_instance': 'fixture-task32a', 'source_type': 'Task'}
        with patch.object(migration, 'frappe', fake), patch.object(hero_frappe_mapping, '_lock_active_target'), \
             patch.object(migration, 'managed_target_update', return_value=nullcontext()):
            result = _migrate_native_task(record, SimpleNamespace(mapping_revision=1, mode='Execute'), _Plan(), scope)
        self.assertEqual(result.code, 'CONFLICT')
        self.assertEqual(values, {'subject': 'Local title', 'description': '<p>New</p>'})
        self.assertEqual(decode(link.source_field_hashes)['subject'], baseline['subject'])
        self.assertIn('subject', link.conflict_detail)


class TestNativeTaskIntake(TestCase):
    def setUp(self):
        if not os.environ.get('HERO_TEST_PRIVATE_SITE') or os.environ['HERO_TEST_PRIVATE_SITE'] != frappe.local.site:
            self.skipTest('Requires explicit isolated site identity')
        self.run = SimpleNamespace(mapping_revision=1, mode='Execute')
        self.scope = {'confirmed': True, 'source_instance': 'fixture-task32a', 'source_type': 'Task'}
        self.record = {'id': 982011, '_hero_source_instance': 'fixture-task32a',
                       'title': 'Synthetic Task 32A fixture', 'comment': 'Plain <text>'}

    def tearDown(self):
        frappe.db.rollback()

    def apply(self, record):
        return _migrate_native_task(record, self.run, _Plan(), self.scope)

    def test_replay_local_conflict_and_omitted_fields(self):
        created = self.apply(self.record)
        self.assertEqual(created.code, 'CREATED')
        task = frappe.get_doc('Task', created.target_name)
        self.assertEqual(task.description, '<p>Plain &lt;text&gt;</p>')
        link = frappe.get_doc('HERO Integration Link', {'source_key': 'HERO:fixture-task32a:Task:982011'})
        baseline = (link.source_field_hashes, link.target_field_hashes)
        self.assertEqual(self.apply(self.record).target_name, task.name)
        task.subject = 'Local edit'
        task.save()
        changed = self.apply({**self.record, 'title': 'Remote edit'})
        self.assertEqual(changed.code, 'CONFLICT')
        task.reload(); link.reload()
        self.assertEqual(task.subject, 'Local edit')
        self.assertEqual((link.source_field_hashes, link.target_field_hashes), baseline)
        self.assertEqual(self.apply({'id': 982011, '_hero_source_instance': 'fixture-task32a'}).code, 'CONFLICT')
        task.reload(); link.reload()
        self.assertEqual(task.subject, 'Local edit')
        self.assertEqual((link.source_field_hashes, link.target_field_hashes), baseline)

    def test_link_failure_rolls_back_task(self):
        original = frappe.get_doc

        def inject(*args, **kwargs):
            if args and isinstance(args[0], dict) and args[0].get('doctype') == 'HERO Integration Link':
                raise RuntimeError('injected link failure')
            return original(*args, **kwargs)

        with patch.object(frappe, 'get_doc', side_effect=inject):
            with self.assertRaisesRegex(RuntimeError, 'injected link failure'):
                self.apply(self.record)
        self.assertFalse(frappe.db.exists('Task', {'subject': self.record['title']}))
        self.assertFalse(frappe.db.exists('HERO Integration Link', {'source_key': 'HERO:fixture-task32a:Task:982011'}))

    def test_unselected_description_is_never_written_or_marked_pending(self):
        plan = _Plan()
        plan.definitions = [SimpleNamespace(source_path=path) for path in ('id', 'title')]
        created = _migrate_native_task(self.record, self.run, plan, self.scope)
        task = frappe.get_doc('Task', created.target_name)
        self.assertFalse(task.description)
        link = frappe.get_doc('HERO Integration Link', {'source_key': 'HERO:fixture-task32a:Task:982011'})
        self.assertEqual(link.sync_status, 'Synchronized')
        task.description = 'Local description'
        task.save()
        result = _migrate_native_task({**self.record, 'comment': None}, self.run, plan, self.scope)
        task.reload(); link.reload()
        self.assertEqual(task.description, 'Local description')
        self.assertEqual(link.sync_status, 'Synchronized')
        self.assertNotEqual(result.code, 'CONFLICT')

    def test_remote_update_and_empty_comment_hold(self):
        created = self.apply(self.record)
        changed = self.apply({**self.record, 'title': 'Remote update'})
        self.assertEqual(changed.code, 'UPDATED')
        task = frappe.get_doc('Task', created.target_name)
        self.assertEqual(task.subject, 'Remote update')
        link = frappe.get_doc('HERO Integration Link', {'source_key': 'HERO:fixture-task32a:Task:982011'})
        old_source, old_target = link.source_field_hashes, link.target_field_hashes
        self.run.mode = 'Preview'
        self.assertEqual(self.apply({**self.record, 'title': 'Remote update', 'comment': ''}).code, 'CONFLICT')
        self.run.mode = 'Execute'
        held = self.apply({**self.record, 'title': 'Remote update', 'comment': ''})
        self.assertEqual(held.code, 'CONFLICT')
        task.reload(); link.reload()
        self.assertEqual(task.description, '<p>Plain &lt;text&gt;</p>')
        self.assertEqual((link.source_field_hashes, link.target_field_hashes), (old_source, old_target))

    def test_empty_title_preserves_subject_but_does_not_block_safe_description_update(self):
        created = self.apply(self.record)
        task = frappe.get_doc('Task', created.target_name)
        link = frappe.get_doc('HERO Integration Link', {'source_key': 'HERO:fixture-task32a:Task:982011'})
        from hero_integration.services.change_detection import decode
        before_subject = decode(link.source_field_hashes)['subject']
        result = self.apply({**self.record, 'title': '  ', 'comment': 'Safe remote update'})
        self.assertEqual(result.code, 'CONFLICT')
        task.reload(); link.reload()
        self.assertEqual(task.subject, self.record['title'])
        self.assertEqual(task.description, '<p>Safe remote update</p>')
        self.assertEqual(decode(link.source_field_hashes)['subject'], before_subject)
        self.assertIn('subject', link.conflict_detail)

    def test_scope_and_mapping_revision_are_enforced(self):
        created = self.apply(self.record)
        self.run.mapping_revision = 2
        self.assertEqual(self.apply({**self.record, 'title': 'Unsafe change'}).code, 'CONFLICT')
        self.assertEqual(frappe.get_doc('Task', created.target_name).subject, self.record['title'])
        self.scope['source_instance'] = 'other-source'
        with self.assertRaises(ValueError):
            self.apply(self.record)

    def test_non_manager_cannot_onboard(self):
        frappe.set_user('Guest')
        try:
            with self.assertRaises(frappe.PermissionError):
                self.apply(self.record)
        finally:
            frappe.set_user('Administrator')
        self.assertFalse(frappe.db.exists('HERO Integration Link', {'source_key': 'HERO:fixture-task32a:Task:982011'}))

    def test_optional_comment_rule_is_a_valid_native_binding(self):
        snapshot = frappe.get_all('HERO GraphQL Schema Snapshot', filters={
            'schema_fingerprint': CONTRACTS['HeroTasks']['fingerprint'],
            'analysis_status': 'Validated', 'source_instance': 'default'}, pluck='name')[0]
        rule = frappe.get_doc({'doctype': 'HERO Frappe Mapping', 'schema_snapshot': snapshot,
            'operation': 'HeroTasks', 'source_path': 'comment', 'source_type': 'string|null',
            'target_doctype': 'Task', 'target_field': 'description', 'purpose': 'Native Binding',
            'transformation': 'Native Adapter', 'definition_id': 'task32a-native-comment',
            'priority': 5, 'status': 'Active'}).insert()
        self.assertEqual(rule.status, 'Active')

    def test_registered_projection_reaches_native_task_with_saved_contract(self):
        snapshots = frappe.get_all('HERO GraphQL Schema Snapshot', filters={
            'schema_fingerprint': CONTRACTS['HeroTasks']['fingerprint'],
            'analysis_status': 'Validated', 'source_instance': 'default'}, pluck='name')
        self.assertEqual(len(snapshots), 1)
        snapshot = frappe.get_doc('HERO GraphQL Schema Snapshot', snapshots[0])
        from hero_integration.api import graphql_mapping_save, workspace_service_save, workspace_service_validate, approve_read_service
        specs = [
            MappingSpec('HeroTasks', 'id', 'HERO Integration Link', 'source_id', 'int', 'Source Identity', 'Positive Int To Text', definition_id='task32a-native-id'),
            MappingSpec('HeroTasks', 'title', 'Task', 'subject', 'string|null', 'Record Name', 'Trim', definition_id='task32a-native-title'),
        ]
        for spec in specs:
            frappe.get_doc({'doctype': 'HERO Frappe Mapping', 'schema_snapshot': snapshot.name,
                'operation': spec.operation, 'source_path': spec.source_path, 'source_type': spec.source_type,
                'target_doctype': spec.target_doctype, 'target_field': spec.target_field, 'purpose': spec.purpose,
                'transformation': spec.transformation, 'relation': spec.relation, 'definition_id': spec.definition_id,
                'priority': spec.priority, 'status': 'Active'}).insert()
        config = {'target_operation': 'HeroTasks', 'records_path': 'tasks', 'identity_path': 'id',
                  'mapping_definition_ids': [spec.definition_id for spec in specs],
                  'variables': {'first': '$page_size', 'offset': '$offset'}, 'page_size': 2,
                  'native_task_onboarding': {'confirmed': True, 'source_instance': 'default', 'source_type': 'Task'}}
        saved = graphql_mapping_save(snapshot.name, 'QUERY', 'tasks', 'QUERY_TO_FRAPPE', json.dumps(config), activate=1)
        mapping = frappe.get_doc('HERO GraphQL Mapping', saved['name'])
        payload = json.dumps({'name': 'task32a fixture', 'operation_name': 'tasks', 'query': approved_query('HeroTasks')})
        service_info = workspace_service_save(snapshot.name, payload)['query']
        workspace_service_validate(snapshot.name, query_id=service_info['query_id'], expected_revision=service_info['revision'])
        service = frappe.get_doc('HERO GraphQL Service', service_info['query_id'])
        service.read_mapping = mapping.name
        service.save()
        frappe.set_user('Guest')
        try:
            with self.assertRaises(frappe.PermissionError):
                approve_read_service(service.name, mapping.name, service.service_revision, mapping.mapping_revision)
        finally:
            frappe.set_user('Administrator')
        with self.assertRaises(ValueError):
            from hero_integration.services.read_contract import from_documents
            from_documents(service, mapping, snapshot)
        incomplete = {**config, 'mapping_definition_ids': [specs[0].definition_id]}
        rejected = graphql_mapping_save(snapshot.name, 'QUERY', 'tasks', 'QUERY_TO_FRAPPE', json.dumps(incomplete), activate=1)
        with self.assertRaises(ValueError):
            approve_read_service(service.name, rejected['name'], service.service_revision, rejected['revision'])
        service.reload()
        self.assertNotEqual(service.execution_status, 'Approved')
        approval = approve_read_service(service.name, mapping.name, service.service_revision, mapping.mapping_revision)
        self.assertEqual(approval['status'], 'Approved')
        service.reload()
        self.assertEqual(service.execution_status, 'Approved')
        print('NORMAL_APPROVAL native_rules=2 project_relation=absent service=Approved', flush=True)

        class Run(SimpleNamespace):
            def save(self): pass
            def reload(self): pass

        class Source:
            contract = SimpleNamespace(target_operation='HeroTasks')
            source_instance = 'default'
            row = {'id': 982012, 'title': 'Full path synthetic 32A',
                   'comment': 'Test', '_hero_source_instance': 'default'}
            def projection(self, offset):
                return SimpleNamespace(records=(self.row,), next_offset=None)

        run = Run(graphql_mapping=mapping.name, graphql_service=service.name, service_revision=1,
            mapping_revision=1, source_instance='default', mode='Execute', resume_offset=0,
            resume_record_index=0, state='Running', name=None, processed=0, created_records=0,
            conflict_records=0, skipped_records=0)
        from hero_integration.services.migration import _mapping_plan_for_run
        identity_rule = frappe.db.get_value('HERO Frappe Mapping', {'definition_id': specs[0].definition_id}, 'name')
        frappe.db.set_value('HERO Frappe Mapping', identity_rule, 'schema_fingerprint', '0' * 64)
        with self.assertRaises(ValueError):
            _mapping_plan_for_run(run, 'HeroTasks')
        frappe.db.set_value('HERO Frappe Mapping', identity_rule, 'schema_fingerprint', snapshot.schema_fingerprint)
        source = Source()
        _process_projection(run, source)
        link = frappe.get_doc('HERO Integration Link', {'source_key': 'HERO:default:Task:982012'})
        self.assertEqual((run.created_records, link.target_doctype), (1, 'Task'))
        self.assertEqual(frappe.get_doc('Task', link.target_name).subject, 'Full path synthetic 32A')
        self.assertFalse(frappe.get_doc('Task', link.target_name).description)
        task_name = link.target_name
        baseline = (link.source_field_hashes, link.target_field_hashes)
        run.resume_record_index = 0
        _process_projection(run, source)
        self.assertEqual(frappe.db.count('HERO Integration Link', {'source_key': 'HERO:default:Task:982012'}), 1)
        task = frappe.get_doc('Task', task_name); task.subject = 'Local protected text'; task.save()
        source.row = {**source.row, 'title': 'Remote conflicting text'}
        run.resume_record_index = 0
        _process_projection(run, source)
        source.row = {'id': 982012, '_hero_source_instance': 'default'}
        run.resume_record_index = 0
        _process_projection(run, source)
        link.reload()
        self.assertEqual((link.source_field_hashes, link.target_field_hashes), baseline)
        self.assertEqual(frappe.get_doc('Task', task_name).subject, 'Local protected text')
        frappe.db.commit()
        readback = []
        def independent():
            frappe.init(site=os.environ['HERO_TEST_PRIVATE_SITE']); frappe.connect(); frappe.set_user('Administrator')
            try:
                saved = frappe.get_doc('HERO Integration Link', {'source_key': 'HERO:default:Task:982012'})
                readback.append((saved.target_name, frappe.get_doc('Task', saved.target_name).subject,
                                 saved.source_field_hashes, saved.target_field_hashes))
            finally: frappe.destroy()
        thread = threading.Thread(target=independent); thread.start(); thread.join(timeout=20)
        self.assertFalse(thread.is_alive())
        self.assertEqual(readback, [(task_name, 'Local protected text', *baseline)])
        source.row = {'id': 982013, 'title': 'Registered rollback 33', '_hero_source_instance': 'default'}
        run.resume_record_index = 0
        original = frappe.get_doc
        def inject(*args, **kwargs):
            if args and isinstance(args[0], dict) and args[0].get('doctype') == 'HERO Integration Link':
                raise RuntimeError('registered link failure')
            return original(*args, **kwargs)
        with patch.object(frappe, 'get_doc', side_effect=inject):
            with self.assertRaisesRegex(RuntimeError, 'registered link failure'):
                _process_projection(run, source)
        self.assertFalse(frappe.db.exists('HERO Integration Link', {'source_key': 'HERO:default:Task:982013'}))
        self.assertFalse(frappe.db.exists('Task', {'subject': 'Registered rollback 33'}))
        print('REGISTERED_REPLAY_CONFLICT_OMISSION_READBACK_ROLLBACK=PASS', flush=True)
