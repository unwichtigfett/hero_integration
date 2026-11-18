"""Offline metadata-preservation and local responsibility validation contracts."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

APP = Path(__file__).parents[1]
FIELD = "custom_project_responsible"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Denied(Exception):
    pass


class MetadataFixture:
    def __init__(self):
        self.original = ["naming_series", "project_name", "custom_hero_display_id", "customer_details",
            "customer", "company", "column_break_5", "custom_hero_project_manager", "priority", "status",
            "custom_project_documents_tab", "documents", "custom_project_images_tab", "images",
            "monitor_progress_tab", "users_section", "users", "costing_tab", "custom_project_source_section", "source_id"]
        self.order = list(self.original)
        self.stored = list(self.original)
        self.fields = {}
        for name in self.order:
            kind = "Data"
            if name in ("customer_details", "users_section", "custom_project_source_section"):
                kind = "Section Break"
            elif name.endswith("_tab"):
                kind = "Tab Break"
            elif name == "column_break_5":
                kind = "Column Break"
            self.fields[name] = NS(fieldname=name, fieldtype=kind, label="Project and Customer" if name == "customer_details" else name,
                read_only=int(name == "custom_hero_project_manager"), permlevel=0, options=None)
        self.writes, self.created = [], 0
        self.manager = True
        self.user = NS(enabled=1, user_type="System User")
        self.reads = []
        self.frappe = NS(only_for=self.only_for, get_meta=self.meta,
            db=NS(get_value=self.get_value), clear_cache=Mock(),
            _=lambda value: value, throw=lambda text: (_ for _ in ()).throw(ValueError(text)))

    def only_for(self, role):
        assert role == "System Manager"
        if not self.manager:
            raise Denied()

    def meta(self, doctype):
        assert doctype == "Project"
        return NS(fields=[self.fields[name] for name in self.order], get_field=self.fields.get)

    def get_value(self, doctype, name, fields, **kwargs):
        self.reads.append((doctype, name))
        if doctype == "Property Setter":
            assert name == "Project-main-field_order"
            return json.dumps(self.stored)
        assert doctype == "User"
        assert fields == ["enabled", "user_type"] and kwargs == {"as_dict": True}
        return self.user

    def ensure_field(self):
        if FIELD not in self.fields:
            self.created += 1
            self.fields[FIELD] = NS(fieldname=FIELD, fieldtype="Link", options="User",
                read_only=0, permlevel=0, label="Project Responsible")
            self.order.insert(self.order.index("monitor_progress_tab") + 1, FIELD)

    def setter(self, doctype, field, prop, value, kind, **kwargs):
        assert doctype == "Project" and kind == "Data"
        self.writes.append((field, prop, value))
        if prop == "field_order":
            self.stored = json.loads(value)
            self.order = list(self.stored)
        else:
            assert (field, prop) == ("customer_details", "label")
            self.fields[field].label = value


class LayoutTests(unittest.TestCase):
    def setUp(self):
        self.fixture = MetadataFixture()
        self.modules = patch.dict(sys.modules, {
            "frappe": self.fixture.frappe,
            "hero_integration.services.project_responsibility": NS(FIELD=FIELD, ensure_field=self.fixture.ensure_field),
            "frappe.custom.doctype.property_setter.property_setter": NS(make_property_setter=self.fixture.setter),
        })
        self.modules.start(); self.addCleanup(self.modules.stop)
        self.layout = load("workspace_layout_test", APP / "services/project_workspace_layout.py")
        self.responsibility = load("responsibility_test", APP / "services/project_responsibility.py")

    def test_compaction_preserves_fields_moves_name_above_customer_and_keeps_source_manager_separate(self):
        result = self.layout.apply_compact_details()
        order = self.fixture.order
        self.assertTrue(result["changed"])
        self.assertEqual(set(order), set(self.fixture.original) | {FIELD})
        self.assertEqual(len(order), len(set(order)))
        self.assertEqual(order[0], "customer_details")
        self.assertEqual(order[order.index("customer") - 1], "project_name")
        self.assertEqual(order[order.index("monitor_progress_tab") + 1], FIELD)
        self.assertGreater(order.index("custom_hero_project_manager"), order.index("custom_project_source_section"))
        self.assertEqual(self.fixture.fields["custom_hero_project_manager"].read_only, 1)
        self.assertFalse(self.fixture.fields["customer_details"].label)

    def test_layout_is_idempotent_and_does_not_recreate_controls_or_reset_data(self):
        self.layout.apply_compact_details()
        writes = list(self.fixture.writes)
        self.assertFalse(self.layout.apply_compact_details()["changed"])
        self.assertEqual(self.fixture.writes, writes)
        self.assertEqual(self.fixture.created, 1)

    def test_foreign_fields_keep_their_relative_order(self):
        current = self.fixture.original
        current.insert(current.index("company"), "third_party_field")
        target = self.layout.compact_details_order(current)
        moved = {"customer_details", "project_name", "custom_hero_project_manager", FIELD}
        self.assertEqual([x for x in target if x not in moved], [x for x in current if x not in moved])

    def test_unconfigured_sites_are_untouched(self):
        self.fixture.order.remove("custom_project_images_tab")
        result = self.layout.apply_compact_details()
        self.assertEqual(result, {"state": "not_configured", "changed": False})
        self.assertEqual(self.fixture.created, 0)
        self.assertEqual(self.fixture.writes, [])

    def test_unknown_layout_changes_and_structural_prefixes_fail_before_schema_changes(self):
        self.fixture.stored.remove("company")
        with self.assertRaisesRegex(ValueError, "layout changed"):
            self.layout.apply_compact_details()
        self.fixture.stored = list(self.fixture.order)
        self.fixture.fields["naming_series"].fieldtype = "Section Break"
        with self.assertRaisesRegex(ValueError, "unexpected opening"):
            self.layout.apply_compact_details()
        self.assertEqual(self.fixture.created, 0)
        self.assertEqual(self.fixture.writes, [])

    def test_non_admin_cannot_mutate_layout(self):
        self.fixture.manager = False
        with self.assertRaises(Denied):
            self.layout.apply_compact_details()
        self.assertEqual(self.fixture.writes, [])

    def test_duplicate_or_missing_field_orders_are_rejected(self):
        for order in [self.fixture.original + ["customer"], ["customer", "project_name"]]:
            with self.assertRaises(ValueError):
                self.layout.compact_details_order(order)

    def test_responsible_person_must_be_an_enabled_system_user(self):
        document = NS(get=lambda key: "user@example.invalid", is_new=lambda: False, has_value_changed=lambda key: True)
        self.responsibility.validate_project_responsible(document)
        for user in [None, NS(enabled=0, user_type="System User"), NS(enabled=1, user_type="Website User")]:
            self.fixture.user = user
            with self.assertRaises(ValueError):
                self.responsibility.validate_project_responsible(document)

    def test_unchanged_disabled_responsibility_and_empty_values_do_not_block_unrelated_saves(self):
        self.fixture.user = NS(enabled=0, user_type="System User")
        self.responsibility.validate_project_responsible(
            NS(get=lambda key: "user@example.invalid", is_new=lambda: False, has_value_changed=lambda key: False))
        self.responsibility.validate_project_responsible(NS(get=lambda key: None))
        self.assertEqual(self.fixture.reads, [])

    def test_conflicting_responsibility_field_is_not_overwritten(self):
        self.fixture.ensure_field()
        self.fixture.fields[FIELD].options = "Employee"
        with self.assertRaises(ValueError):
            self.responsibility.ensure_field()

    def test_native_hero_guard_remains_registered_before_local_responsibility_validation(self):
        hooks = runpy.run_path(str(APP / "hooks.py"))
        self.assertEqual(hooks["doc_events"]["Project"]["validate"], [
            "hero_integration.services.guards.protect_hero_project_ids",
            "hero_integration.services.project_responsibility.validate_project_responsible",
            "hero_integration.services.project_address.validate_project_address",
        ])


if __name__ == "__main__":
    unittest.main()
