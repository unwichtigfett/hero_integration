from __future__ import annotations

import importlib
import sys
import types
import unittest
from types import SimpleNamespace
from unittest import mock


class TestInstallContract(unittest.TestCase):
    def test_preinstalled_erpnext_guard_rejects_before_schema_writes(self):
        environment = _InstallEnvironment(apps=["frappe"], version="16.35.0")
        install = environment.load()

        with self.assertRaisesRegex(RuntimeError, "must be installed before"):
            environment.call(install.before_install)

        self.assertEqual(environment.create_calls, [])

    def test_non_v16_and_incomplete_schema_are_rejected_before_schema_writes(self):
        version_environment = _InstallEnvironment(apps=["frappe", "erpnext"], version="15.99.0")
        install = version_environment.load()
        with self.assertRaisesRegex(RuntimeError, "requires ERPNext >=16.0.0-dev,<17.0.0"):
            version_environment.call(install.before_install)
        self.assertEqual(version_environment.create_calls, [])

        schema_environment = _InstallEnvironment(
            apps=["frappe", "erpnext"], version="16.35.0", doctypes={"Customer", "Project"}
        )
        install = schema_environment.load()
        with self.assertRaisesRegex(RuntimeError, "schema is incomplete: Contact"):
            schema_environment.call(install.before_install)
        self.assertEqual(schema_environment.create_calls, [])

    def test_empty_erpnext_schema_is_rejected_before_schema_writes(self):
        environment = _InstallEnvironment(apps=["frappe", "erpnext"], version="16.35.0", doctypes=set())
        install = environment.load()
        with self.assertRaisesRegex(RuntimeError, "schema is incomplete: Customer"):
            environment.call(install.after_migrate)
        self.assertEqual(environment.create_calls, [])

    def test_schema_creation_is_create_only_idempotent_and_conflict_safe(self):
        environment = _InstallEnvironment(apps=["frappe", "erpnext"], version="16.35.0")
        install = environment.load()

        environment.call(install.after_install)
        environment.call(install.after_migrate)

        self.assertEqual(len(environment.create_calls), 1)
        fields, update = environment.create_calls[0]
        self.assertFalse(update)
        self.assertEqual(sum(map(len, fields.values())), 31)
        self.assertEqual(
            {row["fieldname"] for rows in fields.values() for row in rows},
            {
                "custom_hero_source_id", "custom_hero_customer_number", "custom_hero_customer_category",
                "custom_hero_related_project_id", "custom_hero_display_id",
                "custom_hero_source_created", "custom_hero_source_modified", "custom_hero_manufacturer_nr",
                "custom_hero_lead_source", "custom_hero_designation", "custom_hero_project_manager",
                "custom_hero_partner_id", "custom_hero_user_id", "custom_hero_project", "custom_hero_logbook_context",
                *{field["fieldname"] for field in install.SERVICE_ITEM_FIELDS},
                *{field for fields in install.OBSERVATIONS.values() for field, _ in fields},
            },
        )

        # A different label is not a conflict: the app gives its fields plain labels itself.
        environment.custom_fields[("Customer", "custom_hero_customer_number")]["label"] = "HERO Customer Number"
        environment.call(install.after_migrate)
        self.assertEqual(len(environment.create_calls), 1)
        self.assertEqual(environment.relabel_calls, 3)

        environment.custom_fields[("Customer", "custom_hero_customer_number")]["fieldtype"] = "Int"
        with self.assertRaisesRegex(RuntimeError, "conflicts with the HERO schema \\(fieldtype\\)"):
            environment.call(install.after_migrate)
        self.assertEqual(len(environment.create_calls), 1)

    def test_native_project_notes_must_exist_and_remain_editable(self):
        environment = _InstallEnvironment(apps=["frappe", "erpnext"], version="16.35.0", project_notes=False)
        install = environment.load()

        with self.assertRaisesRegex(RuntimeError, "Project.notes is required"):
            environment.call(install.before_install)

        locked = _InstallEnvironment(apps=["frappe", "erpnext"], version="16.35.0", project_notes=True, project_notes_read_only=True)
        install = locked.load()
        with self.assertRaisesRegex(RuntimeError, "Project.notes must remain editable"):
            locked.call(install.before_install)

    def test_install_and_migrate_refuse_missing_project_notes_before_creating_fields(self):
        for hook in ("after_install", "after_migrate"):
            with self.subTest(hook=hook):
                environment = _InstallEnvironment(apps=["frappe", "erpnext"], version="16.35.0", project_notes=False)
                install = environment.load()
                with self.assertRaisesRegex(RuntimeError, "Project.notes is required"):
                    environment.call(getattr(install, hook))
                self.assertEqual(environment.create_calls, [])
                self.assertEqual(environment.custom_fields, {})

    def test_conflicting_existing_field_blocks_creation_of_other_missing_fields(self):
        environment = _InstallEnvironment(apps=["frappe", "erpnext"], version="16.35.0")
        install = environment.load()
        definition = next(row for row in install.HERO_CUSTOM_FIELDS["Project"]
                          if row["fieldname"] == "custom_hero_display_id")
        environment.custom_fields[("Project", "custom_hero_display_id")] = {**definition, "fieldtype": "Int"}

        with self.assertRaisesRegex(RuntimeError, "conflicts with the HERO schema"):
            environment.call(install.after_migrate)

        self.assertEqual(environment.create_calls, [])
        self.assertEqual(len(environment.custom_fields), 1)


class _InstallEnvironment:
    def __init__(self, apps, version, doctypes=None, project_notes=True, project_notes_read_only=False):
        self.apps = apps
        self.version = version
        self.doctypes = ({"Customer", "Contact", "Contact Email", "Contact Phone", "Address", "Project"}
                         if doctypes is None else doctypes)
        self.custom_fields: dict[tuple[str, str], dict[str, object]] = {}
        self.create_calls: list[tuple[dict[str, list[dict[str, object]]], bool]] = []
        self.relabel_calls = 0
        self.project_notes = project_notes
        self.project_notes_read_only = project_notes_read_only

    def load(self):
        frappe_module = types.ModuleType("frappe")
        frappe_module._ = lambda message: message
        frappe_module.get_installed_apps = lambda: self.apps
        frappe_module.db = types.SimpleNamespace(exists=self.exists)
        frappe_module.get_doc = self.get_doc
        frappe_module.get_meta = self.get_meta
        frappe_module.throw = lambda message: (_ for _ in ()).throw(RuntimeError(message))
        erpnext_module = types.ModuleType("erpnext")
        erpnext_module.__version__ = self.version
        custom_field_module = types.ModuleType("frappe.custom.doctype.custom_field.custom_field")
        custom_field_module.create_custom_fields = self.create_custom_fields
        self.modules = {
            "frappe": frappe_module,
            "erpnext": erpnext_module,
            "frappe.custom.doctype.custom_field.custom_field": custom_field_module,
        }
        package = importlib.import_module("hero_integration")
        missing = object()
        previous_alias = getattr(package, "install", missing)
        previous = sys.modules.pop("hero_integration.install", None)
        try:
            with mock.patch.dict(sys.modules, self.modules):
                module = importlib.import_module("hero_integration.install")
        finally:
            if previous is None:
                sys.modules.pop("hero_integration.install", None)
            else:
                sys.modules["hero_integration.install"] = previous
            if previous_alias is missing:
                if hasattr(package, "install"):
                    delattr(package, "install")
            else:
                package.install = previous_alias
        return module

    def call(self, function):
        # Theme lifecycle has its own metadata tests; this fixture owns ERPNext fields.
        with mock.patch("hero_integration.services.desk_theme.ensure_navy_theme"), \
                mock.patch("hero_integration.integrations.field_labels.relabel_fields") as relabel, \
                mock.patch.dict(sys.modules, self.modules):
            result = function()
            self.relabel_calls += relabel.call_count
            return result

    def exists(self, doctype, filters):
        if doctype == "DocType":
            return filters in self.doctypes
        if doctype == "Custom Field":
            key = (filters["dt"], filters["fieldname"])
            return ":".join(key) if key in self.custom_fields else None
        raise AssertionError(f"unexpected exists: {doctype}, {filters}")

    def get_doc(self, doctype, name):
        if doctype != "Custom Field" or ":" not in name:
            raise AssertionError(f"unexpected document lookup: {doctype}, {name}")
        return SimpleNamespace(get=self.custom_fields[tuple(name.split(":", 1))].get)

    def get_meta(self, doctype):
        if doctype != "Project":
            raise AssertionError(f"unexpected metadata lookup: {doctype}")
        field = SimpleNamespace(read_only=self.project_notes_read_only) if self.project_notes else None
        return SimpleNamespace(get_field=lambda fieldname: field if fieldname == "notes" else None)

    def create_custom_fields(self, fields, update):
        copied = {doctype: [dict(row) for row in rows] for doctype, rows in fields.items()}
        self.create_calls.append((copied, update))
        for doctype, definitions in copied.items():
            for definition in definitions:
                self.custom_fields[(doctype, definition["fieldname"])] = definition
