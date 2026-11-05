from frappe.model.document import Document


class HEROPendingDependency(Document):
    def validate(self):
        from hero_integration.services.deferred_references import assert_owned_write
        assert_owned_write()
