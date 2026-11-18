"""Per-user design persistence and permission-invariant native metadata projection."""
from copy import deepcopy
import importlib.util
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

APP = Path(__file__).parents[1]


class Denied(Exception):
    pass


class DesignModeTests(unittest.TestCase):
    def setUp(self):
        self.values = {"reader@example.invalid": "Custom", "other@example.invalid": "Native"}
        self.exposed = []
        self.fake = NS(session=NS(user="reader@example.invalid"),
            db=NS(has_column=Mock(return_value=True),
                get_value=Mock(side_effect=lambda doctype, user, field: self.values.get(user)),
                set_value=Mock(side_effect=lambda doctype, user, field, value: self.values.__setitem__(user, value))),
            _=lambda text: text, PermissionError=Denied,
            throw=lambda text, exception=ValueError: (_ for _ in ()).throw(exception(text)),
            clear_cache=Mock(), clear_document_cache=Mock(),
            whitelist=lambda **options: self.exposed.append(options) or (lambda function: function))
        spec = importlib.util.spec_from_file_location("design_mode_isolated", APP / "services/design_mode.py")
        self.mode = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"frappe": self.fake}):
            spec.loader.exec_module(self.mode)

    def test_only_current_user_preference_is_written(self):
        self.assertEqual(self.mode.set_design_mode("Native"), {"mode": "Native"})
        self.fake.db.set_value.assert_called_once_with("User", "reader@example.invalid", self.mode.FIELD, "Native")
        self.assertEqual(self.values["other@example.invalid"], "Native")
        self.fake.clear_cache.assert_called_once_with(user="reader@example.invalid")

    def test_guest_invalid_input_and_missing_schema_cannot_write(self):
        self.fake.session.user = "Guest"
        with self.assertRaises(Denied):
            self.mode.set_design_mode("Custom")
        self.fake.session.user = "reader@example.invalid"
        for mode in ("native", "", "Navy", None, ["Custom"]):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                self.mode.set_design_mode(mode)
        self.fake.db.has_column.return_value = False
        with self.assertRaises(ValueError):
            self.mode.set_design_mode("Native")
        self.fake.db.set_value.assert_not_called()

    def test_existing_users_keep_custom_and_anonymous_is_native(self):
        self.fake.db.has_column.return_value = False
        self.assertEqual(self.mode.get_design_mode(), "Custom")
        self.fake.session.user = "Guest"
        self.assertEqual(self.mode.get_design_mode(), "Native")

    def test_missing_request_session_is_native_without_touching_user_storage(self):
        self.fake.local = NS()
        self.assertEqual(self.mode.get_design_mode(), "Native")
        self.fake.db.get_value.assert_not_called()

    def test_native_effective_palette_does_not_destroy_the_stored_navy_preference(self):
        self.values["reader@example.invalid"] = "Native"
        boot = {"desk_theme": "Navy"}
        self.mode.extend_bootinfo(boot)
        self.assertEqual(boot, {"desk_theme": "Dark", "hero_design_mode": "Native"})
        self.fake.db.set_value.assert_not_called()

    def test_native_projection_keeps_business_fields_and_every_security_attribute(self):
        raw = {"name": "Project", "doctype": "DocType", "permissions": [{"role": "Reader", "read": 1}],
            "__js": "business and Project scripts", "fields": [
                {"fieldname": "custom_project_documents_tab", "fieldtype": "Tab Break", "idx": 1},
                {"fieldname": "customer_details", "fieldtype": "Section Break", "label": "", "idx": 2},
                {"fieldname": "project_name", "fieldtype": "Data", "reqd": 1, "read_only": 1, "permlevel": 1, "hidden": 0, "idx": 3},
                {"fieldname": "customer", "fieldtype": "Link", "options": "Customer", "permlevel": 2, "read_only": 0, "idx": 4},
                {"fieldname": "custom_edit_project_address", "fieldtype": "Check", "insert_after": "customer"},
                {"fieldname": "custom_project_address", "fieldtype": "Link", "options": "Address",
                    "insert_after": "custom_edit_project_address", "reqd": 1,
                    "read_only_depends_on": "eval:!doc.custom_edit_project_address"},
                {"fieldname": "monitor_progress_tab", "fieldtype": "Tab Break", "idx": 5},
                {"fieldname": "custom_project_responsible", "fieldtype": "Link", "options": "User",
                    "insert_after": "monitor_progress_tab", "permlevel": 0, "idx": 6},
                {"fieldname": "custom_hero_project_manager", "fieldtype": "Link", "options": "User",
                    "read_only": 1, "hidden": 1, "permlevel": 1, "idx": 7},
            ]}
        original = [
            {"fieldname": "project_name", "fieldtype": "Data"},
            {"fieldname": "customer_details", "fieldtype": "Section Break", "label": "Customer Details"},
            {"fieldname": "customer", "fieldtype": "Link"},
            {"fieldname": "monitor_progress_tab", "fieldtype": "Tab Break"},
        ]
        snapshot = deepcopy(raw)
        result = self.mode.native_project_meta(raw, original)
        fields = {field["fieldname"]: field for field in result["fields"]}
        self.assertNotIn("custom_project_documents_tab", fields)
        self.assertIn("custom_project_responsible", fields)
        self.assertIn("custom_hero_project_manager", fields)
        self.assertIn("custom_edit_project_address", fields)
        self.assertIn("custom_project_address", fields)
        self.assertEqual(result["fields"][0]["fieldname"], "project_name")
        for field in raw["fields"]:
            if field["fieldname"] not in fields:
                continue
            for key in ("read_only", "permlevel", "hidden", "reqd", "options", "depends_on", "read_only_depends_on"):
                self.assertEqual(fields[field["fieldname"]].get(key), field.get(key))
        self.assertEqual([field["idx"] for field in result["fields"]], list(range(1, len(fields) + 1)))
        self.assertEqual(raw, snapshot)
        self.assertEqual(result["permissions"], raw["permissions"])
        self.assertEqual(result["__js"], raw["__js"])

    def test_foreign_field_with_an_app_presentation_name_is_not_deleted_if_it_has_data_type(self):
        raw = {"fields": [{"fieldname": "custom_project_documents_tab", "fieldtype": "Data", "read_only": 1}]}
        result = self.mode.native_project_meta(raw, [])
        self.assertEqual(result["fields"][0]["fieldname"], "custom_project_documents_tab")

    def test_unrecognized_site_labels_are_not_overwritten(self):
        raw = {"fields": [{"fieldname": "customer_details", "fieldtype": "Section Break", "label": "Site-specific business term"}]}
        result = self.mode.native_project_meta(raw, [{"fieldname": "customer_details", "label": "Customer Details"}])
        self.assertEqual(result["fields"][0]["label"], "Site-specific business term")

    def test_metadata_override_keeps_native_transport_contract(self):
        self.assertEqual(self.exposed, [{"methods": ["POST"]}, {"methods": ["GET", "POST"]}])

    def test_native_response_is_private_and_never_changes_shared_custom_metadata(self):
        raw = {"name": "Project", "doctype": "DocType", "__js": "business scripts", "fields": [
            {"fieldname": "custom_project_documents_tab", "fieldtype": "Tab Break"},
            {"fieldname": "project_name", "fieldtype": "Data", "read_only": 1},
        ]}
        child = {"name": "Project User", "doctype": "DocType", "fields": []}
        snapshot = deepcopy(raw)
        self.fake.response = {"docs": []}
        def native(doctype, with_parent):
            self.assertEqual((doctype, with_parent), ("Project User", True))
            self.fake.response["docs"] = [raw, child]
        directory = "/tmp/opencode" if Path("/tmp/opencode").is_dir() else None
        with tempfile.TemporaryDirectory(prefix="native-design-meta-", dir=directory) as temporary:
            path = Path(temporary) / "project.json"
            path.write_text('{"fields":[{"fieldname":"project_name","fieldtype":"Data"}]}')
            self.fake.get_app_path = lambda *args: str(path)
            self.values["reader@example.invalid"] = "Native"
            with patch.dict(sys.modules, {"frappe.desk.form.load": NS(getdoctype=native)}):
                self.mode.getdoctype("Project User", True)
            self.assertEqual(len(self.fake.response["docs"][0]["fields"]), 1)
            self.assertIs(self.fake.response["docs"][1], child)
            self.assertEqual(raw, snapshot)
            self.values["reader@example.invalid"] = "Custom"
            with patch.dict(sys.modules, {"frappe.desk.form.load": NS(getdoctype=native)}):
                self.mode.getdoctype("Project User", True)
            self.assertEqual(self.fake.response["docs"][0], snapshot)


if __name__ == "__main__":
    unittest.main()
