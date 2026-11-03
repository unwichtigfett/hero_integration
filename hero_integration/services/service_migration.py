"""Services -> non-stock Items, selling prices and source calculation rows.

Native pricing and conflict helpers are shared with the article importer.
Composition is a fixed read detail contract, like file attachment details;
scalar assignments use the configurable HeroServices Query-v2 mapping.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
import tempfile
import time

import frappe

from hero_integration.services import direct_runtime as runtime, product_migration as products
from hero_integration.services.direct_mapping import lookup, MISSING
from hero_integration.services.file_migration import SourceBindingError
from hero_integration.services.service_schema import ITEM_FIELDS

OPERATION = "HeroServices"
SOURCE_TYPE = "supply_service"
VERSION = "service-migration-v1"
CALCULATION = "custom_hero_service_calculation"
DETAIL_QUERY = '''query ServiceComposition($ids:[Int], $first:Int!) {
 supply_services(service_ids:$ids, first:$first) {
  id company_id modified created is_deleted
  positions { __typename
   ... on Documents_SupplyProduct { id name unit_type quantity net_price_per_unit base_price time_minutes
    company_id nr ean manufacturer manufacturer_nr internal_identifier supply_service_id supplier_id }
   ... on WageGroup { id name unit_type quantity time_minutes net_price_per_unit wage_cost_price
    wage_per_hour profit_markup activity tracking_time_id created modified company_id }
  }
  file_upload { id uuid filename type size is_deleted modified url_download }
 }
}'''


class ServiceBlocked(SourceBindingError):
    pass


def number(value, *, nonnegative=True):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or (nonnegative and value < 0):
        raise ServiceBlocked("Service calculation contains an invalid number.")
    return value


def service_values(row, read):
    values = runtime.record_values(read.plan, row, "Item")
    barcode = values.pop("barcodes", None)
    code = values.pop("item_code", None) or str(row["id"])
    if not isinstance(code, str) or not code.strip() or len(code) > 140:
        raise ServiceBlocked("Service article number is empty or too long.")
    values["item_name"] = values.get("item_name") or code
    if len(values["item_name"]) > 140:
        raise ServiceBlocked("Service article name exceeds the ERPNext limit.")
    unit = values.get("stock_uom")
    if not unit:
        raise ServiceBlocked("A service unit is required.")
    values["stock_uom"] = "Hour" if unit == "Std" else unit
    values.update(is_stock_item=0, item_group="Services")
    if "disabled" in values:
        values["disabled"] = int(bool(values["disabled"]))
    for spec in read.plan.definitions:
        if spec.target_doctype == "Item Price":
            raw = lookup(row, spec.source_path)
            if raw is not MISSING:
                number(raw)
    selling = runtime.price_values(read.plan, row).get("Standard Selling")
    return code.strip(), values, barcode, selling


def preflight(instance):
    from graphql import build_client_schema, parse, validate
    from hero_integration.integrations.erpnext.company import project_company
    from hero_integration.services.graphql_graph import normalize_schema

    company = project_company()
    if not company.is_valid:
        raise ServiceBlocked(company.reason)
    if not frappe.db.exists("DocType", "HERO Service Calculation") or any(
            not frappe.get_meta("Item").has_field(f["fieldname"]) for f in ITEM_FIELDS):
        raise ServiceBlocked("Install the service mapping schema before starting migration.")
    price_list = frappe.db.get_value("Price List", "Standard Selling", ["currency", "enabled", "selling"], as_dict=True)
    if not price_list or not price_list.currency or not price_list.enabled or not price_list.selling:
        raise ServiceBlocked("An enabled Standard Selling price list with currency is required.")
    if price_list.currency != frappe.db.get_value("Company", company.name, "default_currency"):
        raise ServiceBlocked("Service selling price currency must match the ERPNext company currency.")
    names = frappe.get_all("HERO GraphQL Schema Snapshot", filters={"source_instance": instance,
                           "analysis_status": "Validated"}, pluck="name", order_by="modified desc", limit_page_length=1)
    if not names:
        raise ServiceBlocked("Service migration requires a validated source schema.")
    snapshot = frappe.get_doc("HERO GraphQL Schema Snapshot", names[0])
    schema = build_client_schema({"__schema": normalize_schema(json.loads(snapshot.snapshot_json))})
    if validate(schema, parse(DETAIL_QUERY)):
        raise ServiceBlocked("The service composition query differs from the validated source schema.")
    return company.name, snapshot.schema_fingerprint


def scoped_link(instance, kind, identity, target):
    names = frappe.get_all("HERO Integration Link", filters={"source_system": "HERO", "source_instance": instance,
                           "source_type": kind, "source_id": str(identity)}, pluck="name", limit_page_length=2)
    if len(names) > 1:
        raise ServiceBlocked("Service source identity has several bindings.")
    if not names:
        return None
    link = frappe.get_doc("HERO Integration Link", names[0], for_update=True)
    if link.target_doctype != target or not frappe.db.exists(target, link.target_name):
        raise ServiceBlocked("The service binding target is missing or has another type.")
    return link


def write_link(link, instance, kind, identity, doc, source, baseline, *, extra=None):
    values = {"target_doctype": doc.doctype, "target_name": doc.name, "sync_status": "Synchronized",
              "source_payload_hash": source, "target_payload_hash": products.digest(baseline),
              "target_field_hashes": json.dumps({"values": baseline, **(extra or {})}, sort_keys=True),
              "mapping_version": VERSION, "conflict_detail": "", "last_synced_on": frappe.utils.now_datetime()}
    if link:
        frappe.db.set_value("HERO Integration Link", link.name, values)
    else:
        frappe.get_doc({"doctype": "HERO Integration Link", "source_system": "HERO", "source_instance": instance,
                        "source_type": kind, "source_id": str(identity), **values}).insert(ignore_permissions=True)


def normalized(doc, fields):
    def value(raw):
        if isinstance(raw, list):
            return [{k: v for k, v in (r.as_dict() if hasattr(r, "as_dict") else dict(r)).items()
                     if k not in {"name", "owner", "creation", "modified", "modified_by", "parent", "parenttype", "parentfield", "idx", "docstatus", "doctype"}}
                    for r in raw]
        return raw
    return {key: value(doc.get(key)) for key in fields}


def baseline_check(link, doc):
    if not link:
        return {}
    baseline = json.loads(link.target_field_hashes or "{}")
    fields = baseline.get("values")
    if not fields or products.digest(normalized(doc, fields)) != link.target_payload_hash:
        raise ServiceBlocked("The service article or activity type was edited locally; review the conflict.")
    return baseline


def tax_template(company, rate):
    """Use an unambiguous existing company template; never invent tax accounts."""
    if rate is None:
        return None
    number(rate)
    matches = []
    names = frappe.get_all("Item Tax Template", filters={"company": company, "disabled": 0}, pluck="name", limit_page_length=0)
    for name in names:
        rows = frappe.get_doc("Item Tax Template", name).taxes or []
        rates = {number(row.tax_rate) for row in rows}
        if rows and (rates == {0} if rate == 0 else rates - {0} == {rate}):
            matches.append(name)
    if len(matches) != 1:
        raise ServiceBlocked("Service tax rate requires one unambiguous Item Tax Template in the ERPNext company.")
    return matches[0]


def activity(component, instance):
    identity = component.get("id")
    if not isinstance(identity, str) or not identity:
        raise ServiceBlocked("A labor group identity is required.")
    link = scoped_link(instance, "WageGroup", identity, "Activity Type")
    if link and link.sync_status in {"Rejected", "Pending Review", "Source Deleted"}:
        raise ServiceBlocked("The labor group binding requires review.")
    name = component.get("name") or identity
    if not isinstance(name, str) or len(name) > 140:
        raise ServiceBlocked("Labor group name is invalid or too long.")
    rate = number(component.get("wage_per_hour"))
    values = {"activity_type": name, **({"billing_rate": rate} if rate is not None else {})}
    if link:
        doc = frappe.get_doc("Activity Type", link.target_name, for_update=True)
        baseline_check(link, doc)
        if rate is not None and doc.billing_rate != rate:
            raise ServiceBlocked("Labor group billing rates differ between service calculations; review the mapping.")
    else:
        if frappe.db.exists("Activity Type", name):
            raise ServiceBlocked("An existing activity type requires an explicit labor group binding.")
        doc = frappe.get_doc({"doctype": "Activity Type", **values})
    # Cost unit is unproven; costing_rate is intentionally not derived from wage_cost_price.
    if not link or any(doc.get(key) != value for key, value in values.items()):
        doc.update(values)
        doc.save(ignore_permissions=True) if link else doc.insert(ignore_permissions=True)
    doc.reload()
    write_link(link, instance, "WageGroup", identity, doc, products.digest(values), normalized(doc, values))
    return doc.name


def composition(positions, instance):
    if not isinstance(positions, list) or len(positions) > 1000:
        raise ServiceBlocked("Service composition is missing or exceeds its bound.")
    result = []
    for row in positions:
        kind = row.get("__typename") if isinstance(row, dict) else None
        if kind not in {"Documents_SupplyProduct", "WageGroup"} or not row.get("id"):
            raise ServiceBlocked("Unknown service component type or identity.")
        values = {"component_type": "Material" if kind == "Documents_SupplyProduct" else "Labor",
                  "source_id": str(row["id"]), "description": row.get("name"), "uom": row.get("unit_type"),
                  "quantity": number(row.get("quantity")), "unit_price": number(row.get("net_price_per_unit")),
                  "time_minutes": number(row.get("time_minutes")), "resolution": "Linked"}
        if kind == "Documents_SupplyProduct":
            link = scoped_link(instance, "supply_product", row["id"], "Item")
            if link and link.sync_status == "Synchronized":
                values["item_code"] = link.target_name
            else:
                values["resolution"] = "Unresolved"
        else:
            values.update(activity_type=activity(row, instance), cost_price=number(row.get("wage_cost_price")),
                          hourly_rate=number(row.get("wage_per_hour")), markup=number(row.get("profit_markup")))
        consumed = {"__typename", "id", "name", "unit_type", "quantity", "net_price_per_unit", "time_minutes"}
        if kind == "WageGroup":
            consumed |= {"wage_cost_price", "wage_per_hour", "profit_markup"}
        values["metadata"] = json.dumps({k: v for k, v in row.items() if k not in consumed}, sort_keys=True)
        result.append(values)
    return result


def attach_file(detail, item, instance, downloader, prior):
    """Use guarded streaming and Frappe's transactional private-file lifecycle."""
    if not detail or detail.get("is_deleted"):
        return prior  # deletion never removes an existing local attachment
    fingerprint = products.digest({k:v for k,v in detail.items() if k != "url_download"})
    if prior and prior.get("source") == fingerprint:
        file = frappe.get_doc("File", prior["name"], for_update=True)
        if (not file.is_private or file.attached_to_doctype != "Item" or file.attached_to_name != item.name
                or file.content_hash != prior.get("hash")):
            raise ServiceBlocked("The service attachment was changed locally.")
        return prior
    if prior:
        # Preserve existing versions, but validate ownership before adding a newer version.
        file = frappe.get_doc("File", prior["name"], for_update=True)
        if not file.is_private or file.attached_to_doctype != "Item" or file.attached_to_name != item.name or file.content_hash != prior.get("hash"):
            raise ServiceBlocked("The service attachment was changed locally.")
    from frappe.utils.file_manager import save_file
    from hero_integration.services.file_transfer import safe_name
    with tempfile.TemporaryDirectory(prefix="hero-service-") as directory:
        path = str(Path(directory) / "attachment")
        downloader.fetch(detail.get("url_download"), path, detail.get("size"))
        file = save_file(safe_name(detail.get("filename")), Path(path).read_bytes(), "Item", item.name, is_private=1)
    return {"name": file.name, "hash": file.content_hash, "source": fingerprint}


def upsert(row, detail, read, instance, company, downloader):
    code, fields, barcode, selling = service_values(row, read)
    identity = runtime.identity(read, row)
    link = scoped_link(instance, SOURCE_TYPE, identity, "Item")
    if link and link.sync_status == "Rejected":
        return "skipped_rejected"
    if link and link.sync_status in {"Pending Review", "Source Deleted"}:
        raise ServiceBlocked("The service binding requires review.")
    item = frappe.get_doc("Item", link.target_name, for_update=True) if link else None
    if item is None and frappe.db.exists("Item", code):
        raise ServiceBlocked("Service article number is already used; bind the existing article explicitly.")
    baseline = baseline_check(link, item) if item else {}
    if item and item.name != code:
        raise ServiceBlocked("A changed service article number requires an explicit rename.")
    # Every check uses the saved baseline even when the upstream source hash is unchanged.
    template = tax_template(company, fields.get("custom_hero_service_vat_percent"))
    if template:
        fields["taxes"] = [{"item_tax_template": template}]
    if item and selling is not None and not baseline.get("price") and frappe.db.exists("Item Price", {"item_code": code, "price_list": "Standard Selling"}):
        raise ServiceBlocked("An existing selling price requires explicit review before adoption.")
    price = products.plan_price(code, "Standard Selling", selling, fields["stock_uom"], baseline.get("price"))
    fields[CALCULATION] = composition(detail.get("positions"), instance)
    for doctype, name, values in [("Item Group", "Services", {"item_group_name": "Services", "parent_item_group": "All Item Groups"}),
                                   ("UOM", fields["stock_uom"], {"uom_name": fields["stock_uom"]}),
                                   ("Brand", fields.get("brand"), {"brand": fields.get("brand")})]:
        if name and not frappe.db.exists(doctype, name):
            frappe.get_doc({"doctype": doctype, **values}).insert(ignore_permissions=True)
    stable_detail = {**detail, "file_upload": {k:v for k,v in (detail.get("file_upload") or {}).items() if k != "url_download"}}
    source = products.digest([row, stable_detail, fields, barcode, selling, read.mapping_version, company, VERSION])
    prior_attachment = baseline.get("attachment")
    if prior_attachment:
        file = frappe.get_doc("File", prior_attachment["name"], for_update=True)
        if not file.is_private or file.attached_to_doctype != "Item" or file.attached_to_name != item.name or file.content_hash != prior_attachment.get("hash"):
            raise ServiceBlocked("The service attachment was changed locally.")
    if link and link.source_payload_hash == source:
        return "unchanged"
    is_new = item is None
    item = item or frappe.get_doc({"doctype": "Item", "item_code": code})
    item.update(fields)
    if barcode:
        # Source barcodes are additional rows; the complete table remains protected by the baseline.
        products._set_barcode(item, barcode)
    item.insert(ignore_permissions=True) if is_new else item.save(ignore_permissions=True)
    price_state = products.apply_price(item.name, "Standard Selling", price) if price is not None else baseline.get("price")
    attachment = attach_file(detail.get("file_upload"), item, instance, downloader, baseline.get("attachment"))
    item.reload()  # baseline reflects stored native numeric/default and child-row normalization
    owned = set(fields) | {"barcodes"} | set(baseline.get("values", {}))
    values = normalized(item, owned)
    write_link(link, instance, SOURCE_TYPE, identity, item, source, values,
               extra={"price": price_state, "attachment": attachment})
    return "added" if is_new else "updated"


def sync_services(client, *, source_instance, run_name, downloader, should_stop=None, progress=None, verify_source=None,
                  time_budget=1200, read=None):
    frappe.only_for("System Manager")
    company, schema = preflight(source_instance)
    read = read or runtime.resolve_read(OPERATION, source_instance)
    key = "hero_service_migration:" + run_name
    raw = frappe.db.get_default(key)
    state = json.loads(raw) if raw else {"offset": 0, "counts": {}}
    stamp = products.digest([source_instance, company, schema, read.mapping_version])
    if state.get("stamp") and state["stamp"] != stamp:
        raise ServiceBlocked("Service source, schema, company or mapping changed; start a new run.")
    state.update(stamp=stamp, done=False, stopped=False)
    deadline = time.monotonic() + time_budget
    while time.monotonic() < deadline:
        if should_stop and should_stop():
            state["stopped"] = True
            break
        rows = runtime.fetch_page(client, read, state["offset"])
        if not rows:
            state["done"] = True
            break
        ids = [row.get("id") for row in rows]
        if any(type(identity) is not int or identity <= 0 for identity in ids) or len(set(ids)) != len(ids):
            raise ServiceBlocked("HERO returned invalid or duplicate service identities.")
        if ids != sorted(ids) or (state.get("last_id") is not None and ids[0] <= state["last_id"]):
            raise ServiceBlocked("Service pagination changed or repeated; start a new run.")
        detailed = client.query(DETAIL_QUERY, {"ids": ids, "first": len(ids)}).get("supply_services")
        if not isinstance(detailed, list) or len(detailed) != len(ids) or {d.get("id") for d in detailed} != set(ids):
            raise ServiceBlocked("HERO returned an incomplete service composition list.")
        details = {d["id"]: d for d in detailed}
        blocked = False
        for row in rows:
            if time.monotonic() >= deadline:
                break
            if should_stop and should_stop():
                state["stopped"] = True
                break
            if verify_source:
                verify_source()
            detail = details[row["id"]]
            if detail.get("modified") != row.get("modified") or detail.get("company_id") != row.get("company_id"):
                raise ServiceBlocked("Service changed between its record and composition reads; retry the slice.")
            try:
                result = upsert(row, detail, read, source_instance, company, downloader)
            except Exception as error:
                frappe.db.rollback()
                result = "conflict" if isinstance(error, (ServiceBlocked, products.PriceConflict)) else "failed"
                state["last_error"] = str(error) if result == "conflict" else "Service validation failed; check the error log."
                state["blocked_source_id"] = str(row["id"])
                if result == "failed":
                    frappe.log_error(title="Service migration failed")
                previous = state.setdefault("outcomes", {}).get(str(row["id"]))
                if previous:
                    state["counts"][previous] -= 1
                state["outcomes"][str(row["id"])] = result
                state["counts"][result] = state["counts"].get(result, 0) + 1
                blocked = True
                break  # retry exactly this identity on resume
            state["offset"] += 1
            state["last_id"] = row["id"]
            previous = state.setdefault("outcomes", {}).get(str(row["id"]))
            if previous:
                state["counts"][previous] -= 1
            state["outcomes"][str(row["id"])] = result
            state["counts"][result] = state["counts"].get(result, 0) + 1
            state.pop("last_error", None)
            state.pop("blocked_source_id", None)
            frappe.db.set_default(key, json.dumps(state))
            frappe.db.commit()
            if progress:
                progress(state["counts"])
        if blocked or state["stopped"]:
            break
    frappe.db.set_default(key, json.dumps(state))
    frappe.db.commit()
    return state
