"""Auxiliary reads retain schema and approval boundaries."""
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
from graphql import build_schema, introspection_from_schema, parse, validate
from hero_integration.services.graphql_graph import analyze_snapshot
from hero_integration.services.mapping_contract_v1 import SnapshotAdapter, StoredSnapshot
from hero_integration.services.read_contract import ReadContractError
from hero_integration.sources.hero import GraphQLServiceSource


class TestAuxiliaryMaterializer(unittest.TestCase):
    def setUp(self):
        self.schema = build_schema('type Query { company:Company } type Company { partners(show_deleted:Boolean):[Partner] } '
            'type Partner { user:User } type User { id:Int email:String }')
        raw = introspection_from_schema(self.schema)
        self.snapshot = SnapshotAdapter().adapt(StoredSnapshot('aux', raw, analyze_snapshot(raw).fingerprint, 'source'))
        self.contract = NS()
        self.client = NS(query=Mock(return_value={'company': {'partners': [{'user': {'id': 1, 'email': 'a@example.invalid'}}]}}))
        self.source = GraphQLServiceSource(self.client, NS(), self.contract, 'source', revalidate=Mock(return_value=self.contract))

    def test_nested_user_argument_is_parameterized_and_schema_valid(self):
        with patch.object(self.source, '_auxiliary_snapshot', return_value=self.snapshot):
            self.assertEqual(self.source.user_emails(), {1: 'a@example.invalid'})
        document, variables = self.client.query.call_args.args
        self.assertFalse(validate(self.schema, parse(document)))
        self.assertIn('show_deleted: $', document)
        self.assertIn(True, variables.values())
        self.assertEqual(self.source.revalidate.call_count, 2)

    def test_changed_approval_blocks_transport_or_discards_response(self):
        self.source.revalidate.return_value = object()
        with self.assertRaises(ReadContractError):
            self.source.user_emails()
        self.client.query.assert_not_called()
        self.source.revalidate.side_effect = [self.contract, object()]
        with patch.object(self.source, '_auxiliary_snapshot', return_value=self.snapshot), self.assertRaises(ReadContractError):
            self.source.user_emails()
        self.client.query.assert_called_once()
