"""Retired Customer ancestors preserve provenance without authorizing inactive rules."""
from copy import deepcopy
import json
from types import SimpleNamespace
import unittest

from hero_integration.services.mapping_rule_lineage import customer_legacy_literals, next_lineage
from hero_integration.services.personal_mapping_content import RULE_FIELDS


class Row(dict):
    __getattr__ = dict.__getitem__


class TestCustomerHistoricalLineage(unittest.TestCase):
    def setUp(self):
        self.mapping = SimpleNamespace(name='customer-mapping', schema_snapshot='snapshot', schema_fingerprint='f'*64)
        self.root = Row(
            definition_id='customer-primary-address-line2',
            portable_definition_id='a16b15fa-7ca8-4722-b63c-ac60cc46629a', portable_lineage_json='',
            status='Active', operation='HeroCustomers', schema_snapshot='snapshot', schema_fingerprint='f'*64,
            personal_graphql_mapping='', schema_manifest='', schema_dependencies='[]',
            source_path='address.line_2', source_type='string|null', target_doctype='Address',
            target_field='address_line2', purpose='Native Binding', transformation='Trim',
            relation='Customer Primary Address', priority=5)
        self.middle = self.revision(self.root, '09a2bc52-4b21-46b7-9a11-3a62bf5a6348')
        self.leaf = self.revision(self.middle, '9d8fe02d-9eb0-49b1-a599-e0630f421010')
        self.rows = [self.root, self.middle, self.leaf]

    def revision(self, original, identity):
        revised = Row(deepcopy(dict(original)))
        revised.update(definition_id=identity, portable_definition_id=identity,
                       personal_graphql_mapping=self.mapping.name)
        revised['portable_lineage_json'] = next_lineage(original, identity,
                                                        {key:revised[key] for key in RULE_FIELDS})
        return revised

    def resolve(self, *selected):
        return customer_legacy_literals(selected or (self.leaf.definition_id,), self.mapping,
            lambda identity:[row for row in self.rows if row['definition_id']==identity],
            lambda identity:[row for row in self.rows if row['portable_definition_id']==identity])

    def test_cleanup_may_disable_root_and_intermediate_while_active_leaf_remains_valid(self):
        self.root['status'] = self.middle['status'] = 'Disabled'
        self.assertEqual(self.resolve(), {'customer-primary-address-line2'})

    def test_selected_disabled_leaf_never_authorizes_execution(self):
        self.leaf['status'] = 'Disabled'
        with self.assertRaisesRegex(ValueError, 'outside the approved mapping context'):
            self.resolve()

    def test_selected_disabled_literal_root_never_authorizes_execution(self):
        self.root['status'] = 'Disabled'
        with self.assertRaisesRegex(ValueError, 'outside the approved mapping context'):
            self.resolve(self.root.definition_id)

    def test_ancestors_in_draft_or_unknown_states_remain_rejected(self):
        for row in (self.root, self.middle):
            for state in ('Draft', 'Pending', '', None):
                with self.subTest(identity=row.definition_id, state=state):
                    row['status'] = state
                    with self.assertRaisesRegex(ValueError, 'outside the approved mapping context'):
                        self.resolve()
            row['status'] = 'Active'

    def test_disabled_ancestors_keep_exact_schema_operation_and_parent_scope(self):
        for row in (self.root, self.middle):
            row['status'] = 'Disabled'
            for key, value in (('schema_snapshot','other-source-snapshot'), ('schema_fingerprint','other-schema'),
                               ('operation','HeroProducts'), ('personal_graphql_mapping','other-parent')):
                with self.subTest(identity=row.definition_id, key=key):
                    original = row[key]
                    row[key] = value
                    with self.assertRaisesRegex(ValueError, 'outside the approved mapping context'):
                        self.resolve()
                    row[key] = original

    def test_disabled_revision_content_and_schema_provenance_are_still_fingerprinted(self):
        self.middle['status'] = 'Disabled'
        for key, value in (('priority',6), ('source_path','address.street'),
                           ('schema_dependencies','["unexpected"]'), ('schema_manifest','{"changed":true}')):
            with self.subTest(key=key):
                original = self.middle[key]
                self.middle[key] = value
                with self.assertRaisesRegex(ValueError, 'fingerprint changed'):
                    self.resolve()
                self.middle[key] = original

    def test_disabled_root_cannot_change_reserved_literal_semantics(self):
        self.root['status'] = 'Disabled'
        self.root['target_field'] = 'address_line1'
        with self.assertRaisesRegex(ValueError, 'literal lineage is incompatible'):
            self.resolve()

    def test_disabled_ancestor_uuid_and_lineage_identity_are_still_checked(self):
        self.middle['status'] = 'Disabled'
        raw = json.loads(self.middle['portable_lineage_json'])
        for replacement in ('not-a-uuid', self.root.portable_definition_id):
            with self.subTest(replacement=replacement):
                self.middle['portable_lineage_json'] = json.dumps({**raw, 'revision_id':replacement})
                with self.assertRaises(ValueError):
                    self.resolve()

    def test_missing_or_ambiguous_disabled_predecessor_still_fails(self):
        self.middle['status'] = 'Disabled'
        self.rows.remove(self.middle)
        with self.assertRaisesRegex(ValueError, 'predecessor is missing or ambiguous'):
            self.resolve()
        self.rows.extend([self.middle, deepcopy(dict(self.middle))])
        with self.assertRaisesRegex(ValueError, 'predecessor is missing or ambiguous'):
            self.resolve()


if __name__ == '__main__':
    unittest.main()
