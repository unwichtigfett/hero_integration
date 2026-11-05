"""One-time: read the lead source, the project designation and the HERO people (owner decision 2026-11-03)."""


def execute():
    import frappe

    from hero_integration.integrations.people_and_source_fields import run

    frappe.log_error(title="HERO source/people fields", message=frappe.as_json(run()))
