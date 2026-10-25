from __future__ import annotations

import frappe
from frappe.model.document import Document


class HEROMappingRevision(Document):
    def validate(self) -> None:
        if not self.is_new():
            frappe.throw("Mapping revisions are immutable.")
        from hero_integration.services.frappe_mapping_draft_storage import assert_storage_write
        assert_storage_write(self)

    def on_trash(self):
        frappe.throw("Mapping revision history must be retained.")

    def db_set(self, *args, **kwargs):
        frappe.throw("Mapping revisions are immutable.")

    def before_rename(self, *args, **kwargs):
        frappe.throw("Mapping revisions are immutable.")
