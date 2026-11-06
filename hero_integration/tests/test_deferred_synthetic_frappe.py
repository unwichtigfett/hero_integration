"""Native deferred bindings on an explicitly selected disposable Frappe site."""
from __future__ import annotations

import os
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

import frappe

from hero_integration.services import deferred_references as deferred, migration
from hero_integration.services.direct_mapping import MappingSpec
from hero_integration.services.mapping_runtime import plan_from_definitions


class TestSyntheticDeferredMariaDB(TestCase):
    def setUp(self):
        if (not os.environ.get('HERO_TEST_PRIVATE_SITE') or os.environ['HERO_TEST_PRIVATE_SITE'] != frappe.local.site or
                os.environ.get('HERO_SYNTHETIC_BINDING_TEST') != '1'):
            self.skipTest('Requires explicitly selected synthetic Frappe site')
        frappe.set_user('Administrator')
        self.source = 'synthetic-binding-' + uuid4().hex[:10]
        self.scope = {'confirmed': True, 'source_instance': self.source, 'source_type': 'Task'}
        rules = [
            MappingSpec('HeroTasks', 'id', 'HERO Integration Link', 'source_id', 'int',
                        'Source Identity', 'Positive Int To Text', definition_id='synthetic-id'),
            MappingSpec('HeroTasks', 'title', 'Task', 'subject', 'string|null',
                        'Record Name', 'Trim', definition_id='synthetic-title'),
            MappingSpec('HeroTasks', 'target_project_match_id', 'Task', 'project', 'int|null',
                        'Project Relation', 'HERO Project Link', 'HERO Project Link', definition_id='synthetic-project'),
        ]
        self.plan = plan_from_definitions('HeroTasks', rules, target_operation='HeroTasks', adapter_options={'version': 2})
        self.row = {'id': 10001, 'title': 'Synthetic deferred task ' + uuid4().hex[:10], 'is_deleted': False,
                    'target_project_match_id': 20001, '_hero_source_instance': self.source}
        self.run = frappe.get_doc({'doctype': 'HERO Integration Run', 'action': 'Projection Migration', 'mode': 'Execute',
            'state': 'Completed', 'source_instance': self.source, 'source_binding_revision': 1}).insert()

    def tearDown(self):
        frappe.db.rollback()
        frappe.clear_messages()

    def _task_and_pending(self):
        result = migration._migrate_mapped_task(self.row, self.run, self.plan, self.scope)
        self.assertEqual(result.code, 'CREATED')
        self.assertFalse(frappe.get_doc('Task', result.target_name).project)
        with patch.object(migration, '_mapping_plan_for_run', return_value=self.plan):
            deferred.remember(self.run, 'Task', self.row, result)
        pending = deferred.key(self.source, 'HeroTasks', '10001')
        self.assertEqual(frappe.db.get_value(deferred.MAIN, pending, 'status'), 'Waiting')
        return result.target_name, pending

    def _project(self, source_id):
        project = frappe.get_doc({'doctype': 'Project', 'project_name': 'Synthetic Project ' + uuid4().hex[:10],
                                  'company': 'Synthetic Test Company'}).insert()
        frappe.get_doc({'doctype': 'HERO Integration Link', 'source_system': 'HERO', 'source_type': 'project',
            'source_id': str(source_id), 'source_instance': self.source, 'target_doctype': 'Project',
            'target_name': project.name, 'sync_status': 'Synchronized', 'source_payload_hash': 'fixture',
            'target_payload_hash': 'fixture', 'mapping_version': 'fixture'}).insert()
        return project

    def _resolve(self, pending):
        original = frappe.get_doc
        def get_doc(doctype, name=None, **kwargs):
            if doctype == 'HERO GraphQL Mapping' and not name:
                return SimpleNamespace(mapping_json='{}')
            return original(doctype, name, **kwargs) if name is not None else original(doctype, **kwargs)
        with patch('hero_integration.client.settings.get_configuration',
                   return_value=SimpleNamespace(source_instance=self.source)), \
             patch.object(migration, 'validate_run_mappings'), patch.object(migration, '_source_for_run',
                   return_value=SimpleNamespace(source_instance=self.source)), \
             patch.object(migration, '_mapping_plan_for_run', return_value=self.plan), \
             patch.object(migration, '_projection_task_scope', return_value=self.scope), \
             patch.object(frappe, 'get_doc', side_effect=get_doc):
            deferred.resolve_one(pending)

    def test_task_is_created_without_project_and_linked_after_project_arrives(self):
        task_name, pending = self._task_and_pending()
        project = self._project(20001)
        self._resolve(pending)
        self.assertEqual(frappe.get_doc('Task', task_name).project, project.name)
        self.assertEqual(frappe.db.get_value(deferred.MAIN, pending, 'status'), 'Resolved')
        self.assertFalse(frappe.db.get_value(deferred.MAIN, pending, 'record_json'))

    def test_invalid_assignee_does_not_prevent_task_import(self):
        assignment = MappingSpec('HeroTasks', 'target_user_id', 'ToDo', 'allocated_to', 'int|null',
                                 'Native Binding', 'HERO User Link', definition_id='synthetic-assignee')
        plan = plan_from_definitions('HeroTasks', [*self.plan.definitions, assignment],
                                     target_operation='HeroTasks', adapter_options={'version': 2})
        result = migration._migrate_mapped_task({**self.row, 'target_user_id': 0}, self.run, plan, self.scope)
        self.assertEqual(result.code, 'CREATED')
        self.assertTrue(frappe.db.exists('Task', result.target_name))
        self.assertTrue(frappe.db.exists('HERO Integration Record', {'run': self.run.name,
            'target_name': result.target_name, 'technical_action': 'task_assignment_review', 'result': 'PENDING_REVIEW'}))

    def test_resolving_project_does_not_hide_unresolved_task_assignee(self):
        assignment = MappingSpec('HeroTasks', 'target_user_id', 'ToDo', 'allocated_to', 'int|null',
                                 'Native Binding', 'HERO User Link', definition_id='synthetic-assignee')
        self.plan = plan_from_definitions('HeroTasks', [*self.plan.definitions, assignment],
                                          target_operation='HeroTasks', adapter_options={'version': 2})
        self.row['target_user_id'] = 0
        task_name, pending = self._task_and_pending()
        project = self._project(20001)
        self._resolve(pending)
        self.assertEqual(frappe.get_doc('Task', task_name).project, project.name)
        self.assertEqual(frappe.db.get_value(deferred.MAIN, pending, 'status'), 'Conflict')
        self.assertTrue(frappe.db.get_value(deferred.MAIN, pending, 'record_json'))

    def test_event_mapping_change_reuses_only_unchanged_field_baselines(self):
        from hero_integration.services.change_detection import encode, field_hashes

        values = {'subject': 'Synthetic event ' + uuid4().hex[:10]}
        event = frappe.get_doc({'doctype': 'Event', **values, 'event_type': 'Private',
                                'starts_on': '2026-11-05 09:00:00'}).insert()
        link = frappe.get_doc({'doctype': 'HERO Integration Link', 'source_system': 'HERO',
            'source_type': 'CalendarEvent', 'source_id': '50001', 'source_instance': self.source,
            'target_doctype': 'Event', 'target_name': event.name, 'sync_status': 'Synchronized',
            'source_payload_hash': 'fixture', 'target_payload_hash': 'fixture', 'mapping_version': 'old',
            'source_field_hashes': encode(field_hashes(values)), 'target_field_hashes': encode(field_hashes(values))}).insert()
        result = migration._upsert('CalendarEvent', '50001', {'id': 50001}, values, 'Event', 'subject',
                                   'new', self.run, source_instance=self.source, preserve_omitted=True)
        self.assertEqual(result.code, 'SKIPPED')
        link.reload()
        self.assertEqual(link.mapping_version, 'new')
        event.subject = 'Local event change'; event.save()
        result = migration._upsert('CalendarEvent', '50001', {'id': 50001}, values, 'Event', 'subject',
                                   'newer', self.run, source_instance=self.source, preserve_omitted=True)
        self.assertEqual(result.code, 'CONFLICT')
        self.assertEqual(frappe.get_doc('Event', event.name).subject, 'Local event change')
        self.assertEqual(frappe.get_doc('HERO Integration Link', link.name).mapping_version, 'new')

    def test_existing_unbound_customer_address_waits_until_exact_binding_exists(self):
        from hero_integration.services import mapped_collection_relations as collections
        from hero_integration.tests.test_mapped_collection_relations import ADDRESSES

        customer = frappe.get_doc({'doctype': 'Customer', 'customer_name': 'Synthetic customer ' + uuid4().hex[:10],
            'customer_type': 'Individual', 'customer_group': 'Commercial', 'territory': 'Germany'}).insert()
        address = frappe.get_doc({'doctype': 'Address', 'address_title': 'Synthetic secondary address',
            'address_type': 'Other', 'address_line1': 'Example 2', 'city': 'Example City',
            'pincode': '12345', 'country': 'Germany',
            'links': [{'link_doctype': 'Customer', 'link_name': customer.name}]}).insert()
        record = {'id': 777, 'address': {'id': 1}, '_hero_source_instance': self.source,
                  'customer_addresses': [{'is_deleted': False, 'address': {'id': 2, 'street': 'Example 2',
                      'city': 'Example City', 'zipcode': '12345', 'country': {'code': 'DE'}}}]}
        plan = SimpleNamespace(definitions=[], adapter_options={'native_relations': [ADDRESSES]},
                               target_doctype='Customer', mapping_version='fixture', source_identity=lambda _: '777')
        result = migration.Result('SKIPPED', 'Customer synchronized.', 'Customer', customer.name)
        actual = migration._sync_mapped_relations(plan, record, result, self.run)
        self.assertEqual(actual.code, 'SKIPPED')
        self.assertTrue(frappe.db.exists('HERO Integration Record', {'run': self.run.name,
            'technical_action': 'customer_address_review', 'target_name': customer.name, 'result': 'PENDING_REVIEW'}))
        deps = deferred.references(plan, record)
        deferred.save(self.run.name, self.source, 'HeroCustomers', '777', 'fixture', record, deps, result_code=actual.code)
        pending = deferred.key(self.source, 'HeroCustomers', '777')
        self.assertEqual(frappe.db.get_value(deferred.MAIN, pending, 'status'), 'Waiting')

        item = collections.prepare(ADDRESSES, record, 'Customer', customer.name)[0]
        values = {'address_line1': 'Example 2', 'city': 'Example City', 'pincode': '12345',
                  'country': 'Germany', 'address_type': 'Other'}
        observed = collections._observed({'collection_kind': 'customer_addresses', 'document': address, 'values': values})
        with patch.object(deferred, '_enqueue_dependency') as enqueue:
            frappe.get_doc({'doctype': 'HERO Integration Link', 'source_system': 'HERO', 'source_type': 'customer_address',
                'source_id': '2', 'source_instance': self.source, 'target_doctype': 'Address', 'target_name': address.name,
                'sync_status': 'Synchronized', 'source_payload_hash': collections._hash(item['values']),
                'target_payload_hash': collections._hash(observed), 'mapping_version': 'fixture'}).insert()
            enqueue.assert_called_once_with(self.source, 'customer_address', '2', enqueue_after_commit=True)
        with patch('hero_integration.client.settings.get_configuration', return_value=SimpleNamespace(source_instance=self.source)), \
             patch.object(migration, 'validate_run_mappings'), \
             patch.object(migration, '_source_for_run', return_value=SimpleNamespace(source_instance=self.source)), \
             patch.object(migration, '_mapping_plan_for_run', return_value=plan), \
             patch.object(migration, '_migrate_customer', side_effect=lambda row, run: migration._sync_mapped_relations(plan, row, result, run)):
            deferred.resolve_one(pending, dependency=('customer_address', '2'))
        self.assertEqual(frappe.db.get_value(deferred.MAIN, pending, 'status'), 'Resolved')
        self.assertEqual(frappe.db.count('Address', {'address_title': 'Synthetic secondary address'}), 1)

    def test_equal_secondary_source_address_reuses_bound_primary_address(self):
        from hero_integration.tests.test_mapped_collection_relations import ADDRESSES
        customer = frappe.get_doc({'doctype': 'Customer', 'customer_name': 'Synthetic alias ' + uuid4().hex[:10],
            'customer_type': 'Individual', 'customer_group': 'Commercial', 'territory': 'Germany'}).insert()
        address = frappe.get_doc({'doctype': 'Address', 'address_title': 'Synthetic alias ' + uuid4().hex[:10],
            'address_type': 'Billing', 'address_line1': 'Example 2', 'city': 'Example City',
            'pincode': '12345', 'country': 'Germany', 'is_primary_address': 1,
            'links': [{'link_doctype': 'Customer', 'link_name': customer.name}]}).insert()
        frappe.get_doc({'doctype': 'HERO Integration Link', 'source_system': 'HERO', 'source_type': 'customer_address',
            'source_id': '1', 'source_instance': self.source, 'target_doctype': 'Address', 'target_name': address.name,
            'sync_status': 'Synchronized', 'source_payload_hash': 'source', 'target_payload_hash': 'target',
            'mapping_version': 'fixture'}).insert()
        record = {'id': 777, 'address': {'id': 1}, '_hero_source_instance': self.source,
                  'customer_addresses': [{'is_deleted': False, 'address': {'id': 2, 'street': 'Example 2',
                      'city': 'Example City', 'zipcode': '12345', 'country': None}}]}
        plan = SimpleNamespace(definitions=[], adapter_options={'native_relations': [ADDRESSES]},
                               target_doctype='Customer', mapping_version='fixture', source_identity=lambda _: '777')
        result = migration.Result('SKIPPED', 'Customer synchronized.', 'Customer', customer.name)
        actual = migration._sync_mapped_relations(plan, record, result, self.run)
        self.assertFalse(actual.pending_relation)
        self.assertEqual(frappe.db.get_value('HERO Integration Link', {'source_type': 'customer_address',
            'source_instance': self.source, 'source_id': '2'}, 'target_name'), address.name)
        self.assertEqual(frappe.db.get_value('Address', address.name, 'address_type'), 'Billing')
        self.assertEqual(frappe.db.get_value('Address', address.name, 'is_primary_address'), 1)

    def test_local_project_edit_is_preserved_as_conflict(self):
        task_name, pending = self._task_and_pending()
        local = self._project(20002)
        task = frappe.get_doc('Task', task_name); task.project = local.name; task.save()
        self._project(20001)
        self._resolve(pending)
        self.assertEqual(frappe.get_doc('Task', task_name).project, local.name)
        self.assertEqual(frappe.db.get_value(deferred.MAIN, pending, 'status'), 'Conflict')

    def test_large_test_selection_uses_longtext_and_rejects_changed_content(self):
        from hero_integration.services import file_migration
        selection = {'version': 1, 'project_ids': ['20001'], 'customer_ids': [], 'user_ids': [],
                     'task_relation_hashes': {str(i): 'a' * 64 for i in range(5000)}}
        digest = file_migration._save_related_test(self.run.name, selection)
        file_migration._save_full(self.run.name, {'related_test_enabled': True, 'related_test_digest': digest})
        raw = frappe.db.get_value('HERO Integration Run', self.run.name, 'testing_cohort_json')
        self.assertGreater(len(raw.encode()), 65535)
        self.assertEqual(len(file_migration._load_related_test(self.run.name, expected_digest=digest)['task_relation_hashes']), 5000)
        frappe.db.set_value('HERO Integration Run', self.run.name, 'testing_cohort_json', raw + ' ')
        with self.assertRaisesRegex(file_migration.SourceBindingError, 'selection changed'):
            file_migration._load_related_test(self.run.name, expected_digest=digest)
