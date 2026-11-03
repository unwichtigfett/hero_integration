"""HERO employees (company partners) as ERPNext users, so people fields and assignments can link them.

Read-only towards HERO (one query). For every active partner with an e-mail and no
ERPNext user yet, an enabled System User is created: no roles, no welcome e-mail,
no password. Every user of a HERO partner carries the partner ID and the HERO user
ID, so projects, assignments and tasks link people by ID, not by e-mail. Of an
existing user only these two empty ID fields are filled; nothing else changes.
Users of partners that left HERO are reported, not disabled. Admins grant roles in ERPNext.
"""
from __future__ import annotations

import frappe

PEOPLE_QUERY = """
query HeroPeople {
  company {
    partners {
      id user_id first_name last_name full_name email phone mobile status account_type
      user { id email }
    }
  }
}
"""


def partners(client) -> list[dict]:
    data = client.query(PEOPLE_QUERY, {})
    data = data.get("data", data) if isinstance(data, dict) else {}
    company = data.get("company") if isinstance(data, dict) else None
    return [row for row in (company or {}).get("partners") or [] if isinstance(row, dict)]


def person_email(partner: dict) -> str | None:
    """The login e-mail (the HERO user's, else the partner's), lower case."""
    for value in ((partner.get("user") or {}).get("email"), partner.get("email")):
        if isinstance(value, str) and "@" in value.strip():
            return value.strip().lower()
    return None


PARTNER_FIELD, USER_ID_FIELD = "custom_hero_partner_id", "custom_hero_user_id"


def _id(value) -> str | None:
    return str(value) if type(value) is int and value > 0 else None


def _user_with(field: str, value) -> str | None:
    key = _id(value)
    if not key or not frappe.db.has_column("User", field):
        return None
    rows = frappe.db.sql(f"SELECT name FROM `tabUser` WHERE enabled=1 AND `{field}`=%s LIMIT 2", (key,))
    return rows[0][0] if len(rows) == 1 else None


def user_by_partner(partner_id) -> str | None:
    """The enabled ERPNext User of a HERO partner ID, else None."""
    return _user_with(PARTNER_FIELD, partner_id)


def user_by_hero_user(user_id) -> str | None:
    """The enabled ERPNext User of a HERO user ID (assignments, tasks), else None."""
    return _user_with(USER_ID_FIELD, user_id)


def _existing_user(email: str, partner_id: str | None = None) -> str | None:
    if partner_id and frappe.db.has_column("User", PARTNER_FIELD):
        rows = frappe.db.sql(f"SELECT name FROM `tabUser` WHERE `{PARTNER_FIELD}`=%s LIMIT 1", (partner_id,))
        if rows:
            return rows[0][0]
    rows = frappe.db.sql("SELECT name FROM `tabUser` WHERE LOWER(email)=%s OR LOWER(name)=%s LIMIT 1", (email, email))
    return rows[0][0] if rows else None


def _ids(partner: dict) -> dict:
    return {PARTNER_FIELD: _id(partner.get("id")), USER_ID_FIELD: _id((partner.get("user") or {}).get("id"))}


def _link_ids(name: str, ids: dict, conflicts: list) -> bool:
    """Fill the empty ID fields of an existing user; a different stored ID is a conflict, not overwritten."""
    if not frappe.db.has_column("User", PARTNER_FIELD):
        return False
    current = frappe.db.get_value("User", name, list(ids), as_dict=True) or {}
    missing = {field: value for field, value in ids.items() if value and not current.get(field)}
    clash = [field for field, value in ids.items() if value and current.get(field) and current.get(field) != value]
    if clash:
        conflicts.append(f"{name}: stored {', '.join(clash)} differ from HERO")
    if missing:
        frappe.db.set_value("User", name, missing, update_modified=False)
    return bool(missing)


def sync_users(client) -> dict:
    """Create the missing ERPNext users of active HERO partners; returns what happened."""
    created, existing, skipped, failed, linked, conflicts = [], [], [], [], [], []
    active = set()
    for partner in partners(client):
        email = person_email(partner)
        if not email or (partner.get("status") or "active") != "active":
            skipped.append(partner.get("full_name") or str(partner.get("id")))
            continue
        active.add(email)
        ids = _ids(partner)
        name = _existing_user(email, ids[PARTNER_FIELD])
        if name:
            existing.append(email)
            if _link_ids(name, ids, conflicts):
                linked.append(email)
            continue
        first = (partner.get("first_name") or "").strip() or (partner.get("full_name") or email.split("@")[0]).strip()
        user = frappe.get_doc({
            "doctype": "User", "email": email, "first_name": first[:140],
            "last_name": (partner.get("last_name") or "").strip()[:140] or None,
            "user_type": "System User", "enabled": 1, "send_welcome_email": 0,
            "phone": (partner.get("phone") or "").strip() or None,
            **{field: value for field, value in ids.items() if value and frappe.db.has_column("User", field)},
        })
        user.flags.no_welcome_mail = True
        savepoint = "hero_people_user"
        frappe.db.savepoint(savepoint)
        try:
            user.insert(ignore_permissions=True)
            if "Customer" in {row.role for row in user.get("roles") or []}:
                user.remove_roles("Customer")  # ERPNext's default portal role is wrong for an employee
            created.append(email)
        except Exception as error:  # one bad record (e.g. an invalid phone) never stops the others
            frappe.db.rollback(save_point=savepoint)
            failed.append(f"{email}: {error}")
    domains = {email.split("@")[-1] for email in active}
    emails = [row[0] for row in frappe.db.sql(
        "SELECT LOWER(email) FROM `tabUser` WHERE enabled=1 AND user_type='System User'") if row[0]]
    # Enabled users of the company domains without an active HERO partner (left the company?); only reported.
    left = sorted(email for email in emails if email not in active and email.split("@")[-1] in domains)
    return {"created": created, "existing": existing, "ids_linked": linked, "id_conflicts": conflicts,
            "skipped": skipped, "failed": failed, "not_in_hero": left}


def _client():
    from hero_integration.services.project_send import _read_client

    return _read_client()


@frappe.whitelist(methods=["POST"])
def sync_hero_people() -> dict:
    """Desk/API action: create the missing users now (System Manager)."""
    frappe.only_for("System Manager")
    return sync_users(_client())


def sync_daily() -> None:
    """Scheduler: once a day while automatic synchronisation is on, so new HERO employees get a user."""
    from hero_integration.services.polling import effective_mode

    if effective_mode(frappe.get_single("HERO Integration Settings")) != "AUTO_SYNC":
        return
    try:
        result = sync_users(_client())
        frappe.db.commit()
        if result["created"] or result["failed"]:
            frappe.log_error(title="HERO people: users created", message=frappe.as_json(result))
    except Exception:
        frappe.db.rollback()
        frappe.log_error(title="HERO people: user sync failed", message=frappe.get_traceback())
