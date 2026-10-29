"""Immutable identity evidence; this document grants no mapping authority."""
import frappe
from frappe.model.document import Document


class HEROMappingIdentityClaim(Document):
    def validate(self):
        from hero_integration.services.mapping_identity_claim import claim_insert_admitted
        if not self.is_new() or not claim_insert_admitted(self):
            frappe.throw('Mapping identity claims can only be inserted by the claim service.', frappe.PermissionError)

    def on_trash(self):
        frappe.throw('Mapping identity claims must be retained.', frappe.PermissionError)

    def before_rename(self, *_args, **_kwargs):
        frappe.throw('Mapping identity claims cannot be renamed.', frappe.PermissionError)

    def db_set(self, *_args, **_kwargs):
        frappe.throw('Mapping identity claims are immutable.', frappe.PermissionError)
