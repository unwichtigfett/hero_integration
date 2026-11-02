"""Helpdesk and CRM reference the real ERPNext records instead of copying them.

User decision 2026-11-01 (option A): HD Ticket, CRM Lead and CRM Deal get two
plain Link fields to the ERPNext Customer and Project.  No HD Customer or CRM
Organization mirror is created, and the apps' own ERPNext integrations stay
disabled.  Contacts are already the shared Frappe ``Contact``.
"""

from __future__ import annotations

import json

import frappe

CUSTOMER_FIELD, PROJECT_FIELD = "erp_customer", "erp_project"
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
    _show_in_helpdesk()
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
    """doc_events validate: the apps' own ERPNext integrations would copy or create records.

    ERPNext HD Settings mirrors every Customer as an HD Customer; ERPNext CRM
    Settings can create Customers from deals.  Both contradict option A
    (docs/app_integration_1.0.md): ERPNext data is referenced, not copied.
    """
    if doc.doctype == "ERPNext HD Settings" and doc.get("enabled"):
        frappe.throw(frappe._("The Helpdesk ERPNext mirror stays disabled: tickets reference the ERPNext Customer "
                              "and Project directly (HERO integration, option A)."))
    if doc.doctype == "ERPNext CRM Settings" and doc.get("enabled") and doc.get("create_customer_on_status_change"):
        frappe.throw(frappe._("CRM must not create ERPNext Customers: customers are created in HERO and imported; "
                              "deals reference them (HERO integration, option A)."))
