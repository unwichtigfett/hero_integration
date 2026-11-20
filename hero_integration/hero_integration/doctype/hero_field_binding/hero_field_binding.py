from frappe.model.document import Document

class HEROFieldBinding(Document):
    """Persistent B6 field state; no public mutation interface exists."""


def on_doctype_update():
    import frappe

    frappe.db.add_index("HERO Field Binding", ['object_binding', 'direction'], "hero_field_parent")
    frappe.db.add_index("HERO Field Binding", ['target_doctype', 'target_name', 'site_scope'], "hero_field_target")
