"""Publication retains the relation contracts of selected schema fields."""
import json
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from hero_integration.services import mapping_draft_exchange as exchange
from hero_integration.services import schema_provisioning
from hero_integration.services.frappe_mapping_draft_storage import FrappeMappingDraftStorage


def schema():
    return {
        'custom_doctypes': [
            {'id': 'doc:child', 'name': 'HERO Test Child', 'label': 'Child', 'is_child': True,
             'fields': [{'id': 'field:nested', 'fieldname': 'custom_hero_target', 'label': 'Target',
                         'fieldtype': 'Link', 'options': 'HERO Test Target'}]},
            {'id': 'doc:target', 'name': 'HERO Test Target', 'label': 'Target', 'is_child': False,
             'fields': [{'id': 'field:title', 'fieldname': 'custom_hero_title', 'label': 'Title',
                         'fieldtype': 'Data', 'options': ''}]},
        ],
        'custom_fields': [
            {'id': 'field:table', 'doctype': 'Project', 'fieldname': 'custom_hero_rows',
             'label': 'Rows', 'fieldtype': 'Table', 'options': 'HERO Test Child', 'insert_after': ''},
            {'id': 'field:unrelated', 'doctype': 'Project', 'fieldname': 'custom_hero_other',
             'label': 'Other', 'fieldtype': 'Data', 'options': '', 'insert_after': ''},
        ],
        'relations': [
            {'id': 'relation:table', 'doctype': 'Project', 'field_id': 'field:table',
             'fieldname': 'custom_hero_rows', 'relation_type': 'Table',
             'target_doctype': 'HERO Test Child', 'dependencies': []},
            {'id': 'relation:nested', 'doctype': 'HERO Test Child', 'field_id': 'field:nested',
             'fieldname': 'custom_hero_target', 'relation_type': 'Link',
             'target_doctype': 'HERO Test Target', 'dependencies': []},
        ],
    }


class TestPortableSchemaRelationClosure(TestCase):
    def test_table_field_retains_nested_relation_and_target_without_unrelated_field(self):
        declarations, closure = exchange._portable_schema_graph(schema())
        self.assertEqual(closure(['field:table']), {
            'field:table', 'relation:table', 'doc:child', 'relation:nested', 'doc:target'})
        self.assertIn('field:unrelated', declarations)
        self.assertEqual(closure(['field:unrelated']), {'field:unrelated'})

    def test_whole_or_nested_doctype_field_retains_its_relation(self):
        _, closure = exchange._portable_schema_graph(schema())
        expected = {'doc:child', 'relation:nested', 'doc:target'}
        self.assertEqual(closure(['doc:child']), expected)
        self.assertEqual(closure(['field:nested']), expected)

    def test_first_publication_slices_a_complete_valid_relation_schema(self):
        context = {'direction': 'QUERY_TO_FRAPPE', 'schema_fingerprint': 'a' * 64,
                   'definition_id': 'fc0d8954-515b-455c-b311-a2227b723e9e',
                   'operation_kind': 'QUERY', 'operation_name': 'project_matches'}
        content = {'records_path': 'project_matches', 'identity_path': 'id',
                   'mapping_definition_ids': [], 'field_rules': {}}
        fake = SimpleNamespace(db=SimpleNamespace(has_column=lambda *args: True))
        with patch.object(schema_provisioning, '_doctype_exists', return_value=False), \
             patch.object(schema_provisioning, '_recheck_required_schema_current') as recheck:
            result = FrappeMappingDraftStorage(fake)._first_publication_schema(
                context, content, {'schema': schema(), 'dependencies': ['field:table']})
        manifest = json.loads(result['portable_schema_manifest'])
        self.assertEqual([row['id'] for row in manifest['custom_fields']], ['field:table'])
        self.assertEqual({row['id'] for row in manifest['relations']},
                         {'relation:table', 'relation:nested'})
        self.assertEqual(json.loads(result['portable_schema_dependencies']), ['field:table'])
        recheck.assert_called_once()
        self.assertEqual(recheck.call_args.args[1], {
            'field:table', 'relation:table', 'doc:child', 'relation:nested', 'doc:target'})

    def test_relation_with_missing_field_owner_stays_invalid(self):
        value = schema()
        value['relations'][0]['field_id'] = 'field:missing'
        with self.assertRaisesRegex(ValueError, 'dependency is missing'):
            exchange._portable_schema_graph(value)
