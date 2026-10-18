from frappe.model.document import Document


class HEROIntegrationLink(Document):
    def validate(self) -> None:
        self.source_system = self.source_system or "HERO"
        self.source_key = ":".join(
            (self.source_system, self.source_instance or "default", self.source_type, self.source_id)
        )
