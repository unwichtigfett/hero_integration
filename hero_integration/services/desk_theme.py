"""Persist Navy alongside native Desk themes without changing Frappe sources."""
from __future__ import annotations

import frappe


THEMES = frozenset({"Light", "Dark", "Automatic", "Navy"})


@frappe.whitelist(methods=["POST"])
def switch_theme(theme: str):
    """Change only the authenticated user's Desk appearance."""
    user = getattr(getattr(frappe, "session", None), "user", None)
    if not user or user == "Guest":
        frappe.throw(frappe._("Log in to change your Desk theme."), frappe.PermissionError)
    if not isinstance(theme, str) or theme not in THEMES:
        frappe.throw(frappe._("Unsupported Desk theme."))
    if theme == "Navy":
        frappe.db.set_value("User", user, "desk_theme", theme)
    else:
        return _switch_standard_theme(theme)


def _switch_standard_theme(theme):
    from frappe.core.doctype.user.user import switch_theme as native_switch_theme

    return native_switch_theme(theme)


def ensure_navy_theme():
    """Append Navy to the existing User preference during install/migrate."""
    field = frappe.get_meta("User").get_field("desk_theme")
    if field is None or field.fieldtype != "Select":
        frappe.throw(frappe._("The User Desk Theme field must be a Select field."))
    options = field.options or ""
    if "Navy" not in options.split("\n"):
        separator = "" if not options or options.endswith("\n") else "\n"
        _write_options(options + separator + "Navy")


def remove_navy_theme():
    """Keep saved preferences valid when this app is explicitly uninstalled."""
    frappe.db.set_value("User", {"desk_theme": "Navy"}, "desk_theme", "Dark", update_modified=False)
    field = frappe.get_meta("User").get_field("desk_theme")
    if field is None or field.fieldtype != "Select":
        return
    options = (field.options or "").split("\n")
    if "Navy" in options:
        _write_options("\n".join(option for option in options if option != "Navy"))


def _write_options(value):
    filters = {"doc_type": "User", "field_name": "desk_theme", "property": "options"}
    name = frappe.db.get_value("Property Setter", filters, "name")
    if name:
        setter = frappe.get_doc("Property Setter", name)
        setter.value = value
        setter.save(ignore_permissions=True)
    else:
        from frappe.custom.doctype.property_setter.property_setter import make_property_setter

        make_property_setter("User", "desk_theme", "options", value, "Text")
