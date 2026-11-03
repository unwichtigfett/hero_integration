"""Picker provenance is verified before portable and first-publication transitions."""
from copy import deepcopy
import json
import unittest

from graphql import build_schema, introspection_from_schema
from hero_integration.services import mapping_draft_exchange as exchange
from hero_integration.services.mapping_draft_service import MappingDraftError
from hero_integration.services.object_mapping_catalog import catalog
from hero_integration.tests import test_portable_draft_exchange as portable

RULE = '09a2bc52-4b21-46b7-9a11-3a62bf5a6348'


def content(schema, object_type='Customer', record_path='customers', source_path='email'):
    entry = next(item for item in catalog(schema, object_type)['items'] if item['path'] == source_path)
    return {'records_path': record_path, 'identity_path': 'id', 'target_operation': 'HeroCustomers',
        'query_adapter': {'version': 2}, 'mapping_definition_ids': [RULE],
        'field_rules': {RULE: {'source_path': source_path, 'source_type': 'string|null',
            'target_doctype': 'Customer', 'target_field': 'custom_hero_email', 'purpose': 'Field',
            'transformation': 'Trim', 'relation': '', 'priority': 5}},
        'field_contexts': {RULE: {'object_type': object_type, 'schema_context': entry['schema_context']}}}


class TestPickerRecordProvenance(unittest.TestCase):
    def test_context_is_retained_in_draft_and_stripped_only_after_validation(self):
        value = content(portable.SCHEMA)
        result = exchange._validated_record_contexts(value, 'customers', portable.SCHEMA)
        self.assertEqual(result, value)
        result = exchange._validated_record_contexts(value, 'customers', portable.SCHEMA, strip=True)
        self.assertNotIn('field_contexts', result)
        self.assertIn('field_contexts', value)
        self.assertEqual(result['field_rules'], value['field_rules'])

    def test_wrong_record_object_is_rejected_even_when_field_and_context_exist(self):
        schema = introspection_from_schema(build_schema('''
            type Query { customers: [Customer] }
            type Customer { id: ID, email: String }
            type Other { email: String }
        '''))
        value = content(schema, 'Other')
        with self.assertRaises(MappingDraftError):
            exchange._validated_record_contexts(value, 'customers', schema, strip=True)
        self.assertIn('field_contexts', value)

    def test_nested_employee_and_file_record_paths_are_validated_from_actual_record_type(self):
        schema = introspection_from_schema(build_schema('''
            type Query { company: Company!, FileUpload_FileUploads: Connection! }
            type Company { partners: [Partner!]! }
            type Partner { id: ID, email: String }
            type Connection { edges: [Edge!]! }
            type Edge { node: File! }
            type File { id: ID, email: String }
        '''))
        for root, record, typename in [('company', 'company.partners', 'Partner'),
                ('FileUpload_FileUploads', 'FileUpload_FileUploads.edges.node', 'File')]:
            value = content(schema, typename, record)
            with self.subTest(record=record):
                self.assertNotIn('field_contexts', exchange._validated_record_contexts(value, root, schema, strip=True))

    def test_modified_context_type_reference_and_unsupported_native_context_fail_closed(self):
        initial = content(portable.SCHEMA)
        changes = [lambda v: v['field_contexts'][RULE].update(schema_context='{}'),
                   lambda v: v['field_contexts'][RULE].update(extra='authority'),
                   lambda v: v.update(mapping_definition_ids=[]),
                   lambda v: v['field_rules'][RULE].update(source_type='int|null'),
                   lambda v: v.update(records_path='another.customers'),
                   lambda v: v.pop('query_adapter')]
        for change in changes:
            value = deepcopy(initial)
            change(value)
            with self.subTest(change=change), self.assertRaises(MappingDraftError):
                exchange._validated_record_contexts(value, 'customers', portable.SCHEMA, strip=True)

    def test_real_parentless_save_preserves_picker_context_export_validates_and_removes_it(self):
        fake = portable.Fake('site', 'source', 'snapshot')
        definition = {'site_scope': 'site', 'definition_id': portable.IDENTITY, 'source_instance': 'source',
            'snapshot_name': 'snapshot', 'schema_fingerprint': fake.snapshot.schema_fingerprint,
            'direction': 'QUERY_TO_FRAPPE', 'operation_kind': 'QUERY', 'operation_name': 'customers'}
        value = content(portable.SCHEMA)
        with portable.TestPortableDraftExchange().context(fake):
            draft = exchange.FrappeMappingDraftStorage().service().open_new_draft(definition, fake.session.user, value)
            self.assertEqual(draft.content['field_contexts'], value['field_contexts'])
            package = json.loads(exchange.export_package('source', 'snapshot', 'QUERY_TO_FRAPPE',
                format_version=exchange.PORTABLE_FORMAT_VERSION, drafts_only=True))
            self.assertNotIn('field_contexts', package['mappings'][0]['content'])
            self.assertEqual(package['mappings'][0]['content']['field_rules'], value['field_rules'])
            self.assertEqual(json.loads(fake.drafts[draft.key]['content_json'])['field_contexts'], value['field_contexts'])

    def test_parentless_creation_rejects_invalid_picker_context_before_persistence(self):
        fake = portable.Fake('site', 'source', 'snapshot')
        definition = {'site_scope': 'site', 'definition_id': portable.IDENTITY, 'source_instance': 'source',
            'snapshot_name': 'snapshot', 'schema_fingerprint': fake.snapshot.schema_fingerprint,
            'direction': 'QUERY_TO_FRAPPE', 'operation_kind': 'QUERY', 'operation_name': 'customers'}
        value = content(portable.SCHEMA)
        value['field_contexts'][RULE]['schema_context'] = '{}'
        with portable.TestPortableDraftExchange().context(fake), self.assertRaises(MappingDraftError):
            exchange.FrappeMappingDraftStorage().service().open_new_draft(definition, fake.session.user, value)
        self.assertEqual(fake.drafts, {})
        self.assertEqual(fake.revisions, {})


if __name__ == '__main__':
    unittest.main()
