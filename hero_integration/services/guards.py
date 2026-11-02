"""Fail-closed guards shared by manual, queued and automatic app paths."""

from __future__ import annotations

from contextlib import contextmanager

import frappe


def deletion_is_allowed() -> bool:
    return bool(frappe.db.get_single_value("HERO Integration Settings", "allow_deletion"))


def assert_deletion_allowed(*, action: str) -> None:
    if not deletion_is_allowed():
        frappe.throw(
            frappe._("HERO Integration deletion is disabled; refusing {0}.").format(action),
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
            frappe._("{0} {1} is imported from HERO and cannot be deleted here. Change it in HERO, "
                     "reject it in its HERO Integration Link while it awaits review, or use the HERO data purge.")
            .format(doc.doctype, doc.name),
            frappe.PermissionError,
        )


@contextmanager
def hero_project_naming():
    """Only the HERO import and the HERO rename tool may give a Project an ID ending in -H."""
    try:
        flags = frappe.flags
        previous = getattr(flags, "hero_project_naming", False)
    except (AttributeError, RuntimeError):  # no site context (offline doubles)
        yield
        return
    flags.hero_project_naming = True
    try:
        yield
    finally:
        flags.hero_project_naming = previous


def _reserved_for_hero(name) -> bool:
    from hero_integration.services.business_numbers import HERO_PROJECT_SUFFIX

    return isinstance(name, str) and name.upper().endswith(HERO_PROJECT_SUFFIX)


def protect_hero_project_ids(doc, method=None) -> None:
    """Project validate: a new Project created outside the HERO import must not end in -H."""
    if doc.is_new() and _reserved_for_hero(doc.name) and not getattr(frappe.flags, "hero_project_naming", False):
        frappe.throw(frappe._("Project IDs ending in -H are reserved for projects imported from HERO."))


def protect_hero_project_rename(doc, method=None, olddn=None, newdn=None, merge=False) -> None:
    """Project before_rename: only the HERO tools may rename a Project to an ID ending in -H."""
    if _reserved_for_hero(newdn) and not getattr(frappe.flags, "hero_project_naming", False):
        frappe.throw(frappe._("Project IDs ending in -H are reserved for projects imported from HERO."))
