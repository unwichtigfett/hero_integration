"""Source-local person IDs are observations, not globally unique User identities."""
import frappe


FIELDS = ("custom_hero_partner_id", "custom_hero_user_id")


def execute():
    if not frappe.db.table_exists("Custom Field") or not frappe.db.table_exists("User"):
        return
    fields = []
    for fieldname in FIELDS:
        name = frappe.db.exists("Custom Field", {"dt": "User", "fieldname": fieldname})
        if not name:
            continue
        field = frappe.get_doc("Custom Field", name)
        if field.dt != "User" or field.fieldname != fieldname or field.fieldtype != "Data":
            raise ValueError("HERO person ID Custom Field has an unexpected schema.")
        fields.append(field)
    for field in fields:
        if field.get("unique"):
            frappe.db.set_value("Custom Field", field.name, "unique", 0, update_modified=False)
    if fields:
        frappe.clear_cache(doctype="User")
        # The native schema synchronizer removes only single-column unique
        # indexes whose metadata flag changed; composite indexes remain intact.
        # Always synchronize, including retries after metadata was saved but
        # the preceding migration stopped before its DDL completed.
        frappe.db.updatedb("User")
