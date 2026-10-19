from __future__ import annotations

import json
from pathlib import Path
import unittest

from frappe.model.base_document import get_controller


ROOT = Path(__file__).resolve().parents[1] / "hero_integration" / "doctype"
CONTROLLERS = (
    "hero_graphql_schema_snapshot",
    "hero_graphql_service",
)


class TestDocTypeControllerContract(unittest.TestCase):
    def test_graphql_controllers_import_with_frappe_exact_class_names(self):
        for directory in CONTROLLERS:
            metadata = json.loads((ROOT / directory / f"{directory}.json").read_text())
            expected = metadata["name"].replace(" ", "").replace("-", "")

            with self.subTest(doctype=metadata["name"]):
                controller = get_controller(metadata["name"])
                self.assertEqual(controller.__name__, expected)
