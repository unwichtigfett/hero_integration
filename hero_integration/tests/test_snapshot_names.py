"""Display names S-ddmmyy-NN for schema snapshots; the internal name stays the identity."""
from __future__ import annotations

import unittest
from datetime import datetime

from hero_integration.services.snapshot_names import display_names, newest


class TestSnapshotNames(unittest.TestCase):
    def test_third_snapshot_of_a_day_is_numbered_03(self):
        rows = [
            {"name": "c-uid", "creation": "2026-10-31 18:00:00.000001"},
            {"name": "a-uid", "creation": "2026-10-31 08:00:00"},
            {"name": "b-uid", "creation": datetime(2026, 10, 31, 9, 30)},
            {"name": "d-uid", "creation": "2026-11-01 00:00:01"},
            {"name": "e-uid", "creation": "2026-10-30 23:59:59"},
        ]
        self.assertEqual(display_names(rows), {
            "e-uid": "S-301026-01", "a-uid": "S-311026-01", "b-uid": "S-311026-02", "c-uid": "S-311026-03", "d-uid": "S-011126-01",
        })

    def test_fallback_times_and_unusable_rows(self):
        rows = [{"name": "x", "captured_at": "2026-10-29T19:00:00Z"}, {"name": "y", "modified": "2026-10-29 07:00:00"},
                {"name": "z"}, {"name": "", "creation": "2026-10-29 01:00:00"}]
        self.assertEqual(display_names(rows), {"y": "S-291026-01", "x": "S-291026-02", "z": "z"})

    def test_same_second_is_ordered_by_name_and_numbers_continue_past_99(self):
        rows = [{"name": f"n{index:03d}", "creation": "2026-10-29 10:00:00"} for index in range(101)]
        names = display_names(rows)
        self.assertEqual((names["n000"], names["n098"], names["n100"]), ("S-291026-01", "S-291026-99", "S-291026-101"))

    def test_newest_uses_creation_and_breaks_ties_by_name(self):
        rows = [{"name": "old", "creation": "2026-10-28 10:00:00"}, {"name": "b", "creation": "2026-10-29 10:00:00"},
                {"name": "a", "creation": "2026-10-29 10:00:00"}, {"name": "none"}]
        self.assertEqual(newest(rows)["name"], "b")
        self.assertIsNone(newest([{"name": "none"}]))


if __name__ == "__main__":
    unittest.main()
