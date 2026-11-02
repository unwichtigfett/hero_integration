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


class TestProjectLogbook(unittest.TestCase):
    def run_sync(self, client, known=()):
        added = []
        fake = SimpleNamespace(
            db=SimpleNamespace(sql=lambda *a, **k: [("1", "PV-1-H"), ("2", "PV-2-H")], commit=lambda: None),
            get_all=lambda *a, **k: list(known), log_error=lambda **k: None)
        with patch.object(lb, "frappe", fake), patch.object(lb, "add_entry", side_effect=lambda p, e, u: added.append((p, e["id"]))):
            counts = lb.sync_logbook(client, {})
        return counts, added

    def test_new_entries_are_added_once_and_unknown_projects_skipped(self):
        page = [{"id": 1, "histories": [{"id": 10}, {"id": 11}]}, {"id": 3, "histories": [{"id": 30}]}]
        counts, added = self.run_sync(_Client({(25, 0): page}), known={"11"})
        self.assertEqual(added, [("PV-1-H", 10)])
        self.assertEqual((counts["known"], counts["without_project"], counts["requests"]), (1, 1, 1))

    def test_a_rejected_page_is_retried_smaller(self):
        client = _Client({(12, 0): [{"id": 2, "histories": [{"id": 20}]}]}, fail_sizes={25})
        counts, added = self.run_sync(client)
        self.assertEqual(client.calls, [(25, 0), (12, 0)])
        self.assertEqual(added, [("PV-2-H", 20)])


if __name__ == "__main__":
    unittest.main()
