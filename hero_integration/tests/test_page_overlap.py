"""Synthetic offset contracts; no external transports or site are required."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from hero_integration.services import polling
from hero_integration.services.offset_pagination import advance, validate_overlap
from hero_integration.sources.hero import SourcePage


class TestOffsetWindow(unittest.TestCase):
    def test_full_pages_advance_with_overlap_and_short_page_to_empty(self):
        rows = [{"id": i, "v": 1} for i in range(123)]
        for overlap in (0, 1, 10, 20):
            with self.subTest(overlap=overlap):
                pages, result, checkpoint, offset = [], [], None, 0
                while offset is not None:
                    pages.append(offset)
                    page = advance(rows[offset:offset + 50], offset=offset, size=50,
                                   previous=checkpoint, overlap=overlap)
                    result.extend(page.rows)
                    offset, checkpoint = page.next_offset, page.checkpoint
                self.assertEqual(result, rows)
                self.assertEqual(pages[:2], [0, 50-overlap])
                self.assertEqual(pages[-1], len(rows))

    def test_resume_and_new_version_at_previous_position(self):
        rows = [{"id": i, "value": "a"} for i in range(8)]
        first = advance(rows[:5], offset=0, size=5, overlap=2)
        rows[3]["value"] = "b"
        page = advance(rows[3:], offset=3, size=5, previous=first.checkpoint, overlap=2)
        self.assertEqual([r["id"] for r in page.rows], [3, 5, 6, 7])
        self.assertEqual(page.changed, {"3"})
        for offset, size, overlap in [(4, 5, 2), (3, 6, 2), (3, 5, 1)]:
            with self.assertRaises(ValueError):
                advance(rows[3:], offset=offset, size=size, overlap=overlap, previous=first.checkpoint)

    def test_invalid_size_value_and_response_are_rejected(self):
        for value, size in [(True, 50), (1.5, 50), ("1.5", 50), (-1, 50), (21, 50), (2, 2), (3, 2)]:
            with self.assertRaises(ValueError):
                validate_overlap(value, size)
        with self.assertRaises(ValueError):
            advance([{"id": 1}, {"id": 2}], offset=0, size=1, overlap=0)
        with self.assertRaises(ValueError):
            advance([{"id": 1}, {"id": 1}], offset=0, size=2, overlap=0)
        with self.assertRaises(ValueError):
            advance([{"id": None}], offset=0, size=2, overlap=1)

    def test_saved_reader_passes_physical_offsets_and_marks_changed_repetitions(self):
        from hero_integration.tests.saved_reader_fixture import versioned_contract_source
        from hero_integration.services.migration import _validate_enumeration_page

        rows = [{"id": i, "version": 1} for i in range(7)]
        offsets = []
        def query(_document, variables):
            offset, size = variables["offset"], variables["first"]
            offsets.append(offset)
            if offset == 3:
                rows[3] = {"id": 3, "version": 2}
            return {"project_matches": rows[offset:offset + size]}
        source = versioned_contract_source(SimpleNamespace(query=query), page_size=5)
        source.page_overlap = 2
        seen = set()
        first = source.projects(0)
        _validate_enumeration_page(first, 0, seen)
        second = source.projects(first.next_offset)
        _validate_enumeration_page(second, first.next_offset, seen)
        self.assertEqual(list(second.changed_overlap_ids), ["3"])
        self.assertEqual([row["id"] for row in second.records], [3, 5, 6])
        self.assertEqual(second.next_offset, 7)
        self.assertEqual(offsets, [0, 3])

    def test_overlap_is_inherited_from_parent_and_immutable_after_insert(self):
        from frappe import _dict
        from hero_integration.hero_integration.doctype.hero_integration_run.hero_integration_run import HEROIntegrationRun
        parent = SimpleNamespace(page_overlap=10)
        run = SimpleNamespace(flags=_dict(parent_execution_run="parent"), checkpoint_contract_json='{"version":1}')
        with patch("frappe.get_doc", return_value=parent), patch("frappe.get_single") as current_settings:
            HEROIntegrationRun.before_insert(run)
        self.assertEqual(run.page_overlap, 10)
        self.assertIn('"page_overlap": 10', run.checkpoint_contract_json)
        current_settings.assert_not_called()
        run.get_doc_before_save = lambda: parent
        run.page_overlap = 11
        with self.assertRaises(ValueError):
            HEROIntegrationRun.validate(run)

    def test_monitor_processes_new_versions_but_ignores_unchanged_overlap(self):
        requests, observations = [], []
        rows = [{"id": i, "version": 1} for i in range(6)]
        def read(offset):
            requests.append(offset)
            if offset == 2:
                rows[2] = {"id": 2, "version": 2}
            selected = rows[offset:offset + 4]
            return SourcePage(tuple(selected), offset + len(selected) if selected else None)
        with patch.object(polling.time, "sleep"):
            result = polling.monitor(SimpleNamespace(customers=read, source_instance="test"),
                                     {"sources": ["HeroCustomers"], "page_size": 4, "page_overlap": 2},
                                     record_observer=lambda _, row: observations.append(row))
        self.assertEqual(requests, [0, 2, 4, 6])
        self.assertEqual([(r["id"], r["version"]) for r in observations],
                         [(0, 1), (1, 1), (2, 1), (3, 1), (2, 2), (4, 1), (5, 1)])
        self.assertEqual(result.customer_records, 7)

    def test_bounded_monitor_can_resume_only_with_its_actual_offset_window(self):
        rows = [{"id": i} for i in range(7)]
        def read(offset):
            records = rows[offset:offset + 4]
            return SourcePage(tuple(records), offset + len(records) if records else None)
        plan = {"sources": ["HeroCustomers"], "page_size": 4, "page_overlap": 2, "max_pages": 1}
        source = SimpleNamespace(customers=read, source_instance="test")
        first = polling.monitor(source, plan)
        self.assertEqual((first.customer_records, first.customer_next_offset), (4, 2))
        with self.assertRaises(polling.PaginationProgressError):
            polling.monitor(source, plan, customer_offset=first.customer_next_offset)
        second = polling.monitor(source, plan, customer_offset=first.customer_next_offset,
                                 offset_checkpoints=first.offset_checkpoints)
        self.assertEqual((second.customer_records, second.customer_next_offset), (2, 4))

    def test_plan_identifies_unsupported_fast_mode_and_invalid_overlap(self):
        plan = polling.runtime_plan(SimpleNamespace(polling_read_mode="FAST", polling_page_overlap=5, polling_page_size=5))
        self.assertFalse(plan["fast_available"])
        self.assertFalse(plan["can_auto_sync"])
        self.assertIn("setting polling_page_overlap", plan["unsupported_sources"])
