from __future__ import annotations

import unittest
from unittest.mock import patch

from hero_integration import api


class SnapshotFrappe:
    def __init__(self, allowed=True):
        self.allowed = allowed
        self.calls = []
        self.rows = [{"name": f"snapshot-{i:03}"} for i in range(205)]

    def only_for(self, role):
        if role != "System Manager" or not self.allowed:
            raise PermissionError("not permitted")

    def throw(self, message):
        raise ValueError(message)

    def get_all(self, doctype, **kwargs):
        self.calls.append((doctype, kwargs))
        start = kwargs["limit_start"]
        return self.rows[start:start + kwargs["limit_page_length"]]


class TestSnapshotPagination(unittest.TestCase):
    def test_default_shape_and_complete_successive_pages(self):
        fake = SnapshotFrappe()
        with patch.object(api, "frappe", fake):
            first = api.schema_snapshot_list()
            second = api.schema_snapshot_list("100", "100")
            third = api.schema_snapshot_list(200, 100)
            last = api.schema_snapshot_list(205, 100)
        self.assertIsInstance(first, list)
        self.assertEqual(first + second + third, fake.rows)
        self.assertEqual(last, [])
        for doctype, args in fake.calls:
            self.assertEqual(doctype, "HERO GraphQL Schema Snapshot")
            self.assertEqual(args["order_by"], "modified desc, name asc")
            self.assertEqual(args["limit_page_length"], 100)
            self.assertNotIn("snapshot_json", args["fields"])

    def test_rejects_invalid_page_inputs(self):
        fake = SnapshotFrappe()
        with patch.object(api, "frappe", fake):
            for args in [(-1, 100), (True, 100), (0, 0), (0, 101), (0, True), ("1.5", 1), (2147483648, 1)]:
                with self.subTest(args=args), self.assertRaises(ValueError):
                    api.schema_snapshot_list(*args)
        self.assertEqual(fake.calls, [])

    def test_permission_precedes_query(self):
        fake = SnapshotFrappe(allowed=False)
        with patch.object(api, "frappe", fake), self.assertRaises(PermissionError):
            api.schema_snapshot_list()
        self.assertEqual(fake.calls, [])


if __name__ == "__main__":
    unittest.main()
