"""Show the site's configured logo at the top left of the Desk; app marks stay as they are."""
from __future__ import annotations

import frappe


def site_logo() -> str | None:
    for doctype in ("Navbar Settings", "Website Settings"):
        logo = frappe.db.get_single_value(doctype, "app_logo")
        if logo:
            return logo
    return None


def extend_bootinfo(bootinfo) -> None:
    """Only a logo configured on this site is announced; without one the Desk is unchanged."""
    logo = site_logo()
    if logo:
        bootinfo["hero_site_logo"] = logo
