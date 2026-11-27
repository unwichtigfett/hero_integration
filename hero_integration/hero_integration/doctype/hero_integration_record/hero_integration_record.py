from frappe.model.document import Document


class HEROIntegrationRecord(Document):
    pass


def on_doctype_update():
    import frappe

    frappe.db.add_index("HERO Integration Record", ["run", "source_id"], index_name="hero_run_source")
