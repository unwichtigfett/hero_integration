"""Complete documents retain GraphQL semantics at the materializer boundary."""
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock
from graphql import build_schema, introspection_from_schema, parse, validate
from hero_integration.graphql_protocol.graphql_operation_materializer import GraphQLOperationMaterializer as Materializer, GraphQLOperationMaterializationError
from hero_integration.services.graphql_graph import analyze_snapshot
from hero_integration.services.mapping_contract_v1 import StoredSnapshot
from hero_integration.services.direct_runtime import DirectRead, fetch_page


class TestDocumentMaterializer(unittest.TestCase):
    def setUp(self):
        self.schema = build_schema('type Query { nodes(first:Int!, offset:Int!):[Node] } '
            'type Mutation { update(id:ID!):Node } type Node { id:ID! name:String }')
        raw = introspection_from_schema(self.schema)
        self.snapshot = StoredSnapshot('document', raw, analyze_snapshot(raw).fingerprint, 'source')
        self.document = 'query Read($n:Int!, $offset:Int!, $include:Boolean=true) { items:nodes(first:$n,offset:$offset) { ...Fields name @include(if:$include) } } fragment Fields on Node { id }'

    def compile(self, variables):
        return Materializer.materialize_document(self.schema, self.snapshot.fingerprint, self.document, variables)

    def test_fragments_aliases_directives_and_defaults_are_preserved(self):
        request = self.compile({'n': 2, 'offset': 0})
        self.assertEqual(validate(self.schema, parse(request.document)), [])
        self.assertTrue(request.variables['include'])
        self.assertIn('...Fields', request.document)
        self.assertIn('items: nodes', request.document)
        self.assertIsNone(request.mutation_safety_request)
        self.assertEqual(self.compile({'offset': 0, 'n': 2}), request)

    def test_invalid_documents_or_variable_types_fail_before_transport(self):
        for variables in ({'n': True, 'offset': 0}, {'n': 2}, {'n': 2, 'offset': 0, 'extra': 1}):
            with self.subTest(variables=variables), self.assertRaises(GraphQLOperationMaterializationError):
                self.compile(variables)
        with self.assertRaises(GraphQLOperationMaterializationError):
            Materializer.materialize_document(self.schema, self.snapshot.fingerprint, 'query { invalid }', {})
        with self.assertRaises(GraphQLOperationMaterializationError):
            Materializer.materialize_document(self.schema, self.snapshot.fingerprint, 'query {', {})

    def test_mutation_is_sealed_to_explicit_scope(self):
        document = 'mutation Update($id:ID!) { update(id:$id) { id } }'
        with self.assertRaises(GraphQLOperationMaterializationError):
            Materializer.materialize_document(self.schema, self.snapshot.fingerprint, document, {'id': '1'})
        request = Materializer.materialize_document(self.schema, self.snapshot.fingerprint, document, {'id': '1'}, request_scope='test')
        self.assertEqual(request.mutation_safety_request.variables['id'], '1')

    def test_named_operation_selection_keeps_only_its_transitive_fragments_and_variables(self):
        document = '''query First($n:Int!) { nodes(first:$n,offset:0) { ...Outer } }
            query Other($offset:Int!) { nodes(first:1,offset:$offset) { ...UnusedByFirst } }
            fragment Outer on Node { ...Inner }
            fragment Inner on Node { id }
            fragment UnusedByFirst on Node { name }'''
        request = Materializer.materialize_document(self.schema,self.snapshot.fingerprint,document,
                                                    {'n':2},operation_name='First')
        self.assertEqual(request.variables, {'n':2})
        tree = parse(request.document)
        self.assertEqual([node.name.value for node in tree.definitions], ['First','Outer','Inner'])
        self.assertEqual(validate(self.schema,tree), [])
        with self.assertRaises(GraphQLOperationMaterializationError):
            Materializer.materialize_document(self.schema,self.snapshot.fingerprint,document,{'n':2})
        with self.assertRaises(GraphQLOperationMaterializationError):
            Materializer.materialize_document(self.schema,self.snapshot.fingerprint,document,{'n':2},operation_name='Missing')

    def test_selected_operation_still_validates_the_entire_document(self):
        document = 'query Selected { nodes(first:1,offset:0) { id } } query Invalid { missing }'
        with self.assertRaises(GraphQLOperationMaterializationError):
            Materializer.materialize_document(self.schema,self.snapshot.fingerprint,document,{},operation_name='Selected')

    def test_named_selection_cannot_bypass_mutation_scope(self):
        document = 'query Read { nodes(first:1,offset:0) { id } } mutation Write($id:ID!) { update(id:$id) { id } }'
        read = Materializer.materialize_document(self.schema,self.snapshot.fingerprint,document,{},operation_name='Read')
        self.assertNotIn('mutation',read.document)
        self.assertIsNone(read.mutation_safety_request)
        with self.assertRaises(GraphQLOperationMaterializationError):
            Materializer.materialize_document(self.schema,self.snapshot.fingerprint,document,{'id':'1'},operation_name='Write')
        write = Materializer.materialize_document(self.schema,self.snapshot.fingerprint,document,{'id':'1'},
                                                  operation_name='Write',request_scope='named-write')
        self.assertNotIn('query',write.document)
        self.assertEqual(write.mutation_safety_request.operation_name,'Write')

    def test_saved_adapters_forward_explicit_operation_selection(self):
        from hero_integration.services.mapping_contract_v1 import materialize_saved_query, materialize_saved_mutation
        document = 'query Read { nodes(first:1,offset:0) { id } } mutation Write($id:ID!) { update(id:$id) { id } }'
        read = materialize_saved_query(self.snapshot,document,{},operation_name='Read')
        write = materialize_saved_mutation(self.snapshot,document,{'id':'1'},operation_name='Write',request_scope='saved-write')
        self.assertNotIn('mutation',read.document)
        self.assertEqual(write.mutation_safety_request.operation_name,'Write')

    def test_operation_templates_validate_required_inputs_defaults_and_composite_shapes(self):
        schema = build_schema('''
            input Filter { name:String! }
            type Query { search(filter:Filter!, ids:[ID!]!, limit:Int!=10):[Result!]! count:Int! _:Boolean _1:Boolean }
            union Result = Node | Other
            type Node { id:ID! }
            type Other { name:String }
            type Mutation { update_filter(filter:Filter!):Boolean! }
        ''')
        document = Materializer.operation_template(schema,'query','search')
        self.assertEqual(validate(schema,parse(document)), [])
        operation = parse(document).definitions[0]
        self.assertEqual([variable.variable.name.value for variable in operation.variable_definitions],['filter','ids'])
        self.assertIn('__typename',document)
        scalar = Materializer.operation_template(schema,'query','count')
        self.assertEqual(validate(schema,parse(scalar)), [])
        self.assertNotIn('__typename',scalar)
        for root in ('_', '_1'):
            template = Materializer.operation_template(schema,'query',root)
            self.assertEqual(validate(schema,parse(template)), [])
        mutation = Materializer.operation_template(schema,'mutation','update_filter')
        self.assertEqual(parse(mutation).definitions[0].name.value,'UpdateFilter')
        self.assertEqual(validate(schema,parse(mutation)), [])
        with self.assertRaises(GraphQLOperationMaterializationError):
            Materializer.operation_template(schema,'query','missing')
        with self.assertRaises(GraphQLOperationMaterializationError):
            Materializer.operation_template(schema,'invalid','search')

    def test_approved_direct_read_passes_through_document_materializer(self):
        read = DirectRead('HeroProducts', None, self.document, 'items', {'n':'$page_size', 'offset':'$offset'},
                          2, 'approved', 'mapping', self.snapshot)
        client = NS(query=Mock(return_value={'items': [{'id': '1'}]}))
        self.assertEqual(fetch_page(client, read, 0), [{'id': '1'}])
        document, variables = client.query.call_args.args
        self.assertEqual(validate(self.schema, parse(document)), [])
        self.assertEqual(variables, {'n':2, 'offset':0, 'include':True})

    def test_saved_service_page_materializes_complete_document_before_transport(self):
        from unittest.mock import patch
        from hero_integration.services.read_contract import ReadContract
        from hero_integration.sources.hero import GraphQLServiceSource
        contract = ReadContract('service', 1, 'mapping', 1, 'HeroCustomers', 'items', 'id', (),
            {'n':'$page_size', 'offset':'$offset'}, 2)
        service = NS(query_document=self.document)
        client = NS(query=Mock(return_value={'items': [{'id': '1'}]}))
        source = GraphQLServiceSource(client, service, contract, 'source', revalidate=lambda: contract)
        with patch.object(source, '_stored_snapshot', return_value=self.snapshot):
            page = source.customers(3)
        self.assertEqual(page.records[0]['_hero_source_instance'], 'source')
        self.assertEqual(client.query.call_args.args[1], {'n':2, 'offset':3, 'include':True})

    def test_invalid_stored_schema_blocks_saved_service_transport(self):
        from unittest.mock import patch
        from dataclasses import replace
        from hero_integration.services.read_contract import ReadContract
        from hero_integration.sources.hero import GraphQLServiceSource
        contract = ReadContract('service', 1, 'mapping', 1, 'HeroCustomers', 'items', 'id', (),
            {'n':2, 'offset':0}, 2)
        client = NS(query=Mock())
        source = GraphQLServiceSource(client, NS(query_document=self.document), contract, 'source')
        with patch.object(source, '_stored_snapshot', return_value=replace(self.snapshot, fingerprint='a'*64)):
            with self.assertRaises(ValueError):
                source.customers()
        client.query.assert_not_called()

    def test_auxiliary_document_uses_materializer_and_checks_approval_twice(self):
        from unittest.mock import patch
        from hero_integration.sources.hero import GraphQLServiceSource
        contract = NS()
        client = NS(query=Mock(return_value={'items': []}))
        revalidate = Mock(return_value=contract)
        source = GraphQLServiceSource(client, NS(), contract, 'source', revalidate=revalidate)
        with patch.object(source, '_stored_snapshot', return_value=self.snapshot):
            self.assertEqual(source._auxiliary_document(self.document, {'n': 1, 'offset': 100}), {'items': []})
        self.assertTrue(client.query.call_args.args[1]['include'])
        self.assertEqual(revalidate.call_count, 2)

    def test_saved_mutation_adapter_binds_snapshot_and_audit_scope(self):
        from hero_integration.services.mapping_contract_v1 import materialize_saved_mutation
        document = 'mutation Update($id:ID!) { update(id:$id) { id } }'
        first = materialize_saved_mutation(self.snapshot, document, {'id':'1'}, request_scope='audit-1')
        second = materialize_saved_mutation(self.snapshot, document, {'id':'1'}, request_scope='audit-2')
        self.assertNotEqual(first.request_fingerprint, second.request_fingerprint)
        self.assertEqual(first.mutation_safety_request.request_scope, 'audit-1')
        self.assertEqual(first.schema_fingerprint, self.snapshot.fingerprint)
