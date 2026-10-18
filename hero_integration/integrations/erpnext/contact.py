"""Safe native Contact handling for the approved HERO phone fields."""
from __future__ import annotations


def synchronize_home_phone(customer, source_phone_home: object) -> None:
    """Add the HERO home phone only to this Customer's exclusive Contact.

    HERO provides no Contact identity. A pre-existing shared Contact is never
    changed: changing it could alter another Customer's master data.
    """
    phone = _text(source_phone_home)
    if not phone:
        return
    contact = _primary_contact(customer)
    if contact is None:
        contact = _create_primary_contact(customer)
    if not _is_exclusive_customer_contact(contact, customer.name):
        raise HomePhoneConflict("The Customer primary Contact is shared or not linked to this Customer.")
    if any(_field_value(row, "phone") == phone for row in contact.get("phone_nos") or []):
        return
    contact.append("phone_nos", {"phone": phone, "is_primary_phone": 0})
    contact.save()


def home_phone_for_customer(customer) -> str | None:
    """Return the non-mobile phone retained by this integration, if present."""
    contact = _primary_contact(customer)
    if contact is None or not _is_exclusive_customer_contact(contact, customer.name):
        return None
    for row in contact.get("phone_nos") or []:
        if not _field_value(row, "is_primary_mobile_no"):
            phone = _text(_field_value(row, "phone"))
            if phone:
                return phone
    return None


def _primary_contact(customer):
    contact_name = customer.get("customer_primary_contact")
    if not contact_name:
        return None
    import frappe

    return frappe.get_doc("Contact", contact_name)


def _create_primary_contact(customer):
    from erpnext.selling.doctype.customer.customer import make_contact

    contact = make_contact(customer)
    customer.db_set("customer_primary_contact", contact.name)
    return contact


def _is_exclusive_customer_contact(contact, customer_name: str) -> bool:
    links = contact.get("links") or []
    customer_links = [
        row for row in links
        if _field_value(row, "link_doctype") == "Customer" and _field_value(row, "link_name") == customer_name
    ]
    return bool(customer_links) and len(links) == len(customer_links)


def _field_value(row, fieldname: str):
    return row.get(fieldname) if hasattr(row, "get") else getattr(row, fieldname, None)


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


class HomePhoneConflict(Exception):
    """A source phone cannot safely mutate an existing shared Contact."""

