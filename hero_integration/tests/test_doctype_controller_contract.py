from __future__ import annotations

import importlib
import json
from pathlib import Path
import sys
import types
import unittest
from unittest import mock


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
            module_name = f"hero_integration.hero_integration.doctype.{directory}.{directory}"

            with self.subTest(doctype=metadata["name"]), self._frappe_import_environment():
                sys.modules.pop(module_name, None)
                module = importlib.import_module(module_name)
                controller = getattr(module, expected)
                self.assertTrue(issubclass(controller, _Document))

    def _frappe_import_environment(self):
        frappe = types.ModuleType("frappe")
        frappe.__path__ = []
        model = types.ModuleType("frappe.model")
        model.__path__ = []
        document = types.ModuleType("frappe.model.document")
        document.Document = _Document
        frappe.model = model
        model.document = document
        return mock.patch.dict(
            sys.modules,
            {"frappe": frappe, "frappe.model": model, "frappe.model.document": document},
        )


class _Document:
    pass
