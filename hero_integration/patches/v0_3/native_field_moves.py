"""One-time: HERO values that duplicate a native ERPNext field move into it (notes, colour, done, customer type)."""


def execute():
    import frappe

    from hero_integration.integrations.native_field_moves import run

    result = run()
    if result["rules"] or result["copied"] or result["hidden"]:
        frappe.log_error(title="HERO native field move", message=frappe.as_json(result))
