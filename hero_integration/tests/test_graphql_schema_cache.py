"""Content caching must never cache source authority or mutable input values."""
from copy import deepcopy
from dataclasses import replace
from unittest import TestCase
from unittest.mock import patch

from graphql import build_client_schema, build_schema, introspection_from_schema

from hero_integration.services import graphql_schema_cache as cache
from hero_integration.services import mapping_contract_v1 as contract, query_draft
from hero_integration.services.graphql_graph import analyze_snapshot


class TestGraphQLSchemaCache(TestCase):
    def setUp(self):
        cache.clear_schema_cache()
        contract._snapshot_ir.cache_clear()
        self.raw = introspection_from_schema(build_schema('''
            type Query { node(id:ID!):Node }
            type Node { id:ID! value:String }
            type Mutation { update(id:ID!,value:String):Node }
        '''))
        self.text = cache.schema_text(self.raw)
        self.snapshot = contract.StoredSnapshot('snapshot',self.text,analyze_snapshot(self.raw).fingerprint,'source-a')
        self.document = 'query Read($id:ID!) { node(id:$id) { id value } }'
        self.addCleanup(cache.clear_schema_cache)
        self.addCleanup(contract._snapshot_ir.cache_clear)

    def test_saved_reads_build_once_and_keep_each_requests_variables(self):
        with patch('graphql.build_client_schema',wraps=build_client_schema) as build, \
             patch.object(contract,'_snapshot_ir',side_effect=AssertionError('Saved reads need no generated IR')):
            first = contract.materialize_saved_query(self.snapshot,self.document,{'id':'1'})
            second = contract.materialize_saved_query(self.snapshot,self.document,{'id':'2'})
        build.assert_called_once()
        self.assertEqual(first.variables,{'id':'1'})
        self.assertEqual(second.variables,{'id':'2'})
        self.assertNotEqual(first.request_fingerprint,second.request_fingerprint)
        self.assertEqual(cache.compiled_schema(self.text).fingerprint,self.snapshot.fingerprint)

    def test_warm_schema_does_not_skip_status_identity_or_fingerprint_validation(self):
        contract.materialize_saved_query(self.snapshot,self.document,{'id':'1'})
        for changes in ({'analysis_status':'Rejected'},{'source_instance':''},{'name':''},{'fingerprint':'0'*64}):
            with self.subTest(changes=changes), self.assertRaises(contract.MappingContractError):
                contract.materialize_saved_query(replace(self.snapshot,**changes),self.document,{'id':'1'})

    def test_changed_json_with_an_old_fingerprint_is_rejected_after_a_cache_hit(self):
        before = contract.materialize_saved_query(self.snapshot,self.document,{'id':'1'})
        changed = deepcopy(self.raw)
        node = next(t for t in changed['__schema']['types'] if t['name']=='Node')
        next(field for field in node['fields'] if field['name']=='value')['type']['name']='Boolean'
        text = cache.schema_text(changed)
        with self.assertRaisesRegex(contract.MappingContractError,'Stale snapshot'):
            contract.materialize_saved_query(replace(self.snapshot,snapshot_json=text),self.document,{'id':'1'})
        fresh = replace(self.snapshot,snapshot_json=text,fingerprint=analyze_snapshot(changed).fingerprint)
        after = contract.materialize_saved_query(fresh,self.document,{'id':'1'})
        self.assertNotEqual(before.schema_fingerprint,after.schema_fingerprint)

    def test_mutating_a_dictionary_cannot_change_or_reuse_its_cached_schema(self):
        compiled = cache.compiled_schema(self.raw)
        node = next(t for t in self.raw['__schema']['types'] if t['name']=='Node')
        node['fields'] = [field for field in node['fields'] if field['name']!='value']
        with self.assertRaises(query_draft.QueryDraftError):
            query_draft.validate(self.raw,self.document)
        self.assertIn('value',compiled.core.get_type('Node').fields)
        self.assertIsNot(compiled.core,cache.compiled_schema(self.raw).core)

    def test_cached_schema_still_validates_new_documents_variables_and_mutation_scopes(self):
        contract.materialize_saved_query(self.snapshot,self.document,{'id':'1'})
        with self.assertRaises(contract.MappingContractError):
            contract.materialize_saved_query(self.snapshot,'query { missing }',{})
        with self.assertRaises(contract.MappingContractError):
            contract.materialize_saved_query(self.snapshot,self.document,{'id':{'invalid':'ID'}})
        document = 'mutation Write($id:ID!) { update(id:$id) { id } }'
        first = contract.materialize_saved_mutation(self.snapshot,document,{'id':'1'},request_scope='audit-1')
        second = contract.materialize_saved_mutation(self.snapshot,document,{'id':'1'},request_scope='audit-2')
        self.assertNotEqual(first.request_fingerprint,second.request_fingerprint)

    def test_generated_schema_cache_does_not_retain_another_sources_provenance(self):
        first = contract.SnapshotAdapter().adapt(self.snapshot)
        second = contract.SnapshotAdapter().adapt(replace(self.snapshot,name='other',source_instance='source-b'))
        self.assertIs(first.ir,second.ir)
        self.assertIs(first.full_ir,second.full_ir)
        self.assertEqual((first.name,first.source_instance),('snapshot','source-a'))
        self.assertEqual((second.name,second.source_instance),('other','source-b'))
        with self.assertRaises(contract.MappingContractError):
            contract.SnapshotAdapter().adapt(replace(self.snapshot,analysis_status='Rejected'))
        with self.assertRaises(contract.MappingContractError):
            contract.SnapshotAdapter().adapt(replace(self.snapshot,fingerprint='0'*64))

    def test_core_and_generated_schema_caches_evict_old_entries(self):
        old = cache.compiled_schema(self.text)
        old_ir = contract.SnapshotAdapter().adapt(self.snapshot).ir
        for index in range(cache.SCHEMA_CACHE_SIZE):
            raw = deepcopy(self.raw)
            raw['__schema']['description'] = 'changed-'+str(index)
            text = cache.schema_text(raw)
            cache.compiled_schema(text)
            contract.SnapshotAdapter().adapt(replace(self.snapshot,snapshot_json=text,
                fingerprint=analyze_snapshot(raw).fingerprint))
        self.assertEqual(cache._compiled.cache_info().currsize,cache.SCHEMA_CACHE_SIZE)
        self.assertEqual(contract._snapshot_ir.cache_info().currsize,cache.SCHEMA_CACHE_SIZE)
        self.assertIsNot(old.core,cache.compiled_schema(self.text).core)
        self.assertIsNot(old_ir,contract.SnapshotAdapter().adapt(self.snapshot).ir)

    def test_oversized_entries_work_without_being_retained(self):
        with patch.object(cache,'MAX_CACHEABLE_SCHEMA_CHARS',1), \
             patch('graphql.build_client_schema',wraps=build_client_schema) as build:
            cache.compiled_schema(self.text)
            cache.compiled_schema(self.text)
        self.assertEqual(build.call_count,2)
        self.assertEqual(cache._compiled.cache_info().currsize,0)
        with patch.object(contract,'MAX_CACHEABLE_SCHEMA_CHARS',1):
            contract.SnapshotAdapter().adapt(self.snapshot)
        self.assertEqual(contract._snapshot_ir.cache_info().currsize,0)

    def test_incomplete_schema_errors_are_not_retained_as_successful_compilation(self):
        with self.assertRaises(ValueError):
            cache.compiled_schema({'__schema':{}})
        self.assertEqual(cache._compiled.cache_info().currsize,0)
        self.assertIn('node',cache.compiled_schema(self.text).core.query_type.fields)
