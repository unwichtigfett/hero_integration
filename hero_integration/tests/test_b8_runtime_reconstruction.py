from __future__ import annotations

import json
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from hero_integration.services import b8_runtime
from hero_integration.services.mapping_contract_v1 import QueryAdapter, QueryDraft, SnapshotAdapter, StoredSnapshot
from hero_integration.services.migration_engine import SealedRead
from hero_integration.services.read_contract import ReadContract
from hero_integration.services.runtime_plan import FrappeRuntimePlanReferences, RuntimePlanSelection
from hero_integration.services.graphql_graph import analyze_snapshot


RAW_SCHEMA = {"__schema": {"queryType": {"name": "Query"}, "mutationType": None, "types": [
    {"kind": "OBJECT", "name": "Query", "fields": [{"name": "things", "args": [{"name": "first", "type": {"kind": "NON_NULL", "ofType": {"kind": "SCALAR", "name": "Int"}}}], "type": {"kind": "LIST", "ofType": {"kind": "OBJECT", "name": "Thing"}}}]},
    {"kind": "OBJECT", "name": "Thing", "fields": [{"name": "id", "args": [], "type": {"kind": "SCALAR", "name": "ID"}}, {"name": "name", "args": [], "type": {"kind": "SCALAR", "name": "String"}}]},
    {"kind": "SCALAR", "name": "ID"}, {"kind": "SCALAR", "name": "Int"}, {"kind": "SCALAR", "name": "String"},
]}}


def _context():
    fingerprint = analyze_snapshot(RAW_SCHEMA).fingerprint
    snapshot = SimpleNamespace(name="B8-SNAPSHOT", snapshot_json=json.dumps(RAW_SCHEMA), schema_fingerprint=fingerprint, source_instance="b8-source", analysis_status="Validated")
    draft = QueryDraft("SearchThings", "things", "things", ("id", "name"), {"first": 1})
    request = QueryAdapter().materialize(SnapshotAdapter().adapt(StoredSnapshot(snapshot.name, snapshot.snapshot_json, fingerprint, snapshot.source_instance)), draft)
    selection = RuntimePlanSelection("test-site", "b8-source", "B8-SERVICE", 3, 3, "B8-MAPPING", 4, snapshot.name, fingerprint, "SearchThings", "AUTO_SYNC")
    compilation = {"b8_compilation": {"version": 1, "operation_name": "SearchThings", "alias": "things", "field": "things", "selected_paths": ["id", "name"], "variables": draft.variables}}
    mapping = SimpleNamespace(name="B8-MAPPING", mapping_json=json.dumps(compilation))
    service = SimpleNamespace(name="B8-SERVICE", query_document=request.document)
    sealed = SealedRead(ReadContract("B8-SERVICE", 3, "B8-MAPPING", 4, "Projection", "things", "id", ("d",), {"first": "$page_size"}, 1), request.document, fingerprint, sha256(request.document.encode()).hexdigest())
    docs = {("HERO GraphQL Schema Snapshot", snapshot.name): snapshot, ("HERO GraphQL Service", service.name): service, ("HERO GraphQL Mapping", mapping.name): mapping}
    return selection, request, sealed, mapping, SimpleNamespace(get_doc=lambda doctype, name: docs[(doctype, name)])


class TestB8RuntimeReconstruction(unittest.TestCase):
    def test_reconstructs_the_same_b2_request_from_approved_inputs(self):
        selection, expected, sealed, _mapping, frappe = _context()
        with patch.object(FrappeRuntimePlanReferences, "validate_current"), patch.object(SealedRead, "from_documents", return_value=sealed):
            first = b8_runtime.reconstruct_materialized_read_plan(selection, frappe_module=frappe)
            second = b8_runtime.reconstruct_materialized_read_plan(selection, frappe_module=frappe)
        self.assertEqual(first.request, expected)
        self.assertEqual(second.request, expected)
        self.assertEqual(first.sealed, sealed)

    def test_rejects_inputs_that_no_longer_reproduce_the_approved_document(self):
        selection, _expected, sealed, mapping, frappe = _context()
        payload = json.loads(mapping.mapping_json)
        payload["b8_compilation"]["selected_paths"] = ["id"]
        mapping.mapping_json = json.dumps(payload)
        with patch.object(FrappeRuntimePlanReferences, "validate_current"), patch.object(SealedRead, "from_documents", return_value=sealed):
            with self.assertRaises(b8_runtime.RuntimeReconstructionError):
                b8_runtime.reconstruct_materialized_read_plan(selection, frappe_module=frappe)

    def test_rejects_compilation_arguments_or_bindings_outside_the_sealed_read_contract(self):
        selection, _expected, sealed, mapping, frappe = _context()
        payload = json.loads(mapping.mapping_json)
        payload["b8_compilation"]["variables"] = {"first": 2}
        mapping.mapping_json = json.dumps(payload)
        with patch.object(FrappeRuntimePlanReferences, "validate_current"), patch.object(SealedRead, "from_documents", return_value=sealed):
            with self.assertRaises(b8_runtime.RuntimeReconstructionError):
                b8_runtime.reconstruct_materialized_read_plan(selection, frappe_module=frappe)
        payload["b8_compilation"]["variables"] = {"first": 1}
        payload["b8_compilation"]["alias"] = "not_the_sealed_root"
        mapping.mapping_json = json.dumps(payload)
        with patch.object(FrappeRuntimePlanReferences, "validate_current"), patch.object(SealedRead, "from_documents", return_value=sealed):
            with self.assertRaises(b8_runtime.RuntimeReconstructionError):
                b8_runtime.reconstruct_materialized_read_plan(selection, frappe_module=frappe)

    def test_rejects_nested_dynamic_compilation_values_before_materialization(self):
        selection, _expected, _sealed, _mapping, _frappe = _context()
        with self.assertRaises(b8_runtime.RuntimeReconstructionError):
            b8_runtime._draft(
                {"version": 1, "operation_name": "SearchThings", "alias": "things", "field": "things",
                 "selected_paths": ["id"], "variables": {"first": {"unreviewed": "$cursor"}}},
                selection,
            )
