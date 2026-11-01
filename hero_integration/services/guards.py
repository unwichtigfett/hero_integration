"""Fail-closed guards shared by manual, queued and automatic app paths."""

from __future__ import annotations

from contextlib import contextmanager

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


@contextmanager
def bound_target_deletion():
    """Allow deleting HERO-bound targets inside the purge or a review rejection."""
    try:
        flags = frappe.flags
        previous = getattr(flags, "hero_bound_target_deletion", False)
    except (AttributeError, RuntimeError):  # no site context (offline doubles)
        yield
        return
    flags.hero_bound_target_deletion = True
    try:
        yield
    finally:
        flags.hero_bound_target_deletion = previous


def protect_hero_bound_target(doc, method=None) -> None:
    """Wildcard on_trash: a HERO-bound record is deleted only via the purge or a rejection.

    Otherwise its binding would point to nothing and every later run would
    report a conflict.  Changes to such records belong in HERO.
    """
    if getattr(frappe.flags, "hero_bound_target_deletion", False) or str(doc.doctype).startswith("HERO "):
        return
    if frappe.db.exists("HERO Integration Link", {"target_doctype": doc.doctype, "target_name": doc.name}):
        frappe.throw(
            f"{doc.doctype} {doc.name} is imported from HERO and cannot be deleted here. Change it in HERO, "
            "reject it in its HERO Integration Link while it awaits review, or use the HERO data purge.",
            frappe.PermissionError,
        )
