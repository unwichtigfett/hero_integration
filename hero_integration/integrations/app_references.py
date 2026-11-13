"""Helpdesk and CRM link to native ERPNext Customer/Project and shared Contact.
ERPNext owns the customer data: Helpdesk may list every ERPNext Customer through
its linked HD Customer, but cannot create, change or delete one. CRM never creates Customers."""

from __future__ import annotations

import json

import frappe

CUSTOMER_FIELD, PROJECT_FIELD = "erp_customer", "erp_project"
HELPDESK_CUSTOMER_FIELD = "customer"
# Set by Helpdesk while it applies an ERPNext-side rename or deletion to the linked record.
HELPDESK_CASCADE_FLAG = "erpnext_hd_cascade_in_progress"
ERPNEXT_OWNED_HD_CUSTOMER_FIELDS = ("customer_type", "image")
REFERENCE_DOCTYPES = {
    # doctype: (insert_after, contact field, email field)
    "HD Ticket": ("customer", "contact", "raised_by"),
    "CRM Lead": ("organization", None, "email"),
    "CRM Deal": ("organization", "contact", "email"),
}
READ_ROLES = {
    "Customer": ("Agent", "Agent Manager"),
    "Project": ("Agent", "Agent Manager", "Sales User", "Sales Manager"),
    "Contact": ("Agent",),
}


def _field_definitions(insert_after: str) -> list[dict]:
    return [
        {"fieldname": CUSTOMER_FIELD, "label": "ERPNext Customer", "fieldtype": "Link", "options": "Customer",
         "insert_after": insert_after, "search_index": 1,
         "description": "Reference to the ERPNext Customer; no copy is stored."},
        {"fieldname": PROJECT_FIELD, "label": "ERPNext Project", "fieldtype": "Link", "options": "Project",
         "insert_after": CUSTOMER_FIELD, "search_index": 1,
         "description": "Reference to the ERPNext Project; sets the Customer when empty."},
    ]


def ensure_reference_fields() -> None:
    """Create-only: add the reference fields to installed Helpdesk/CRM DocTypes."""
    from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

    missing = {}
    for doctype, (insert_after, _contact, _email) in REFERENCE_DOCTYPES.items():
        if not frappe.db.exists("DocType", doctype):
            continue
        for definition in _field_definitions(insert_after):
            if not frappe.db.exists("Custom Field", {"dt": doctype, "fieldname": definition["fieldname"]}):
                missing.setdefault(doctype, []).append(definition)
    if missing:
        create_custom_fields(missing, update=False)


def setup_reference_access() -> None:
    """One-time setup: read access for agents/sales users and visible fields in both UIs."""
    from frappe.permissions import add_permission, update_permission_property

    ensure_reference_fields()
    for doctype, roles in READ_ROLES.items():
        for role in roles:
            if not frappe.db.exists("Role", role):
                continue
            if not frappe.db.exists("Custom DocPerm", {"parent": doctype, "role": role, "permlevel": 0, "if_owner": 0}):
                add_permission(doctype, role, 0)
            update_permission_property(doctype, role, 0, "read", 1)
    installed_apps = set(frappe.get_installed_apps())
    if "helpdesk" in installed_apps:
        _show_in_helpdesk()
    if "crm" in installed_apps:
        for doctype in ("CRM Lead", "CRM Deal"):
            _show_in_crm_side_panel(doctype)


def _show_in_helpdesk() -> None:
    if not frappe.db.exists("HD Ticket Template", "Default"):
        return
    template = frappe.get_doc("HD Ticket Template", "Default")
    present = {row.fieldname for row in template.get("fields") or []}
    for fieldname in (CUSTOMER_FIELD, PROJECT_FIELD):
        if fieldname not in present:
            template.append("fields", {"fieldname": fieldname, "required": 0, "visible_to": "Agents",
                                       "editable_after_creation": 1})
    template.save(ignore_permissions=True)


def _show_in_crm_side_panel(doctype: str) -> None:
    name = frappe.db.get_value("CRM Fields Layout", {"dt": doctype, "type": "Side Panel"}, "name")
    if not name:
        return
    layout_doc = frappe.get_doc("CRM Fields Layout", name)
    layout = json.loads(layout_doc.layout or "[]")
    if any(CUSTOMER_FIELD in column.get("fields", []) for section in layout for column in section.get("columns", [])):
        return
    layout.insert(0, {"label": "ERPNext", "name": "erpnext_reference_section", "opened": True,
                      "columns": [{"name": "column_erpnext_reference", "fields": [CUSTOMER_FIELD, PROJECT_FIELD]}]})
    layout_doc.layout = json.dumps(layout)
    layout_doc.save(ignore_permissions=True)


def fill_references(doc, method=None) -> None:
    """doc_events validate: keep Customer and Project consistent; fill the Customer when unambiguous."""
    if not doc.meta.has_field(CUSTOMER_FIELD):
        return
    project = doc.get(PROJECT_FIELD)
    if project:
        project_customer = frappe.db.get_value("Project", project, "customer")
        if project_customer and not doc.get(CUSTOMER_FIELD):
            doc.set(CUSTOMER_FIELD, project_customer)
        elif project_customer and doc.get(CUSTOMER_FIELD) != project_customer:
            frappe.throw(frappe._("ERPNext Project {0} belongs to Customer {1}, not {2}.").format(
                project, project_customer, doc.get(CUSTOMER_FIELD)))
    if not doc.get(CUSTOMER_FIELD):
        customer = _customer_for(doc)
        if customer:
            doc.set(CUSTOMER_FIELD, customer)
    _link_helpdesk_customer(doc)


def _link_helpdesk_customer(doc) -> None:
    """Show the ERPNext Customer in Helpdesk's own customer field, lists and statistics."""
    customer = doc.get(CUSTOMER_FIELD)
    if doc.doctype != "HD Ticket" or not customer or not doc.meta.has_field(HELPDESK_CUSTOMER_FIELD):
        return
    linked = frappe.get_all("HD Customer", filters={"erpnext_customer": customer}, pluck="name", limit=1)
    if linked and doc.get(HELPDESK_CUSTOMER_FIELD) != linked[0]:
        doc.set(HELPDESK_CUSTOMER_FIELD, linked[0])


def link_existing_helpdesk_tickets() -> int:
    """Idempotent: give existing tickets the HD Customer linked to their ERPNext Customer."""
    if not (frappe.db.exists("DocType", "HD Customer") and frappe.get_meta("HD Ticket").has_field(CUSTOMER_FIELD)):
        return 0
    linked = dict(frappe.get_all("HD Customer", filters={"erpnext_customer": ["is", "set"]},
                                 fields=["erpnext_customer", "name"], as_list=True))
    changed = 0
    for ticket in frappe.get_all("HD Ticket", filters={CUSTOMER_FIELD: ["in", list(linked)]},
                                 fields=["name", CUSTOMER_FIELD, HELPDESK_CUSTOMER_FIELD]) if linked else []:
        target = linked[ticket[CUSTOMER_FIELD]]
        if ticket[HELPDESK_CUSTOMER_FIELD] != target:
            frappe.db.set_value("HD Ticket", ticket["name"], HELPDESK_CUSTOMER_FIELD, target, update_modified=False)
            changed += 1
    return changed


def _customer_for(doc) -> str | None:
    """The single ERPNext Customer of the document's Contact (or its e-mail address)."""
    _after, contact_field, email_field = REFERENCE_DOCTYPES.get(doc.doctype, (None, None, None))
    contacts = []
    if contact_field and doc.get(contact_field):
        contacts = [doc.get(contact_field)]
    elif email_field and doc.get(email_field):
        email = str(doc.get(email_field)).strip()
        contacts = frappe.get_all("Contact Email", filters={"email_id": email, "parenttype": "Contact"},
                                  pluck="parent", distinct=True)
    if not contacts:
        return None
    customers = set(frappe.get_all(
        "Dynamic Link",
        filters={"parenttype": "Contact", "parent": ["in", contacts], "link_doctype": "Customer"},
        pluck="link_name",
    ))
    return customers.pop() if len(customers) == 1 else None


def refuse_record_mirrors(doc, method=None) -> None:
    """CRM must reference existing Customers. Helpdesk may list them: see protect_erpnext_customer."""
    if doc.doctype == "ERPNext CRM Settings" and doc.get("enabled") and doc.get("create_customer_on_status_change"):
        frappe.throw(frappe._("CRM must not create ERPNext Customers: customers are created in HERO and imported; "
                              "deals reference them (HERO integration, option A)."))


def _helpdesk_lists_erpnext_customers() -> bool:
    return bool(frappe.db.get_single_value("ERPNext HD Settings", "enabled"))


def _applied_from_erpnext(doc) -> bool:
    return bool(doc.flags.get("ignore_erpnext_sync") or frappe.flags.get(HELPDESK_CASCADE_FLAG))


def protect_erpnext_customer(doc, method=None, *args, **kwargs) -> None:
    """HD Customer validate/before_rename/on_trash: the linked ERPNext Customer is the only place to
    create, rename, change or delete a customer; Helpdesk keeps the link, never an independent value."""
    if not _helpdesk_lists_erpnext_customers() or _applied_from_erpnext(doc):
        return
    if method == "validate":
        if doc.is_new():
            if not doc.get("erpnext_customer"):
                frappe.throw(frappe._("Create the customer in ERPNext; Helpdesk lists it automatically."))
            return
        if any(doc.has_value_changed(field) for field in ERPNEXT_OWNED_HD_CUSTOMER_FIELDS):
            frappe.throw(frappe._("Change this value on the ERPNext Customer; Helpdesk shows it automatically."))
    elif method == "before_rename":
        frappe.throw(frappe._("Rename the ERPNext Customer; Helpdesk follows automatically."))
    elif method == "on_trash" and doc.get("erpnext_customer"):
        frappe.throw(frappe._("Delete the ERPNext Customer; Helpdesk follows automatically."))
