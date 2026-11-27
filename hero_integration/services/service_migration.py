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
from hero_integration.services import migration_archive
from hero_integration.services.binding_registry import BindingConflict
from hero_integration.services.file_migration import DirectContractChanged

from hero_integration.services import direct_runtime as runtime, product_migration as products
from hero_integration.services.direct_mapping import lookup, MISSING
from hero_integration.services.file_migration import SourceBindingError
from hero_integration.services.service_schema import ITEM_FIELDS

OPERATION = "HeroServices"
SOURCE_TYPE = "supply_service"
VERSION = "service-migration-v2"
CALCULATION = "custom_hero_service_calculation"
DETAIL_PATHS = (
    'id', 'company_id', 'modified', 'created', 'is_deleted', 'positions.__typename',
    *(f'positions.@Documents_SupplyProduct.{name}' for name in (
        'id', 'name', 'unit_type', 'quantity', 'net_price_per_unit', 'base_price', 'time_minutes',
        'company_id', 'nr', 'ean', 'manufacturer', 'manufacturer_nr', 'internal_identifier', 'supply_service_id', 'supplier_id')),
    *(f'positions.@WageGroup.{name}' for name in (
        'id', 'name', 'unit_type', 'quantity', 'time_minutes', 'net_price_per_unit', 'wage_cost_price',
        'wage_per_hour', 'profit_markup', 'activity', 'tracking_time_id', 'created', 'modified', 'company_id')),
    *(f'file_upload.{name}' for name in ('id', 'uuid', 'filename', 'type', 'size', 'is_deleted', 'modified', 'url_download')),
)


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


def _composition_request(snapshot, ids):
    from hero_integration.services.mapping_contract_v1 import QueryAdapter, QueryDraft
    return QueryAdapter().materialize(snapshot, QueryDraft('ServiceComposition', 'supply_services', 'supply_services',
        DETAIL_PATHS, {'service_ids': ids, 'first': max(len(ids), 1)}))


def _attachment_request(snapshot, ids):
    from hero_integration.services.mapping_contract_v1 import QueryAdapter, QueryDraft
    paths=tuple(path for path in DETAIL_PATHS if path in {'id','company_id'} or path.startswith('file_upload.'))
    return QueryAdapter().materialize(snapshot, QueryDraft('ServiceAttachment','supply_services','supply_services',
        paths,{'service_ids':ids,'first':max(len(ids),1)}))


def preflight(instance, read=None):
    from hero_integration.integrations.erpnext.company import project_company

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
    read = read or runtime.resolve_read(OPERATION, instance)
    try:
        snapshot = runtime.validated_snapshot(read, instance, frappe_module=frappe)
        _composition_request(snapshot, [])
    except ValueError as error:
        raise ServiceBlocked('The service composition cannot be materialized from the approved schema.') from error
    return company.name, snapshot.fingerprint


def scoped_link(instance, kind, identity, target):
    # A locking read sees bindings committed after this transaction's snapshot.
    names = frappe.db.sql("""SELECT name FROM `tabHERO Integration Link`
        WHERE source_system='HERO' AND source_instance=%s AND source_type=%s AND source_id=%s
        LIMIT 2 FOR UPDATE""", (instance, kind, str(identity)), pluck=True)
    if len(names) > 1:
        raise ServiceBlocked("Service source identity has several bindings.")
    if not names:
        return None
    if not frappe.db.get_value('HERO Integration Link', names[0], 'name'):
        # Native Link validation uses consistent reads too. Refresh the whole
        # record before attaching a target absent from that older snapshot.
        raise frappe.QueryDeadlockError('Service binding was committed after the snapshot. Retry the record.')
    link = frappe.get_doc("HERO Integration Link", names[0], for_update=True)
    if link.target_doctype != target or not frappe.db.get_value(target, link.target_name, 'name', for_update=True):
        raise ServiceBlocked("The service binding target is missing or has another type.")
    return link


def write_link(link, instance, kind, identity, doc, source, baseline, *, extra=None):
    values = {"target_doctype": doc.doctype, "target_name": doc.name, "sync_status": "Synchronized",
              "source_payload_hash": source, "target_payload_hash": products.digest(baseline),
              "target_field_hashes": json.dumps({"values": baseline, **(extra or {})}, sort_keys=True),
              "mapping_version": VERSION, "conflict_detail": ""}
    if link and all((link.get(field) or "") == value for field, value in values.items()):
        return False
    values["last_synced_on"] = frappe.utils.now_datetime()
    if link:
        frappe.db.set_value("HERO Integration Link", link.name, values)
    else:
        frappe.get_doc({"doctype": "HERO Integration Link", "source_system": "HERO", "source_instance": instance,
                        "source_type": kind, "source_id": str(identity), **values}).insert(ignore_permissions=True)
    return True


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
    from hero_integration.services.execution_runtime import adapter_baseline
    if not fields or products.digest(adapter_baseline(doc, normalized(doc, fields), fields, verify=True)) != link.target_payload_hash:
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
        if frappe.db.get_value("Activity Type", name, 'name', for_update=True):
            if scoped_link(instance, "WageGroup", identity, "Activity Type"):
                raise frappe.QueryDeadlockError('Labor group binding arrived during creation. Retry the record.')
            raise ServiceBlocked("An existing activity type requires an explicit labor group binding.")
        doc = frappe.get_doc({"doctype": "Activity Type", **values})
    # Cost unit is unproven; costing_rate is intentionally not derived from wage_cost_price.
    try:
        if not link or any(doc.get(key) != value for key, value in values.items()):
            doc.update(values)
            doc.save(ignore_permissions=True) if link else doc.insert(ignore_permissions=True)
        doc.reload()
        write_link(link, instance, "WageGroup", identity, doc, products.digest(values), normalized(doc, values))
    except (frappe.DuplicateEntryError, frappe.UniqueValidationError) as error:
        if link:
            raise
        # Reuse the record worker's rollback/retry, including READ COMMITTED
        # where an absent source key has no gap lock. Recheck identity on retry.
        raise frappe.QueryDeadlockError('Labor group was created concurrently. Retry the record.') from error
    return doc.name


def historical_materials(observation):
    """Validate the deletion evidence against the immutable service composition."""
    historical = observation.get('historical_materials', {})
    for key, proof in historical.items():
        rows = [row for row in observation['detail']['positions']
                if row['__typename'] == 'Documents_SupplyProduct' and str(row['id']) == key]
        if (not rows or proof.get('product_id') != key or proof.get('is_deleted') is not True
                or not proof.get('reader_version') or proof.get('service_hash') != observation['source_hash']
                or proof.get('positions_hash') != products.digest(rows)
                or any(row.get('nr') != proof.get('nr') for row in rows)):
            raise ServiceBlocked("Historical product proof differs from the saved service composition.")
    return historical


def active_service_dependencies(pending, dependencies):
    historical = historical_materials(json.loads(pending.record_json))
    return [(kind, key) for kind, key in dependencies if kind != 'supply_product' or str(key) not in historical]


def composition(positions, instance, *, realize=True, historical=None):
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
            proof = (historical or {}).get(str(row['id']))
            link = scoped_link(instance, "supply_product", row["id"], "Item") if realize and not proof else None
            if proof:
                values["resolution"] = "Historical"
            elif link and link.sync_status == "Synchronized":
                values["item_code"] = link.target_name
            else:
                values["resolution"] = "Unresolved"
        else:
            values.update(cost_price=number(row.get("wage_cost_price")),
                          hourly_rate=number(row.get("wage_per_hour")), markup=number(row.get("profit_markup")))
            if realize:
                values['activity_type'] = activity(row, instance)
            else:
                values['resolution'] = 'Unresolved'
        consumed = {"__typename", "id", "name", "unit_type", "quantity", "net_price_per_unit", "time_minutes"}
        if kind == "WageGroup":
            consumed |= {"wage_cost_price", "wage_per_hour", "profit_markup"}
        metadata = {k: v for k, v in row.items() if k not in consumed}
        if values['resolution'] == 'Historical':
            metadata['historical_material'] = proof
            metadata['source_values'] = {k: v for k, v in row.items() if k in consumed}
        values["metadata"] = json.dumps(metadata, sort_keys=True)
        result.append(values)
    return result


def attach_file(detail, item, instance, downloader, prior):
    """Use guarded streaming and Frappe's transactional private-file lifecycle."""
    if not detail or detail.get("is_deleted") and not migration_archive.includes_archived():
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


def _separate_item_code(code, identity, instance):
    return products._collision_code(code, instance, f"{SOURCE_TYPE}:{identity}")


def upsert(row, detail, read, instance, company, downloader, *, separate_product_code=False):
    code, fields, barcode, selling = service_values(row, read)
    identity = runtime.identity(read, row)
    link = scoped_link(instance, SOURCE_TYPE, identity, "Item")
    if link and link.sync_status == "Rejected":
        return "skipped_rejected"
    if link and link.sync_status in {"Pending Review", "Source Deleted"}:
        raise ServiceBlocked("The service binding requires review.")
    item = frappe.get_doc("Item", link.target_name, for_update=True) if link else None
    baseline = baseline_check(link, item) if item else {}
    original_field = "custom_hero_original_article_number"
    separate_number = baseline.get("separate_number", False)
    if item is not None and separate_number:
        if item.get(original_field) != code or item.name != _separate_item_code(code, identity, instance):
            raise ServiceBlocked("A changed service article number requires an explicit rename.")
        fields[original_field], code = code, item.name
    elif item is None and frappe.db.exists("Item", code):
        if not separate_product_code:
            raise ServiceBlocked("Service article number is already used; bind the existing article explicitly.")
        owners = frappe.get_all("HERO Integration Link", filters={
            "source_system": ["!=", products.RETIRED_LINK_SYSTEM], "target_doctype": "Item", "target_name": code,
        }, fields=["source_system", "source_instance", "source_type", "sync_status"], limit_page_length=2)
        if (len(owners) != 1 or owners[0].source_system != "HERO" or owners[0].source_instance != instance
                or owners[0].source_type != products.SOURCE_TYPE or owners[0].sync_status != "Synchronized"):
            raise ServiceBlocked("A separate service number requires a verified product owner of the original number.")
        if not frappe.get_meta("Item").has_field(original_field):
            raise ServiceBlocked("The original article number field is missing.")
        separate_number = True
        fields[original_field], code = code, _separate_item_code(code, identity, instance)
        if frappe.db.exists("Item", code):
            raise ServiceBlocked("The separate service article number is already used.")
    if item:
        from hero_integration.services.change_detection import new_mapped_field_conflicts
        if new_mapped_field_conflicts(item, fields, baseline.get('values', {})):
            raise ServiceBlocked("The service article or activity type was edited locally; review the conflict.")
    if item and item.name != code:
        raise ServiceBlocked("A changed service article number requires an explicit rename.")
    # Every check uses the saved baseline even when the upstream source hash is unchanged.
    if (item and fields.get("custom_hero_service_vat_percent") is not None
            and "taxes" not in baseline.get("values", {}) and item.get("taxes")):
        # Selecting a VAT rate also takes ownership of the derived native tax
        # table. A populated local table needs its own prior import proof.
        raise ServiceBlocked("The service article or activity type was edited locally; review the conflict.")
    template = tax_template(company, fields.get("custom_hero_service_vat_percent"))
    if template:
        fields["taxes"] = [{"item_tax_template": template}]
    if item and selling is not None and not baseline.get("price") and frappe.db.exists("Item Price", {"item_code": code, "price_list": "Standard Selling"}):
        raise ServiceBlocked("An existing selling price requires explicit review before adoption.")
    price = products.plan_price(code, "Standard Selling", selling, fields["stock_uom"], baseline.get("price"))
    observed_calculation = composition(detail.get("positions"), instance, realize=False)
    if item is None:
        fields[CALCULATION] = observed_calculation
    for doctype, name, values in [("Item Group", "Services", {"item_group_name": "Services", "parent_item_group": "All Item Groups"}),
                                   ("UOM", fields["stock_uom"], {"uom_name": fields["stock_uom"]}),
                                   ("Brand", fields.get("brand"), {"brand": fields.get("brand")})]:
        if name and not frappe.db.exists(doctype, name):
            frappe.get_doc({"doctype": doctype, **values}).insert(ignore_permissions=True)
    stable_detail = _stable_detail(detail)
    source = products.digest([row, stable_detail, {**fields, CALCULATION:observed_calculation},
                              barcode, selling, read.mapping_version, company, VERSION])
    prior_attachment = baseline.get("attachment")
    if prior_attachment:
        file = frappe.get_doc("File", prior_attachment["name"], for_update=True)
        if not file.is_private or file.attached_to_doctype != "Item" or file.attached_to_name != item.name or file.content_hash != prior_attachment.get("hash"):
            raise ServiceBlocked("The service attachment was changed locally.")
    from hero_integration.services.execution_runtime import adapter_baseline, field_decisions, preserve_local_fields
    if item:
        preserve_local_fields(item, fields)
    if link and link.source_payload_hash == source and "apply" not in field_decisions("Item", item.name).values():
        return "unchanged"
    is_new = item is None
    item = item or frappe.get_doc({"doctype": "Item", "item_code": code})
    item.update(fields)
    if barcode:
        # Source barcodes are additional rows; the complete table remains protected by the baseline.
        products._set_barcode(item, barcode)
    item.insert(ignore_permissions=True) if is_new else item.save(ignore_permissions=True)
    price_state = products.apply_price(item.name, "Standard Selling", price) if price is not None else baseline.get("price")
    attachment = baseline.get("attachment")
    item.reload()  # baseline reflects stored native numeric/default and child-row normalization
    owned = set(fields) | {"barcodes"} | set(baseline.get("values", {}))
    values = adapter_baseline(item, normalized(item, owned), baseline.get("values", {}))
    write_link(link, instance, SOURCE_TYPE, identity, item, source, values,
               extra={"price": price_state, "attachment": attachment, "separate_number": separate_number,
                      "historical_materials": baseline.get("historical_materials", {})})
    return "added" if is_new else "updated"


def _stable_detail(detail):
    return {**detail, 'file_upload':{key:value for key,value in (detail.get('file_upload') or {}).items()
                                   if key != 'url_download'}}


def confirm_historical_material(product, positions, service_hash, reader_version):
    """Keep the original line only when the exact product reader proves its deletion."""
    identity = str(product['product_id'])
    base = product.get('base_data') or {}
    if (product.get('is_deleted') is not True or not positions or not reader_version
            or any(str(row.get('id')) != identity or row.get('__typename') != 'Documents_SupplyProduct'
                   or row.get('nr') != product.get('nr') or row.get('name') != base.get('name')
                   or row.get('unit_type') != base.get('unit_type')
                   or row.get('ean') and row['ean'] != base.get('ean') for row in positions)):
        raise ServiceBlocked("The deleted product does not match the observed service position.")
    return {'product_id': identity, 'nr': product['nr'], 'is_deleted': True, 'reader_version': reader_version,
            'service_hash': service_hash, 'positions_hash': products.digest(positions)}


def _historical_groups(link, detail):
    previous = json.loads(link.target_field_hashes or '{}').get('historical_materials', {}) if link else {}
    grouped = {}
    for row in detail['positions']:
        key = str(row['id'])
        if row['__typename'] == 'Documents_SupplyProduct' and key in previous:
            grouped.setdefault(key, []).append(row)
    return grouped


def historical_inventory_evidence(client, instance, details):
    """Reread previously historical products without holding any native target locks."""
    from hero_integration.services.scoped_test_reads import exact_rows
    links = runtime.page_links(frappe, instance, SOURCE_TYPE, [str(key) for key in details], ["target_field_hashes"])
    keys = {key: sorted(_historical_groups(links.get(str(key)), detail)) for key, detail in details.items()}
    wanted = sorted({identity for selected in keys.values() for identity in selected})
    if not wanted:
        return {}
    read = runtime.approved_read("HeroProducts", instance)
    if not read:
        return {key: {"requested": values, "reader": None, "rows": []} for key, values in keys.items() if values}
    proof = _historical_reader_proof(read)
    by_key = {}
    for start in range(0, len(wanted), 48):
        rows = exact_rows(client, read, wanted[start:start + 48], allow_absent=True)
        for row in rows:
            if row.get("is_deleted") is True:
                fields = ("name", "unit_type", "ean")
                by_key[str(row["product_id"])] = {"product_id":row["product_id"], "nr":row.get("nr"),
                    "is_deleted":True, "base_data":{field:(row.get("base_data") or {}).get(field) for field in fields}}
    return {key: {"requested": values, "reader": proof, "rows":[by_key[k] for k in values if k in by_key]}
            for key, values in keys.items() if values}


def _historical_reader_proof(read):
    return products.digest([read.origin, read.service_revision, read.mapping, read.mapping_version,
                            read.snapshot.fingerprint, read.document, read.variables])


def _refresh_historical_materials(run_name, instance, link, detail, inventory_evidence=None):
    grouped = _historical_groups(link, detail)
    if not grouped:
        return {}
    if inventory_evidence is not None:
        from hero_integration.services.file_migration import DirectContractChanged
        read = runtime.approved_read("HeroProducts", instance)
        if (not read or inventory_evidence.get("reader") != _historical_reader_proof(read)
                or sorted(grouped) != inventory_evidence.get("requested")):
            raise DirectContractChanged("The approved product reader or historical service positions changed.")
        return {str(row["product_id"]): confirm_historical_material(row, grouped[str(row["product_id"])],
                    link.source_payload_hash, read.mapping_version) for row in inventory_evidence["rows"]}
    from hero_integration.services.file_migration import bound_configuration
    from hero_integration.services.file_transfer import _client_and_downloader
    from hero_integration.services.scoped_test_reads import exact_rows
    run = frappe.get_doc('HERO Integration Run', run_name)
    client, _, source = _client_and_downloader(bound_configuration(run))
    read = runtime.approved_read('HeroProducts', instance)
    if source != instance or read is None:
        raise ServiceBlocked("Historical position verification needs the approved product reader for this source.")
    rows = exact_rows(client, read, sorted(grouped), allow_absent=True)
    return {str(row['product_id']): confirm_historical_material(
                row, grouped[str(row['product_id'])], link.source_payload_hash, read.mapping_version)
            for row in rows if row.get('is_deleted') is True}


def remember_relationships(run_name, instance, company, read, row, detail, result, *, inventory_evidence=None):
    from hero_integration.services.deferred_references import save, invalidate
    identity = runtime.identity(read, row)
    if result == 'skipped_rejected':
        invalidate(instance, OPERATION, identity)
        return
    link = scoped_link(instance, SOURCE_TYPE, identity, 'Item')
    if not link:
        raise ServiceBlocked('Service relationship observations require their imported source identity.')
    dependencies = _composition_dependencies(detail)
    history = (_refresh_historical_materials(run_name, instance, link, detail, inventory_evidence)
               if inventory_evidence is not None else _refresh_historical_materials(run_name, instance, link, detail))
    save(run_name, instance, OPERATION, identity, read.mapping_version,
        {'row':row, 'detail':detail, 'company':company, 'target_name':link.target_name,
         'source_hash':link.source_payload_hash, 'historical_materials':history}, sorted(dependencies), defer_empty=True)


def _composition_details(client, snapshot, ids):
    request = _composition_request(snapshot, ids)
    detailed = client.query(request.document, request.variables).get("supply_services")
    if not isinstance(detailed, list) or len(detailed) != len(ids) or {d.get("id") for d in detailed} != set(ids):
        raise ServiceBlocked("HERO returned an incomplete service composition list.")
    return {d["id"]: d for d in detailed}


def _composition_dependencies(detail):
    dependencies = set()
    for component in detail['positions']:
        kind = 'supply_product' if component['__typename'] == 'Documents_SupplyProduct' else 'WageGroup'
        dependencies.add((kind, str(component['id'])))
    file = detail.get('file_upload') or {}
    if file.get('id') is not None and (not file.get('is_deleted') or migration_archive.includes_archived()):
        dependencies.add(('service_file', str(file['id'])))
    return dependencies


def realize_relationships(pending, config, *, read=None):
    """Realize one frozen calculation without repeating the scalar import."""
    from hero_integration.services.deferred_references import realizing
    if not realizing():
        raise ServiceBlocked('Service relationships are owned by the post-success realization service.')
    observation = json.loads(pending.record_json)
    row, detail = observation['row'], observation['detail']
    read = read or runtime.resolve_read(OPERATION, pending.source_instance)
    company, _schema = preflight(pending.source_instance, read)
    if (runtime.identity(read, row) != pending.source_id or read.mapping_version != pending.mapping_version
            or company != observation['company']):
        raise ServiceBlocked('The deferred service mapping, company or source identity changed.')
    link = scoped_link(pending.source_instance, SOURCE_TYPE, pending.source_id, 'Item')
    if (not link or link.target_name != observation['target_name'] or link.mapping_version != VERSION
            or link.source_payload_hash != observation['source_hash'] or link.sync_status != 'Synchronized'):
        raise ServiceBlocked('The imported service observation changed before relationship realization.')
    item = frappe.get_doc('Item', link.target_name, for_update=True)
    baseline = baseline_check(link, item)
    file = detail.get('file_upload')
    downloader = None
    if file and (not file.get('is_deleted') or migration_archive.includes_archived()) and (not baseline.get('attachment') or
            baseline['attachment'].get('source') != products.digest(file)):
        # Signed URLs are never persisted. Refresh only this service and require
        # its file identity/metadata and owning service to match the frozen import.
        from hero_integration.services.file_transfer import _client_and_downloader
        client, downloader, source = _client_and_downloader(config)
        if source != pending.source_instance:
            raise ServiceBlocked('The deferred service attachment belongs to another source.')
        snapshot = runtime.validated_snapshot(read, pending.source_instance, frappe_module=frappe)
        request = _attachment_request(snapshot, [row['id']])
        current = client.query(request.document, request.variables).get('supply_services')
        if (not isinstance(current, list) or len(current) != 1 or not isinstance(current[0],dict) or
                current[0].get('id') != detail.get('id') or current[0].get('company_id') != detail.get('company_id') or
                products.digest(_stable_detail({'file_upload':current[0].get('file_upload')})) !=
                products.digest(_stable_detail({'file_upload':detail.get('file_upload')}))):
            raise ServiceBlocked('The service changed before its attachment could be realized.')
        file = current[0].get('file_upload')
    def link_components():
        calculation = composition(detail.get('positions'), pending.source_instance, historical=historical_materials(observation))
        from hero_integration.services.change_detection import managed_target_update
        child_type = item.meta.get_field(CALCULATION).options
        keys = [field.fieldname for field in frappe.get_meta(child_type).fields if field.fieldname]
        def stored_lines():
            result = []
            for line in item.get(CALCULATION) or []:
                values = line.get_valid_dict(ignore_virtual=True)
                result.append({key: values.get(key) for key in keys})
            return result

        before = stored_lines()
        item.set(CALCULATION, calculation)
        lines_changed = before != stored_lines()
        if lines_changed:
            with managed_target_update('Item', item.name, link.name):
                item.save(ignore_permissions=True)
        attachment = attach_file(file, item, pending.source_instance, downloader, baseline.get('attachment'))
        item.reload()
        values = normalized(item, set(baseline['values']) | {CALCULATION})
        link_changed = write_link(link, pending.source_instance, SOURCE_TYPE, pending.source_id, item, link.source_payload_hash, values,
                   extra={'price':baseline.get('price'), 'attachment':attachment, 'separate_number':baseline.get('separate_number',False),
                          'historical_materials':historical_materials(observation)})
        return 'UPDATED' if lines_changed or link_changed else 'SKIPPED'

    return runtime.realize_record(read, row, pending, SOURCE_TYPE, link_components)


def sync_services(client, *, source_instance, run_name, downloader, should_stop=None, progress=None, verify_source=None,
                  time_budget=1200, read=None, record_limit=None, continue_on_conflict=False, page_overlap=0):
    from hero_integration.services.migration_testing import validate_checkpoint
    frappe.only_for("System Manager")
    read = read or runtime.resolve_read(OPERATION, source_instance)
    from hero_integration.services.offset_pagination import advance, fingerprint, validate_overlap
    if page_overlap:
        validate_overlap(page_overlap, read.page_size, migration=True)
    company, schema = preflight(source_instance, read)
    key = "hero_service_migration:" + run_name
    raw = frappe.db.get_value('DefaultValue', {'parent': '__default', 'defkey': key}, 'defvalue')
    state = json.loads(raw) if raw else {"offset": 0, "counts": {}}
    stamp = products.digest([source_instance, company, schema, read.mapping_version, VERSION])
    if state.get("stamp") and state["stamp"] != stamp:
        raise ServiceBlocked("Service source, schema, company or mapping changed; start a new run.")
    validate_checkpoint(state if raw else {}, record_limit, stamp=stamp)
    from hero_integration.services.deferred_references import require_storage
    require_storage(frappe)
    state.update(stamp=stamp, done=False, stopped=False)
    deadline = time.monotonic() + time_budget
    from hero_integration.services.migration_testing import bounded_rows, processed
    while time.monotonic() < deadline:
        if record_limit is not None and processed(state['counts']) >= record_limit:
            state.update(done=True, testing_limit_reached=True)
            break
        if should_stop and should_stop():
            state["stopped"] = True
            break
        received = runtime.fetch_page(client, read, state["offset"])
        state["counts"]["rows_read"] = state["counts"].get("rows_read", 0) + len(received)
        window = None
        start_index = 0
        rows = received
        if page_overlap:
            window = advance(received, offset=state["offset"], size=read.page_size, overlap=page_overlap, migration=True,
                             previous=state.get("offset_window"))
            page_hash = fingerprint(received)
            if state.get("pending_page_hash", page_hash) != page_hash:
                raise ServiceBlocked("The interrupted service page changed; its checkpoint cannot be resumed.")
            start_index = int(state.get("pending_page_index", 0))
            rows = window.rows[start_index:]
            if received and not rows:
                state.update(offset=window.next_offset, offset_window=window.checkpoint)
                state.pop("pending_page_hash", None)
                state.pop("pending_page_index", None)
                continue
        rows = bounded_rows(state, rows, record_limit)
        if not rows:
            state["done"] = True
            break
        ids = [row.get("id") for row in rows]
        if any(type(identity) is not int or identity <= 0 for identity in ids) or len(set(ids)) != len(ids):
            raise ServiceBlocked("HERO returned invalid or duplicate service identities.")
        new_ids = [identity for identity in ids if not window or str(identity) not in window.changed]
        if ids != sorted(ids) or (new_ids and state.get("last_id") is not None and new_ids[0] <= state["last_id"]):
            raise ServiceBlocked("Service pagination changed or repeated; start a new run.")
        snapshot = runtime.validated_snapshot(read, source_instance, frappe_module=frappe)
        if snapshot.fingerprint != schema:
            raise ServiceBlocked('The service schema changed; start a new run.')
        from hero_integration.services.execution_budget import current_budget
        budget = current_budget()
        frozen_batch = bool(budget and budget.version == 3 and read.testing_rows is not None)
        if frozen_batch and not all(budget.allows_record(SOURCE_TYPE,identity) for identity in ids):
            raise ServiceBlocked('Service composition batch is outside the frozen cohort.')
        eligible_ids = [row["id"] for row in rows if not migration_archive.excluded(OPERATION, row)]
        details = _composition_details(client,snapshot,eligible_ids) if eligible_ids and (frozen_batch or not budget) else {}
        blocked = False
        for index, row in enumerate(rows, start_index):
            if time.monotonic() >= deadline:
                break
            if should_stop and should_stop():
                state["stopped"] = True
                break
            if verify_source:
                verify_source()
            if migration_archive.excluded(OPERATION, row):
                result = "skipped_archived"
            else:
                if budget:
                    budget.reserve_record(SOURCE_TYPE, row['id'])
                    if not frozen_batch:
                        details = _composition_details(client, snapshot, [row['id']])
                detail = details[row["id"]]
                if any(detail.get(key) != row.get(key) for key in ("modified", "company_id", "is_deleted")):
                    raise ServiceBlocked("Service changed between its record and composition reads; retry the slice.")
                if budget:
                    budget.reserve_many(_composition_dependencies(detail), relation='service_composition')
                try:
                    result = runtime.process_record(read, row, source_instance, SOURCE_TYPE, run_name,
                        lambda: upsert(row, detail, read, source_instance, company, downloader))
                    remember_relationships(run_name, source_instance, company, read, row, detail, result)
                except DirectContractChanged:
                    raise
                except Exception as error:
                    frappe.db.rollback()
                    result = "conflict" if isinstance(error, (ServiceBlocked, products.PriceConflict, BindingConflict)) else "failed"
                    state["last_error"] = str(error) if result == "conflict" else "Service validation failed; check the error log."
                    state["blocked_source_id"] = str(row["id"])
                    if result == "failed":
                        frappe.log_error(title="Service migration failed")
                    previous = state.setdefault("outcomes", {}).get(str(row["id"]))
                    if previous:
                        state["counts"][previous] -= 1
                    state["outcomes"][str(row["id"])] = result
                    state["counts"][result] = state["counts"].get(result, 0) + 1
                    state.setdefault('errors', {})[str(row['id'])] = state['last_error']
                    if not continue_on_conflict:
                        if budget:
                            budget.outcome(SOURCE_TYPE, row['id'], result)
                        blocked = True
                        break  # retry exactly this identity on explicit legacy resume
            if budget and result != 'skipped_archived':
                budget.outcome(SOURCE_TYPE, row['id'], result if result in ('failed', 'conflict') else 'processed')
            if window:
                if index + 1 == len(window.rows):
                    state.update(offset=window.next_offset, offset_window=window.checkpoint)
                    state.pop("pending_page_hash", None)
                    state.pop("pending_page_index", None)
                else:
                    state.update(pending_page_hash=page_hash, pending_page_index=index + 1)
            else:
                state["offset"] += 1
            state["last_id"] = max(row["id"], state.get("last_id", row["id"]))
            previous = state.setdefault("outcomes", {}).get(str(row["id"]))
            if previous:
                state["counts"][previous] -= 1
            state["outcomes"][str(row["id"])] = result
            state["counts"][result] = state["counts"].get(result, 0) + 1
            state["counts"]["unique_processed"] = len(state["outcomes"])
            state.pop("last_error", None)
            state.pop("blocked_source_id", None)
            frappe.db.set_default(key, json.dumps(state))
            frappe.db.commit()
            if progress:
                state["counts"]["confirmed_offset"] = state.get("offset")
                progress(state["counts"])
        if blocked or state["stopped"]:
            break
    frappe.db.set_default(key, json.dumps(state))
    frappe.db.commit()
    return state
