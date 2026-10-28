import unittest
from copy import deepcopy

from hero_integration.services.mapping_draft_service import MappingDraftError
from hero_integration.services.personal_mapping_content import prepare_exchange_content


class ExchangeContentTest(unittest.TestCase):
    def setUp(self):
        self.rule = {'source_path': 'subject', 'target_field': 'subject'}
        self.base = {'name': 'rule-1', 'revision': 2, 'schema_snapshot': 'snap'}
        self.local = {'mapping_definition_ids': [], 'field_rules': {}, 'field_rule_bases': {}}
        self.incoming = {'mapping_definition_ids': ['rule-1'],
                         'field_rules': {'rule-1': deepcopy(self.rule)},
                         'field_rule_bases': {'rule-1': deepcopy(self.base)}}

    def resolve(self, identity):
        self.assertEqual(identity, 'rule-1')
        return deepcopy(self.rule), deepcopy(self.base)

    def test_provenance_is_resolved_from_target(self):
        prepared = prepare_exchange_content(self.incoming, self.local, self.resolve)
        self.assertTrue(prepared['can_save'])
        self.assertEqual(prepared['content']['field_rule_bases'], {})
        self.assertEqual(prepared['references'][0]['base'], self.base)

    def test_mismatch_or_missing_target_blocks_save(self):
        changed = deepcopy(self.incoming)
        changed['field_rule_bases']['rule-1']['revision'] = 3
        self.assertFalse(prepare_exchange_content(changed, self.local, self.resolve)['can_save'])
        def missing(_):
            raise LookupError('Not found')
        result = prepare_exchange_content(self.incoming, self.local, missing)
        self.assertEqual(result['conflicts'][0]['state'], 'UNRESOLVED')
        self.assertFalse(result['can_save'])

    def test_unknown_authority_and_unconfirmed_onboarding_are_rejected(self):
        for extra in ({'execution_approval': True}, {'native_task_onboarding': {'confirmed': True}}):
            with self.subTest(extra=extra), self.assertRaises(MappingDraftError):
                prepare_exchange_content({**self.incoming, **extra}, self.local, self.resolve)

    def test_unreferenced_new_snapshot_is_rejected(self):
        incoming = {**self.incoming, 'mapping_definition_ids': []}
        with self.assertRaises(MappingDraftError):
            prepare_exchange_content(incoming, self.local, self.resolve)


if __name__ == '__main__':
    unittest.main()
