"""Pure employee mapping preview. No account creation or business writes.

The manifest maps all supplied Partner/User fields without duplicating native
Employee contact/name fields or confusing HERO IDs with Employee.user_id.
The employee run adapter supplies company and joining-date defaults and retains execution gates.
"""
from __future__ import annotations

from importlib.resources import files
import json

from hero_integration.services.direct_mapping import MISSING, lookup


OBSERVATIONS = {
    "Employee": [("custom_hero_partner_role", "Role"), ("custom_hero_account_type", "Account Type")],
    "User": [("custom_hero_role", "Role in Source System"), ("custom_hero_last_login", "Last Login in Source System"),
             ("custom_hero_created", "Created in Source System"), ("custom_hero_modified", "Modified in Source System")],
}


def mapping_contract():
    return json.loads(files("hero_integration").joinpath("config/employee_mapping.json").read_text())


def _positive_id(value):
    if type(value) is not int or value <= 0:
        raise ValueError("HERO employee identity must be a positive integer.")
    return str(value)


def preview(record, *, languages=()):
    """Return target proposals and blockers; never infer a login binding.

    Missing and null remain distinct. Full name is checked against ERPNext's
    computed name. A HERO locale needs an installed, explicitly supplied
    language code. Source audit timestamps stay source observations.
    """
    if not isinstance(record, dict):
        raise ValueError("HERO employee record must be an object.")
    contract = mapping_contract()
    proposals, blockers = [], []
    partner_id = _positive_id(record.get("id"))
    user_id = record.get("user_id", MISSING)
    nested_id = lookup(record, "user.id")
    if user_id not in (MISSING, None):
        _positive_id(user_id)
    if nested_id not in (MISSING, None):
        _positive_id(nested_id)
    if user_id not in (MISSING, None) and nested_id not in (MISSING, None) and user_id != nested_id:
        blockers.append("HERO employee user_id differs from user.id.")
    for field in contract["fields"]:
        value = lookup(record, field["source_path"])
        if value is MISSING:
            continue
        handling = field["handling"]
        if handling in {"partner_identity", "user_identity", "user_identity_check"}:
            value = _positive_id(value) if value is not None else None
        elif value is not None and not isinstance(value, str):
            raise ValueError("HERO employee text fields must be strings or null.")
        elif handling == "employee_status" and value is not None:
            states = {"active": "Active", "inactive": "Inactive", "suspended": "Suspended", "left": "Left"}
            if value not in states:
                blockers.append("HERO employee status requires an explicit mapping.")
            else:
                value = states[value]
        elif handling == "computed_name" and value is not None:
            name = " ".join(record.get(k) for k in ("first_name", "last_name") if isinstance(record.get(k), str) and record[k])
            if value != name:
                blockers.append("HERO full_name differs from the computed Employee name.")
        elif handling == "locale" and value is not None:
            choices = (value, value.replace("_", "-"), value.split("_")[0].split("-")[0])
            found = next((code for code in choices if code in languages), None)
            if found is None:
                blockers.append("HERO user locale requires an installed ERPNext language.")
            else:
                value = found
        proposals.append({**field, "value": value})
    return {"partner_id": partner_id, "fields": proposals, "blockers": blockers,
            "requires_user_binding": any(f["target_doctype"] == "User" for f in proposals)}
