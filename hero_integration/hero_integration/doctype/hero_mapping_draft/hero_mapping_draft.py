from __future__ import annotations

import frappe
from frappe.model.document import Document


class HEROMappingDraft(Document):
    def check_permission(self, *args, **kwargs):
        from hero_integration.services.mapping_draft_access import check_personal_document
        check_personal_document(self)
        return super().check_permission(*args, **kwargs)

    def validate(self) -> None:
        from hero_integration.services.frappe_mapping_draft_storage import assert_storage_write
        try:
            assert_storage_write(self)
        except PermissionError:
            frappe.throw("Personal mapping document write denied.", frappe.PermissionError)
        previous = self.get_doc_before_save() if not self.is_new() else None
        if previous is None:
            if self.owner_user != frappe.session.user:
                frappe.throw("Draft owner must be the current administrator.")
            return
        for field in ("draft_key", "site_scope", "definition_id", "owner_user"):
            if self.get(field) != previous.get(field):
                frappe.throw(f"Personal mapping draft {field} is immutable.")
        # The immutable base belongs to a generation. Opening a discarded
        # draft creates a new generation from the currently locked active
        # definition, so its base may legitimately differ from the old one.
        reopened = (previous.state == "DISCARDED" and self.state == "OPEN"
                    and int(self.generation or 0) == int(previous.generation or 0) + 1
                    and int(self.content_revision or 0) == 0
                    and int(self.reconciliation_revision or 0) == 0
                    and not self.reconciliation_json)
        if previous.state == "DISCARDED" and self.state == "OPEN" and not reopened:
            frappe.throw("Personal mapping draft reopen must start a new generation.")
        if self.base_content_json != previous.base_content_json and not reopened:
            frappe.throw("Personal mapping draft base_content_json is immutable.")

    def db_set(self, *args, **kwargs):
        frappe.throw("Use the personal draft storage service.")

    def before_rename(self, *args, **kwargs):
        frappe.throw("Personal draft identity is immutable.")

    def on_trash(self):
        frappe.throw("Discard personal drafts through their service; history must be retained.")
