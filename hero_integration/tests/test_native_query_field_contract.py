from dataclasses import replace
from types import SimpleNamespace
import unittest

from hero_integration.services.direct_mapping import DirectMappingError, MappingSpec, validate_spec
from hero_integration.services.frappe_writeback import WritebackContractError, normalise_mapping


class Metadata:
    def __init__(self):
        self.fields = {
            ('Customer', 'alias'): SimpleNamespace(fieldtype='Data', read_only=0, is_custom_field=0, hidden=0, fetch_from='', is_virtual=0),
            ('Customer', 'website'): SimpleNamespace(fieldtype='Data', read_only=0, is_custom_field=0),
            ('Customer', 'custom_hero_test'): SimpleNamespace(fieldtype='Data', read_only=0, is_custom_field=1),
        }

    def ensure_write(self, doctype):
        if doctype != 'Customer':
            raise PermissionError('Not permitted')

    def field(self, doctype, fieldname):
        return self.fields.get((doctype, fieldname))


class TestNativeQueryFieldContract(unittest.TestCase):
    def setUp(self):
        self.spec = MappingSpec('HeroCustomers', 'full_name', 'Customer', 'alias', 'string|null')

    def test_reviewed_native_field_and_custom_field(self):
        validate_spec(self.spec, Metadata())
        validate_spec(replace(self.spec, transformation='Trim'), Metadata())
        validate_spec(replace(self.spec, source_path='company_name'), Metadata())
        validate_spec(replace(self.spec, target_field='custom_hero_test'), Metadata())

    def test_not_a_global_native_field_or_source_grant(self):
        for spec in (replace(self.spec, target_field='website'), replace(self.spec, target_field='name'),
                     replace(self.spec, target_field='missing'), replace(self.spec, source_path='nr'),
                     replace(self.spec, transformation='Scalar Text'), replace(self.spec, relation='unexpected')):
            with self.subTest(spec=spec), self.assertRaises(DirectMappingError):
                validate_spec(spec, Metadata())

    def test_unavailable_readonly_calculated_or_wrong_type_fails_closed(self):
        for attrs in ({'read_only': 1}, {'hidden': 1}, {'fetch_from': 'customer_group.group_name'},
                      {'is_virtual': 1}, {'fieldtype': 'Link'}):
            metadata = Metadata()
            for key, value in attrs.items():
                setattr(metadata.fields[('Customer', 'alias')], key, value)
            with self.subTest(attrs=attrs), self.assertRaises(DirectMappingError):
                validate_spec(self.spec, metadata)

    def test_query_destination_does_not_grant_mutation_source(self):
        payload = {'source': {'doctype': 'Customer', 'field': 'alias'},
                   'target': {'operation': 'update_contact', 'argument': 'contact',
                              'input_type': 'CustomerInput', 'field': 'full_name'}}
        with self.assertRaises(WritebackContractError):
            normalise_mapping(payload, {})

    def test_missing_doctype_write_permission_is_not_bypassed(self):
        class Denied(Metadata):
            def ensure_write(self, doctype):
                raise PermissionError('Denied')
        with self.assertRaises(PermissionError):
            validate_spec(self.spec, Denied())

    def test_project_title_and_notes_remain_protected(self):
        class ProjectMetadata(Metadata):
            def ensure_write(self, doctype): pass
        for field in ('project_name', 'notes', 'company', 'name'):
            with self.subTest(field=field), self.assertRaises(DirectMappingError):
                validate_spec(MappingSpec('HeroProjectMatches', 'project_title', 'Project', field, 'string'), ProjectMetadata())


if __name__ == '__main__':
    unittest.main()
