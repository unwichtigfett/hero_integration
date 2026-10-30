"""Drop the stored polling data sources and filters; polling now always reads every reviewed source unfiltered."""
from __future__ import annotations

import frappe


def execute() -> None:
    frappe.db.delete("Singles", {
        "doctype": "HERO Integration Settings",
        "field": ("in", ("polling_data_sources", "polling_filters_json")),
    })
