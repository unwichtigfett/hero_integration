"""Draft controller: unchanged JSON context stays unchanged even if an older row stored NULL."""
from __future__ import annotations

import importlib
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from hero_integration.services import frappe_mapping_draft_storage as storage


class _Row:
    def __init__(self, **values):
        self.__dict__.update(values)

    def get(self, key):
        return getattr(self, key, None)


def _rows(previous_values, current_values):
    base = dict(draft_key="key", site_scope="site", definition_id="def", owner_user="owner", generation=3,
                content_revision=0, base_active_mapping="HERO-GQL-MAP-1", base_active_revision=1, base_active_hash="hash",
                reconciliation_revision=0, reconciliation_json="")
    previous = _Row(**{**base, "state": "OPEN", **previous_values})
    current = _Row(**{**base, "state": "DISCARDED", **current_values})
    current.is_new = lambda: False
    current.get_doc_before_save = lambda: previous
    return current


class TestStoredJsonComparison(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.package = importlib.import_module("hero_integration.hero_integration.doctype.hero_mapping_draft")
        cls.module_name = cls.package.__name__ + ".hero_mapping_draft"
        cls.missing = object()
        cls.saved_alias = getattr(cls.package, "hero_mapping_draft", cls.missing)
        names = ("frappe", "frappe.model", "frappe.model.document", cls.module_name)
        cls.saved_modules = {name: sys.modules.get(name, cls.missing) for name in names}
        cls.addClassCleanup(cls.restore_imports)
        stub = types.ModuleType("frappe")
        stub.__path__ = []
        model = types.ModuleType("frappe.model")
        model.__path__ = []
        document = types.ModuleType("frappe.model.document")
        document.Document = type("Document", (), {})
        stub.model = model
        model.document = document
        sys.modules.pop(cls.module_name, None)
        sys.modules.update({"frappe": stub, "frappe.model": model, "frappe.model.document": document})
        cls.controller = importlib.import_module(cls.module_name)

    @classmethod
    def restore_imports(cls):
        for name, module in cls.saved_modules.items():
            if module is cls.missing:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
        if cls.saved_alias is cls.missing:
            if hasattr(cls.package, "hero_mapping_draft"):
                delattr(cls.package, "hero_mapping_draft")
        else:
            cls.package.hero_mapping_draft = cls.saved_alias

    def validate(self, current):
        def fail(message, *_):
            raise ValueError(message)

        fake = SimpleNamespace(session=SimpleNamespace(user="owner"), throw=fail, PermissionError=PermissionError,
                               _=lambda message: message)
        with patch.object(self.controller, "frappe", fake), patch.object(storage, "assert_storage_write"):
            self.controller.HEROMappingDraft.validate(current)

    def test_discarding_an_older_draft_with_null_context_is_allowed(self):
        # Older rows store NULL; the storage writes "" for "no new-definition context" and "no base content".
        self.validate(_rows({"new_definition_json": None, "base_content_json": None},
                            {"new_definition_json": "", "base_content_json": ""}))

    def test_equal_json_in_another_spelling_is_unchanged(self):
        self.validate(_rows({"new_definition_json": None, "base_content_json": '{"b": 1, "a": [1, 2]}'},
                            {"new_definition_json": "", "base_content_json": '{"a":[1,2],"b":1}'}))

    def test_real_changes_are_still_rejected(self):
        with self.assertRaisesRegex(ValueError, "base_content_json is immutable"):
            self.validate(_rows({"new_definition_json": None, "base_content_json": '{"a": 1}'},
                                {"new_definition_json": "", "base_content_json": '{"a": 2}'}))
        with self.assertRaisesRegex(ValueError, "base_content_json is immutable"):
            self.validate(_rows({"new_definition_json": None, "base_content_json": None},
                                {"new_definition_json": "", "base_content_json": '{"a": 1}'}))
        self.assertTrue(self.controller._same_json(None, ""))
        self.assertFalse(self.controller._same_json('{"a":1}', ""))
        self.assertFalse(self.controller._same_json("{broken", "{broken "))


if __name__ == "__main__":
    unittest.main()
