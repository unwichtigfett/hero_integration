import unittest
from types import SimpleNamespace
from unittest import mock

from hero_integration.integrations import field_labels


class _Row(dict):
    __getattr__ = dict.get


class TestFieldLabels(unittest.TestCase):
    def test_prefix_is_removed_from_labels(self):
        self.assertEqual(field_labels.strip_prefix("HERO Source ID"), "Source ID")
        self.assertEqual(field_labels.strip_prefix("HERO-Kategorie"), "Kategorie")
        self.assertEqual(field_labels.strip_prefix("Heroic Deeds"), "Heroic Deeds")
        self.assertEqual(field_labels.strip_prefix("HERO"), "HERO")
        self.assertEqual(field_labels.display_label("Customer", "custom_hero_customer_type", "HERO Customer Type"),
                         "Customer Type")
        self.assertEqual(field_labels.display_label("Event", "custom_hero_new_value", "HERO New Value"), "New Value")
        for label in field_labels.LABELS.values():
            self.assertNotIn("HERO", label)

    def test_relabel_sets_plain_label_and_provenance_once(self):
        fields = [_Row(name="Customer-custom_hero_customer_type", dt="Customer", fieldname="custom_hero_customer_type",
                       label="HERO Customer Type", module=None),
                  _Row(name="Task-custom_hero_assignee", dt="Task", fieldname="custom_hero_assignee",
                       label="Responsible", module="HERO Integration")]
        child = [_Row(name="row1", parent="HERO Event Resource", fieldname="custom_hero_source_id", label="HERO Source ID")]
        writes = []

        def get_all(doctype, **kwargs):
            return {"Custom Field": fields, "DocType": ["HERO Event Resource"], "DocField": child}[doctype]

        def set_value(doctype, name, field, value=None, update_modified=True):
            writes.append((doctype, name, field, value))
            self.assertFalse(update_modified)

        fake = SimpleNamespace(get_all=get_all, db=SimpleNamespace(set_value=set_value), clear_cache=mock.Mock())
        with mock.patch.dict("sys.modules", {"frappe": fake}), \
                mock.patch.object(field_labels, "hide_technical_fields", return_value=0):
            result = field_labels.relabel_fields()
        self.assertEqual(writes, [
            ("Custom Field", "Customer-custom_hero_customer_type",
             {"label": "Customer Type", "module": "HERO Integration"}, None),
            ("DocField", "row1", "label", "Source ID"),
        ])
        self.assertEqual(result, {"fields_relabelled": 2, "doctypes": ["Customer", "HERO Event Resource"], "fields_hidden": 0})
        fake.clear_cache.assert_any_call(doctype="Customer")
