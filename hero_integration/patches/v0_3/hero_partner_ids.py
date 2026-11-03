"""One-time: ERPNext users carry the HERO partner ID; projects link people by ID (contract v5)."""


def execute():
    import frappe

    from hero_integration.integrations.people_and_source_fields import run_partner_ids

    frappe.log_error(title="HERO partner IDs", message=frappe.as_json(run_partner_ids()))
