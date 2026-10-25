from __future__ import annotations

import frappe
from frappe.model.document import Document


class HEROMappingRevision(Document):
    def validate(self) -> None:
        frappe.only_for("System Manager")
        if not self.is_new():
            frappe.throw("Mapping revisions are immutable.")

