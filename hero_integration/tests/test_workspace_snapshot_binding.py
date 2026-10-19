from __future__ import annotations

import json
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch

try:
    import frappe  # noqa: F401
except ModuleNotFoundError:
    frappe = types.ModuleType("frappe")
    frappe.whitelist = lambda: lambda function: function
    sys.modules["frappe"] = frappe

from hero_integration import api
from hero_integration.services.graphql_graph import analyze_snapshot


SNAPSHOT = {"__schema": {
    "queryType": {"name": "Query"},
    "types": [
        {"kind": "OBJECT", "name": "Query", "fields": [
            {"name": "customer", "type": {"kind": "OBJECT", "name": "Customer"}},
        ]},
        {"kind": "OBJECT", "name": "Customer", "fields": [
            {"name": "id", "type": {"kind": "SCALAR", "name": "ID"}},
        ]},
        {"kind": "SCALAR", "name": "ID"},
    ],
}}


class _FrappeSnapshotMock:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.permission_calls: list[str] = []
        self.document_calls: list[tuple[str, str]] = []

    def only_for(self, role: str) -> None:
        self.permission_calls.append(role)

    def get_doc(self, doctype: str, name: str):
        self.document_calls.append((doctype, name))
        return self.snapshot


class TestWorkspaceSnapshotBinding(unittest.TestCase):
    def setUp(self):
        self.graph = analyze_snapshot(SNAPSHOT)
        self.snapshot = SimpleNamespace(
            analysis_status="Validated",
            snapshot_json=json.dumps(SNAPSHOT),
            schema_fingerprint=self.graph.fingerprint,
            modified="2026-10-19 10:00:00",
        )
        self.frappe = _FrappeSnapshotMock(self.snapshot)
        self.patch = patch.object(api, "frappe", self.frappe)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_workspace_schema_keeps_snapshot_metadata(self):
        result = api.workspace_schema("SAVED-SNAPSHOT", page_size=1)

        self.assertEqual(result["captured_at"], self.snapshot.modified)
        self.assertEqual(result["schema_fingerprint"], self.graph.fingerprint)
        self.assertEqual(result["total"], 3)
        self.assertEqual(self.frappe.permission_calls, ["System Manager"])

    def test_workspace_graph_node_focus_keeps_snapshot_fingerprint(self):
        result = api.workspace_graph("SAVED-SNAPSHOT", node_id="field:Customer.id")

        self.assertEqual(result["node"]["node_id"], "field:Customer.id")
        self.assertEqual(result["schema_fingerprint"], self.graph.fingerprint)
        self.assertEqual(self.frappe.permission_calls, ["System Manager"])

    def test_workspace_graph_rejects_unknown_node_with_permissions_intact(self):
        with self.assertRaisesRegex(ValueError, "does not exist"):
            api.workspace_graph("SAVED-SNAPSHOT", node_id="type:Unknown")

        self.assertEqual(self.frappe.permission_calls, ["System Manager"])
