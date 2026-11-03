"""HERO project logbook in the Project activity."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from hero_integration.services import project_logbook as lb


class _Client:
    def __init__(self, pages, fail_sizes=()):
        self.pages, self.fail_sizes, self.calls = pages, set(fail_sizes), []

    def query(self, document, variables):
        self.calls.append((variables["first"], variables["offset"]))
        if variables["first"] in self.fail_sizes:
            raise RuntimeError("Internal server error")
        return {"project_matches": self.pages.get((variables["first"], variables["offset"]), [])}


# Links of two HERO sources; ids repeat across sources.
PROJECT_LINKS = [("1", "PV-1-H", "default"), ("2", "PV-2-H", "default"), ("1", "B-PROJECT-1", "source-b")]
HISTORY_LINKS = [("11", "default"), ("10", "source-b")]


class TestProjectLogbook(unittest.TestCase):
    def run_sync(self, client, known=(), instance="default"):
        added, links = [], [*HISTORY_LINKS, *((key, "default") for key in known)]

        def sql(query, params):
            return [(source_id, target) for source_id, target, source in PROJECT_LINKS if source == params[0]]

        def get_all(doctype, filters, pluck, limit_page_length):
            return [key for key, source in links if source == filters["source_instance"]]

        fake = SimpleNamespace(db=SimpleNamespace(sql=sql, commit=lambda: None), get_all=get_all,
                               log_error=lambda **k: None)
        with patch.object(lb, "frappe", fake), \
                patch.object(lb, "add_entry", side_effect=lambda p, e, u, s: added.append((p, e["id"], s))):
            counts = lb.sync_logbook(client, {}, source_instance=instance)
        return counts, added

    def test_new_entries_are_added_once_and_unknown_projects_skipped(self):
        page = [{"id": 1, "histories": [{"id": 10}, {"id": 11}]}, {"id": 3, "histories": [{"id": 30}]}]
        counts, added = self.run_sync(_Client({(25, 0): page}), known={"11"})
        self.assertEqual(added, [("PV-1-H", 10, "default")])
        self.assertEqual((counts["known"], counts["without_project"], counts["requests"]), (1, 1, 1))

    def test_a_rejected_page_is_retried_smaller(self):
        client = _Client({(12, 0): [{"id": 2, "histories": [{"id": 20}]}]}, fail_sizes={25})
        counts, added = self.run_sync(client)
        self.assertEqual(client.calls, [(25, 0), (12, 0)])
        self.assertEqual(added, [("PV-2-H", 20, "default")])

    def test_projects_and_known_entries_belong_to_the_run_source(self):
        page = [{"id": 1, "histories": [{"id": 10}, {"id": 11}]}]
        _counts, added = self.run_sync(_Client({(25, 0): page}), instance="source-b")
        # Source B: project 1 is B-PROJECT-1; entry 10 is known there, 11 is new for B.
        self.assertEqual(added, [("B-PROJECT-1", 11, "source-b")])
        _counts, added = self.run_sync(_Client({(25, 0): page}), instance="default")
        self.assertEqual(added, [("PV-1-H", 10, "default")])

    def test_a_source_instance_is_required(self):
        with self.assertRaises(TypeError):
            lb.sync_logbook(_Client({}), {})
        with self.assertRaises(ValueError):
            lb.sync_logbook(_Client({}), {}, source_instance=" ")


if __name__ == "__main__":
    unittest.main()
