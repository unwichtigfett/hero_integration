"""Check code transformations against the HERO Frappe Mapping Select options offline."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from hero_integration.services.direct_mapping import TRANSFORMATIONS

DOCTYPE = Path(__file__).resolve().parents[1] / "hero_integration/doctype/hero_frappe_mapping/hero_frappe_mapping.json"


class TestTransformationOptionsContract(unittest.TestCase):
    def test_every_transformation_is_a_select_option(self):
        fields = json.loads(DOCTYPE.read_text())["fields"]
        options = set(next(f for f in fields if f["fieldname"] == "transformation")["options"].split("\n"))
        self.assertEqual(set(TRANSFORMATIONS) - options, set())


if __name__ == "__main__":
    unittest.main()
