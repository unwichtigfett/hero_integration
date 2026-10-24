import frappe
from frappe.model.document import Document


class HEROMappingRegistryVersion(Document):
    """Immutable complete mapping-set revision; never an execution approval."""

    def before_save(self):
        if not self.is_new():
            frappe.throw("Mapping Registry versions are immutable.")
