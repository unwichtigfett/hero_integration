"""Operator review of current field events and unresolved import relationships."""

import hashlib
import json

import frappe

from hero_integration.api import _configured_source_instance
from hero_integration.services import deferred_references as refs


def fail(message):
    frappe.throw(frappe._(message))


@frappe.whitelist(methods=["GET"])
def list_open(start=0, page_size=8):
    frappe.only_for("System Manager")
    source = _configured_source_instance()
    start, size = max(0, int(start)), min(50, max(1, int(page_size)))
    rows = []
    total = 0
    # Waiting rows of an active import belong to its workers. Only terminal
    # waiting cases with completed processing belong on the review queue.
    for kind, dt, base, fields in (
        (
            "field",
            "HERO Sync Change Event",
            {
                "outcome": "FIELD_CONFLICT",
                "open_field": ["is", "set"],
                "site_scope": frappe.local.site,
            },
            [
                "external_id as source_id",
                "object_type as operation",
                "outcome as detail",
            ],
        ),
        (
            "relationship",
            refs.MAIN,
            {"status": "Conflict"},
            ["source_id", "operation", "detail"],
        ),
        (
            "relationship",
            refs.MAIN,
            {
                "status": "Waiting",
                "operation": "HeroCustomers",
                "origin_run": [
                    "in",
                    frappe.get_all(
                        "HERO Integration Run",
                        filters={
                            "source_instance": source,
                            "action": "Customer Migration",
                            "state": "Completed With Conflicts",
                        },
                        pluck="name",
                    )
                    or [""],
                ],
            },
            ["source_id", "operation", "detail"],
        ),
        (
            "link",
            "HERO Integration Link",
            {
                "source_system": "HERO",
                "sync_status": ["in", ["Conflict", "Pending Review"]],
            },
            ["source_id", "source_type as operation", "conflict_detail as detail"],
        ),
    ):
        filters = dict(base, source_instance=source)
        count = frappe.db.count(dt, filters)
        offset = max(0, start - total)
        if len(rows) < size and offset < count:
            for row in frappe.get_all(
                dt,
                filters=filters,
                fields=["name", *fields],
                order_by="creation asc, name asc",
                limit_start=offset,
                limit_page_length=size - len(rows),
            ):
                row["kind"] = kind
                rows.append(row)
        total += count
    return {"rows": rows, "total": total, "start": start}


def observation(name, lock=False):
    frappe.only_for("System Manager")
    p = frappe.get_doc(refs.MAIN, name, for_update=lock)
    p.check_permission("write" if lock else "read")
    if p.source_instance != _configured_source_instance() or p.status not in (
        "Conflict",
        "Waiting",
    ):
        fail("This review is no longer open for this source.")
    return p


def address_rows(record, owner):
    from hero_integration.services.address_correction import verified_address

    primary = record.get("address") or {}
    rows = [("primary", primary)]
    rows.extend(
        ("collection", r.get("address") or {})
        for r in record.get("customer_addresses") or []
        if r.get("is_deleted") is False
        and (r.get("address") or {}).get("id") != primary.get("id")
    )
    missing = {}
    for role, raw in rows:
        identity = str(raw.get("id") or "")
        if identity and not all((raw.get(k) or "").strip() for k in ("street", "city")):
            if not verified_address(record, identity, role, expected_customer=owner):
                missing[identity] = (role, raw)
    return missing


def identity_cases(p, rec, customer, issues=None):
    """Only expose children for which the approved native preflight requests a decision."""
    from hero_integration.services import migration, mapped_relations as relations

    parent = frappe.get_doc("Customer", customer)
    plan = migration._mapping_plan_for_run(
        frappe.get_doc("HERO Integration Run", p.origin_run), p.operation
    )
    cases = {}
    reasons = {
        "customer_contacts": (
            "Contact",
            "An existing customer contact requires explicit source reconciliation.",
        ),
        "customer_addresses": (
            "Address",
            "An existing customer address requires explicit source reconciliation.",
        ),
        "customer_bank_account": (
            "Bank Account",
            "An existing IBAN requires explicit account reconciliation; no account was adopted.",
        ),
    }
    from types import SimpleNamespace

    for config in plan.adapter_options.get("native_relations", []):
        kind = config["kind"]
        if kind not in reasons:
            continue
        dt, reason = reasons[kind]
        if issues is not None and not any(
            reason in (row.detail or "") for row in issues
        ):
            continue
        if not frappe.has_permission(dt, "read"):
            continue
        scope = SimpleNamespace(adapter_options={"native_relations": [config]})
        source = rec
        if kind == "customer_addresses" and issues is not None:
            # Location supplements are reviewed separately. Do not take the writer's
            # correction locks while listing complete, unbound address identities.
            source = {
                **rec,
                "customer_addresses": [
                    r
                    for r in rec.get("customer_addresses") or []
                    if all(
                        ((r.get("address") or {}).get(k) or "").strip()
                        for k in ("street", "city")
                    )
                ],
            }
        for item in relations.prepare(scope, source, "Customer", customer):
            if frappe.db.exists(
                "HERO Integration Link",
                {
                    "source_system": "HERO",
                    "source_instance": p.source_instance,
                    "source_type": item.get("source_type", kind),
                    "source_id": item["source_id"],
                },
            ):
                continue
            if issues is None:
                try:
                    relations._preflight(item, "Customer", customer, parent=parent)
                except ValueError as exc:
                    if str(exc) != reason:
                        continue
                else:
                    continue
            cases[item["source_id"]] = (dt, item)
    return cases


def confirm_distinct_child(p, rec, plan, identity):
    """The operator explicitly confirms a separate person or separate delivery address."""
    from hero_integration.services import (
        mapped_collection_relations as collection,
        migration,
    )

    customer = rec["__hero_origin_binding"]["target_name"]
    parent = frappe.get_doc("Customer", customer, for_update=True)
    parent.check_permission("write")
    cases = identity_cases(p, rec, customer)
    if identity not in cases or cases[identity][0] not in ("Contact", "Address"):
        fail("The case changed. Reopen it before saving.")
    dt, item = cases[identity]
    frappe.has_permission(dt, "create", throw=True)
    if migration._link(
        item["source_type"], identity, p.source_instance, for_update=True
    ):
        fail("The case changed. Reopen it before saving.")
    values = dict(item["values"])
    if dt == "Address":
        country = frappe.db.get_value(
            "Country", {"code": values.pop("country_code")}, "name"
        )
        if not country:
            fail("Address country code must resolve to exactly one ERPNext Country.")
        values.update(country=country, address_type=item["config"]["address_type"])
    prepared = {
        "collection_kind": item["config"]["kind"],
        "document": None,
        "link": None,
        "values": values,
        "price_list": None,
    }
    collection.apply(plan, item, prepared, "Customer", customer, parent)
    parent.add_comment(
        "Info",
        frappe._("Separate ERPNext record confirmed for HERO ID {0}.").format(identity),
    )


def review_token(p, record):
    # Worker timestamps and scheduling metadata carry no operator decision.
    proof = record.get("__hero_origin_binding") or {}
    identity = {key: proof.get(key) for key in ("name", "target_doctype", "target_name", "mapping_version")}
    source = {key: value for key, value in record.items() if not key.startswith("__hero_")}
    state = [frappe.local.site, p.name, p.source_instance, p.operation, p.source_id,
             p.origin_run, p.mapping_version, p.status, identity, source]
    raw = json.dumps(state, sort_keys=True, separators=(",", ":")).encode()
    return "review:" + hashlib.sha256(raw).hexdigest()


def relationship_details(pending, issues, proof):
    details = [row.detail for row in issues if row.detail]
    if pending.operation == "HeroCustomers" and proof.get("target_doctype") == "Customer":
        from hero_integration.services.migration import decode_hashes, field_hashes

        field = "custom_hero_accounting_debitor_number"
        link = frappe.db.get_value("HERO Integration Link", {
            "name": proof.get("name"), "source_system": "HERO", "source_type": "customer",
            "source_instance": pending.source_instance, "source_id": pending.source_id,
            "target_doctype": "Customer", "target_name": proof.get("target_name"),
        }, ["source_field_hashes", "target_field_hashes"], as_dict=True)
        if (link and (decode_hashes(link.source_field_hashes) or {}).get(field) == field_hashes({field: None})[field]
                and (decode_hashes(link.target_field_hashes) or {}).get(field) == field_hashes({field: 0})[field]
                and frappe.db.get_value("Customer", proof["target_name"], field) == 0):
            # This legacy import note is unrelated to the current relationship decision.
            note = "Local changes were preserved: " + field + "."
            prefix = "Local field change observed; no outbound HERO mutation was prepared. " + note
            details = [detail.removeprefix("Deferred relationship reconciliation: ").removeprefix(prefix).strip()
                       if detail.startswith((prefix, "Deferred relationship reconciliation: " + prefix))
                       else detail for detail in details]
    return list(dict.fromkeys(detail for detail in details if detail))[:3] or [pending.detail]


@frappe.whitelist(methods=["GET"])
def review(name):
    p = observation(name)
    rec = refs._source_observation(p)
    proof = rec.get("__hero_origin_binding") or {}
    dt, target = proof.get("target_doctype"), proof.get("target_name")
    if target and (not dt or not frappe.has_permission(dt, "read", doc=target)):
        frappe.throw(frappe._("Not permitted"), frappe.PermissionError)
    issues = frappe.get_all(
        "HERO Integration Record",
        filters={
            "run": p.origin_run,
            "source_id": p.source_id,
            "source_type": refs._pending_identity(p)[0],
            "result": ["in", ["CONFLICT", "PENDING_REVIEW", "FAILED"]],
        },
        fields=["detail"],
        order_by="creation desc",
        limit_page_length=8,
    )
    missing = (
        address_rows(rec, target) if p.operation == "HeroCustomers" and target else {}
    )
    related = []
    if dt == "Customer" and target:
        for kind in ("Address", "Contact", "Bank Account"):
            if kind == "Bank Account":
                filters = {"party_type": "Customer", "party": target}
            else:
                ids = frappe.get_all(
                    "Dynamic Link",
                    filters={
                        "parenttype": kind,
                        "link_doctype": "Customer",
                        "link_name": target,
                    },
                    pluck="parent",
                )
                filters = {"name": ["in", ids or [""]]}
            if frappe.has_permission(kind, "read"):
                related.extend(
                    {"doctype": kind, "name": d.name}
                    for d in frappe.get_list(
                        kind, filters=filters, fields=["name"], limit_page_length=0
                    )
                )
    identities = (
        identity_cases(p, rec, target, issues)
        if p.operation == "HeroCustomers" and target and not missing
        else {}
    )
    return {
        "identities": [
            {
                "id": key,
                "doctype": dt,
                "values": {
                    k: (("…" + str(v)[-4:]) if k == "iban" else v)
                    for k, v in item["values"].items()
                    if v
                },
            }
            for key, (dt, item) in identities.items()
        ],
        "related": related,
        "modified": str(p.modified),
        "review_token": review_token(p, rec),
        "customer": target if dt == "Customer" else None,
        "doctype": dt,
        "target": target,
        "operation": p.operation,
        "run": p.origin_run,
        "source_id": p.source_id,
        "status": p.status,
        "details": relationship_details(p, issues, proof),
        "addresses": [
            dict(
                id=aid,
                role=role,
                street=raw.get("street"),
                city=raw.get("city"),
                zipcode=raw.get("zipcode"),
            )
            for aid, (role, raw) in missing.items()
        ],
    }


def checked_customer(name, expected_modified):
    from hero_integration.services import (
        migration,
        file_migration,
        direct_runtime,
        file_transfer,
        scoped_test_reads,
    )
    from hero_integration.services.test_environment import guard_run

    p = observation(name, True)
    rec = refs._source_observation(p)
    if expected_modified not in (review_token(p, rec), str(p.modified)):
        fail("The case changed. Reopen it before saving.")
    run = frappe.get_doc("HERO Integration Run", p.origin_run)
    guard_run(run)
    if (
        p.operation != "HeroCustomers"
        or run.action != "Customer Migration"
        or run.mode != "Execute"
        or run.state not in ("Completed", "Completed With Conflicts")
        or run.testing_enabled
        or run.execution_budget_run
        or run.source_instance != p.source_instance
    ):
        fail("Correct the source or target and repeat the original import.")
    proof = rec.get("__hero_origin_binding") or {}
    refs._verify_origin_binding(p, proof)
    if proof.get("target_doctype") != "Customer" or not proof.get("target_name"):
        fail("The customer identity cannot be verified.")
    frappe.has_permission("Customer", "write", doc=proof["target_name"], throw=True)
    migration.validate_run_mappings(run)
    config = file_migration.bound_configuration(run)
    plan = migration._mapping_plan_for_run(run, p.operation)
    reader = direct_runtime.approved_read(p.operation, p.source_instance)
    if (
        plan.mapping_version != p.mapping_version
        or reader.mapping_version != p.mapping_version
    ):
        fail("The mapping changed. Run the customer import again.")
    client, _, instance = file_transfer._client_and_downloader(config)
    if instance != p.source_instance:
        fail("The configured source changed.")
    fresh = list(scoped_test_reads.exact_rows(client, reader, [p.source_id]))
    original = {k: v for k, v in rec.items() if not k.startswith("_")}
    if len(fresh) != 1 or fresh[0] != original:
        fail(
            "The HERO customer changed. Import the new source record before reviewing it."
        )
    if (
        rec.get("is_deleted") is not False
        or rec.get("is_contact_person") is not False
        or str(rec["id"]) != p.source_id
    ):
        fail("The customer identity cannot be verified.")
    return p, run, rec, plan


def save_location(p, run, rec, plan, aid, street, city, reuse_exact):
    from hero_integration.services import address_correction as ac, migration
    from hero_integration.services.binding_registry import (
        FrappeBindingStorage,
        incarnation,
        verify_incarnation,
    )
    from hero_integration.services.execution_runtime import process_migration_record
    from hero_integration.services.primary_address import fill_primary_address

    link = migration._link("customer", p.source_id, p.source_instance, for_update=True)
    if (
        not link
        or link.target_doctype != "Customer"
        or link.sync_status != "Synchronized"
    ):
        fail("The customer identity cannot be verified.")
    customer = frappe.get_doc("Customer", link.target_name, for_update=True)
    customer.check_permission("write")
    binding = FrappeBindingStorage().lock_source_object(
        frappe.local.site,
        p.source_instance,
        plan.binding_contract.object_type,
        "Customer",
        p.source_id,
    )
    if (
        not binding
        or binding.lifecycle != "VALID"
        or binding.reconciliation
        or binding.target_name != customer.name
    ):
        fail("The customer identity cannot be verified.")
    verify_incarnation(binding, customer)
    missing = address_rows(rec, customer.name)
    if aid not in missing or migration._link(
        "customer_address", aid, p.source_instance, for_update=True
    ):
        fail("This address already has a decision or a link. Reopen the case.")
    role, raw = missing[aid]
    values = {}
    for field, provided in [("street", street), ("city", city)]:
        value = (provided or "").strip()
        source = (raw.get(field) or "").strip()
        if not value or len(value) > 140 or (source and source != value):
            fail(
                "Enter the missing street and city. Existing source values must be preserved."
            )
        values[field] = value
    country = frappe.db.get_value(
        "Country",
        {"code": ((raw.get("country") or {}).get("code") or "de").lower()},
        "name",
    )
    if not country:
        fail("Address country code must resolve to exactly one ERPNext Country.")
    fields = {
        "address_line1": values["street"],
        "city": values["city"],
        "pincode": raw.get("zipcode") or "",
        "country": country,
    }

    def materialize(existing):
        if existing:
            fail("This address already has a decision or a link. Reopen the case.")
        # Reuse only an exact match belonging exclusively to this customer.
        related = frappe.get_all(
            "Dynamic Link",
            filters={
                "parenttype": "Address",
                "link_doctype": "Customer",
                "link_name": customer.name,
            },
            pluck="parent",
        )
        matches = frappe.get_all(
            "Address",
            filters={
                **{k: v for k, v in fields.items() if k != "pincode"},
                "name": ["in", related or [""]],
                "disabled": 0,
            },
            fields=["name", "pincode"],
        )
        exact = [m.name for m in matches if (m.pincode or "") == fields["pincode"]]
        if len(exact) > 1:
            fail(
                "Several customer addresses match. Reconcile the addresses in ERPNext first."
            )
        genuine = {}
        if role == "primary":
            from hero_integration.services import query_mapping

            genuine = dict(
                query_mapping.mapped_values(
                    plan,
                    rec,
                    target_doctype="Address",
                    skip_fields={"country"}
                    if not (raw.get("country") or {}).get("code")
                    else (),
                )
            )
        if exact:
            if not reuse_exact:
                fail(
                    "An identical address already exists for this customer. Confirm reuse to link it."
                )
            address = frappe.get_doc("Address", exact[0], for_update=True)
            address.check_permission("write")
            migration._require_exclusive_customer_address(address, customer.name)
            if any((address.get(k) or "") != v for k, v in fields.items()):
                fail("The case changed. Reopen it before saving.")
            if frappe.db.exists(
                "HERO Integration Link",
                {
                    "target_doctype": "Address",
                    "target_name": address.name,
                    "source_instance": ["!=", p.source_instance],
                },
            ):
                fail("The address belongs to another HERO source.")
        else:
            address = frappe.get_doc(
                {
                    "doctype": "Address",
                    "address_type": "Billing" if role == "primary" else "Other",
                    "address_title": customer.name,
                    **genuine,
                    **fields,
                    "links": [{"link_doctype": "Customer", "link_name": customer.name}],
                }
            ).insert()
        from hero_integration.services import mapped_collection_relations as collection

        confirmed = {
            field: value
            for field, value in genuine.items()
            if address.get(field) == value
        }
        baseline = migration.encode_hashes(migration.field_hashes(confirmed))
        original = {k: raw.get(k) for k in ac.LOCATION}
        source_values = {
            "address_line1": (raw.get("street") or "").strip(),
            "city": (raw.get("city") or "").strip(),
            "pincode": (raw.get("zipcode") or "").strip(),
            "country_code": ((raw.get("country") or {}).get("code") or "de").lower(),
        }
        observed = dict(
            fields,
            address_type=address.address_type,
            links=[("Customer", customer.name)],
            is_primary_address=bool(address.is_primary_address),
        )
        frappe.get_doc(
            {
                "doctype": "HERO Integration Link",
                "source_system": "HERO",
                "source_instance": p.source_instance,
                "source_type": "customer_address",
                "source_id": aid,
                "target_doctype": "Address",
                "target_name": address.name,
                "target_incarnation": incarnation(address.creation),
                "mapping_version": p.mapping_version,
                "sync_status": "Synchronized",
                "source_payload_hash": ac.digest(original)
                if role == "primary"
                else collection._hash(source_values),
                "target_payload_hash": ac.digest(fields)
                if role == "primary"
                else collection._hash(observed),
                "source_field_hashes": baseline
                if role == "primary"
                else '{"preserved_address_alias":true}',
                "target_field_hashes": baseline if role == "primary" else None,
                "last_synced_on": frappe.utils.now_datetime(),
            }
        ).insert()
        proof = {
            "version": 2,
            "method": "operator_location",
            "operator": frappe.session.user,
            "site": frappe.local.site,
            "run": run.name,
            "mapping": p.mapping_version,
            "source_revision": int(run.source_binding_revision or 0),
            "owner": {
                "binding": binding.identity_key,
                "incarnation": binding.target_incarnation,
            },
            "native": {"creation": incarnation(address.creation), "fields": fields},
            "original": {k: raw.get(k) for k in ac.LOCATION},
        }
        frappe.get_doc(
            {
                "doctype": ac.DOCTYPE,
                "source_instance": p.source_instance,
                "customer_id": p.source_id,
                "address_id": aid,
                "role": role,
                "active": 1,
                "customer": customer.name,
                "address": address.name,
                "source_fingerprint": ac.source_fingerprint(rec),
                "evidence_json": json.dumps(proof, ensure_ascii=False),
            }
        ).insert(ignore_permissions=True)
        return "created"

    if role == "primary":
        process_migration_record(
            plan,
            rec,
            p.source_instance,
            "customer_address",
            materialize,
            evidence=run.name,
            successful=lambda value: value == "created",
        )
        fill_primary_address(customer.name, p.source_instance)
    else:
        materialize(None)
    if not ac.verified_address(
        rec, aid, role, expected_customer=customer.name, locked=True
    ):
        fail("The address decision could not be verified.")


@frappe.whitelist(methods=["POST"])
def reconcile(
    name,
    expected_modified,
    address_id="",
    street="",
    city="",
    distinct_child="",
    reuse_exact=0,
):
    try:
        return _apply_review(
            name,
            expected_modified,
            address_id,
            street,
            city,
            distinct_child,
            reuse_exact,
        )
    except (ValueError, PermissionError) as error:
        frappe.throw(frappe._(str(error)))


def _apply_review(
    name, expected_modified, address_id, street, city, distinct_child, reuse_exact
):
    p, run, rec, plan = checked_customer(name, expected_modified)
    actor = frappe.session.user
    if distinct_child:
        if address_id:
            fail("The case changed. Reopen it before saving.")
        confirm_distinct_child(p, rec, plan, str(distinct_child))
    if address_id:
        save_location(
            p,
            run,
            rec,
            plan,
            str(address_id),
            street,
            city,
            frappe.utils.cint(reuse_exact) == 1,
        )
    if all(d.source_type == "customer_address" for d in p.dependencies):
        # The customer writer creates these dependencies itself on its first pass.
        p.realization_started = 0
    try:
        refs._resolve_admitted(p, run, p.name, None)
    finally:
        if frappe.session.user != actor:
            frappe.set_user(actor)
    p.reload()
    return {"status": p.status, "detail": p.detail}


@frappe.whitelist(methods=["GET"])
def case_for_record(name):
    frappe.only_for("System Manager")
    record = frappe.get_doc("HERO Integration Record", name)
    record.check_permission("read")
    instance = _configured_source_instance()
    run = frappe.get_doc("HERO Integration Run", record.run)
    if run.source_instance != instance:
        fail("This review is no longer open for this source.")
    from hero_integration.services.sync_page import AREAS

    op = next(
        (
            area.operation
            for area in AREAS
            if area.source_type == record.source_type and area.run_action == run.action
        ),
        None,
    )
    if op:
        return frappe.db.get_value(
            refs.MAIN,
            {
                "source_instance": instance,
                "origin_run": run.name,
                "source_id": record.source_id,
                "operation": op,
                "status": ["in", ["Conflict", "Waiting"]],
            },
            "name",
        )
