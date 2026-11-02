from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from hero_integration.sources.hero import SourcePage, TestingPreviewSource as PreviewSource

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
    def test_legacy_midpage_resume_blocks_before_read_without_page_evidence(self):
        run = SimpleNamespace(resume_phase='customer', resume_offset=0, resume_record_index=1,
                              state='Running', reload=lambda:None, save=lambda:None)
        calls = []
        def reader(offset):
            calls.append(offset)
            return SourcePage(({'id':2}, {'id':3}), None)
        with patch.object(migration, '_phase_state', return_value='Pending'), \
             patch.object(migration, '_set_phase_state') as phase, patch.object(migration, '_block_run') as block, \
             patch.object(migration, '_migrate_customer'), patch.object(migration, '_record_result'), \
             patch.object(migration, '_record_phase_result'), patch.object(migration, '_phase_has_blockers', return_value=False):
            result = migration._process_phase(run, 'customer', reader)
        self.assertFalse(result)
        self.assertEqual(calls, [])
        self.assertEqual(phase.call_args_list[-1].args, (run, 'customer', 'Blocked'))
        self.assertIn('page', block.call_args.args[1].lower())

    def test_legacy_page_boundary_resume_blocks_without_stable_snapshot(self):
        run = SimpleNamespace(resume_phase='customer', resume_offset=2, resume_record_index=0,
                              state='Running', reload=lambda:None, save=lambda:None)
        calls = []
        def reader(offset):
            calls.append(offset)
            return SourcePage((), None)
        with patch.object(migration, '_phase_state', return_value='Pending'), \
             patch.object(migration, '_set_phase_state'), patch.object(migration, '_block_run') as block, \
             patch.object(migration, '_phase_has_blockers', return_value=False):
            result = migration._process_phase(run, 'customer', reader)
        self.assertFalse(result)
        self.assertEqual(calls, [])
        self.assertIn('snapshot', block.call_args.args[1].lower())

    def test_projection_resume_blocks_without_snapshot_before_read(self):
        from hero_integration.services import read_contract
        for operation in ('HeroTasks', 'HeroCalendarEvents'):
            run = SimpleNamespace(graphql_service='service', graphql_mapping='mapping', service_revision=1,
                                  mapping_revision=1, resume_offset=1, resume_record_index=0)
            service = SimpleNamespace(schema_snapshot='snapshot')
            mapping = SimpleNamespace(mapping_json='{}')
            fake = SimpleNamespace(get_doc=lambda doctype, _name: {'HERO GraphQL Service':service,
                'HERO GraphQL Mapping':mapping, 'HERO GraphQL Schema Snapshot':object()}[doctype])
            calls = []
            source = SimpleNamespace(contract=SimpleNamespace(target_operation=operation),
                                     projection=lambda offset: calls.append(offset))
            reviewed = SimpleNamespace(target_operation=operation, service_revision=1, mapping_revision=1)
            with patch.object(migration, 'frappe', fake), patch.object(migration, '_mapping_plan_for_run'), \
                 patch.object(read_contract, 'from_documents', return_value=reviewed), \
                 patch.object(migration, '_block_run') as block:
                migration._process_projection(run, source)
            self.assertEqual(calls, [])
            self.assertIn('snapshot', block.call_args.args[1].lower())

    def test_legacy_phase_rejects_repeated_identity_before_second_page_write(self):
        run = SimpleNamespace(resume_phase='', resume_offset=0, resume_record_index=0,
                              state='Running', reload=lambda:None, save=lambda:None)
        pages = {0:SourcePage(({'id':1}, {'id':2}), 2), 2:SourcePage(({'id':2}, {'id':3}), None)}
        processed = []
        with patch.object(migration, '_phase_state', return_value='Pending'), \
             patch.object(migration, '_set_phase_state'), patch.object(migration, '_block_run'), \
             patch.object(migration, '_migrate_isolated', side_effect=lambda action: action()), \
             patch.object(migration, '_migrate_customer', side_effect=lambda record, _run: processed.append(record['id'])), \
             patch.object(migration, '_record_result'), patch.object(migration, '_record_phase_result'), \
             patch.object(migration, '_phase_has_blockers', return_value=False):
            with self.assertRaisesRegex(migration.MappingPlanError, 'identity'):
                migration._process_phase(run, 'customer', lambda offset: pages[offset])
        self.assertEqual(processed, [1, 2])

    def test_legacy_phase_rejects_nonadvancing_page_before_record_write(self):
        run = SimpleNamespace(resume_phase='', resume_offset=0, resume_record_index=0,
                              state='Running', reload=lambda:None, save=lambda:None)
        with patch.object(migration, '_phase_state', return_value='Pending'), \
             patch.object(migration, '_set_phase_state'), patch.object(migration, '_migrate_customer') as writer:
            with self.assertRaisesRegex(migration.MappingPlanError, 'pagination'):
                migration._process_phase(run, 'customer', lambda _offset: SourcePage(({'id':1},), 0))
        writer.assert_not_called()

    def test_shared_budget_limits_multiple_sources_and_pagination(self):
        source = _VerifiedSource()
        limited = PreviewSource(source, 2)

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
        limited = PreviewSource(source, 5)

        page = limited.customers(0)

        self.assertEqual(page.records, ())
        self.assertEqual((limited.executed_actions, limited.records_received, limited.status), (0, 0, "BLOCKED"))
        self.assertEqual(limited.results["HeroCustomers"]["status"], "BLOCKED_ORDER_UNVERIFIED")

    def test_source_that_never_receives_budget_is_marked_skipped_limit(self):
        source = _VerifiedSource()
        limited = PreviewSource(source, 1)

        limited.customers(0)
        limited.projects(0)

        self.assertEqual(source.calls, [("customers", 0)])
        self.assertEqual(limited.results["HeroProjectMatches"]["status"], "SKIPPED_LIMIT")

    def test_failed_preview_request_consumes_budget_without_retry(self):
        source = _VerifiedSource()
        calls = []
        def fail(offset):
            calls.append(offset)
            raise RuntimeError("synthetic transport failure")
        source.customers = fail
        limited = PreviewSource(source, 1)

        with self.assertRaisesRegex(RuntimeError, "transport failure"):
            limited.customers(0)
        self.assertEqual((calls, limited.executed_actions, limited.status), ([0], 1, "FAILED"))
        self.assertEqual(limited.projects(0).records, ())
        self.assertEqual(source.calls, [])
        self.assertEqual(limited.results["HeroProjectMatches"]["status"], "SKIPPED_LIMIT")

    def test_unverified_descending_id_is_not_accepted_as_newest_first(self):
        source = _VerifiedSource()
        source.testing_ordering = lambda operation: {"verified": True, "order": "id"}
        limited = PreviewSource(source, 5)

        self.assertEqual(limited.customers(0).records, ())
        self.assertEqual(limited.status, "BLOCKED")

    def test_historical_runs_are_rejected_before_any_client_is_constructed(self):
        calls = []

        class Client:
            def __init__(self, *args, **kwargs):
                calls.append(kwargs["max_retries"])

        config = SimpleNamespace(endpoint="synthetic", api_token="synthetic", timeout_seconds=1, source_instance="synthetic",
                                 source_binding_revision=0, source_binding_confirmed=True)
        # Runs without approved endpoint-bound reads fail before client creation;
        # approved testing previews use a zero-retry client (_source_for_run).
        with patch.object(migration, "HeroGraphQLClient", Client):
            for testing in (1, 0):
                with self.assertRaises(migration.MappingPlanError):
                    migration._source_for_run(SimpleNamespace(mode="Preview", testing_enabled=testing, graphql_service=""), config)

        self.assertEqual(calls, [])
