"""Fail-closed guards shared by manual, queued and automatic app paths."""

from __future__ import annotations

import frappe


def deletion_is_allowed() -> bool:
    return bool(frappe.db.get_single_value("HERO Integration Settings", "allow_deletion"))


def assert_deletion_allowed(*, action: str) -> None:
    if not deletion_is_allowed():
        frappe.throw(
            f"HERO Integration deletion is disabled; refusing {action}.",
            frappe.PermissionError,
        )


def assert_app_document_deletion_allowed(doc, method=None) -> None:
    """Frappe hook for HERO-owned records; foreign application records are untouched."""

    assert_deletion_allowed(action=f"deletion of {doc.doctype} {doc.name}")
