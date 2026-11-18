"""Offline contract tests for optional local Project addresses and draft reads."""
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch


class Document(dict):
    def __getattr__(self, key):
        return self.get(key)

    def set(self, key, value):
        self[key] = value

    def check_permission(self, permission):
        if self.get("denied"):
            raise PermissionError("Denied")

    def apply_fieldlevel_read_permissions(self):
        for key in self.get("masked", []):
            self.pop(key, None)


class ProjectAddressTests(unittest.TestCase):
    def setUp(self):
        self.records = {
            ("Customer", "C1"): Document(customer_primary_address="A1"),
            ("Customer", "C2"): Document(customer_primary_address="A2"),
            ("Address", "A1"): Document(address_line1="Main 1", city="City", pincode="12345", country="Germany",
                phone="private", email_id="private", links=[{"private": True}]),
            ("Address", "A2"): Document(address_line1="Site 2", city="Town", country="Germany"),
            ("Project", "P1"): Document(name="P1"),
        }
        self.fake = NS(session=NS(user="user@example.invalid"), _=lambda text: text,
            throw=lambda text, exception=ValueError: (_ for _ in ()).throw(exception(text)),
            PermissionError=PermissionError, DoesNotExistError=KeyError, get_doc=Mock(side_effect=lambda dt, name: self.records[(dt, name)]),
            has_permission=Mock(), whitelist=lambda **kw: lambda fn: fn)
        spec = importlib.util.spec_from_file_location("project_address_isolated",
            Path(__file__).parents[1] / "services/project_address.py")
        self.service = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"frappe": self.fake}):
            spec.loader.exec_module(self.service)
        self.field, self.edit = self.service.ADDRESS, self.service.EDIT

    def project(self, customer="C1", address=None, manual=0, **values):
        return Document(customer=customer, **{self.field: address, self.edit: manual}, **values)

    def test_default_is_persisted_for_native_and_hero_projects(self):
        for source in (None, "99"):
            doc = self.project(custom_hero_source_id=source)
            self.service.validate_project_address(doc)
            self.assertEqual(doc[self.field], "A1")

    def test_chosen_address_is_preserved_without_manual_toggle(self):
        doc = self.project(address="A2")
        self.service.validate_project_address(doc)
        self.assertEqual(doc[self.field], "A2")

    def test_customer_change_preserves_existing_project_address(self):
        doc = self.project(customer="C2", address="A1")
        self.service.validate_project_address(doc)
        self.assertEqual(doc[self.field], "A1")

    def test_later_primary_link_changes_preserve_existing_project_address(self):
        self.records[("Customer", "C1")]["customer_primary_address"] = "A2"
        doc = self.project(address="A1")
        self.service.validate_project_address(doc)
        self.assertEqual(doc[self.field], "A1")

    def test_override_is_preserved_even_when_customer_changes_or_is_missing(self):
        for customer in ("C1", "C2", None):
            doc = self.project(customer=customer, address="A2", manual=1)
            self.service.validate_project_address(doc)
            self.assertEqual(doc[self.field], "A2")

    def test_unchecking_override_preserves_existing_project_address(self):
        doc = self.project(address="A2", manual=0)
        self.service.validate_project_address(doc)
        self.assertEqual(doc[self.field], "A2")

    def test_missing_address_never_blocks_save(self):
        self.records[("Customer", "C2")]["customer_primary_address"] = None
        for customer in (None, "C2"):
            for manual in (0, 1):
                doc = self.project(customer=customer, manual=manual)
                self.service.validate_project_address(doc)
                self.assertIsNone(doc[self.field])

    def test_incomplete_or_disabled_explicit_address_is_preserved(self):
        for values in ({"disabled": 1}, {"city": ""}, {"address_line1": None}, {"country": ""}):
            with self.subTest(values=values):
                self.records[("Address", "A2")].update(values)
                doc = self.project(address="A2", manual=1)
                self.service.validate_project_address(doc)
                self.assertEqual(doc[self.field], "A2")

    def test_unusable_or_unreadable_optional_default_is_not_copied(self):
        for values in ({"disabled": 1}, {"city": ""}, {"denied": True}, {"masked": ["city"]}):
            original = self.records[("Address", "A1")]
            self.records[("Address", "A1")] = Document(original, **values)
            doc = self.project()
            self.service.validate_project_address(doc)
            self.assertIsNone(doc[self.field])
            self.records[("Address", "A1")] = original
        self.records[("Customer", "C1")]["denied"] = True
        self.service.validate_project_address(self.project())

    def test_new_address_selection_still_requires_read_permission(self):
        self.records[("Address", "A2")]["denied"] = True
        with self.assertRaises(PermissionError):
            self.service.validate_project_address(self.project(address="A2"))

    def test_no_customer_or_hero_source_fields_are_ever_updated(self):
        doc = self.project(address="A2", custom_adresse="LEGACY", custom_hero_project_address="SOURCE")
        before = {key: dict(value) for key, value in self.records.items()}
        self.service.validate_project_address(doc)
        self.assertEqual(self.records, before)
        self.assertEqual(doc.custom_adresse, "LEGACY")
        self.assertEqual(doc.custom_hero_project_address, "SOURCE")

    def test_preview_is_read_only_minimal_plain_text_and_draft_create_checked(self):
        result = self.service.get_address_context(customer="C1")
        self.assertEqual(result, {"name": "A1", "address": "Main 1\n12345 City\nGermany", "state": "ready", "manual": False})
        self.fake.has_permission.assert_called_once_with("Project", "create", throw=True)
        self.assertNotIn("phone", result)
        self.assertNotIn("email_id", result)

    def test_saved_preview_checks_project_before_customer(self):
        self.records[("Project", "P1")]["denied"] = True
        with self.assertRaises(PermissionError):
            self.service.get_address_context(customer="C1", project="P1")
        self.assertEqual(self.fake.get_doc.call_args_list[0].args, ("Project", "P1"))
        self.assertEqual(self.fake.get_doc.call_count, 1)

    def test_manual_preview_does_not_fetch_customer(self):
        result = self.service.get_address_context(customer="C1", address="A2", manual="1", project="P1")
        self.assertEqual(result["name"], "A2")
        self.assertNotIn(("Customer", "C1"), [call.args for call in self.fake.get_doc.call_args_list])

    def test_missing_preview_is_explicit_and_guest_is_denied_before_reads(self):
        self.assertEqual(self.service.get_address_context()["state"], "missing")
        self.fake.get_doc.reset_mock()
        self.fake.session.user = "Guest"
        with self.assertRaises(PermissionError):
            self.service.get_address_context(customer="C1")
        self.fake.get_doc.assert_not_called()

    def test_invalid_inputs_do_not_reach_document_reads(self):
        for kw in ({"customer": ["C1"]}, {"project": "x" * 141}, {"address": {}}, {"manual": "yes"}):
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                self.service.get_address_context(**kw)
        self.fake.get_doc.assert_not_called()

    def test_schema_is_optional_and_compatible(self):
        created = {}
        def create(fields, update):
            self.assertFalse(update)
            created.update({field["fieldname"]: Document(field) for field in fields["Project"]})
        self.fake.get_meta = lambda dt: NS(get_field=lambda field: created.get(field))
        module = NS(create_custom_fields=Mock(side_effect=create))
        with patch.dict(sys.modules, {"frappe.custom.doctype.custom_field.custom_field": module}):
            self.service.ensure_fields()
            self.service.ensure_fields()
        module.create_custom_fields.assert_called_once()
        self.assertEqual(created[self.field].reqd, 0)
        self.assertEqual(created[self.field].options, "Address")
        self.assertEqual(created[self.field].read_only_depends_on, "eval:!doc." + self.edit)
        created[self.field]["options"] = "Customer"
        with self.assertRaisesRegex(ValueError, "incompatible"):
            self.service.ensure_fields()

    def test_existing_required_address_becomes_optional_without_touching_values(self):
        definitions = {
            self.edit: Document(fieldtype="Check"),
            self.field: Document(fieldtype="Link", options="Address", reqd=1,
                read_only_depends_on="eval:!doc." + self.edit),
        }
        self.fake.get_meta = lambda dt: NS(get_field=lambda field: definitions.get(field))
        setter = Mock(side_effect=lambda *args, **kwargs: definitions[self.field].update(reqd=0))
        with patch.dict(sys.modules, {"frappe.custom.doctype.property_setter.property_setter":
                NS(make_property_setter=setter)}):
            self.service.ensure_fields()
            self.service.ensure_fields()
        setter.assert_called_once_with("Project", self.field, "reqd", "0", "Check",
            validate_fields_for_doctype=False)

    def test_unchanged_existing_address_does_not_require_new_read_access(self):
        doc = self.project(address="A2")
        doc.get_doc_before_save = lambda: Document({self.field: "A2"})
        self.records[("Address", "A2")]["denied"] = True
        self.service.validate_project_address(doc)
        self.fake.get_doc.assert_not_called()

    def test_only_the_proven_invalid_legacy_fetch_is_retired_without_converting_the_field(self):
        legacy = Document(fieldtype="Link", options="Customer", read_only=1,
            fetch_from="customer.customer_primary_address", value="KEEP")
        self.fake.get_meta = lambda dt: NS(get_field=lambda field: legacy)
        setter = Mock()
        with patch.dict(sys.modules, {"frappe.custom.doctype.property_setter.property_setter":
                NS(make_property_setter=setter)}):
            self.service._retire_invalid_legacy_fetch()
            setter.assert_called_once_with("Project", "custom_adresse", "fetch_from", "", "Small Text",
                validate_fields_for_doctype=False)
            self.assertEqual(legacy["options"], "Customer")
            self.assertEqual(legacy["value"], "KEEP")
            for values in ({"options": "Address"}, {"fetch_from": "customer.other_field"}, {"read_only": 0}):
                setter.reset_mock()
                original = dict(legacy)
                legacy.update(values)
                self.service._retire_invalid_legacy_fetch()
                setter.assert_not_called()
                legacy.clear()
                legacy.update(original)


if __name__ == "__main__":
    unittest.main()
