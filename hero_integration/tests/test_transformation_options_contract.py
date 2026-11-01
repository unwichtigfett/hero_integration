"""Every code transformation must be a valid HERO Frappe Mapping Select option.

Frappe validates the Select only on a real site; a missing option failed the
first salutation rule insert on the dev site (2026-11-02).
"""

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
