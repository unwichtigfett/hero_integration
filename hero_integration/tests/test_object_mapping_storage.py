from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import hero_integration.api as api
from hero_integration.services.frappe_mapping_draft_storage import FrappeMappingDraftStorage
from hero_integration.services.mapping_draft_service import MappingDraftError
from hero_integration.services.object_mapping_catalog import catalog
from hero_integration.tests.test_object_mapping_catalog import schema, ref


class ObjectMappingStorageTest(unittest.TestCase):
    def setUp(self):
        self.storage = FrappeMappingDraftStorage(SimpleNamespace())
        self.storage._definition = SimpleNamespace(direction='QUERY_TO_FRAPPE', schema_snapshot='snapshot',
                                                   schema_fingerprint='fingerprint')
        self.snapshot = SimpleNamespace(schema_fingerprint='fingerprint')
        self.entry = catalog(schema(), 'Task')['items'][0]
        self.source = {'field': self.entry['path'], 'object_type': 'Task',
                       'schema_context': self.entry['schema_context']}

    def validate(self, content):
        with patch.object(api, '_validated_snapshot', return_value=(self.snapshot, schema())):
            self.storage._validate_object_contexts(content)

    def test_primary_and_referenced_context_require_exact_schema(self):
        content = {'source': self.source, 'mapping_definition_ids': ['r'],
                   'field_rules': {'r': {'source_path': 'title'}},
                   'field_contexts': {'r': {'object_type': 'Task', 'schema_context': self.entry['schema_context']}}}
        self.validate(content)
        for path in ('source', 'field_contexts'):
            bad = deepcopy(content)
            target = bad['source'] if path == 'source' else bad['field_contexts']['r']
            target['schema_context'] = '{}'
            with self.subTest(path=path), self.assertRaises(MappingDraftError):
                self.validate(bad)
        bad = deepcopy(content); bad['field_rules']['r']['source_path'] = 'unknown'
        with self.assertRaises(MappingDraftError):
            self.validate(bad)

    def test_object_draft_cannot_implicitly_publish(self):
        for content in ({'source': self.source},
                        {'field_contexts': {'r': {'object_type': 'Task', 'schema_context': self.entry['schema_context']}}},
                        {'additional_mappings': [{'source': self.source}]}):
            with self.subTest(content=content), self.assertRaisesRegex(MappingDraftError, 'approved fetch'):
                self.storage._publication_content(SimpleNamespace(content=content), schema())

    def test_only_exact_existing_customer_resolver_matches_object_context(self):
        value = schema()
        query = next(t for t in value['__schema']['types'] if t['name'] == 'Query')
        query['fields'].append({'name': 'project_matches', 'type': ref('LIST', of=ref('OBJECT', 'ProjectMatch'))})
        value['__schema']['types'].append({'kind': 'OBJECT', 'name': 'ProjectMatch', 'fields': [
            {'name': 'customer_id', 'type': ref('SCALAR', 'Int')}]})
        selected = catalog(value, 'ProjectMatch')['items'][0]
        rule = {'source_path': 'customer_id', 'source_type': 'int|null', 'target_doctype': 'Project',
                'target_field': 'customer', 'purpose': 'Customer Relation', 'transformation': 'HERO Customer Link',
                'relation': 'HERO Customer Link'}
        content = {'target_operation': 'HeroProjectMatches', 'mapping_definition_ids': ['r'],
                   'field_rules': {'r': rule}, 'field_contexts': {'r': {'object_type': 'ProjectMatch',
                   'schema_context': selected['schema_context']}}}
        self.assertTrue(self.storage._approved_relationship_contexts(content, value))
        for key, replacement in [('source_path', 'id'), ('target_field', 'company'),
                                 ('transformation', 'Identity'), ('relation', '')]:
            bad = deepcopy(content); bad['field_rules']['r'][key] = replacement
            with self.subTest(key=key):
                self.assertFalse(self.storage._approved_relationship_contexts(bad, value))
        bad = deepcopy(content); bad['field_contexts']['r']['object_type'] = 'Customer'
        self.assertFalse(self.storage._approved_relationship_contexts(bad, value))
        bad = deepcopy(content); bad['field_contexts']['r']['schema_context'] = '{}'
        self.assertFalse(self.storage._approved_relationship_contexts(bad, value))

    def test_existing_primary_metadata_is_preserved_and_paths_must_agree(self):
        source = {**self.source, 'doctype': 'legacy', 'type': 'legacy', 'path': 'title', 'source_path': 'title'}
        self.validate({'source': source})
        self.assertEqual(source['doctype'], 'legacy')
        bad = deepcopy(source); bad['path'] = 'other'
        with self.assertRaises(MappingDraftError):
            self.validate({'source': bad})


if __name__ == '__main__':
    unittest.main()
