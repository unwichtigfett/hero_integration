from __future__ import annotations

import unittest
from types import SimpleNamespace

from hero_integration.services.frappe_mapping_catalog import catalog, fields, validate_draft_target
from hero_integration.services.mapping_suggestions import PickerField, picker_page
from hero_integration.services.mapping_suggestions import suggest_from_source


class TestFrappeMappingCatalog(unittest.TestCase):
    def setUp(self):
        self.metadata = _Metadata()

    def test_installed_apps_and_doctypes_are_runtime_metadata_not_an_erpnext_list(self):
        result = catalog(self.metadata)

        self.assertEqual(result["apps"], ["acme_app", "erpnext"])
        cases = next(item for item in result["doctypes"] if item["doctype"] == "Acme Case")
        self.assertTrue(cases["selectable"])
        self.assertFalse(next(item for item in result["doctypes"] if item["doctype"] == "Acme Settings")["selectable"])
        self.assertFalse(next(item for item in result["doctypes"] if item["doctype"] == "Denied Record")["selectable"])

    def test_foreign_standard_and_custom_fields_are_draft_only_and_links_stay_blocked(self):
        target_fields = fields(self.metadata, app_name="acme_app", doctype="Acme Case")

        subject = next(item for item in target_fields if item.path == "subject")
        custom = next(item for item in target_fields if item.path == "custom_hero_category")
        link = next(item for item in target_fields if item.path == "customer")
        children = next(item for item in target_fields if item.path == "items")
        self.assertTrue(subject.selectable)
        self.assertTrue(custom.selectable)
        self.assertFalse(subject.activation_allowed)
        self.assertIn("Draft proposal", subject.reason)
        self.assertFalse(link.selectable)
        self.assertIn("resolver", link.reason)
        self.assertFalse(children.selectable)

    def test_draft_validation_uses_type_and_permission_without_opening_an_active_write_path(self):
        target = validate_draft_target("HeroCustomers", "string|null", "Acme Case", "subject", self.metadata)

        self.assertEqual((target.app_name, target.doctype, target.path), ("acme_app", "Acme Case", "subject"))
        self.assertFalse(target.activation_allowed)
        with self.assertRaisesRegex(ValueError, "incompatible"):
            validate_draft_target("HeroCustomers", "boolean|null", "Acme Case", "subject", self.metadata)
        with self.assertRaisesRegex(ValueError, "No write permission"):
            validate_draft_target("HeroCustomers", "string|null", "Denied Record", "subject", self.metadata)

    def test_suggestions_cover_a_foreign_app_but_mark_them_draft_only(self):
        schema = {"__schema": {"types": [
            {"kind": "OBJECT", "name": "Query", "fields": [
                {"name": "contacts", "type": {"kind": "LIST", "ofType": {"kind": "OBJECT", "name": "Contact"}}},
            ]},
            {"kind": "OBJECT", "name": "Contact", "fields": [
                {"name": "category", "type": {"kind": "SCALAR", "name": "String"}},
            ]},
        ]}}
        matches = suggest_from_source("HeroCustomers", "category", schema, self.metadata, "Acme Case")

        match = next(item for item in matches if item["target_field"] == "custom_hero_category")
        self.assertEqual((match["target_app"], match["target_doctype"]), ("acme_app", "Acme Case"))
        self.assertTrue(match["draft_only"])
        self.assertIn(match["confidence"], {"high", "medium", "low"})

    def test_field_search_includes_type_and_description_within_the_selected_doctype(self):
        self.metadata._fields["Acme Case"].append(_field(
            "custom_reference", "Reference", "Data", is_custom_field=1, description="External partner key",
        ))

        results = fields(self.metadata, app_name="acme_app", doctype="Acme Case", search="partner")

        self.assertEqual([item.path for item in results], ["custom_reference"])
        self.assertEqual(fields(self.metadata, app_name="acme_app", doctype="Acme Case", search="Data")[0].doctype, "Acme Case")

    def test_picker_page_caps_visible_results_at_fifty_and_keeps_more_results_reachable(self):
        many = [PickerField(f"custom_field_{index}", f"Field {index}", "Data", True, "", True, "Draft") for index in range(123)]

        first = picker_page(many, start=0, page_size=999)
        second = picker_page(many, start=50, page_size=50)
        last = picker_page(many, start=100, page_size=50)

        self.assertEqual((len(first["items"]), first["total"], first["has_more"]), (50, 123, True))
        self.assertEqual((second["items"][0]["path"], len(second["items"])), ("custom_field_50", 50))
        self.assertEqual((last["items"][-1]["path"], last["has_more"]), ("custom_field_122", False))


class _Metadata:
    _doctypes = (
        {"name": "Customer", "module": "Selling", "app_name": "erpnext"},
        {"name": "Acme Case", "module": "Acme", "app_name": "acme_app"},
        {"name": "Acme Settings", "module": "Acme", "app_name": "acme_app", "issingle": 1},
        {"name": "Acme Row", "module": "Acme", "app_name": "acme_app", "istable": 1},
        {"name": "Denied Record", "module": "Acme", "app_name": "acme_app"},
    )

    def __init__(self):
        self._fields = {
            "Acme Case": [
                _field("subject", "Subject", "Data"),
                _field("custom_hero_category", "HERO Category", "Data", is_custom_field=1),
                _field("customer", "Customer", "Link", options="Customer"),
                _field("items", "Items", "Table", options="Acme Row"),
            ],
        }

    def doctypes(self):
        return self._doctypes

    @staticmethod
    def can_write(doctype):
        return doctype != "Denied Record"

    def fields(self, doctype):
        if doctype in self._fields:
            return self._fields[doctype]
        return [_field("subject", "Subject", "Data")]


def _field(fieldname, label, fieldtype, **kwargs):
    values = {"fieldname": fieldname, "label": label, "fieldtype": fieldtype, "read_only": 0, "reqd": 0, "options": "", "is_custom_field": 0, "description": ""}
    values.update(kwargs)
    return SimpleNamespace(**values)
