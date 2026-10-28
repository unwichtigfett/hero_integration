"""Isolated-site tests: run only with an explicitly selected private Frappe site."""
from types import SimpleNamespace
import json
import os
from unittest import TestCase
from unittest.mock import patch

import frappe

from hero_integration.services.migration import _migrate_native_task, _process_projection
from hero_integration.services.projection_contracts import CONTRACTS, approved_query
from hero_integration.tests.test_projection_contracts import definitions


class _Plan:
    mapping_version = 'fixture-v1'

    def source_identity(self, record):
        return str(record['id'])


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

    def test_remote_update_and_empty_comment_hold(self):
        created = self.apply(self.record)
        changed = self.apply({**self.record, 'title': 'Remote update'})
        self.assertEqual(changed.code, 'UPDATED')
        task = frappe.get_doc('Task', created.target_name)
        self.assertEqual(task.subject, 'Remote update')
        link = frappe.get_doc('HERO Integration Link', {'source_key': 'HERO:fixture-task32a:Task:982011'})
        old_source, old_target = link.source_field_hashes, link.target_field_hashes
        held = self.apply({**self.record, 'title': 'Remote update', 'comment': ''})
        self.assertEqual(held.code, 'CONFLICT')
        task.reload(); link.reload()
        self.assertEqual(task.description, '<p>Plain &lt;text&gt;</p>')
        self.assertEqual((link.source_field_hashes, link.target_field_hashes), (old_source, old_target))

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

    def test_registered_projection_reaches_native_task_with_saved_contract(self):
        snapshots = frappe.get_all('HERO GraphQL Schema Snapshot', filters={
            'schema_fingerprint': CONTRACTS['HeroTasks']['fingerprint'],
            'analysis_status': 'Validated', 'source_instance': 'default'}, pluck='name')
        self.assertEqual(len(snapshots), 1)
        snapshot = frappe.get_doc('HERO GraphQL Schema Snapshot', snapshots[0])
        specs = definitions('HeroTasks')
        config = {'target_operation': 'HeroTasks', 'records_path': 'tasks', 'identity_path': 'id',
                  'mapping_definition_ids': [spec.definition_id for spec in specs],
                  'variables': {'first': '$page_size', 'offset': '$offset'}, 'page_size': 2,
                  'native_task_onboarding': {'confirmed': True, 'source_instance': 'default', 'source_type': 'Task'}}
        mapping = frappe.get_doc({'doctype': 'HERO GraphQL Mapping', 'name': 'task32a-fixture-map',
            'schema_snapshot': snapshot.name, 'schema_fingerprint': snapshot.schema_fingerprint,
            'definition_id': 'task32a-fixture-map', 'source_instance': 'default', 'operation_kind': 'QUERY',
            'operation_name': 'tasks', 'direction': 'QUERY_TO_FRAPPE', 'mapping_json': json.dumps(config),
            'status': 'Active', 'mapping_revision': 1})
        mapping.db_insert()
        service = frappe.get_doc({'doctype': 'HERO GraphQL Service', 'name': 'task32a-fixture-service',
            'service_name': 'task32a fixture', 'operation_name': 'tasks', 'query_document': approved_query('HeroTasks'),
            'schema_snapshot': snapshot.name, 'schema_fingerprint': snapshot.schema_fingerprint,
            'service_revision': 1, 'read_mapping': mapping.name, 'execution_status': 'Approved',
            'approved_service_revision': 1, 'approved_mapping_revision': 1, 'status': 'Validated'})
        service.db_insert()
        for index, spec in enumerate(specs):
            frappe.get_doc({'doctype': 'HERO Frappe Mapping', 'name': f'task32a-fixture-{index}',
                'schema_snapshot': snapshot.name, 'schema_fingerprint': snapshot.schema_fingerprint,
                'operation': spec.operation, 'source_path': spec.source_path, 'source_type': spec.source_type,
                'target_doctype': spec.target_doctype, 'target_field': spec.target_field, 'purpose': spec.purpose,
                'transformation': spec.transformation, 'relation': spec.relation, 'definition_id': spec.definition_id,
                'priority': spec.priority, 'mapping_revision': 1, 'status': 'Active'}).db_insert()

        class Run(SimpleNamespace):
            def save(self): pass
            def reload(self): pass

        class Source:
            contract = SimpleNamespace(target_operation='HeroTasks')
            source_instance = 'default'
            def projection(self, offset):
                return SimpleNamespace(records=({'id': 982012, 'title': 'Full path synthetic 32A',
                    'comment': 'Test', '_hero_source_instance': 'default'},), next_offset=None)

        run = Run(graphql_mapping=mapping.name, graphql_service=service.name, service_revision=1,
            mapping_revision=1, source_instance='default', mode='Execute', resume_offset=0,
            resume_record_index=0, state='Running', name=None, processed=0, created_records=0,
            conflict_records=0, skipped_records=0)
        _process_projection(run, Source())
        link = frappe.get_doc('HERO Integration Link', {'source_key': 'HERO:default:Task:982012'})
        self.assertEqual((run.created_records, link.target_doctype), (1, 'Task'))
        self.assertEqual(frappe.get_doc('Task', link.target_name).subject, 'Full path synthetic 32A')
