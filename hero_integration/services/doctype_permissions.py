"""Write permission for a mapping target, including child tables."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar


_configuration_review = ContextVar("hero_configuration_metadata_review", default=None)


@contextmanager
def configuration_metadata_review(frappe_api=None):
    """Review configuration as a manager, not as a business-data writer.

    This is a server-only compilation scope, not a Document flag or an
    execution permission. Site and actor changes invalidate the scope.
    """
    if frappe_api is None:
        import frappe as frappe_api
    actor = frappe_api.session.user
    if actor != "Administrator":
        frappe_api.only_for("System Manager")
    site = getattr(getattr(frappe_api, "local", None), "site", None)
    token = _configuration_review.set((site, actor))
    try:
        yield
    finally:
        _configuration_review.reset(token)


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
    review = _configuration_review.get()
    if review is not None and review == (
            getattr(getattr(frappe_api, "local", None), "site", None), frappe_api.session.user):
        return
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
