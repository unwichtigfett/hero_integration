"""Check recorded result codes against Frappe Select options without requiring a live site."""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1]


class TestResultCodesContract(unittest.TestCase):
    def test_every_migration_result_is_a_record_option(self):
        doctype = json.loads((APP / "hero_integration/doctype/hero_integration_record/hero_integration_record.json").read_text())
        options = set(next(f for f in doctype["fields"] if f["fieldname"] == "result")["options"].split("\n"))
        source = (APP / "services/migration.py").read_text()
        produced = set(re.findall(r"""Result\(\s*['"]([A-Z_]+)['"]""", source))
        self.assertTrue({"CREATED", "PENDING_REVIEW", "SKIPPED_REJECTED"} <= produced)
        self.assertEqual(produced - options, set())


if __name__ == "__main__":
    unittest.main()
