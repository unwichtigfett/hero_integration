from __future__ import annotations

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


class _OverviewFrappe:
    def __init__(self):
        self.roles: list[str] = []
        self.db = SimpleNamespace(count=self._count)

    def only_for(self, role: str) -> None:
        self.roles.append(role)

    @staticmethod
    def get_single(doctype: str):
        assert doctype == "HERO Integration Settings"
        return SimpleNamespace(
            hero_api_url="https://configured.invalid/graphql",
            allowed_hero_hosts="configured.invalid\n",
            scheduled_sync_enabled=0,
            sync_status="Not started",
            last_successful_sync=None,
        )

    @staticmethod
    def _count(doctype: str, filters=None) -> int:
        if doctype == "HERO Frappe Mapping":
            return {"Draft": 2, "Active": 1, "Disabled": 0, "Rejected": 3}[filters["status"]]
        return {"HERO GraphQL Schema Snapshot": 4, "HERO GraphQL Service": 5}[doctype]

    @staticmethod
    def get_all(doctype: str, **kwargs):
        assert doctype == "HERO Integration Run"
        assert kwargs["limit_page_length"] == 1
        return [{"name": "RUN-0001", "action": "Sync", "state": "Completed", "modified": "2026-10-21"}]


class TestWorkspaceOverviewContract(unittest.TestCase):
    def test_overview_returns_only_stored_redacted_status(self):
        frappe_mock = _OverviewFrappe()

        with patch.object(api, "frappe", frappe_mock):
            result = api.integration_overview()

        self.assertEqual(frappe_mock.roles, ["System Manager"])
        self.assertTrue(result["connection"]["endpoint_configured"])
        self.assertEqual(result["connection"]["allowed_host_count"], 1)
        self.assertEqual(result["mapping_counts"], {"Draft": 2, "Active": 1, "Disabled": 0, "Rejected": 3})
        self.assertEqual(result["snapshot_count"], 4)
        self.assertEqual(result["service_count"], 5)
        self.assertEqual(result["latest_run"][0]["name"], "RUN-0001")
        self.assertNotIn("hero_api_token", repr(result))
