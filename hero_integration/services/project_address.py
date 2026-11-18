"""Optional local Project Address; no customer edits or implicit HERO writes."""
from __future__ import annotations

import frappe


ADDRESS = "custom_project_address"
EDIT = "custom_edit_project_address"


def ensure_fields():
    """Maintain the app-owned optional address field without changing its values."""
    definitions = [
        {"fieldname": EDIT, "fieldtype": "Check", "label": "Edit project address",
            "default": "0", "insert_after": "customer",
            "description": "Select a separate address instead of following the customer's primary address."},
        {"fieldname": ADDRESS, "fieldtype": "Link", "options": "Address",
            "label": "Project site address", "reqd": 0, "insert_after": EDIT,
            "read_only_depends_on": "eval:!doc." + EDIT,
            "description": "Optional. An existing project address is preserved; an empty address may use the customer's primary address."},
    ]
    meta = frappe.get_meta("Project")
    missing = []
    for definition in definitions:
        existing = meta.get_field(definition["fieldname"])
        if not existing:
            missing.append(definition)
            continue
        keys = ("fieldtype", "options", "read_only_depends_on")
        if (any((existing.get(key) or "") != (definition.get(key) or "") for key in keys)
                or existing.read_only or existing.hidden or existing.permlevel or existing.fetch_from):
            frappe.throw(frappe._("The Project Address fields have an incompatible definition."))
    if missing:
        from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

        create_custom_fields({"Project": missing}, update=False)
    existing_address = meta.get_field(ADDRESS)
    if existing_address and existing_address.get("reqd"):
        from frappe.custom.doctype.property_setter.property_setter import make_property_setter

        make_property_setter("Project", ADDRESS, "reqd", "0", "Check",
            validate_fields_for_doctype=False)
    _retire_invalid_legacy_fetch()


def _retire_invalid_legacy_fetch():
    """Stop an invalid Address-to-Customer autofetch, retaining type and values."""
    legacy = frappe.get_meta("Project").get_field("custom_adresse")
    if not legacy or legacy.get("fetch_from") != "customer.customer_primary_address":
        return
    if legacy.fieldtype != "Link" or legacy.options != "Customer" or not legacy.read_only:
        return  # A correctly typed or independently managed field is not ours.
    from frappe.custom.doctype.property_setter.property_setter import make_property_setter

    make_property_setter("Project", "custom_adresse", "fetch_from", "", "Small Text",
        validate_fields_for_doctype=False)


def _manual(value):
    if value not in (None, "", 0, 1, "0", "1"):
        frappe.throw(frappe._("Choose whether to edit the project address."))
    return value in (1, "1")


def _name(value):
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > 140:
        frappe.throw(frappe._("Invalid project address reference."))
    return value


def _read(doctype, name):
    document = frappe.get_doc(doctype, name)
    document.check_permission("read")
    document.apply_fieldlevel_read_permissions()
    return document


def _customer_address(customer):
    if not customer:
        return None
    return _read("Customer", customer).get("customer_primary_address")


def _address(name):
    if not name:
        return None
    document = _read("Address", name)
    if document.get("disabled") or any(not str(document.get(key) or "").strip()
            for key in ("address_line1", "city", "country")):
        return None
    return document


def validate_project_address(document, method=None):
    """Preserve chosen addresses and fill only empty fields from a usable default."""
    manual = _manual(document.get(EDIT))
    name = _name(document.get(ADDRESS))
    if name:
        before_save = getattr(document, "get_doc_before_save", None)
        previous = before_save() if callable(before_save) else None
        if not previous or previous.get(ADDRESS) != name:
            _read("Address", name)  # New selections retain document/field authorization.
        return
    if manual:
        return
    try:
        name = _customer_address(_name(document.get("customer")))
        address = _address(name)
    except (frappe.PermissionError, frappe.DoesNotExistError):
        # An optional default must not turn access to an unrelated address into
        # a prerequisite for saving a Project. Never copy unreadable data.
        return
    if address:
        document.set(ADDRESS, name)


@frappe.whitelist(methods=["GET"])
def get_address_context(customer=None, address=None, manual=0, project=None):
    """Read-only draft preview; every Project, Customer and Address is authorized."""
    if not getattr(frappe.session, "user", None) or frappe.session.user == "Guest":
        frappe.throw(frappe._("Log in to view project information."), frappe.PermissionError)
    project, customer, address = map(_name, (project, customer, address))
    if project:
        _read("Project", project)
    else:
        frappe.has_permission("Project", "create", throw=True)
    manual = _manual(manual)
    name = address or (None if manual else _customer_address(customer))
    document = _address(name)
    if not document:
        return {"name": None, "address": None, "state": "missing", "manual": manual}
    # Plain text only; do not expose Address contact details, dynamic links or HTML.
    city = " ".join(str(document.get(key) or "").strip() for key in ("pincode", "city")).strip()
    lines = [document.get("address_line1"), document.get("address_line2"),
        city, document.get("state"), document.get("country")]
    return {"name": name, "address": "\n".join(str(line) for line in lines if line),
        "state": "ready", "manual": manual}
