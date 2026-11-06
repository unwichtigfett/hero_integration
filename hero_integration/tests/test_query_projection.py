"""Projection narrows fields while retaining the approved query's semantics."""
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock,patch

from graphql import build_schema,introspection_from_schema,parse,print_ast,validate
from hero_integration.graphql_protocol.graphql_operation_materializer import (
    GraphQLOperationMaterializer as Materializer,GraphQLOperationMaterializationError,
)
from hero_integration.services.graphql_graph import analyze_snapshot
from hero_integration.services.mapping_contract_v1 import StoredSnapshot,materialize_saved_query


class TestQueryProjection(TestCase):
    def setUp(self):
        self.schema=build_schema('''
            type Query { people(first:Int,offset:Int):[Person] person(id:ID!):Person }
            type Person { id:ID! name:String extra:String modified:String user_id:Int
                          address:Address friends(first:Int):[Person] }
            type Address { city:String street:String }
            type Mutation { update:Person }
        ''')
        raw=introspection_from_schema(self.schema)
        self.snapshot=StoredSnapshot('projection',raw,analyze_snapshot(raw).fingerprint,'source')
        self.document='''query People($first:Int!,$offset:Int!,$friends:Int!) {
            records:people(first:$first,offset:$offset) {
                id name extra modified user_id address { city street }
                friends(first:$friends) { id name }
            }
        }'''
        self.variables={'first':10,'offset':20,'friends':30}

    def project(self, paths, *, preserve=(), document=None, variables=None):
        return Materializer.materialize_projected_query(self.schema,self.snapshot.fingerprint,
            document or self.document,self.variables if variables is None else variables,
            paths,preserve_paths=preserve)

    def test_fields_unused_nested_variables_and_declarations_are_removed(self):
        request=self.project(('records.id','records.address.city'),preserve=('records.modified','records.user_id'))
        self.assertEqual(validate(self.schema,parse(request.document)),[])
        for kept in ('id','city','modified','user_id','records: people(first: $first, offset: $offset)'):
            self.assertIn(kept,request.document)
        for removed in ('extra','street','name','friends'):
            self.assertNotIn(removed,request.document)
        self.assertEqual(request.variables,{'first':10,'offset':20})

    def test_selecting_a_parent_retains_its_full_approved_subtree(self):
        request=self.project(('records.address.city',),preserve=('records.address',))
        self.assertIn('street',request.document)
        self.assertIn('city',request.document)

    def test_missing_required_paths_are_rejected_without_adding_fields(self):
        with self.assertRaisesRegex(GraphQLOperationMaterializationError,'omits projection paths'):
            self.project(('records.address.missing',))

    def test_invalid_unneeded_parts_of_original_query_still_fail_validation(self):
        with self.assertRaises(GraphQLOperationMaterializationError):
            self.project(('person.id',),document='{ person(id:"1") { id missing } }',variables={})

    def test_reused_fragments_are_projected_independently_per_response_path(self):
        document='''query {
            one:person(id:"1") { ...Details }
            two:person(id:"2") { ...Details }
        } fragment Details on Person { id name extra }'''
        request=self.project(('one.id','two.name'),document=document,variables={})
        tree=parse(request.document)
        self.assertEqual(validate(self.schema,tree),[])
        fragments={node.name.value:node for node in tree.definitions[1:]}
        selections=tree.definitions[0].selection_set.selections
        first=fragments[selections[0].selection_set.selections[0].name.value]
        second=fragments[selections[1].selection_set.selections[0].name.value]
        self.assertEqual([node.name.value for node in first.selection_set.selections],['id'])
        self.assertEqual([node.name.value for node in second.selection_set.selections],['name'])

    def test_whole_parent_preserves_transitive_named_fragments(self):
        document='''{ person(id:"1") { ...Outer } }
            fragment Outer on Person { id ...Inner }
            fragment Inner on Person { address { city } }'''
        request=self.project(('person',),document=document,variables={})
        self.assertEqual(validate(self.schema,parse(request.document)),[])
        self.assertIn('fragment Inner',request.document)
        self.assertIn('city',request.document)

    def test_directives_and_aliases_keep_their_original_conditions(self):
        document='''query($show:Boolean!) { person(id:"1") {
            ... on Person @include(if:$show) { label:name extra } id
        } }'''
        request=self.project(('person.label',),document=document,variables={'show':False})
        self.assertIn('@include(if: $show)',request.document)
        self.assertIn('label: name',request.document)
        self.assertNotIn('extra',request.document)
        self.assertEqual(request.variables,{'show':False})

    def test_mutation_or_empty_projection_cannot_be_sent_as_a_read(self):
        with self.assertRaises(GraphQLOperationMaterializationError):
            self.project(('update.id',),document='mutation { update { id } }',variables={})
        with self.assertRaises(GraphQLOperationMaterializationError):
            self.project(())
        with self.assertRaises(GraphQLOperationMaterializationError):
            self.project(('records.id',),variables={**self.variables,'unknown':1})

    def test_saved_adapter_and_direct_reader_use_the_projected_request(self):
        from hero_integration.services.direct_runtime import DirectRead,fetch_page
        read=DirectRead('HeroCustomers',None,self.document,'records',self.variables,10,
            snapshot=self.snapshot,projection_paths=('records.id',),projection_preserve_paths=('records.user_id',))
        client=SimpleNamespace(query=Mock(return_value={'records':[{'id':'1','user_id':2}]}))
        self.assertEqual(fetch_page(client,read,0),[{'id':'1','user_id':2}])
        document,variables=client.query.call_args.args
        self.assertIn('user_id',document)
        self.assertNotIn('extra',document)
        self.assertNotIn('friends',variables)
        request=materialize_saved_query(self.snapshot,self.document,self.variables,selected_paths=('records.id',))
        self.assertNotIn('extra',request.document)

    def test_saved_service_keeps_binding_ids_without_requesting_unmapped_fields(self):
        from hero_integration.services.read_contract import ReadContract
        from hero_integration.sources.hero import GraphQLServiceSource
        contract=ReadContract('service',1,'mapping',1,'HeroCustomers','records','id',(),
            self.variables,10,projection_paths=('records.id','records.name'),
            projection_preserve_paths=('records.user_id',))
        client=SimpleNamespace(query=Mock(return_value={'records':[{'id':1,'name':'Example','user_id':2}]}))
        source=GraphQLServiceSource(client,SimpleNamespace(query_document=self.document),contract,'source',
            revalidate=lambda:contract)
        with patch.object(source,'_stored_snapshot',return_value=self.snapshot):
            self.assertEqual(source.customers(0).records[0]['user_id'],2)
        self.assertNotIn('extra',client.query.call_args.args[0])
        self.assertNotIn('street',client.query.call_args.args[0])

    def test_batch_can_mix_plain_and_polymorphic_queries_from_the_same_snapshot(self):
        from hero_integration.services.mapping_contract_v1 import QueryAdapter,QueryDraft,SnapshotAdapter
        schema=build_schema('''
            type Query { people:[Person] results:[Result] }
            type Person { id:ID! name:String }
            type Company { companyName:String }
            union Result = Person | Company
        ''')
        raw=introspection_from_schema(schema)
        snapshot=SnapshotAdapter().adapt(StoredSnapshot('mixed-batch',raw,analyze_snapshot(raw).fingerprint,'source'))
        drafts=(QueryDraft('People','people','people',('id',),{}),
                QueryDraft('Results','results','results',('__typename','@Person.id','@Company.companyName'),{}))
        request=QueryAdapter().materialize_many(snapshot,drafts,'Combined')
        self.assertEqual(validate(schema,parse(request.document)),[])
        self.assertIn('people: people',request.document)
        self.assertIn('... on Company',request.document)

    def test_cached_projection_revalidates_runtime_values_and_changed_demand(self):
        with patch.object(Materializer,'validate_document',wraps=Materializer.validate_document) as validate_document:
            first=self.project(('records.id',))
            second=self.project(('records.id',),variables={**self.variables,'offset':99})
        self.assertEqual(validate_document.call_count,3)  # Original once; final request on every call.
        self.assertEqual(first.variables['offset'],20)
        self.assertEqual(second.variables['offset'],99)
        with self.assertRaises(GraphQLOperationMaterializationError):
            self.project(('records.id',),variables={**self.variables,'offset':'invalid'})
        changed=self.project(('records.name',))
        self.assertIn('name',changed.document)
        self.assertNotIn('id',changed.document)

    def test_projection_cache_uses_the_actual_schema_not_just_a_claimed_fingerprint(self):
        self.project(('records.id',))
        other=build_schema('type Query { people(first:Int,offset:Int):[Person] } type Person { id:ID! }')
        with self.assertRaises(GraphQLOperationMaterializationError):
            Materializer.materialize_projected_query(other,self.snapshot.fingerprint,self.document,
                self.variables,('records.id',))

    def test_projection_plan_cache_is_bounded_and_does_not_keep_schemas_alive(self):
        import gc,weakref
        from hero_integration.graphql_protocol import graphql_operation_materializer as module
        for index in range(module._MAX_PROJECTION_PLANS+2):
            self.project(('records.id',),document=self.document+'\n# version '+str(index))
        self.assertEqual(len(module._QUERY_PROJECTION_CACHE[self.schema]),module._MAX_PROJECTION_PLANS)
        other=build_schema('type Query { value:Int }')
        reference=weakref.ref(other)
        Materializer.materialize_projected_query(other,self.snapshot.fingerprint,'{ value }',{},('value',))
        del other
        gc.collect()
        self.assertIsNone(reference())
