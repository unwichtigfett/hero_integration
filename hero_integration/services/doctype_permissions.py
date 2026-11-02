"""Write permission for a mapping target, including child tables."""

from __future__ import annotations


def _is_child_table(doctype, frappe_api):
    try:
        return bool(frappe_api.get_meta(doctype).istable)
    except Exception:  # unknown DocType or a test double without metadata
        return False


def _has_writable_parent(doctype, frappe_api):
    parents = frappe_api.get_all("DocField", filters={"options": doctype, "fieldtype": ["in", ["Table", "Table MultiSelect"]]},
                                 pluck="parent", distinct=True)
    parents += frappe_api.get_all("Custom Field", filters={"options": doctype, "fieldtype": ["in", ["Table", "Table MultiSelect"]]},
                                  pluck="dt", distinct=True)
    for parent in sorted(set(parents)):
        if frappe_api.db.exists("DocType", parent) and frappe_api.has_permission(parent, "write"):
            return True
    return False


def can_doctype_write(doctype: str, frappe_api=None) -> bool:
    """Check without showing permission errors while enumerating a catalog."""
    if frappe_api is None:
        import frappe as frappe_api
    if _is_child_table(doctype, frappe_api):
        return _has_writable_parent(doctype, frappe_api)
    return bool(frappe_api.has_permission(doctype, "write"))


def ensure_doctype_write(doctype: str, frappe_api=None) -> None:
    """Require write access on a DocType or a parent of its child table."""
    if frappe_api is None:
        import frappe as frappe_api
    if not _is_child_table(doctype, frappe_api):
        frappe_api.has_permission(doctype, "write", throw=True)
        return
    if _has_writable_parent(doctype, frappe_api):
        return
    frappe_api.throw(
        frappe_api._("You need write permission for a DocType that contains {0} (e.g. Contact) "
                     "to activate HERO mappings into it.").format(doctype),
        frappe_api.PermissionError,
    )
