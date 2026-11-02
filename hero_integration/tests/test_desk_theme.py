"""Offline checks for per-user theme persistence and metadata lifecycle."""
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from hero_integration.services import desk_theme


class TestDeskTheme(unittest.TestCase):
    def setUp(self):
        self.field = NS(fieldtype="Select", options="Light\nDark\nAutomatic")
        def throw(message, exception=ValueError):
            raise exception(message)
        self.frappe = NS(session=NS(user="person@example.invalid"),
            _=lambda text: text, throw=throw, PermissionError=PermissionError,
            db=NS(set_value=Mock(), get_value=Mock(return_value=None)),
            get_meta=Mock(return_value=NS(get_field=Mock(return_value=self.field))),
            get_doc=Mock())
        self.patch = patch.object(desk_theme, "frappe", self.frappe)
        self.patch.start(); self.addCleanup(self.patch.stop)

    def test_navy_is_saved_for_only_the_current_user_without_manager_role(self):
        desk_theme.switch_theme("Navy")
        self.frappe.db.set_value.assert_called_once_with("User", "person@example.invalid", "desk_theme", "Navy")

    def test_standard_choices_keep_the_native_handler_contract(self):
        with patch.object(desk_theme, "_switch_standard_theme", return_value="native") as native:
            for theme in ("Light", "Dark", "Automatic"):
                with self.subTest(theme=theme):
                    self.assertEqual(desk_theme.switch_theme(theme), "native")
                    native.assert_called_with(theme)
        self.frappe.db.set_value.assert_not_called()

    def test_guest_or_missing_session_cannot_save_a_preference(self):
        for user in ("Guest", "", None):
            self.frappe.session.user = user
            with self.subTest(user=user), self.assertRaises(PermissionError):
                desk_theme.switch_theme("Navy")
        self.frappe.db.set_value.assert_not_called()

    def test_unknown_or_non_string_choices_cannot_write(self):
        for theme in ("navy", "Sepia", "Navy\nDark", "", None, ["Navy"]):
            with self.subTest(theme=theme), self.assertRaises((ValueError, TypeError)):
                desk_theme.switch_theme(theme)
        self.frappe.db.set_value.assert_not_called()

    def test_install_preserves_custom_options_order_and_is_idempotent(self):
        self.field.options = "Light\nDark\nAutomatic\nSepia"
        def save(value):
            self.field.options = value
        with patch.object(desk_theme, "_write_options", side_effect=save) as write:
            desk_theme.ensure_navy_theme()
            desk_theme.ensure_navy_theme()
        write.assert_called_once_with("Light\nDark\nAutomatic\nSepia\nNavy")

    def test_install_does_not_duplicate_an_existing_navy_option(self):
        self.field.options = "Light\nNavy\nDark\nAutomatic"
        with patch.object(desk_theme, "_write_options") as write:
            desk_theme.ensure_navy_theme()
        write.assert_not_called()

    def test_install_requires_the_framework_select_field(self):
        for field in (None, NS(fieldtype="Data", options="")):
            self.frappe.get_meta.return_value.get_field.return_value = field
            with self.subTest(field=field), self.assertRaises(ValueError):
                desk_theme.ensure_navy_theme()

    def test_uninstall_changes_only_navy_preferences_and_retains_other_options(self):
        self.field.options = "Light\nNavy\nDark\nAutomatic\nSepia"
        with patch.object(desk_theme, "_write_options") as write:
            desk_theme.remove_navy_theme()
        self.frappe.db.set_value.assert_called_once_with("User", {"desk_theme": "Navy"}, "desk_theme", "Dark", update_modified=False)
        write.assert_called_once_with("Light\nDark\nAutomatic\nSepia")

    def test_uninstall_does_not_rewrite_metadata_without_navy(self):
        with patch.object(desk_theme, "_write_options") as write:
            desk_theme.remove_navy_theme()
        write.assert_not_called()

    def test_existing_property_setter_is_updated_in_place(self):
        self.frappe.db.get_value.return_value = "User-desk_theme-options"
        setter = NS(value="Light", owner="custom-owner", module="Custom", save=Mock())
        self.frappe.get_doc.return_value = setter
        desk_theme._write_options("Light\nNavy")
        self.assertEqual((setter.value, setter.owner, setter.module), ("Light\nNavy", "custom-owner", "Custom"))
        setter.save.assert_called_once_with(ignore_permissions=True)

    def test_new_property_setter_uses_the_framework_api(self):
        with patch("frappe.custom.doctype.property_setter.property_setter.make_property_setter") as create:
            desk_theme._write_options("Light\nDark\nAutomatic\nNavy")
        create.assert_called_once_with("User", "desk_theme", "options", "Light\nDark\nAutomatic\nNavy", "Text")
