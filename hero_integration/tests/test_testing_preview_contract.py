from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from hero_integration.sources.hero import SourcePage, TestingPreviewSource

try:
    import frappe  # noqa: F401
except ModuleNotFoundError:
    import sys
    import types

    frappe = types.ModuleType("frappe")
    frappe_utils = types.ModuleType("frappe.utils")
    frappe_utils.now_datetime = lambda: None
    sys.modules["frappe"] = frappe
    sys.modules["frappe.utils"] = frappe_utils

from hero_integration.services import migration


class _VerifiedSource:
    source_instance = "synthetic"

    def __init__(self):
        self.calls: list[tuple[str, int]] = []

    @staticmethod
    def testing_ordering(operation: str) -> dict[str, object]:
        return {"verified": True, "order": "created DESC"}

    def customers(self, offset: int) -> SourcePage:
        self.calls.append(("customers", offset))
        return SourcePage(({"id": 3, "created": "2026-10-23"},) if offset == 0 else ({"id": 2, "created": "2026-10-22"},), 1 if offset == 0 else None)

    def projects(self, offset: int) -> SourcePage:
        self.calls.append(("projects", offset))
        return SourcePage(({"id": 9, "created": "2026-10-23"},), None)


class _UnverifiedSource(_VerifiedSource):
    @staticmethod
    def testing_ordering(operation: str) -> dict[str, object]:
        return {"verified": False, "order": "id", "reason": "Synthetic endpoint gives no descending-order proof."}

    def customers(self, offset: int) -> SourcePage:
        raise AssertionError("An unverified source must not receive a request.")


class TestTestingPreviewContract(unittest.TestCase):
    def test_shared_budget_limits_multiple_sources_and_pagination(self):
        source = _VerifiedSource()
        limited = TestingPreviewSource(source, 2)

        customers = limited.customers(0)
        projects = limited.projects(0)
        stopped = limited.customers(customers.next_offset or 0)

        self.assertEqual(source.calls, [("customers", 0), ("projects", 0)])
        self.assertEqual([row["id"] for row in customers.records], [3])
        self.assertEqual([row["id"] for row in projects.records], [9])
        self.assertEqual(stopped.records, ())
        self.assertEqual((limited.executed_actions, limited.records_received, limited.status), (2, 2, "LIMIT_REACHED"))
        self.assertEqual(limited.results["HeroCustomers"]["status"], "LIMIT_REACHED")
        self.assertEqual(limited.results["HeroProjectMatches"]["status"], "COMPLETED")

    def test_missing_descending_order_blocks_before_any_request(self):
        source = _UnverifiedSource()
        limited = TestingPreviewSource(source, 5)

        page = limited.customers(0)

        self.assertEqual(page.records, ())
        self.assertEqual((limited.executed_actions, limited.records_received, limited.status), (0, 0, "BLOCKED"))
        self.assertEqual(limited.results["HeroCustomers"]["status"], "BLOCKED_ORDER_UNVERIFIED")

    def test_source_that_never_receives_budget_is_marked_skipped_limit(self):
        source = _VerifiedSource()
        limited = TestingPreviewSource(source, 1)

        limited.customers(0)
        limited.projects(0)

        self.assertEqual(source.calls, [("customers", 0)])
        self.assertEqual(limited.results["HeroProjectMatches"]["status"], "SKIPPED_LIMIT")

    def test_unverified_descending_id_is_not_accepted_as_newest_first(self):
        source = _VerifiedSource()
        source.testing_ordering = lambda operation: {"verified": True, "order": "id"}
        limited = TestingPreviewSource(source, 5)

        self.assertEqual(limited.customers(0).records, ())
        self.assertEqual(limited.status, "BLOCKED")

    def test_testing_run_constructs_a_zero_retry_client_without_changing_normal_runs(self):
        calls = []

        class Client:
            def __init__(self, *args, **kwargs):
                calls.append(kwargs["max_retries"])

        config = SimpleNamespace(endpoint="synthetic", api_token="synthetic", timeout_seconds=1, source_instance="synthetic")
        with patch.object(migration, "HeroGraphQLClient", Client):
            migration._source_for_run(SimpleNamespace(mode="Preview", testing_enabled=1, graphql_service=""), config)
            migration._source_for_run(SimpleNamespace(mode="Preview", testing_enabled=0, graphql_service=""), config)

        self.assertEqual(calls, [0, 2])
