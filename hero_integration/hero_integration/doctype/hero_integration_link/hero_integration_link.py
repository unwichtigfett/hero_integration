from frappe.model.document import Document


class HEROIntegrationLink(Document):
    def validate(self) -> None:
        self.source_system = self.source_system or "HERO"
        self.source_key = ":".join(
            (self.source_system, self.source_instance or "default", self.source_type, self.source_id)
        )


def on_doctype_update():
    """Every document save and delete looks up its binding by target (hardening 2026-11-01)."""
    import frappe

    frappe.db.add_index("HERO Integration Link", ["target_doctype", "target_name"], index_name="target_lookup")
