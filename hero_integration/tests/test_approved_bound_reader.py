"""Exact-id narrowing preserves the sealed selection, aliases and source authority."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from graphql import build_schema, introspection_from_schema

from hero_integration.services.b8_runtime import ApprovedBoundReader, RuntimeReconstructionError
from hero_integration.services.graphql_graph import analyze_snapshot
from hero_integration.services.mapping_contract_v1 import QueryAdapter, QueryDraft, SnapshotAdapter, StoredSnapshot


class TestApprovedBoundReader(unittest.TestCase):
    def reader(self, root, operation, payload):
        schema = introspection_from_schema(build_schema("""
            type Query { contacts(first: Int!, ids: [Int!]): [Contact],
                         project_matches(first: Int!): [Contact], project_match(project_match_id: Int!): Contact,
                         other(first: Int!): [Contact] }
            type Contact { id: ID!, name: String }
        """))
        snapshot = SnapshotAdapter().adapt(StoredSnapshot("s", json.dumps(schema),
            analyze_snapshot(schema).fingerprint, "unit-source"))
        request = QueryAdapter().materialize(snapshot, QueryDraft(
            "Review", "records", root, ("id", "name"), {"first": 5}))
        contract = SimpleNamespace(target_operation=operation, records_path="records", identity_path="id")
        reader = ApprovedBoundReader(SimpleNamespace(source_instance="unit-source", request=request,
            sealed=SimpleNamespace(contract=contract)), schema, 3, 30)
        reader.client = Mock()
        reader.client.query.return_value = payload
        return reader

    def setUp(self):
        self.config = SimpleNamespace(source_binding_confirmed=True, source_binding_revision=3,
                                      source_instance="unit-source")
        patcher = patch("hero_integration.client.settings.get_configuration", return_value=self.config)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_schema_proven_exact_id_preserves_aliased_selected_fields(self):
        row = {"id":"7","name":"Example"}
        for root, operation, result, narrow in [
                ("contacts", "HeroCustomers", [row], "ids: [7]"),
                ("project_matches", "HeroProjectMatches", row, "project_match(project_match_id: 7)")]:
            with self.subTest(root=root):
                reader = self.reader(root, operation, {"records": result})
                self.assertEqual(reader.read_bound("7"), row)
                self.assertIn(narrow, reader.client.query.call_args.args[0])

    def test_unknown_filter_changed_source_or_wrong_response_identity(self):
        reader = self.reader("other", "HeroCustomers", {})
        self.assertFalse(reader.capabilities.bounded_comparison)
        with self.assertRaises(RuntimeReconstructionError):
            reader.read_bound("7")
        reader.client.query.assert_not_called()
        reader = self.reader("contacts", "HeroCustomers", {"records":[{"id":"8","name":"Example"}]})
        with self.assertRaises(RuntimeReconstructionError):
            reader.read_bound("7")
        reader.client.query.reset_mock()
        self.config.source_binding_revision = 4
        with self.assertRaises(RuntimeReconstructionError):
            reader.read_bound("7")
        reader.client.query.assert_not_called()
