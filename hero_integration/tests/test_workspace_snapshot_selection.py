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


def _schema(type_count: int) -> dict[str, object]:
    return {"__schema": {
        "queryType": {"name": "Query"},
        "types": [
            {"kind": "OBJECT", "name": "Query", "fields": []},
            *[
                {"kind": "OBJECT", "name": f"Type{index}", "fields": []}
                for index in range(1, type_count)
            ],
        ],
    }}


class _FrappeSnapshotSelectionMock:
    def __init__(self, documents: dict[str, object], rows: list[dict[str, object]]):
        self.documents = documents
        self.rows = rows
        self.permission_calls: list[str] = []

    def only_for(self, role: str) -> None:
        self.permission_calls.append(role)

    def get_all(self, doctype: str, **_kwargs):
        self.assert_doctype(doctype, "HERO GraphQL Schema Snapshot")
        return self.rows

    def get_doc(self, doctype: str, name: str):
        self.assert_doctype(doctype, "HERO GraphQL Schema Snapshot")
        return self.documents[name]

    def has_permission(self, doctype: str, permission_type: str) -> bool:
        self.assert_doctype(doctype, "HERO GraphQL Service")
        if permission_type != "write":
            raise AssertionError(permission_type)
        return True

    @staticmethod
    def assert_doctype(actual: str, expected: str) -> None:
        if actual != expected:
            raise AssertionError(actual)


class TestWorkspaceSnapshotSelection(unittest.TestCase):
    def test_explicit_full_snapshot_selection_exposes_distinct_metadata(self):
        synthetic_schema = _schema(3)
        full_schema = _schema(300)
        synthetic_fingerprint = analyze_snapshot(synthetic_schema).fingerprint
        full_fingerprint = analyze_snapshot(full_schema).fingerprint
        rows = [
            {"name": "SYNTHETIC", "schema_fingerprint": synthetic_fingerprint, "analysis_status": "Validated", "analysis_detail": "", "modified": "2026-10-19 10:41:22"},
            {"name": "FULL", "schema_fingerprint": full_fingerprint, "analysis_status": "Validated", "analysis_detail": "", "modified": "2026-10-19 10:29:39"},
        ]
        documents = {
            "SYNTHETIC": SimpleNamespace(snapshot_json=json.dumps(synthetic_schema)),
            "FULL": SimpleNamespace(snapshot_json=json.dumps(full_schema)),
        }
        frappe_mock = _FrappeSnapshotSelectionMock(documents, rows)

        with patch.object(api, "frappe", frappe_mock):
            context = api.workspace_context("FULL")
            default_context = api.workspace_context()

        self.assertEqual(context["connection_id"], "FULL")
        self.assertEqual(context["schema_fingerprint"], full_fingerprint)
        self.assertEqual(context["selected_snapshot"], "FULL")
        self.assertEqual(context["selected_snapshot_metadata"]["coverage"]["types"], 300)
        self.assertEqual(context["selected_snapshot_metadata"]["kind"], "Full schema")
        self.assertEqual(default_context["connection_id"], "FULL")
        self.assertEqual(
            [(item["name"], item["kind"], item["coverage"]["types"]) for item in context["snapshots"]],
            [("SYNTHETIC", "Synthetic/test schema", 3), ("FULL", "Full schema", 300)],
        )
        self.assertEqual(frappe_mock.permission_calls, ["System Manager", "System Manager"])
