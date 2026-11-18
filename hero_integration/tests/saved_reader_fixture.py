"""Isolate the storage/compiler boundary in existing pagination and conflict fixtures.

Those fixtures intentionally supply incomplete schemas or placeholder documents.
Complete document compilation is covered by test_document_materializer.
"""
from types import SimpleNamespace
from unittest.mock import patch
from hero_integration.sources.hero import GraphQLServiceSource


def isolate_direct_materialization(test):
    """Keep legacy importer fixtures focused on paging/checkpoints and writes.

    These fixtures contain placeholder documents, not executable schema proofs.
    Only the compiler/storage seam is doubled; paging and import guards run.
    Real schema compilation and rejection are covered by the runtime tests.
    """
    from dataclasses import replace
    from hero_integration.services import direct_runtime
    original = direct_runtime.fetch_page
    def page(client, read, offset, size=None):
        with patch('hero_integration.services.mapping_contract_v1.materialize_saved_query',
                   side_effect=lambda snapshot,document,variables: SimpleNamespace(document=document,variables=variables)):
            return original(client,replace(read,snapshot=object()),offset,size)
    owner = patch.object(direct_runtime,'fetch_page',side_effect=page)
    owner.start()
    test.addCleanup(owner.stop)


def product_paging_read():
    """A complete schema-bound reader for paging and importer fixtures."""
    from dataclasses import replace
    from graphql import build_schema, introspection_from_schema
    from hero_integration.services import direct_runtime
    from hero_integration.services.graphql_graph import analyze_snapshot
    from hero_integration.services.mapping_contract_v1 import StoredSnapshot
    schema = introspection_from_schema(build_schema('''
        type Query { supply_product_versions(first:Int!,offset:Int!,orderBy:String!):[Product!]! }
        type Product { product_id:ID! nr:String }
    '''))
    snapshot = StoredSnapshot('synthetic-products',schema,analyze_snapshot(schema).fingerprint,'source-a')
    return replace(direct_runtime.builtin_read('HeroProducts'),snapshot=snapshot,
        document='query Products($first:Int!,$offset:Int!) { '
                 'supply_product_versions(first:$first,offset:$offset,orderBy:"product_id") { product_id nr } }')


def isolate_saved_materialization(test):
    owner = patch.object(GraphQLServiceSource, '_saved_request', autospec=True,
        side_effect=lambda source, variables: SimpleNamespace(
            document=source.service.query_document, variables=dict(variables)))
    owner.start()
    test.addCleanup(owner.stop)


def versioned_contract_source(client, page_size=100):
    """Exercise the current saved reader with the historical wire fixtures."""
    from hero_integration.services.read_contract import ReadContract
    from hero_integration.sources.hero import CUSTOMER_QUERY_V1, PROJECT_QUERY_V1
    def source(operation, path, document, extra):
        contract = ReadContract('fixture', 1, 'fixture', 1, operation, path, 'id', (),
            {'first':'$page_size', 'offset':'$offset', 'orderBy':'id', **extra}, page_size)
        reader = GraphQLServiceSource(client, SimpleNamespace(query_document=document), contract,
            'default', revalidate=lambda: contract)
        reader._saved_request = lambda variables: SimpleNamespace(document=document, variables=variables)
        return reader
    reader = source('HeroProjectMatches', 'project_matches', PROJECT_QUERY_V1, {})
    reader.customer_source = source('HeroCustomers', 'contacts', CUSTOMER_QUERY_V1, {'showDeleted':True})
    return reader
