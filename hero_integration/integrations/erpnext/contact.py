"""Safe native Contact handling for the approved HERO customer bindings."""
from __future__ import annotations


class DeferredCustomerRelations:
    """Keep ERPNext's automatic relationship writes in the post-success pass."""

    def _hero_relations_deferred(self):
        from hero_integration.services.change_detection import managed_target_update_active
        from hero_integration.services.deferred_references import realizing
        return managed_target_update_active('Customer', self.name) and not realizing()

    def create_primary_contact(self):
        if not self._hero_relations_deferred():
            return super().create_primary_contact()

    def create_primary_address(self):
        if not self._hero_relations_deferred():
            return super().create_primary_address()

    def link_address_and_contact(self):
        if not self._hero_relations_deferred():
            return super().link_address_and_contact()


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


def synchronize_primary_contact(customer, source_values: dict[str, object]) -> None:
    """Persist selected details on the exclusive primary Contact and child rows.
    Customer person fields are read-only projections. Never match by name/email
    or change shared Contacts."""
    values = {
        field: _text(source_values.get(field))
        for field in ("first_name", "last_name", "email_id", "mobile_no", "salutation", "phone", "designation")
    }
    if not any(values.values()):
        return
    contact = _primary_contact(customer)
    if contact is None:
        contact = _create_primary_contact(customer)
    if not _is_exclusive_customer_contact(contact, customer.name):
        raise HomePhoneConflict("The Customer primary Contact is shared or not linked to this Customer.")

    # ERPNext parses an Individual's display name before applying explicit
    # first/last names. A salutation can shift the first name into middle_name.
    # Remove that inferred duplicate only while correcting the parsed name.
    changed = False
    if (values["first_name"] and values["last_name"]
            and _text(_field_value(contact, "first_name")) != values["first_name"]
            and _text(_field_value(contact, "middle_name")) == values["first_name"]
            and _text(_field_value(contact, "last_name")) == values["last_name"]):
        _set_field(contact, "middle_name", None)
        changed = True
    for field in ("first_name", "last_name", "salutation", "designation"):
        if values[field] and _field_value(contact, field) != values[field]:
            _set_field(contact, field, values[field])
            changed = True
    changed = _set_primary_child(
        contact, "email_ids", "email_id", "is_primary", values["email_id"]
    ) or changed
    changed = _set_primary_child(
        contact, "phone_nos", "phone", "is_primary_mobile_no", values["mobile_no"]
    ) or changed
    changed = _set_primary_child(
        contact, "phone_nos", "phone", "is_primary_phone", values["phone"]
    ) or changed
    if changed:
        contact.save()


def primary_contact_values(customer, fields: set[str]) -> dict[str, object]:
    """Read the native Contact state for conflict-safe Customer snapshots."""
    result = {field: None for field in fields}
    contact = _primary_contact(customer)
    if contact is None or not _is_exclusive_customer_contact(contact, customer.name):
        return result
    for field in {"first_name", "last_name", "salutation", "designation"} & fields:
        result[field] = _text(_field_value(contact, field))
    if "email_id" in fields:
        result["email_id"] = _primary_child_value(contact, "email_ids", "email_id", "is_primary")
    if "mobile_no" in fields:
        result["mobile_no"] = _primary_child_value(contact, "phone_nos", "phone", "is_primary_mobile_no")
    if "phone" in fields:
        result["phone"] = _primary_child_value(contact, "phone_nos", "phone", "is_primary_phone")
    return result


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


def _set_primary_child(contact, table: str, value_field: str, primary_field: str, value: str | None) -> bool:
    if not value:
        return False
    rows = contact.get(table) or []
    matching = [row for row in rows if _field_value(row, value_field) == value]
    if matching:
        row = matching[0]
        if not _field_value(row, primary_field):
            _set_field(row, primary_field, 1)
            return True
        return False
    primary_rows = [row for row in rows if _field_value(row, primary_field)]
    if len(primary_rows) == 1:
        _set_field(primary_rows[0], value_field, value)
        return True
    contact.append(table, {value_field: value, primary_field: 1})
    return True


def _primary_child_value(contact, table: str, value_field: str, primary_field: str) -> str | None:
    rows = contact.get(table) or []
    row = next((item for item in rows if _field_value(item, primary_field)), None)
    return _text(_field_value(row, value_field)) if row is not None else None


def _set_field(document, fieldname: str, value: object) -> None:
    if hasattr(document, "set"):
        document.set(fieldname, value)
    else:
        document[fieldname] = value
        try:
            setattr(document, fieldname, value)
        except Exception:
            pass


def _field_value(row, fieldname: str):
    return row.get(fieldname) if hasattr(row, "get") else getattr(row, fieldname, None)


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


class HomePhoneConflict(Exception):
    """A source phone cannot safely mutate an existing shared Contact."""

