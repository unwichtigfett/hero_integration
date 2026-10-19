from __future__ import annotations

import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1] / "hero_integration" / "doctype"
NON_INPUT_FIELD_TYPES = {
    "Button",
    "Column Break",
    "Fold",
    "HTML",
    "Heading",
    "Section Break",
    "Tab Break",
}


class TestDocTypeFieldLabels(unittest.TestCase):
    def test_every_app_input_field_has_a_human_readable_label(self):
        missing = []
        for path in sorted(ROOT.glob("*/*.json")):
            doctype = json.loads(path.read_text())
            for field in doctype.get("fields", []):
                if not field.get("fieldname") or field.get("fieldtype") in NON_INPUT_FIELD_TYPES:
                    continue
                if not str(field.get("label", "")).strip():
                    missing.append(f"{doctype['name']}.{field['fieldname']}")

        self.assertEqual(missing, [])
