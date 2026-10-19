from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1] / "hero_integration" / "doctype"
CONTROLLERS = (
    "hero_graphql_schema_snapshot",
    "hero_graphql_service",
)
IMPORT_CHECK = """
import importlib
import json
import sys
import types

class Document:
    pass

frappe = types.ModuleType("frappe")
frappe.__path__ = []
model = types.ModuleType("frappe.model")
model.__path__ = []
document = types.ModuleType("frappe.model.document")
document.Document = Document
frappe.model = model
model.document = document
sys.modules.update({"frappe": frappe, "frappe.model": model, "frappe.model.document": document})

for module_name, expected in json.loads(sys.argv[1]):
    controller = getattr(importlib.import_module(module_name), expected)
    assert issubclass(controller, Document), (module_name, expected)
"""


class TestDocTypeControllerContract(unittest.TestCase):
    def test_graphql_controllers_import_with_frappe_exact_class_names(self):
        contracts = []
        for directory in CONTROLLERS:
            metadata = json.loads((ROOT / directory / f"{directory}.json").read_text())
            expected = metadata["name"].replace(" ", "").replace("-", "")
            contracts.append((f"hero_integration.hero_integration.doctype.{directory}.{directory}", expected))

        result = subprocess.run(
            [sys.executable, "-c", IMPORT_CHECK, json.dumps(contracts)],
            cwd=Path(__file__).resolve().parents[2],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
