from copy import deepcopy
import unittest

from hero_integration.services.mapping_draft_service import MappingDraftError
from hero_integration.services.personal_mapping_content import validate_exchange_action
from hero_integration.tests.test_frappe_writeback_contract import schema


class TestExchangeActionProjection(unittest.TestCase):
    def setUp(self):
        self.customer = {
            'source': {'doctype': 'Customer', 'field': 'email_id'},
            'target': {'operation': 'update_contact', 'argument': 'contact',
                       'input_type': 'CustomerInput', 'field': 'email'},
            'transformation': 'Trim', 'version': 1,
        }
        self.project = {
            'source': {'doctype': 'Project', 'field': 'custom_hero_partner_notes'},
            'target': {'operation': 'update_project_match', 'argument': 'project_match',
                       'input_type': 'ProjectMatchInput', 'field': 'partner_notes'},
            'transformation': 'Identity', 'version': 1,
        }

    def test_supported_customer_and_project_action_parameters(self):
        for value, operation in ((self.customer, 'update_contact'),
                                 (self.project, 'update_project_match')):
            result = validate_exchange_action(value, schema(), operation)
            self.assertEqual(result['mapping'], value)
            self.assertEqual(result['graphql_type'], 'String')
            self.assertNotIn('identity', result)

    def test_wrong_operation_field_type_or_transformation_is_rejected(self):
        candidates = []
        for section, field, value in (('source', 'field', 'name'),
                                      ('target', 'argument', 'project_match'),
                                      ('target', 'field_path', 'wrong'),
                                      ('target', 'graphql_type', 'Int')):
            candidate = deepcopy(self.customer)
            candidate[section][field] = value
            candidates.append(candidate)
        candidate = deepcopy(self.customer)
        candidate['transformation'] = 'Execute'
        candidates.append(candidate)
        for candidate in candidates:
            with self.subTest(candidate=candidate), self.assertRaises(MappingDraftError):
                validate_exchange_action(candidate, schema(), 'update_contact')
        with self.assertRaises(MappingDraftError):
            validate_exchange_action(self.customer, schema(), 'update_project_match')

    def test_missing_target_schema_is_rejected(self):
        with self.assertRaises(MappingDraftError):
            validate_exchange_action(self.customer, {'__schema': {'types': []}}, 'update_contact')


if __name__ == '__main__':
    unittest.main()
