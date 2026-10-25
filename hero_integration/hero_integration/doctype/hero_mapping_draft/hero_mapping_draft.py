from __future__ import annotations

import frappe
from frappe.model.document import Document


class HEROMappingDraft(Document):
    def validate(self) -> None:
        frappe.only_for("System Manager")
        previous = self.get_doc_before_save() if not self.is_new() else None
        if previous is None:
            if self.owner_user != frappe.session.user:
                frappe.throw("Draft owner must be the current administrator.")
            return
        for field in ("draft_key", "site_scope", "definition_id", "owner_user", "generation"):
            if self.get(field) != previous.get(field):
                frappe.throw(f"Personal mapping draft {field} is immutable.")

