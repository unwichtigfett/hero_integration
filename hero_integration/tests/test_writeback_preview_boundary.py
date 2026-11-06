"""Offline API checks: no site, database, or outbound transport."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from hero_integration import api
from hero_integration.services.sync_engine import Eligibility
from hero_integration.services.writeback_source import SourceContext
from hero_integration.tests.test_frappe_writeback_contract import schema


class Document:
    doctype = 'Customer'
    name = 'CUST-1'
    modified = 'v1'

    def __init__(self, fields):
        self.__dict__.update(fields)

    def get(self, field):
        return self.__dict__.get(field)


class TestWritebackPreviewBoundary(unittest.TestCase):
    def setUp(self):
        mapping = {'source': {'doctype': 'Customer', 'field': 'email_id'},
                   'target': {'operation': 'update_contact', 'argument': 'contact',
                              'input_type': 'CustomerInput', 'field': 'email'},
                   'transformation': 'Identity'}
        self.row = SimpleNamespace(name='mapping', status='Active', direction='FRAPPE_TO_MUTATION',
                                   schema_snapshot='S1', schema_fingerprint='fp', source_instance='hero',
                                   mapping_json=json.dumps(mapping), mapping_revision=3)
        self.snapshot = SimpleNamespace(name='S1', schema_fingerprint='fp', source_instance='hero')
        self.link = SimpleNamespace(source_instance='hero', source_id='42', sync_status='Synchronized',
                                    source_payload_hash='abc', observed_source_hash='abc')
        from hero_integration.services.mutation_binding_contract import parse_mutation
        document = 'mutation Write($contact: CustomerInput!) { update_contact(contact: $contact) { id } }'
        self.service = SimpleNamespace(schema_snapshot='S1', schema_fingerprint='fp', query_document=document)
        self.contract = SimpleNamespace(name='contract', graphql_service='service', contract_revision=1,
                                        contract_fingerprint=parse_mutation(document).fingerprint, schema_fingerprint='fp',
                                        operation_name='update_contact')
        self.doc = Document({'email_id': None})
        self.calls = []
        self.source_proof = SourceContext('hero', 7, 'a' * 64)
        form = patch.object(api, '_form_sync_context', return_value={
            'fingerprint': 'verified', 'intent': SimpleNamespace(value_present=True, binding=None),
        })
        form.start(); self.addCleanup(form.stop)
        def get_doc(doctype, name):
            return self.doc if doctype == 'Customer' else self.service if doctype == 'HERO GraphQL Service' else self.row
        def get_all(doctype, **kwargs):
            if doctype == 'HERO Mutation Contract': return [self.contract]
            if doctype == 'HERO Integration Link': return [self.link]
            raise AssertionError(doctype)
        self.frappe = SimpleNamespace(_=lambda message: message,
            only_for=lambda role: self.calls.append(('role', role)),
            has_permission=lambda *a, **kw: self.calls.append(('permission', a[1])),
            get_doc=get_doc, get_all=get_all,
            throw=lambda message: (_ for _ in ()).throw(ValueError(message)),
        )

    def preview(self):
        with patch.object(api, 'frappe', self.frappe), patch.object(api, '_validated_snapshot', return_value=(self.snapshot, schema())), \
             patch.object(api, '_mutation_execution_state', return_value={'mutation_execution_enabled': False}), \
             patch.object(api, '_reserve_mutation') as reserve:
            result = api.writeback_preview('mapping', 'Customer', 'CUST-1')
            reserve.assert_not_called()
            return result

    def test_preview_preserves_explicit_null_and_checks_permissions(self):
        result = self.preview()
        self.assertIsNone(result['source']['value'])
        self.assertIsNone(result['hero']['value'])
        self.assertFalse(result['can_execute'])
        self.assertEqual(self.calls, [('role', 'System Manager'), ('permission', 'write')])

    def test_absent_field_fails_before_preview(self):
        self.doc = Document({})
        with self.assertRaisesRegex(ValueError, 'field.*missing'):
            self.preview()

    def test_changed_snapshot_provenance_is_rejected(self):
        for changed in (SimpleNamespace(name='S1', schema_fingerprint='new', source_instance='hero'),
                        SimpleNamespace(name='S1', schema_fingerprint='fp', source_instance='other')):
            self.snapshot = changed
            with self.subTest(snapshot=changed), self.assertRaisesRegex(ValueError, 'schema|source'):
                self.preview()

    def test_stale_active_mutation_contract_is_rejected(self):
        self.contract.schema_fingerprint = 'old'
        with self.assertRaisesRegex(ValueError, 'contract.*schema'):
            self.preview()

    def test_foreign_instance_conflict_and_changed_source_link_are_rejected(self):
        cases = ({'source_instance': 'other'}, {'sync_status': 'Conflict'}, {'observed_source_hash': 'changed'})
        for changes in cases:
            original = vars(self.link).copy()
            try:
                vars(self.link).update(changes)
                with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, 'link|source'):
                    self.preview()
            finally:
                vars(self.link).clear()
                vars(self.link).update(original)

    def test_missing_or_multiple_links_are_rejected(self):
        original_get_all = self.frappe.get_all
        for links in ([], [self.link, self.link]):
            with self.subTest(link_count=len(links)):
                self.frappe.get_all = lambda doctype, **kwargs: (
                    links if doctype == 'HERO Integration Link' else original_get_all(doctype, **kwargs)
                )
                with self.assertRaisesRegex(ValueError, 'Exactly one valid HERO Integration Link'):
                    self.preview()

    def test_changed_contract_service_and_document_are_rejected(self):
        self.service.query_document = 'mutation Other($contact: CustomerInput!) { update_contact(contact: $contact) { id } }'
        with self.assertRaisesRegex(ValueError, 'contract|service'):
            self.preview()
        self.service.query_document = 'mutation Write($contact: CustomerInput!) { update_contact(contact: $contact) { id } }'
        self.service.schema_fingerprint = 'other'
        with self.assertRaisesRegex(ValueError, 'contract|service'):
            self.preview()

    def test_non_manager_cannot_probe_mutation_gate_or_access_execute_targets(self):
        def deny(role):
            raise PermissionError(role)
        self.frappe.only_for = deny
        endpoints = (
            lambda: api.writeback_execute('mapping', 'Customer', 'CUST-1', 'fingerprint', 1),
            lambda: api.execute_graphql_service('service', '{}', 'fingerprint', 1),
        )
        with patch.object(api, 'frappe', self.frappe), \
             patch.object(api, '_mutation_execution_state') as gate, \
             patch.object(self.frappe, 'get_doc') as get_doc, \
             patch.object(api, '_reserve_mutation') as reserve:
            for call in endpoints:
                with self.subTest(endpoint=call), self.assertRaises(PermissionError):
                    call()
            gate.assert_not_called()
            get_doc.assert_not_called()
            reserve.assert_not_called()

    def test_disabled_global_gate_blocks_execute_before_document_or_audit_access(self):
        endpoints = (
            lambda: api.writeback_execute('mapping', 'Customer', 'CUST-1', 'fingerprint', 1),
            lambda: api.execute_graphql_service('service', '{}', 'fingerprint', 1),
        )
        with patch.object(api, 'frappe', self.frappe), \
             patch.object(api, '_mutation_execution_state', return_value={'mutation_execution_enabled': False}), \
             patch.object(self.frappe, 'get_doc') as get_doc, \
             patch.object(api, '_reserve_mutation') as reserve:
            for call in endpoints:
                with self.subTest(endpoint=call), self.assertRaisesRegex(ValueError, api.MUTATION_EXECUTION_DISABLED):
                    call()
            get_doc.assert_not_called()
            reserve.assert_not_called()
        self.assertEqual(self.calls, [('role', 'System Manager'), ('role', 'System Manager')])

    def test_execute_missing_field_fails_before_reservation_or_transport(self):
        self.doc = Document({})
        with patch.object(api, 'frappe', self.frappe), patch.object(api, '_validated_snapshot', return_value=(self.snapshot, schema())), \
             patch.object(api, '_assert_mutation_execution_released'), patch.object(api, '_reserve_mutation') as reserve:
            with self.assertRaisesRegex(ValueError, 'field.*missing'):
                api.writeback_execute('mapping', 'Customer', 'CUST-1', 'fingerprint', 1)
            reserve.assert_not_called()

    def test_writeback_requires_current_fingerprint_and_confirmation_before_reservation(self):
        for fingerprint, confirmed in (('current', 0), ('stale', 1), ('', 1)):
            with self.subTest(fingerprint=fingerprint, confirmed=confirmed), \
                 patch.object(api, 'frappe', self.frappe), \
                 patch.object(api, '_assert_mutation_execution_released'), \
                 patch.object(api, '_writeback_context', return_value={'fingerprint': 'current'}), \
                 patch.object(api, '_reserve_mutation') as reserve:
                with self.assertRaisesRegex(ValueError, 'confirmation for this exact writeback preview'):
                    api.writeback_execute('mapping', 'Customer', 'CUST-1', fingerprint, confirmed)
                reserve.assert_not_called()

    def test_failed_writeback_reservation_never_constructs_transport(self):
        context = {'fingerprint': 'current', 'verified_context': {'intent': SimpleNamespace(binding=None)}, 'mapping': {'target': {'operation': 'update_contact'}},
                   'snapshot': self.snapshot, 'mapping_row': self.row}
        with patch.object(api, 'frappe', self.frappe), \
             patch.object(api, '_assert_mutation_execution_released'), \
             patch.object(api, '_writeback_context', return_value=context), \
             patch.object(api, '_reserve_mutation', side_effect=RuntimeError('synthetic reservation failure')), \
             patch('hero_integration.services.sync_engine.sync_hero_eligibility', return_value=Eligibility.LOCAL_CHANGED), \
             patch.object(api, '_finalize_mutation') as finalize, \
             patch('hero_integration.client.graphql_mutation.HeroMutationClient') as transport:
            with self.assertRaisesRegex(RuntimeError, 'synthetic reservation failure'):
                api.writeback_execute('mapping', 'Customer', 'CUST-1', 'current', 1)
        transport.assert_not_called()
        finalize.assert_not_called()

    def sync_context(self):
        from hero_integration.services import binding_registry, sync_runtime_adapter
        binding = SimpleNamespace(object_binding_key='object', source_instance='hero', mapping_revision=3,
                                  target_doctype='Customer', target_field='email_id', target_name='CUST-1',
                                  identity_key='binding', revision=2)
        parent = SimpleNamespace(source_instance='hero', external_object_id='42', lifecycle='VALID', reconciliation='')
        storage = SimpleNamespace(field=lambda *a: binding, object=lambda *a: parent)
        selection = SimpleNamespace(mapping_revision=3, schema_snapshot='S1', schema_fingerprint='fp')
        plan = SimpleNamespace(selection=selection, plan_key='plan', generation=4)
        service = SimpleNamespace(resolve_executable_plan=lambda *a: plan)
        self.frappe.local = SimpleNamespace(site='test.local')
        self.frappe.session = SimpleNamespace(user='owner')
        with patch.object(api, 'frappe', self.frappe), patch.object(api, '_validated_snapshot', return_value=(self.snapshot, schema())), \
             patch.object(binding_registry, 'FrappeBindingStorage', return_value=storage), \
             patch.object(binding_registry, 'BindingRegistryService'), \
             patch.object(sync_runtime_adapter, '_runtime_plan_service', return_value=service), \
             patch('hero_integration.services.writeback_readback.prepare_readback', return_value=object()), \
             patch('hero_integration.services.writeback_source.capture_source', return_value=self.source_proof):
            return api._sync_hero_context('binding', 'mapping', 4)

    def test_runtime_plan_path_rejects_missing_and_stale_contract(self):
        self.doc = Document({})
        with self.assertRaisesRegex(ValueError, 'field.*missing'):
            self.sync_context()
        self.doc = Document({'email_id': 'value'})
        self.contract.schema_fingerprint = 'old'
        with self.assertRaisesRegex(ValueError, 'contract.*schema'):
            self.sync_context()

    def test_runtime_plan_confirmation_changes_when_contract_revision_changes(self):
        self.doc = Document({'email_id': 'value'})
        first = self.sync_context()['fingerprint']
        self.contract.contract_revision = 2
        self.assertNotEqual(first, self.sync_context()['fingerprint'])

    def test_non_admin_is_denied_before_document_access(self):
        self.frappe.only_for = lambda role: (_ for _ in ()).throw(PermissionError(role))
        self.frappe.get_doc = lambda *a: self.fail('unauthorized document access')
        with self.assertRaises(PermissionError):
            self.preview()

    def test_source_revision_and_endpoint_changes_invalidate_the_preview(self):
        from dataclasses import replace
        first = self.sync_context()['fingerprint']
        self.source_proof = replace(self.source_proof, binding_revision=8)
        self.assertNotEqual(first, self.sync_context()['fingerprint'])
        self.source_proof = replace(self.source_proof, binding_revision=7, endpoint_fingerprint='b'*64)
        self.assertNotEqual(first, self.sync_context()['fingerprint'])


class TestTypedMutationPreview(unittest.TestCase):
    def setUp(self):
        from hero_integration.services.mutation_binding_contract import parse_mutation
        from hero_integration.tests.test_graphql_contract import SCHEMA
        document = 'mutation Change($input: CustomerInput!) { changeCustomer(input: $input) { id } }'
        self.service = SimpleNamespace(name='service', schema_snapshot='S1', schema_fingerprint='fp',
                                       operation_name='changeCustomer', query_document=document)
        self.contract = SimpleNamespace(graphql_service='service', schema_snapshot='S1', schema_fingerprint='fp',
                                        contract_fingerprint=parse_mutation(document).fingerprint,
                                        operation_name='changeCustomer', contract_revision=2,
                                        mapping_json='{"mode":"ADMIN_INPUT"}', status='Draft')
        self.snapshot = SimpleNamespace(name='S1', schema_fingerprint='fp', snapshot_json=json.dumps(SCHEMA),
                                        analysis_status='Validated', execution_contract_status='Ready')
        docs = {'HERO Mutation Contract': self.contract, 'HERO GraphQL Service': self.service,
                'HERO GraphQL Schema Snapshot': self.snapshot}
        self.frappe = SimpleNamespace(_=lambda message: message, only_for=lambda role: None, get_doc=lambda doctype, name: docs[doctype],
                                      throw=lambda message: (_ for _ in ()).throw(ValueError(message)))

    def preview(self, variables):
        with patch.object(api, 'frappe', self.frappe), \
             patch.object(api, '_mutation_execution_state', return_value={'mutation_execution_enabled': False}), \
             patch.object(api, '_reserve_mutation') as reserve:
            result = api.workspace_mutation_preview('contract', json.dumps(variables))
            reserve.assert_not_called()
            return result

    def test_typed_valid_preview_does_not_execute(self):
        result = self.preview({'input': {'name': 'Ada', 'state': 'ACTIVE'}})
        self.assertFalse(result['can_execute'])
        self.assertEqual(result['variables_validated'], ['input'])
        self.assertNotIn('Ada', json.dumps(result))

    def test_optional_document_default_is_fingerprinted_and_validated_as_effective_value(self):
        from hero_integration.services.mutation_binding_contract import parse_mutation
        from hero_integration.tests.test_graphql_contract import optional_default_schema
        schema = optional_default_schema()
        self.snapshot.snapshot_json = json.dumps(schema)
        document = "mutation Change($value: Int = 7) { changeCustomer(value: $value) }"
        self.service.query_document = document
        self.contract.contract_fingerprint = parse_mutation(document).fingerprint
        first = self.preview({})
        self.assertEqual(first["variables_validated"], ["value"])
        self.assertNotIn("value", first)
        self.assertNotIn(document, json.dumps(first))
        changed = document.replace("= 7", "= 8")
        self.service.query_document = changed
        self.contract.contract_fingerprint = parse_mutation(changed).fingerprint
        second = self.preview({})
        self.assertNotEqual(first["request_fingerprint"], second["request_fingerprint"])

    def test_unknown_and_missing_required_input_rejected(self):
        for value in ({'input': {'name': 'Ada', 'unknown': 'x'}}, {'input': {'state': 'ACTIVE'}}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.preview(value)


class TestMutationReservationBoundary(unittest.TestCase):
    def test_duplicate_active_reservation_rejects_without_commit(self):
        from unittest.mock import Mock

        class DuplicateEntryError(Exception):
            pass

        audit = SimpleNamespace(insert=Mock(side_effect=DuplicateEntryError()))
        commit = Mock()

        def throw(message):
            raise ValueError(message)

        fake = SimpleNamespace(_=lambda message: message,
            DuplicateEntryError=DuplicateEntryError,
            session=SimpleNamespace(user='synthetic'), local=SimpleNamespace(site='test.local'),
            db=SimpleNamespace(commit=commit), get_doc=Mock(return_value=audit), throw=throw,
        )
        with patch.object(api, 'frappe', fake):
            with self.assertRaisesRegex(ValueError, 'mutation is currently in progress'):
                api._reserve_mutation(operation_name='update', schema_fingerprint='synthetic',
                                      mapping_revision=1, request_fingerprint='synthetic')
        audit.insert.assert_called_once_with(ignore_permissions=True)
        commit.assert_not_called()


class TestWritebackActionDiscovery(unittest.TestCase):
    def setUp(self):
        self.messages = ['existing message']
        self.flags = SimpleNamespace(mute_messages=False)
        self.document = SimpleNamespace(doctype='Customer', name='CUST-1')
        def throw(message):
            if not self.flags.mute_messages:
                self.messages.append(message)
            raise ValueError(message)
        self.frappe = SimpleNamespace(only_for=lambda role: None, has_permission=lambda *a, **kw: True,
            get_doc=lambda *a: self.document, flags=self.flags, throw=throw, _=lambda text: text)

    def row(self, name, content):
        return SimpleNamespace(name=name, mapping_json=json.dumps(content))

    def available(self, rows, context):
        self.frappe.get_all = lambda *a, **kw: rows
        with patch.object(api, 'frappe', self.frappe), patch.object(api, '_writeback_context', side_effect=context) as check:
            result = api.writeback_available('Customer', 'CUST-1')
        return result, check

    def test_configured_profiles_foreign_doctypes_and_malformed_json_do_not_enter_legacy_context(self):
        rows = [self.row('new', {'usage': 'MAPPED_UPDATE_V2', 'source': {'doctype': 'Customer'}, 'assignments': []}),
                self.row('send', {'usage': 'SEND_TO_HERO', 'source': {'doctype': 'Customer', 'field': 'customer_name'}}),
                self.row('foreign', {'source': {'doctype': 'Project', 'field': 'notes'}}),
                self.row('array', []), self.row('invalid-source', {'source': []}),
                SimpleNamespace(name='invalid-json', mapping_json='{'),
                SimpleNamespace(name='duplicate-keys', mapping_json='{"source":{},"source":{"doctype":"Customer"}}')]
        result, check = self.available(rows, AssertionError('Wrong dispatcher'))
        check.assert_not_called()
        self.assertEqual(result, {'actions': []})
        self.assertEqual(self.messages, ['existing message'])
        self.assertFalse(self.flags.mute_messages)

    def test_stale_legacy_action_cannot_queue_dialogs_and_valid_action_remains_available(self):
        content = {'source': {'doctype': 'Customer', 'field': 'email_id'},
                   'target': {'operation': 'update_contact', 'field': 'email'}}
        def context(name, document, **kw):
            if name == 'stale':
                self.frappe.throw('The active writeback mapping is no longer safe.')
            return {'mapping': content}
        for previous in (False, True):
            self.flags.mute_messages = previous
            result, check = self.available([self.row('stale', content), self.row('valid', content)], context)
            self.assertEqual(check.call_count, 2)
            self.assertEqual([row['mapping_name'] for row in result['actions']], ['valid'])
            self.assertEqual(self.messages, ['existing message'])
            self.assertIs(self.flags.mute_messages, previous)

    def test_explicit_preview_still_reports_validation_errors(self):
        with patch.object(api, 'frappe', self.frappe), \
             patch.object(api, '_writeback_context', side_effect=lambda *a, **kw: self.frappe.throw('Validation blocked')):
            with self.assertRaisesRegex(ValueError, 'Validation blocked'):
                api.writeback_preview('stale', 'Customer', 'CUST-1')
        self.assertEqual(self.messages, ['existing message', 'Validation blocked'])
        self.assertFalse(self.flags.mute_messages)

    def test_discovery_permission_failure_is_not_muted_or_hidden(self):
        def deny(role):
            self.frappe.throw('Permission denied')
        self.frappe.only_for = deny
        with patch.object(api, 'frappe', self.frappe), patch.object(api, '_writeback_context') as check:
            with self.assertRaisesRegex(ValueError, 'Permission denied'):
                api.writeback_available('Customer', 'CUST-1')
        check.assert_not_called()
        self.assertEqual(self.messages, ['existing message', 'Permission denied'])
