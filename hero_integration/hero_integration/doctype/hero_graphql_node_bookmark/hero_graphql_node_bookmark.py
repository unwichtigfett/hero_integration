"""Personal bookmark of one saved-schema GraphQL node; grants no mapping authority."""
import frappe
from frappe.model.document import Document

_IDENTITY_FIELDS = ("node_id", "node_kind", "node_name", "schema_snapshot", "schema_fingerprint", "source_instance", "bookmark_key")


class HEROGraphQLNodeBookmark(Document):
    def validate(self):
        if self.owner and self.owner != frappe.session.user:
            frappe.throw("Node bookmarks can only be changed by their owner.", frappe.PermissionError)
        from hero_integration.services.node_bookmark import NodeBookmarkError, normalize_alias

        try:
            self.alias, self.alias_key = normalize_alias(self.alias)
        except NodeBookmarkError as error:
            frappe.throw(str(error))
        if not self.is_new() and any(self.has_value_changed(field) for field in _IDENTITY_FIELDS):
            frappe.throw("A node bookmark identity is immutable; remove it and remember the node again.")
