from frappe.model.document import Document

class HEROObjectBinding(Document):
    """Persistent B6 identity record; writes are service-owned."""


def on_doctype_update():
    import frappe

    frappe.db.add_index("HERO Object Binding", ["external_object_id"], "hero_object_external_id")
    frappe.db.add_index("HERO Object Binding", ['source_instance', 'object_type', 'target_doctype', 'external_object_id'], "hero_object_source")
    frappe.db.add_index("HERO Object Binding", ['target_doctype', 'target_name', 'source_instance'], "hero_object_target")
