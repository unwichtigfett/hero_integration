"""Portable Query rules resolve fields relative to their nested record path."""
import unittest
from graphql import build_schema, introspection_from_schema
from hero_integration.services.mapping_draft_exchange import MappingDraftExchangeError, _validate_portable_query


class TestPortableQueryRecordPath(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = introspection_from_schema(build_schema('''
            type Partner { id: ID!, first_name: String }
            type Company { partners: [Partner!]! }
            type Edge { node: Partner! }
            type Connection { edges: [Edge!]! }
            type Query { company: Company!, uploads: Connection!, contacts: [Partner!]! }
        '''))

    def content(self, record_path, path='first_name', source_type='string|null'):
        return {'query_adapter': {'version': 2}, 'records_path': record_path,
                'field_rules': {'rule': {'source_path': path, 'source_type': source_type}}}

    def test_employee_company_partners_and_file_connection_fields_are_record_relative(self):
        for operation, path in (('company', 'company.partners'), ('uploads', 'uploads.edges.node'),
                                ('contacts', 'contacts')):
            with self.subTest(operation=operation):
                _validate_portable_query(self.content(path), operation, self.schema)
                _validate_portable_query(self.content(path, 'id', 'string'), operation, self.schema)

    def test_nested_field_type_is_not_silently_coerced(self):
        with self.assertRaisesRegex(MappingDraftExchangeError, 'field type differs'):
            _validate_portable_query(self.content('company.partners', source_type='int'), 'company', self.schema)

    def test_record_path_cannot_switch_to_another_operation(self):
        with self.assertRaisesRegex(MappingDraftExchangeError, 'record path does not match'):
            _validate_portable_query(self.content('contacts'), 'company', self.schema)

    def test_missing_nested_field_is_still_rejected(self):
        with self.assertRaisesRegex(ValueError, 'path is absent'):
            _validate_portable_query(self.content('company.partners', 'missing'), 'company', self.schema)
