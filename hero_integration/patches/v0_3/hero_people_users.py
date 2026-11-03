"""One-time: ERPNext users for the active HERO employees (no roles, no welcome e-mail)."""


def execute():
    import frappe

    from hero_integration.integrations.hero_people import _client, sync_users

    try:
        result = sync_users(_client())
    except Exception:
        # HERO unreachable during migrate: the daily job or the API action creates the users later.
        frappe.log_error(title="HERO people: user sync skipped during migrate", message=frappe.get_traceback())
        return
    frappe.log_error(title="HERO people: users created", message=frappe.as_json(result))
