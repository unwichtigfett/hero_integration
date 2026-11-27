"""Exact-id narrowing preserves the sealed selection, aliases and source authority."""
import json
import os
from pathlib import Path
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
        contract = SimpleNamespace(target_operation=operation, records_path="records", identity_path="id", page_size=5)
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

    def test_batch_accepts_only_the_complete_unique_requested_set(self):
        rows = [{"id": str(i), "name": "bound"} for i in (7, 8)]
        reader = self.reader("contacts", "HeroCustomers", {"records": list(reversed(rows))})
        self.assertEqual(reader.read_bound_many(("7", "8")), {row["id"]: row for row in rows})
        self.assertIn("ids: [7, 8]", reader.client.query.call_args.args[0])
        for broken in ([rows[0]], [rows[0], rows[0]], [rows[0], {"id": "9", "name": "foreign"}]):
            reader.client.query.return_value = {"records": broken}
            with self.assertRaises(RuntimeReconstructionError):
                reader.read_bound_many(("7", "8"))
        reader.client.query.reset_mock()
        with self.assertRaises(RuntimeReconstructionError):
            reader.read_bound_many(("7", "7"))
        reader.client.query.assert_not_called()

    def test_list_filters_batch_only_approved_ids_in_all_five_eligible_areas(self):
        areas = (("HeroCustomers", "contacts", "ids"), ("HeroProjectMatches", "project_matches", "ids"),
                 ("HeroTasks", "tasks", "ids"), ("HeroCalendarEvents", "calendar_events", "ids"),
                 ("HeroServices", "supply_services", "service_ids"))
        if reference := os.environ.get("HERO_REFERENCE_SCHEMA"):
            raw = json.loads(Path(reference).read_text())
            schema = raw.get("data", raw)
        else:
            roots = " ".join(f"{root}(first:Int, {arg}:[Int]):[Row]" for _, root, arg in areas)
            schema = introspection_from_schema(build_schema("type Row {id:Int!} type Query {" + roots + "}"))
        snapshot = SnapshotAdapter().adapt(StoredSnapshot("s", json.dumps(schema),
            analyze_snapshot(schema).fingerprint, "unit-source"))
        for area, root, argument in areas:
            with self.subTest(root=root):
                request = QueryAdapter().materialize(snapshot, QueryDraft("Review", "records", root, ("id",), {"first": 10}))
                contract = SimpleNamespace(target_operation=area, records_path="records", identity_path="id", page_size=10)
                reader = ApprovedBoundReader(SimpleNamespace(source_instance="unit-source", request=request,
                    sealed=SimpleNamespace(contract=contract)), schema, 3, 30)
                reader.client = Mock()
                reader.client.query.return_value = {"records": [{"id": 8}, {"id": 7}]}
                self.assertEqual(reader.read_bound_many(("7", "8")), {"7": {"id": 7}, "8": {"id": 8}})
                reader.client.query.assert_called_once()
                self.assertIn(f"{argument}: [7, 8]", reader.client.query.call_args.args[0])

    def test_project_collection_preserves_approved_scope_and_rejects_existing_id_filter(self):
        schema = introspection_from_schema(build_schema("""
            type Query { project_matches(first:Int, status:String, ids:[Int]):[Project]
                         project_match(project_match_id:Int):Project }
            type Project {id:Int!}
        """))
        snapshot = SnapshotAdapter().adapt(StoredSnapshot("s", json.dumps(schema),
            analyze_snapshot(schema).fingerprint, "unit-source"))
        contract = SimpleNamespace(target_operation="HeroProjectMatches", records_path="records", identity_path="id", page_size=5)
        def approved(arguments):
            request = QueryAdapter().materialize(snapshot, QueryDraft("Review", "records", "project_matches", ("id",), arguments))
            return ApprovedBoundReader(SimpleNamespace(source_instance="unit-source", request=request,
                sealed=SimpleNamespace(contract=contract)), schema, 3, 30)

        reader = approved({"first": 5, "status": "assigned"})
        reader.client = Mock()
        reader.client.query.return_value = {"records": [{"id": 7}]}
        self.assertEqual(reader.read_bound("7"), {"id": 7})
        document, variables = reader.client.query.call_args.args
        self.assertIn("project_matches(", document)
        self.assertIn("status:", document)
        self.assertIn("assigned", variables.values())
        reader = approved({"first": 5, "ids": [10]})
        self.assertFalse(reader.capabilities.bounded_comparison)
        reader.client = Mock()
        with self.assertRaises(RuntimeReconstructionError):
            reader.read_bound("7")
        reader.client.query.assert_not_called()

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
