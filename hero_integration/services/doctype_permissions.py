"""Write permission for a mapping target, including child tables."""

from __future__ import annotations


def ensure_doctype_write(doctype: str, frappe_api=None) -> None:
    """Throw unless the current user may write ``doctype``.

    A child table (e.g. Contact Phone) has no permissions of its own; Frappe
    checks its parent.  For a child table the user needs write access on a
    parent DocType that holds it.  ``frappe_api`` is the caller's ``frappe``.
    """
    if frappe_api is None:
        import frappe as frappe_api
    try:
        is_table = bool(frappe_api.get_meta(doctype).istable)
    except Exception:  # unknown DocType or a test double without metadata
        is_table = False
    if not is_table:
        frappe_api.has_permission(doctype, "write", throw=True)
        return
    parents = frappe_api.get_all("DocField", filters={"options": doctype, "fieldtype": ["in", ["Table", "Table MultiSelect"]]},
                                 pluck="parent", distinct=True)
    parents += frappe_api.get_all("Custom Field", filters={"options": doctype, "fieldtype": ["in", ["Table", "Table MultiSelect"]]},
                                  pluck="dt", distinct=True)
    for parent in sorted(set(parents)):
        if frappe_api.db.exists("DocType", parent) and frappe_api.has_permission(parent, "write"):
            return
    frappe_api.throw(f"You need write permission for a DocType that contains {doctype} (e.g. Contact) "
                     "to activate HERO mappings into it.", frappe_api.PermissionError)
