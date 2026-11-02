"""Customer Send uses grouped Mutation mappings; fixed helpers retain compatibility coverage."""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import frappe

from hero_integration.services.localized_error import LocalizedError
from hero_integration.services.project_send import (SEND_ROLES, _decode, _hash, _mutation_client, _read_client,
                                                    _types)

READ_DOCUMENT = (
    "query HeroCustomerSendRead($ids: [Int]) { contacts(ids: $ids, show_deleted: true) { id nr title first_name "
    "last_name full_name email phone_mobile phone_home company_name category type is_contact_person is_deleted "
    "address { id street line_2 zipcode city country { code } } } }"
)
MUTATION_DOCUMENT = (
    "mutation HeroCustomerSend($contact: CustomerInput) { update_contact(contact: $contact) { id title first_name "
    "last_name company_name email phone_mobile phone_home address { id street line_2 zipcode city } } }"
)
# label, ERPNext comparison field, HERO CustomerInput field
CUSTOMER_FIELDS = (
    ("Salutation", "salutation", "title"),
    ("First name", "first_name", "first_name"),
    ("Last name", "last_name", "last_name"),
    ("Company name", "customer_name", "company_name"),
    ("E-mail", "email_id", "email"),
    ("Mobile", "mobile_no", "phone_mobile"),
    ("Phone", "hero_phone_home", "phone_home"),
)
# label, ERPNext Address field, HERO AddressInput field
ADDRESS_FIELDS = (
    ("Street", "address_line1", "street"),
    ("Address line 2", "address_line2", "line_2"),
    ("Postcode", "pincode", "zipcode"),
    ("City", "city", "city"),
)
# ERPNext salutation → HERO title (inverse of the import's salutation table).
HERO_TITLE = {"Dr": "Dr."}


class CustomerSendError(LocalizedError):
    """The customer cannot be sent safely; nothing was sent."""


def may_send() -> bool:
    return bool(set(SEND_ROLES) & set(frappe.get_roles()))


def legacy_preview(customer: str, client=None) -> dict:
    if not may_send():
        raise CustomerSendError("Only System Managers and HERO Writeback users may send customers to HERO.")
    document = frappe.get_doc("Customer", customer)
    frappe.has_permission("Customer", "write", doc=document, throw=True)
    link = _customer_link(customer)
    client = client or _read_client()
    record = _read_hero(client, link.source_id)
    if record.get("is_deleted"):
        raise CustomerSendError("The customer was deleted in HERO.")
    plan = _customer_plan()
    hero, local = _hero_values(plan, record), _local_values(plan, document, record)
    baseline = _decode(link.source_field_hashes)
    changes, conflicts = [], []
    for label, field, key in CUSTOMER_FIELDS:
        if field == "customer_name" and document.customer_type != "Company":
            continue  # a person's HERO name is built from first and last name
        if field not in local or _same(local[field], hero.get(field)):
            continue
        change = {"field": field, "input": key, "label": label, "hero": hero.get(field), "new": local[field]}
        hero_changed = field in baseline and baseline[field] != _hash(field, hero.get(field))
        (conflicts if hero_changed else changes).append(change)
    address_link, address = _address_link(record, link)
    if address is not None:
        hero_address, local_address = _hero_address(plan, record), _local_address(address)
        address_base = _decode(address_link.source_field_hashes)
        for label, field, key in ADDRESS_FIELDS:
            if _same(local_address.get(field), hero_address.get(field)):
                continue
            if field != "address_line2" and not local_address.get(field):
                continue  # HERO requires street, postcode and city
            change = {"field": field, "input": key, "label": label, "hero": hero_address.get(field),
                      "new": local_address.get(field), "address": True}
            hero_changed = field in address_base and address_base[field] != _hash(field, hero_address.get(field))
            (conflicts if hero_changed else changes).append(change)
    from hero_integration.api import _mutation_execution_state

    enabled = bool(_mutation_execution_state()["mutation_execution_enabled"])
    fingerprint = hashlib.sha256(json.dumps([customer, link.source_id, record, changes], sort_keys=True,
                                            default=str).encode()).hexdigest()
    return {"customer": customer, "hero_id": link.source_id, "changes": changes, "conflicts": conflicts,
            "fingerprint": fingerprint, "can_send": bool(changes) and not conflicts and enabled,
            "sending_enabled": enabled}


def legacy_send(customer: str, fingerprint: str = "", confirmed=0, client=None, mutation_client=None) -> dict:
    from hero_integration.api import _assert_mutation_execution_released, _finalize_mutation, _reserve_mutation

    _assert_mutation_execution_released()
    read_client = client or _read_client()
    current = legacy_preview(customer, client=read_client)
    if confirmed not in (True, 1, "1", "true") or fingerprint != current["fingerprint"]:
        raise CustomerSendError("HERO or ERPNext changed since the preview; open the preview again.")
    if current["conflicts"]:
        raise CustomerSendError("A field was changed in HERO since the last import; it is not overwritten.")
    if not current["changes"]:
        raise CustomerSendError("There is nothing to send.")
    snapshot = _require_schema()
    contact = {"id": int(current["hero_id"])}
    record = _read_hero(read_client, current["hero_id"])
    for change in current["changes"]:
        if change.get("address"):
            address = contact.setdefault("address", {"id": int(record["address"]["id"])})
            address[change["input"]] = change["new"] or ""
        else:
            value = change["new"]
            if change["field"] == "salutation" and value:
                value = HERO_TITLE.get(value, value)
            contact[change["input"]] = value or ""  # HERO ignores null; an empty string clears
    audit = _reserve_mutation(operation_name="update_contact", schema_fingerprint=snapshot.schema_fingerprint,
                              mapping_revision=1, request_fingerprint=current["fingerprint"])
    try:
        returned = (mutation_client or _mutation_client()).execute_once(MUTATION_DOCUMENT, {"contact": contact})
        result = returned.get("update_contact") if isinstance(returned, dict) else None
        if not isinstance(result, dict) or str(result.get("id")) != str(current["hero_id"]):
            raise CustomerSendError("HERO did not confirm the customer.")
        after = _read_hero(read_client, current["hero_id"])
        plan = _customer_plan()
        hero, hero_address = _hero_values(plan, after), _hero_address(plan, after)
        for change in current["changes"]:
            stored = (hero_address if change.get("address") else hero).get(change["field"])
            if not _same(stored, change["new"]):
                raise CustomerSendError("HERO did not store {0}.", change['label'])
    except Exception:
        _finalize_mutation(audit, "Failed", "Customer send not confirmed; no retry. Check the customer in HERO "
                                            "before sending again.")
        raise CustomerSendError("Sending to HERO could not be confirmed; nothing is retried automatically. "
                                "Check the customer in HERO.") from None
    _advance_baselines(customer, after, plan, current["changes"])
    _finalize_mutation(audit, "Succeeded", "Customer sent and verified; values are not kept in the audit.")
    return {"status": "SENT", "changes": [change["label"] for change in current["changes"]],
            "correlation_id": audit.correlation_id}


def _same(left, right) -> bool:
    norm = lambda value: value.strip() if isinstance(value, str) and value.strip() else (None if value in ("", None) else value)
    return norm(left) == norm(right)


def _customer_plan():
    """The mapping plan of the approved customer read contract (same rules as the import)."""
    from hero_integration import api
    from hero_integration.services.migration import _mapping_plan_for_run
    from hero_integration.services.read_contract import from_documents

    services = api._sync_area_read_services("default").get("HeroCustomers") or []
    if len(services) != 1:
        raise CustomerSendError("No unique approved HERO customer read contract is available.")
    service = frappe.get_doc("HERO GraphQL Service", services[0]["service"])
    mapping = frappe.get_doc("HERO GraphQL Mapping", services[0]["mapping"])
    snapshot = frappe.get_doc("HERO GraphQL Schema Snapshot", service.schema_snapshot)
    contract = from_documents(service, mapping, snapshot)
    run = SimpleNamespace(graphql_service=service.name, graphql_mapping=mapping.name,
                          service_revision=contract.service_revision, mapping_revision=contract.mapping_revision)
    return _mapping_plan_for_run(run, "HeroCustomers")


def _hero_values(plan, record) -> dict:
    """The ERPNext values the import would derive from this HERO customer."""
    from hero_integration.services.migration import _clean_customer_phones, _drop_invalid_email

    values = plan.values(record)
    _drop_invalid_email(values)
    home, _, _ = _clean_customer_phones(values, plan.home_phone(record))
    result = dict(values)
    if plan.has_home_phone():
        result["hero_phone_home"] = home
    return result


def _local_values(plan, document, record) -> dict:
    from hero_integration.services.migration import _customer_target_values

    values = _hero_values(plan, record)
    local = _customer_target_values(document, values, plan.customer_contact_fields(), values.get("hero_phone_home"),
                                    track_home_phone=plan.has_home_phone())
    home = values.get("hero_phone_home")
    if "hero_phone_home" in local and local["hero_phone_home"] is None and home:
        # The import never stores a number twice: a HERO phone equal to the mobile stays one row.
        from hero_integration.integrations.erpnext.contact import _primary_contact

        contact = _primary_contact(document)
        if contact is not None and any((row.phone or "").strip() == home.strip() for row in contact.get("phone_nos") or []):
            local["hero_phone_home"] = home
    return local


def _hero_address(plan, record) -> dict:
    from hero_integration.services.migration import _customer_primary_address

    try:
        address = _customer_primary_address(record, plan.customer_primary_address_line2(record))
    except Exception:
        address = None
    if not address:
        return {}
    return {"address_line1": address.get("street"), "address_line2": address.get("line_2"),
            "pincode": address.get("zipcode"), "city": address.get("city")}


def _local_address(address) -> dict:
    return {field: (address.get(field) or None) for _, field, _ in ADDRESS_FIELDS}


def _customer_link(customer: str):
    rows = frappe.get_all("HERO Integration Link",
                          filters={"source_system": "HERO", "source_type": "customer", "target_doctype": "Customer",
                                   "target_name": customer},
                          fields=["name", "source_id", "source_instance", "sync_status", "source_field_hashes",
                                  "target_field_hashes"])
    if len(rows) != 1:
        raise CustomerSendError("This customer is not imported from HERO.")
    link = rows[0]
    if link.sync_status != "Synchronized":
        raise CustomerSendError("The HERO link is {0}; resolve it before sending.", link.sync_status)
    from hero_integration.client.settings import get_configuration

    if (link.source_instance or "default") != (get_configuration().source_instance or "default"):
        raise CustomerSendError("The customer belongs to another HERO account.")
    return link


def _address_link(record, link):
    """The imported primary Address of this customer, if HERO and ERPNext both have it."""
    hero_address = record.get("address")
    if not isinstance(hero_address, dict) or type(hero_address.get("id")) is not int:
        return None, None
    rows = frappe.get_all("HERO Integration Link",
                          filters={"source_system": "HERO", "source_type": "customer_address",
                                   "source_id": str(hero_address["id"]), "target_doctype": "Address"},
                          fields=["name", "target_name", "sync_status", "source_field_hashes", "target_field_hashes"])
    if len(rows) != 1 or not frappe.db.exists("Address", rows[0].target_name):
        return None, None
    if rows[0].sync_status != "Synchronized":
        raise CustomerSendError("The HERO address link is {0}; resolve it before sending.", rows[0].sync_status)
    return rows[0], frappe.get_doc("Address", rows[0].target_name)


def _read_hero(client, hero_id) -> dict:
    data = client.query(READ_DOCUMENT, {"ids": [int(hero_id)]})
    data = data.get("data", data) if isinstance(data, dict) else {}
    rows = data.get("contacts") if isinstance(data, dict) else None
    row = rows[0] if isinstance(rows, list) and len(rows) == 1 else None
    if not isinstance(row, dict) or str(row.get("id")) != str(hero_id):
        raise CustomerSendError("HERO did not return this customer.")
    return row


def _require_schema():
    names = frappe.get_all("HERO GraphQL Schema Snapshot", filters={"analysis_status": "Validated"},
                           order_by="creation desc", pluck="name", limit_page_length=1)
    if not names:
        raise CustomerSendError("No validated HERO schema snapshot is available.")
    snapshot = frappe.get_doc("HERO GraphQL Schema Snapshot", names[0])
    types = _types(json.loads(snapshot.snapshot_json))
    inputs = {field["name"] for field in (types.get("CustomerInput") or {}).get("inputFields") or []}
    address = {field["name"] for field in (types.get("AddressInput") or {}).get("inputFields") or []}
    mutation = any(field.get("name") == "update_contact" for entry in types.values() if entry.get("kind") == "OBJECT"
                   for field in entry.get("fields") or [])
    if (not {"id", "address", *(key for _, _, key in CUSTOMER_FIELDS)} <= inputs
            or not {"id", *(key for _, _, key in ADDRESS_FIELDS)} <= address or not mutation):
        raise CustomerSendError("The HERO schema no longer offers the customer fields this button sends.")
    return snapshot


def _advance_baselines(customer, after, plan, changes) -> None:
    """Make the sync baselines of the sent fields equal to what the next import will see."""
    from hero_integration.services.change_detection import encode, field_hashes

    document = frappe.get_doc("Customer", customer)
    link = _customer_link(customer)
    hero, local = _hero_values(plan, after), _local_values(plan, document, after)
    source, target = _decode(link.source_field_hashes), _decode(link.target_field_hashes)
    for change in changes:
        if not change.get("address"):
            source[change["field"]] = field_hashes({change["field"]: hero.get(change["field"])})[change["field"]]
            target[change["field"]] = field_hashes({change["field"]: local.get(change["field"])})[change["field"]]
    frappe.db.set_value("HERO Integration Link", link.name,
                        {"source_field_hashes": encode(source), "target_field_hashes": encode(target)},
                        update_modified=False)
    if any(change.get("address") for change in changes):
        from hero_integration.services.migration import _customer_primary_address, _digest, _target_values

        address_link, address = _address_link(after, link)
        hero_address = _customer_primary_address(after, plan.customer_primary_address_line2(after))
        values = {**_address_values(document, hero_address)}
        payload = {"customer": document.name, **hero_address}
        a_source, a_target = _decode(address_link.source_field_hashes), _decode(address_link.target_field_hashes)
        a_source.update(field_hashes(values))
        a_target.update(field_hashes(_target_values(address, values)))
        frappe.db.set_value("HERO Integration Link", address_link.name, {
            "source_field_hashes": encode(a_source), "target_field_hashes": encode(a_target),
            "source_payload_hash": _digest(payload), "target_payload_hash": _digest(_target_values(address, values)),
        }, update_modified=False)


def _address_values(customer, address) -> dict:
    """The Address values the import writes for a HERO address (see _synchronize_customer_address)."""
    code = address.get("country_code") or "de"
    countries = frappe.get_list("Country", filters={"code": code.strip().lower()}, pluck="name", limit_page_length=2)
    values = {"country": countries[0] if len(countries) == 1 else None, "address_type": "Billing",
              "address_title": customer.name, "address_line1": address["street"], "pincode": address["zipcode"],
              "city": address["city"]}
    if isinstance(address.get("line_2"), str) and address["line_2"].strip():
        values["address_line2"] = address["line_2"].strip()
    return values


def preview(customer: str, client=None) -> dict:
    from hero_integration.services.mapped_send import preview as mapped_preview
    try:
        return mapped_preview('Customer', customer, client=client)
    except ValueError as error:
        raise CustomerSendError.from_error(error) from error


def send(customer: str, fingerprint: str = '', confirmed=0, client=None, mutation_client=None) -> dict:
    from hero_integration.services.mapped_send import send as mapped_send
    try:
        return mapped_send('Customer', customer, fingerprint=fingerprint, confirmed=confirmed,
                           client=client, mutation_client=mutation_client)
    except ValueError as error:
        raise CustomerSendError.from_error(error) from error
