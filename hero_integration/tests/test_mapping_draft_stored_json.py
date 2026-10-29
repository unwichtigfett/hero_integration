"""Draft controller: unchanged JSON context stays unchanged even if an older row stored NULL."""
from __future__ import annotations

import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch

try:
    import frappe  # noqa: F401
except ModuleNotFoundError:
    stub = types.ModuleType("frappe")
    stub.whitelist = lambda *args, **kwargs: (lambda function: function)
    model = types.ModuleType("frappe.model")
    document = types.ModuleType("frappe.model.document")
    document.Document = type("Document", (), {})
    sys.modules.update({"frappe": stub, "frappe.model": model, "frappe.model.document": document})

from hero_integration.hero_integration.doctype.hero_mapping_draft import hero_mapping_draft as controller
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
    def validate(self, current):
        def fail(message, *_):
            raise ValueError(message)

        fake = SimpleNamespace(session=SimpleNamespace(user="owner"), throw=fail, PermissionError=PermissionError)
        with patch.object(controller, "frappe", fake), patch.object(storage, "assert_storage_write"):
            controller.HEROMappingDraft.validate(current)

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
        self.assertTrue(controller._same_json(None, ""))
        self.assertFalse(controller._same_json('{"a":1}', ""))
        self.assertFalse(controller._same_json("{broken", "{broken "))


if __name__ == "__main__":
    unittest.main()
