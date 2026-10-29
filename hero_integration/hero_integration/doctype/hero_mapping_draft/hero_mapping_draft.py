from __future__ import annotations

import frappe
import json
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
        from hero_integration.services.mapping_draft_service import _new_definition
        if self.new_definition_json:
            try:
                context = _new_definition(json.loads(self.new_definition_json))
            except (TypeError, ValueError):
                frappe.throw('New-definition draft context is invalid.')
            if (context['site_scope'] != self.site_scope or context['definition_id'] != self.definition_id
                    or context['schema_fingerprint'] != self.base_schema_fingerprint or self.base_active_mapping
                    or int(self.base_active_revision or 0) != 0 or self.base_active_hash):
                frappe.throw('New-definition draft cannot claim an active baseline.')
        elif not self.base_active_mapping or int(self.base_active_revision or 0) < 1 or not self.base_active_hash:
            frappe.throw('An existing-definition draft requires its active baseline.')
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
                    and int(self.content_revision or 0) == (1 if self.new_definition_json else 0)
                    and int(self.reconciliation_revision or 0) == 0
                    and not self.reconciliation_json)
        if previous.state == "DISCARDED" and self.state == "OPEN" and not reopened:
            frappe.throw("Personal mapping draft reopen must start a new generation.")
        if self.new_definition_json != previous.new_definition_json and not (
                reopened and previous.new_definition_json and not self.new_definition_json):
            frappe.throw('New-definition context is immutable within a generation.')
        if self.base_content_json != previous.base_content_json and not reopened:
            frappe.throw("Personal mapping draft base_content_json is immutable.")

    def db_set(self, *args, **kwargs):
        frappe.throw("Use the personal draft storage service.")

    def before_rename(self, *args, **kwargs):
        frappe.throw("Personal draft identity is immutable.")

    def on_trash(self):
        frappe.throw("Discard personal drafts through their service; history must be retained.")
