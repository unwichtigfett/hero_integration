"""Import used HERO articles (own articles and supplier catalogs) as ERPNext Items.

Read-only towards HERO. Articles come from ``supply_product_versions`` in pages
of 500; HERO ignores the offset unless the list is ordered, so the order is
fixed to ``product_id``. One HERO Integration Link per article (source type
``supply_product``) skips unchanged articles, updates changed ones and marks
an Item that was edited in ERPNext as Conflict instead of overwriting it.

The field mapping is configurable (operation ``HeroProducts``, see
services.direct_operations): an approved own mapping of the source, otherwise
the built-in mapping – name, description, unit → UOM, catalog → Item Group
under "HERO Artikel", manufacturer → Brand, EAN → barcode (only when unique),
purchase price → Standard Buying, default sales price → Standard Selling,
deleted → disabled, configured article number → Item Code on insert. An unbound
existing Item keeps its local values; a service collision gets a stable separate
Item code. Name/group/unit fallbacks and price conflict protection remain active.
"""

from __future__ import annotations

import hashlib
import json
import time

import frappe
from hero_integration.services import migration_archive
from hero_integration.services.binding_registry import BindingConflict, RETIRED_LINK_SYSTEM
from hero_integration.services.file_migration import DirectContractChanged
from hero_integration.services.execution_budget import BudgetReached, current_budget, mapped_dependencies

from hero_integration.services import direct_operations as direct
from hero_integration.services import direct_runtime as runtime

SOURCE_TYPE = "supply_product"
OPERATION = "HeroProducts"
MAPPING_VERSION = "product-migration-v3"  # v3: field values come from the HeroProducts mapping
PAGE_SIZE = 500  # HERO returns at most 500 articles per request
STATE_KEY = "hero_product_migration"
ROOT_GROUP = "HERO Artikel"
SUPPLIER_GROUP = "All Supplier Groups"
# A HERO article whose item code is used by an Item without its link waits for an administrator decision.
CODE_TAKEN = ("Item code {0} is already used by an ERPNext Item without this HERO article's link. Approve to link that "
              "Item to the HERO article; decline to keep them apart (the article is not imported).")
CODE_TAKEN_PREFIX = "Item code "
BUYING, SELLING = "Standard Buying", "Standard Selling"
TIME_BUDGET_SECONDS = 50 * 60
# Item fields of the built-in mapping; a local change to a mapped field is a conflict.
OWNED = ("item_name", "description", "item_group", "stock_uom", "brand", "disabled")
# Item fields ERPNext requires on insert; filled from the fixed fallbacks when no rule maps them.
REQUIRED_FALLBACKS = {"item_group": direct.UNCATALOGUED_GROUP, "stock_uom": direct.DEFAULT_UOM}


# ---------------------------------------------------------------- mapping (pure)

def item_values(row: dict, read=None) -> dict:
    """Item values of one HERO article under the explicitly supplied mapping.

    Keys: item_code, the mapped Item fields (``owned`` lists them), barcode,
    buying, selling."""
    if read is None:
        raise ValueError("An explicit product mapping is required.")
    mapped = runtime.record_values(read.plan, row, "Item")
    barcode = mapped.pop("barcodes", None)
    supplier = mapped.pop("supplier_items", None)
    code = (mapped.pop("item_code", None) or "").strip()[:140]
    if "disabled" in mapped:
        mapped["disabled"] = 1 if mapped["disabled"] else 0
    nr = (row.get("nr") or "").strip() if isinstance(row.get("nr"), str) else ""
    name = (mapped.get("item_name") or nr or str(row.get("product_id") or "")).strip()[:140]
    mapped["item_name"] = name  # an article without description keeps an empty description
    prices = runtime.price_values(read.plan, row)
    return {"item_code": code or str(row["product_id"]), **mapped,
            "owned": tuple(field for field in mapped),
            "barcode": barcode, "supplier": supplier, "buying": prices.get(BUYING), "selling": prices.get(SELLING)}


def selling_price(row: dict) -> float | None:
    return direct.resolve("HERO Default Sales Price", row.get("sales_prices"), row)


def barcode_type(code: str) -> str:
    """EAN only when the check digit is valid; ERPNext rejects an invalid typed barcode."""
    if not code.isdigit() or len(code) not in (8, 13):
        return ""
    digits = [int(c) for c in code]
    weights = [3, 1] * 6 if len(code) == 13 else [3, 1] * 4
    total = sum(d * w for d, w in zip(reversed(digits[:-1]), weights))
    return "EAN" if (10 - total % 10) % 10 == digits[-1] else ""


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _owned_values(doc, fields):
    from hero_integration.services.change_detection import native_table_rows
    return {field: rows if (rows := native_table_rows(doc, field)) is not None else doc.get(field) for field in fields}


def owned_digest(doc, owned=OWNED) -> str:
    return digest(_owned_values(doc, owned))


def field_digest(value):
    # Old baselines hashed all falsy values as null. A tagged hash lets us
    # retain independent evidence for a confirmed null, empty string, zero or false.
    return digest(["native-field-v2", value]) if not value else digest(value)


def _proven_legacy_empty(doc, field, local, incoming):
    # The native Item.disabled Check always represents 0/1. Other formerly
    # collapsed values need an identical typed source value before promotion.
    from hero_integration.services.change_detection import migration_storage_value
    projected = migration_storage_value(doc, field, incoming[field]) if field in incoming else None
    return (field == "disabled" and type(local) is int and local == 0
            or field in incoming and any(type(local) is type(value) and local == value
                                         for value in (incoming[field], projected)))


def verified_field_digests(doc, stored, incoming):
    from hero_integration.services.change_detection import mapped_table_matches, native_table_rows
    current = owned_field_digests(doc, stored)
    for field, previous in stored.items():
        if previous == current[field]:
            continue
        rows = native_table_rows(doc, field)
        if rows and field in incoming and mapped_table_matches(doc, field, incoming[field], complete=True):
            # Prior versions hashed the Python representation of persisted child Documents.
            # A complete identical projection confirms convergence without replacing native rows.
            current[field] = previous
            continue
        value = doc.get(field)
        if (not value and previous == digest(None) and _proven_legacy_empty(doc, field, value, incoming)
                or previous == field_digest(None) and incoming.get(field, False) is None and type(value) in (int, float)
                and _proven_legacy_empty(doc, field, value, incoming)):
            current[field] = previous
    return current


def owned_field_digests(doc, owned=OWNED) -> dict[str, str]:
    """Per-field baseline, so a conflict report can name the field changed in ERPNext."""
    return {field: field_digest(value) for field, value in _owned_values(doc, owned).items()}


# ---------------------------------------------------------------- Frappe side

class _Masters:
    """Create Item Groups, UOMs and Brands once per run."""

    def __init__(self):
        self.seen = set()

    def ensure(self, doctype: str, name: str | None, **values) -> None:
        if not name or (doctype, name) in self.seen:
            return
        if not frappe.db.exists(doctype, name):
            doc = frappe.get_doc({"doctype": doctype, **values})
            doc.insert(ignore_permissions=True, ignore_if_duplicate=True)
        self.seen.add((doctype, name))

    def for_item(self, values: dict) -> None:
        self.ensure("Item Group", ROOT_GROUP, item_group_name=ROOT_GROUP, parent_item_group="All Item Groups", is_group=1)
        if values.get("item_group"):
            self.ensure("Item Group", values["item_group"], item_group_name=values["item_group"], parent_item_group=ROOT_GROUP)
        if values.get("stock_uom"):
            self.ensure("UOM", values["stock_uom"], uom_name=values["stock_uom"])
        if values.get("brand"):
            self.ensure("Brand", values["brand"], brand=values["brand"])

    def supplier(self, name: str) -> str:
        """The Supplier named ``name`` (created when missing); returns its document name."""
        existing = _existing_supplier(name)
        if existing:
            return existing
        doc = frappe.get_doc({"doctype": "Supplier", "supplier_name": name, "supplier_type": "Company",
                              **({"supplier_group": SUPPLIER_GROUP} if frappe.db.exists("Supplier Group", SUPPLIER_GROUP) else {})})
        doc.insert(ignore_permissions=True)
        return doc.name


def _existing_supplier(name: str) -> str | None:
    matches = frappe.get_all("Supplier", filters={"supplier_name": name}, pluck="name", limit_page_length=2)
    if len(matches) > 1:
        raise BindingConflict("The HERO supplier name matches multiple ERPNext Suppliers.")
    return matches[0] if matches else None


def _links(source_instance, identities):
    return runtime.page_links(frappe, source_instance, SOURCE_TYPE, identities, [
        "target_name", "source_payload_hash", "target_payload_hash", "target_field_hashes", "sync_status",
        "conflict_detail", "mapping_version"])


class PriceConflict(Exception):
    """An imported price cannot be identified safely or was changed in ERPNext."""


PRICE_LISTS = ((BUYING, "buying"), (SELLING, "selling"))


def _link_baseline(link) -> dict:
    try:
        data = json.loads(getattr(link, "target_field_hashes", "") or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def original_pending_item(link, item, plan):
    """Recognize only an Item created by this mapped import with a pending price identity."""
    from hero_integration.services.binding_registry import matches_incarnation
    if not link or link.sync_status != "Pending Review" or not item:
        return False
    current = frappe.get_doc("HERO Integration Link", link.name, for_update=True)
    contract = plan.binding_contract
    return bool(contract and current.target_doctype == "Item" and current.target_name == item.name
                and current.source_instance == contract.source_instance and current.source_type == SOURCE_TYPE
                and current.mapping_version == f"{MAPPING_VERSION}:{plan.mapping_version}"[:140]
                and matches_incarnation(current.target_incarnation, item.creation)
                and _link_baseline(current).get("pending_mapping") == [contract.definition_id, contract.revision])


def _managed_prices(link) -> dict:
    prices = _link_baseline(link).get("prices")
    return prices if isinstance(prices, dict) else {}


def _collision_code(code, instance, product_id):
    """Keep a visible original prefix and a stable, source-scoped suffix."""
    return code[:110] + '-HERO-' + digest([instance, str(product_id)])[:20]


def _adopt_existing_product(item, row, source_instance, read, source, owned):
    """Bind an unowned native Item without altering its existing values."""
    baseline = {'fields': owned_field_digests(item, owned), 'prices': {}, 'adopted_locally': True}
    return frappe.get_doc({'doctype': 'HERO Integration Link', 'source_system': 'HERO',
        'source_type': SOURCE_TYPE, 'source_id': str(row['product_id']), 'source_instance': source_instance,
        'target_doctype': 'Item', 'target_name': item.name, 'sync_status': 'Synchronized',
        'source_payload_hash': source, 'target_payload_hash': owned_digest(item, owned),
        'target_field_hashes': json.dumps(baseline, sort_keys=True),
        'mapping_version': f'{MAPPING_VERSION}:{read.mapping_version}'[:140],
        'last_synced_on': frappe.utils.now_datetime()}).insert(ignore_permissions=True)


def _conflict(link, source: str, detail: str) -> str:
    frappe.db.set_value("HERO Integration Link", link.name, {
        "sync_status": "Conflict", "observed_source_hash": source, "last_observed_on": frappe.utils.now_datetime(),
        "conflict_detail": detail})
    return "conflict"


def _item_snapshot(item, fields):
    from hero_integration.services.change_detection import native_table_rows
    stored = item.get_valid_dict(ignore_virtual=True)
    return {**{key: rows if (rows := native_table_rows(item, key)) is not None else stored.get(key) for key in fields},
            'barcodes': [(row.get('barcode'), row.get('barcode_type')) for row in item.get('barcodes') or []],
            'supplier_items': [(row.get('supplier'), row.get('supplier_part_no')) for row in item.get('supplier_items') or []]}


def upsert(row: dict, link, masters: _Masters, source_instance: str, read=None, *, relation_warnings=None) -> str:
    """Create or update one Item; returns added, updated, unchanged or conflict."""
    read = read or runtime.resolve_read(OPERATION, source_instance)
    values = item_values(row, read)
    owned = values.pop("owned")
    source = digest({**values, "mapping": read.mapping_version})
    relations = (getattr(read.plan, 'adapter_options', None) or {}).get('native_relations')
    relation_pending = ''
    status, detail = (getattr(link, "sync_status", "") or "", getattr(link, "conflict_detail", "") or "") if link else ("", "")
    if status == "Rejected":
        return "skipped_rejected"  # declined: HERO changes to this article are not imported
    if status == "Conflict" and detail.startswith(CODE_TAKEN_PREFIX):
        return "conflict"  # the taken item code waits for approve (link that Item) or decline
    if link and (not link.target_name or not frappe.db.exists("Item", link.target_name)):
        return _conflict(link, source, "The linked Item no longer exists in ERPNext; review the binding before importing.")
    from hero_integration.services.execution_runtime import adapter_baseline, field_decisions, preserve_local_fields
    if (link and link.source_payload_hash == source and not relations
            and "apply" not in field_decisions("Item", link.target_name).values()):
        return "unchanged"
    item = frappe.get_doc("Item", link.target_name) if link else None
    if item is None and frappe.db.exists("Item", values["item_code"]):
        existing_links = frappe.get_all('HERO Integration Link', filters={'source_system': ['!=', RETIRED_LINK_SYSTEM],
            'target_doctype': 'Item', 'target_name': values['item_code']}, fields=['source_type', 'source_instance', 'sync_status'], limit_page_length=2)
        if (len(existing_links) == 1 and existing_links[0].source_type == 'supply_service'
                and existing_links[0].source_instance == source_instance
                and existing_links[0].sync_status == 'Synchronized'):
            original_code = values['item_code']
            values['item_code'] = _collision_code(original_code, source_instance, row['product_id'])
            if frappe.db.exists('Item', values['item_code']):
                return _code_taken(link, row, values['item_code'], source_instance, read)
            values['custom_hero_original_article_number'] = original_code
            owned = (*owned, 'custom_hero_original_article_number')
        elif not existing_links:
            item = frappe.get_doc('Item', values['item_code'], for_update=True)
            frappe.has_permission('Item', 'write', doc=item, throw=True)
            link = _adopt_existing_product(item, row, source_instance, read, source, owned)
        else:
            return _code_taken(link, row, values['item_code'], source_instance, read)
    adopted = bool(_link_baseline(link).get('adopted_locally'))
    if adopted and link.source_payload_hash != source:
        return _conflict(link, source, 'A linked local Item was retained; changed HERO fields require review.')
    if item is not None and 'custom_hero_original_article_number' in (_link_baseline(link).get('fields') or {}):
        original_code = item.get('custom_hero_original_article_number')
        if values['item_code'] != original_code:
            return _conflict(link, source, 'The original HERO article number changed; the existing Item code was preserved.')
        values['custom_hero_original_article_number'] = original_code
        owned = (*owned, 'custom_hero_original_article_number')
    # The baseline covers the fields mapped when it was taken; legacy links used the built-in set.
    baseline_fields = _link_baseline(link).get("fields") or {}
    if item is not None:
        # A partial Send advances only its confirmed fields. An old aggregate
        # cannot acknowledge the remaining fields, nor veto a valid partial update.
        changed = (adapter_baseline(item, verified_field_digests(item, baseline_fields, values), baseline_fields,
                                    verify=True) != baseline_fields
                   if baseline_fields else bool(link.target_payload_hash)
                   and owned_digest(item, OWNED) != link.target_payload_hash
                   and not (all(item.get(field) or _proven_legacy_empty(item, field, item.get(field), values) for field in OWNED)
                            and digest({field: item.get(field) or None for field in OWNED}) == link.target_payload_hash))
        if changed:
            return _conflict(link, source, "The Item was changed in ERPNext; the newer HERO article was not applied.")
        from hero_integration.services.change_detection import new_mapped_field_conflicts
        incoming = {key: values[key] for key in owned
                    if values[key] is not None or key not in REQUIRED_FALLBACKS}
        if new_mapped_field_conflicts(item, incoming, tuple(baseline_fields) if baseline_fields else OWNED):
            return _conflict(link, source, "The Item was changed in ERPNext; the newer HERO article was not applied.")
    supplier_baseline = _link_baseline(link).get("supplier")
    if item is not None and not adopted and not _supplier_unchanged(item, values["supplier"], supplier_baseline):
        return _conflict(link, source, "The Item was changed in ERPNext; the newer HERO article was not applied.")
    managed = _managed_prices(link) if item is not None else {}
    plans = {}
    if item is not None:
        try:  # decide every price before anything is written
            for price_list, key in PRICE_LISTS:
                plans[price_list] = plan_price(item.name, price_list, values[key], values.get("stock_uom") or item.stock_uom,
                                               managed.get(price_list))
        except PriceConflict as error:
            return _conflict(link, source, f"{error} The newer HERO article was not applied.")
        if relations:
            from hero_integration.services.mapped_relations import sync
            from hero_integration.services.mapped_collection_relations import MissingCollectionIdentity
            try:
                checks = sync(read.plan, {**row, '_hero_source_instance': source_instance}, 'Item', item.name, preview=True)
            except MissingCollectionIdentity as error:
                relation_pending = str(error)
            except ValueError as error:
                return _conflict(link, source, str(error))
            if not relation_pending and checks.get('conflicts'):
                return _conflict(link, source, 'Mapped item prices changed locally; review before updating.')
    fields = {key: values[key] for key in owned}
    if item is None:
        fields = {**{key: value for key, value in REQUIRED_FALLBACKS.items() if not fields.get(key)}, **{
            key: value for key, value in fields.items() if value is not None or key not in REQUIRED_FALLBACKS}}
    if item is not None:
        preserve_local_fields(item, fields)
    if not adopted:
        masters.for_item(fields)
    if item is None:
        item = frappe.get_doc({"doctype": "Item", "item_code": values["item_code"], "is_stock_item": 0, **fields})
        _set_barcode(item, values["barcode"])
        supplier_baseline = _set_supplier(item, values["supplier"], masters) or supplier_baseline
        item.insert(ignore_permissions=True)
        result = "added"
    elif not adopted:
        from hero_integration.services.change_detection import native_values_changed
        fields = {key: value for key, value in fields.items() if value is not None or key not in REQUIRED_FALLBACKS}
        before = _item_snapshot(item, fields)
        from hero_integration.services.change_detection import mapped_table_matches
        item.update({key: value for key, value in fields.items()
                     if not (isinstance(before.get(key), list) and mapped_table_matches(item, key, value))})
        if not item.get("barcodes"):
            _set_barcode(item, values["barcode"])
        supplier_baseline = _set_supplier(item, values["supplier"], masters) or supplier_baseline
        result = 'unchanged'
        if native_values_changed(before, _item_snapshot(item, fields)):
            item.save(ignore_permissions=True)
            result = 'updated'
    else:
        result = 'unchanged'
    prices = dict(managed)
    for price_list, key in PRICE_LISTS:
        plan = plans[price_list] if price_list in plans else plan_price(
            item.name, price_list, values[key], item.stock_uom, None)
        if plan is not None:
            prices[price_list] = apply_price(item.name, price_list, plan)
    if relations and not relation_pending:
        from hero_integration.services.mapped_relations import sync
        from hero_integration.services.mapped_collection_relations import MissingCollectionIdentity
        try:
            counts = sync(read.plan, {**row, '_hero_source_instance': source_instance}, 'Item', item.name)
        except MissingCollectionIdentity as error:
            relation_pending = str(error)
            counts = {}
        if counts.get('conflicts'):
            return _conflict(link, source, 'Mapped item prices changed locally; review before updating.') if link else 'conflict'
        if result == 'unchanged' and (counts.get('created') or counts.get('updated')):
            result = 'updated'
    if relation_pending and relation_warnings is not None:
        relation_warnings.append(relation_pending)
    link_values = {"sync_status": "Pending Review" if relation_pending else "Synchronized", "target_name": item.name, "source_payload_hash": source,
                   "target_payload_hash": owned_digest(item, owned), "conflict_detail": relation_pending,
                   "target_field_hashes": json.dumps({"prices": prices, "fields": adapter_baseline(item, owned_field_digests(item, owned), baseline_fields),
                                                     **({'adopted_locally': True} if adopted else {}),
                                                     **({"supplier": supplier_baseline} if supplier_baseline else {})},
                                                     sort_keys=True),
                   "mapping_version": f"{MAPPING_VERSION}:{read.mapping_version}"[:140]}
    if relation_pending and read.plan.binding_contract:
        contract = read.plan.binding_contract
        if result == "added" and not link and getattr(item, "creation", None):
            from hero_integration.services.binding_registry import incarnation
            link_values["target_incarnation"] = incarnation(item.creation)
            proof = [contract.definition_id, contract.revision]
        else:
            proof = _link_baseline(link).get("pending_mapping")
        if proof:
            state = json.loads(link_values["target_field_hashes"])
            link_values["target_field_hashes"] = json.dumps({**state, "pending_mapping":proof}, sort_keys=True)
    if link and result == 'unchanged':
        if all((getattr(link, field, None) or '') == value for field, value in link_values.items()):
            return result
        if not adopted:
            result = 'updated'
    link_values['last_synced_on'] = frappe.utils.now_datetime()
    if link:
        frappe.db.set_value("HERO Integration Link", link.name, link_values)
    else:
        frappe.get_doc({"doctype": "HERO Integration Link", "source_system": "HERO", "source_type": SOURCE_TYPE,
                        "source_id": str(row["product_id"]), "source_instance": source_instance,
                        "target_doctype": "Item", **link_values}).insert(ignore_permissions=True)
    return result


def _code_taken(link, row: dict, code: str, source_instance: str, read) -> str:
    """Record the taken item code as a conflict on this article's link (no Item is written)."""
    values = {"sync_status": "Conflict", "target_doctype": "Item", "target_name": code, "source_payload_hash": "",
              "target_payload_hash": "", "conflict_detail": CODE_TAKEN.format(code),
              "mapping_version": f"{MAPPING_VERSION}:{read.mapping_version}"[:140],
              "last_observed_on": frappe.utils.now_datetime()}
    if link:
        frappe.db.set_value("HERO Integration Link", link.name, values)
    else:
        frappe.get_doc({"doctype": "HERO Integration Link", "source_system": "HERO", "source_type": SOURCE_TYPE,
                        "source_id": str(row["product_id"]), "source_instance": source_instance,
                        **values}).insert(ignore_permissions=True)
    return "conflict"


def _supplier_unchanged(item, supplier: dict | None, baseline) -> bool:
    """Check only the managed supplier row, without creating masters or changing Item.

    Old links without a supplier baseline may adopt an existing row only when it
    already equals the incoming value. Otherwise its ownership cannot be proved.
    Unrelated suppliers are never part of this comparison.
    """
    if not supplier or not supplier.get("supplier") or "supplier_part_no" not in supplier:
        return True
    rows = item.get("supplier_items") or []
    if baseline:
        if not isinstance(baseline, dict) or not baseline.get("supplier"):
            return False
        managed = [row for row in rows if row.get("supplier") == baseline["supplier"]]
        if len(managed) != 1 or (managed[0].get("supplier_part_no") or None) != baseline.get("supplier_part_no"):
            return False
    name = _existing_supplier(supplier["supplier"])
    destination = [row for row in rows if row.get("supplier") == name] if name else []
    if not destination:
        return True
    if len(destination) != 1:
        return False
    if baseline and name == baseline["supplier"]:
        return True
    return (destination[0].get("supplier_part_no") or None) == (supplier.get("supplier_part_no") or None)


def _set_supplier(item, supplier: dict | None, masters: _Masters) -> dict | None:
    """Keep one Item Supplier row for the HERO supplier; other rows stay as they are."""
    if not supplier or not supplier.get("supplier"):
        return
    name, part = masters.supplier(supplier["supplier"]), supplier.get("supplier_part_no")
    for row in item.get("supplier_items") or []:
        if row.get("supplier") == name:
            if "supplier_part_no" not in supplier:
                return None  # Missing source data never establishes a new ownership baseline.
            if row.get("supplier_part_no") != part:
                if isinstance(row, dict):
                    row["supplier_part_no"] = part
                else:
                    row.supplier_part_no = part
            return {"supplier": name, "supplier_part_no": row.get("supplier_part_no") or None}
    item.append("supplier_items", {"supplier": name, "supplier_part_no": part})
    return {"supplier": name, "supplier_part_no": part or None}


def _set_barcode(item, code: str | None) -> None:
    if code and not frappe.db.exists("Item Barcode", {"barcode": code}):
        item.append("barcodes", {"barcode": code, "barcode_type": barcode_type(code)})


# A price with any of these set belongs to a customer, supplier, batch,
# packing unit or period; the import never touches such prices.
_DIMENSIONS = ("customer", "supplier", "batch_no", "packing_unit", "valid_upto")


def _rate(value) -> float | None:
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def plan_price(item_code: str, price_list: str, rate: float | None, uom: str, managed: dict | None) -> dict | None:
    """Which Item Price the import may write: its own managed price, or the single
    general price for this item, price list and unit. Read-only; raises PriceConflict."""
    if rate is None or not frappe.db.exists("Price List", price_list):
        return None
    name = (managed or {}).get("name")
    if name and frappe.db.exists("Item Price", name):
        current = frappe.db.get_value("Item Price", name,
                                      ["item_code", "price_list", "price_list_rate", "uom", *_DIMENSIONS], as_dict=True)
        if (current.item_code, current.price_list) != (item_code, price_list):
            raise PriceConflict(f"The imported {price_list} price {name} now belongs to another item or list.")
        if any(current.get(field) for field in _DIMENSIONS):
            raise PriceConflict(f"The {price_list} price {name} is now limited to a customer, supplier, batch, "
                                "packing unit or period in ERPNext.")
        if _rate(current.price_list_rate) != _rate(managed.get("rate")) or (current.uom or "") != (managed.get("uom") or ""):
            raise PriceConflict(f"The {price_list} price {name} was changed in ERPNext.")
        return {"name": name, "rate": rate, "uom": uom}
    candidates = frappe.get_all("Item Price", filters={"item_code": item_code, "price_list": price_list, "uom": uom},
                                fields=["name", *_DIMENSIONS], limit_page_length=0)
    general = [row for row in candidates if not any(row.get(field) for field in _DIMENSIONS)]
    if len(general) > 1:
        raise PriceConflict(f"Several general {price_list} prices exist for {item_code} ({uom}); none was changed.")
    return {"name": general[0].name if general else None, "rate": rate, "uom": uom}


def apply_price(item_code: str, price_list: str, plan: dict) -> dict:
    """Write a planned price; returns its baseline for the next comparison."""
    if plan["name"]:
        current = frappe.db.get_value("Item Price", plan["name"], ["price_list_rate", "uom"], as_dict=True)
        if _rate(current.price_list_rate) != plan["rate"] or (current.uom or "") != plan["uom"]:
            frappe.db.set_value("Item Price", plan["name"], {"price_list_rate": plan["rate"], "uom": plan["uom"]})
        name = plan["name"]
    else:
        name = frappe.get_doc({"doctype": "Item Price", "item_code": item_code, "price_list": price_list,
                               "price_list_rate": plan["rate"], "uom": plan["uom"]}).insert(ignore_permissions=True).name
    return {"name": name, "rate": plan["rate"], "uom": plan["uom"]}


# ---------------------------------------------------------------- run

def reset() -> None:
    # The accepting transaction also owns the run, parent linkage and after-commit enqueue.
    frappe.db.set_default(STATE_KEY, "")


def _state() -> dict:
    raw = frappe.db.get_value('DefaultValue', {'parent': '__default', 'defkey': STATE_KEY}, 'defvalue')
    return json.loads(raw) if raw else {}


def _save(state: dict) -> None:
    state["updated"] = str(frappe.utils.now_datetime())
    frappe.db.set_default(STATE_KEY, json.dumps(state))
    frappe.db.commit()


def _client():
    from hero_integration.client.graphql import HeroGraphQLClient
    from hero_integration.client.settings import get_configuration

    config = get_configuration()
    client = HeroGraphQLClient(config.endpoint, lambda: config.api_token, config.timeout_seconds, max_retries=2)
    return client, config.source_instance


def fetch_page(client, offset: int, size: int = PAGE_SIZE, read=None) -> list[dict]:
    """One page under the explicit query; a rejected page is read in halves."""
    if read is None:
        raise ValueError("An explicit product mapping is required.")
    return runtime.fetch_page(client, read, offset, size)


def _was_used(row: dict) -> bool:
    """Check the HERO usage counter before resolving links or writing an Item."""
    count = row.get("Genutzt")
    if isinstance(count, str):
        count = count.strip()
        if count.removeprefix("-").isdecimal():
            count = int(count)
    if isinstance(count, float) and count.is_integer():
        count = int(count)
    if type(count) is not int:
        raise ValueError(f"HERO-Artikel {row.get('product_id')}: Das Feld „Genutzt“ fehlt oder enthält keine ganze Zahl.")
    return count > 0


def sync_products(client=None, *, source_instance: str | None = None, time_budget: int = TIME_BUDGET_SECONDS,
                  should_stop=None, max_pages: int = 2000, read=None, progress=None, record_limit=None, run_name=None, page_overlap=0, observe_identity=None) -> dict:
    """Process articles from the saved offset until done, stopped or the time budget ends.

    ``source_instance`` is the HERO source ``client`` reads from; lookups, new
    links and the saved offset belong to it."""
    if client is None:
        client, configured = _client()
        if source_instance is not None and configured != source_instance:
            raise RuntimeError("The configured HERO source instance changed; queue a new run.")
        source_instance = configured
    if not isinstance(source_instance, str) or not source_instance.strip():
        raise RuntimeError("A HERO source instance is required.")
    read = read or runtime.resolve_read(OPERATION, source_instance)
    from hero_integration.services.migration_testing import bounded_rows, processed, validate_checkpoint
    from hero_integration.services.offset_pagination import advance, validate_overlap
    if page_overlap:
        validate_overlap(page_overlap, read.page_size, migration=True)
    state, save = runtime.run_progress(read, run_name, _state, _save, record_limit=record_limit)
    from hero_integration.services import product_usage

    usage = product_usage.load(source_instance)
    digest, usage_counts = usage if usage else (None, {})
    keys = sorted(usage_counts)
    if state and state.get('usage_digest') != digest and not (state.get('done') and not read.validate_write):
        raise ValueError('Die Artikel-Positivliste hat sich seit dem Laufstart geändert.')
    if read.validate_write and state.get("done"):
        return state
    if record_limit is not None:
        validate_checkpoint(state, record_limit, source_instance=source_instance, mapping=read.mapping_version)
        if state.get('done'):
            return state
    # A finished pass, another source or a changed mapping starts over (unchanged articles are skipped).
    if (state.get("done") or state.get("source_instance") != source_instance
            or state.get("mapping") != read.mapping_version or state.get("page_overlap", 0) != page_overlap):
        state = {}
    state.update(source_instance=source_instance, mapping=read.mapping_version, mapping_origin=read.origin, page_overlap=page_overlap)
    new_usage_checkpoint = usage and "usage_digest" not in state
    state["usage_digest"] = digest
    counts = state.setdefault("counts", {})
    state.update(done=False, stopped=False, last_error=None)
    if new_usage_checkpoint and read.validate_write:
        save(state)
    masters = _Masters()
    deadline = time.monotonic() + time_budget
    offset = int(state.get("offset") or 0)
    for _ in range(max_pages):
        if record_limit is not None and processed(counts) >= record_limit:
            state.update(done=True, testing_limit_reached=True)
            break
        if should_stop and should_stop():
            state["stopped"] = True
            break
        page_size = min(read.page_size, record_limit - processed(counts)) if record_limit is not None else read.page_size
        if page_overlap:
            page_size = read.page_size
        if usage and read.testing_rows is None:
            page_size = min(page_size, 48)
            received = product_usage.page(client, read, keys, offset, page_size)
        else:
            received = fetch_page(client, offset, page_size, read)
        counts["rows_read"] = counts.get("rows_read", 0) + len(received)
        window = None
        if page_overlap:
            window = advance(received, offset=offset, size=page_size, overlap=page_overlap, migration=True,
                             previous=state.get("offset_window"), identity=lambda row: row.get("product_id"))
        fingerprint = hashlib.sha256(json.dumps(received, sort_keys=True, separators=(',', ':'),
                                                 default=str).encode()).hexdigest() if received else ''
        if fingerprint and state.get('last_page_fingerprint') == fingerprint:
            raise ValueError('HERO repeated a product page; pagination cannot safely continue.')
        candidates = window.rows if window else received
        active_rows = [row for row in candidates if not migration_archive.excluded(OPERATION, row)]
        if omitted := len(candidates) - len(active_rows):
            counts["skipped_archived"] = counts.get("skipped_archived", 0) + omitted
        rows = bounded_rows(state, active_rows, record_limit)
        counts["requests"] = counts.get("requests", 0) + 1
        eligible = []
        for row in rows:
            if not row.get("product_id"):
                counts["skipped_no_id"] = counts.get("skipped_no_id", 0) + 1
            elif usage and str(row['product_id']) not in usage_counts:
                counts["skipped_outside_usage_export"] = counts.get("skipped_outside_usage_export", 0) + 1
            elif usage or _was_used(row):
                eligible.append(row)
            else:
                counts["skipped_unused"] = counts.get("skipped_unused", 0) + 1
        links = _links(source_instance, [str(row["product_id"]) for row in eligible]) if eligible else {}
        for row in eligible:
            key = str(row["product_id"])
            if budget := current_budget():
                scoped_row = {**row, '_hero_source_instance': source_instance}
                budget.reserve_record(SOURCE_TYPE, key, mapped_dependencies(read.plan, scoped_row, 'Item'))
            try:
                warnings = []
                result = runtime.process_record(read, row, source_instance, SOURCE_TYPE, run_name,
                    lambda: upsert(row, links.get(key), masters, source_instance, read=read, relation_warnings=warnings))
                if warnings and run_name:
                    from hero_integration.services.integration_events import record_output
                    target_name = frappe.db.get_value('HERO Integration Link',
                        {'source_system': 'HERO', 'source_type': SOURCE_TYPE, 'source_instance': source_instance, 'source_id': key}, 'target_name')
                    record_output(run=run_name, source_type=SOURCE_TYPE, source_id=key, result='PENDING_REVIEW',
                        action='product_relation_review', target_doctype='Item', target_name=target_name,
                        detail='Article imported; a related price requires a stable source identity.')
                if budget:
                    budget.outcome(SOURCE_TYPE, key, 'conflict' if result == 'conflict' or warnings else 'processed')
                frappe.db.commit()
            except BudgetReached:
                raise
            except DirectContractChanged:
                raise
            except BindingConflict as error:
                frappe.db.rollback()
                masters = _Masters()
                result = "conflict"
                from hero_integration.services.integration_events import record_output
                record_output(run=run_name, source_type=SOURCE_TYPE, source_id=key, result="CONFLICT",
                              action="product_binding_conflict", detail=str(error))
            except Exception as error:
                frappe.db.rollback()
                masters = _Masters()  # rollback invalidates the master existence cache
                result = "failed"
                frappe.log_error(title="HERO product migration failed",
                                 message=f"HERO article {key} ({row.get('nr')}): {error}")
                if run_name:
                    from hero_integration.services.integration_events import record_output
                    record_output(run=run_name, source_type=SOURCE_TYPE, source_id=key, result='BLOCKED_RECORD',
                        action='product_import_failed', detail=f'Article import failed ({type(error).__name__}); see Error Log.')
            if budget and budget.version in (2, 3) and result in ('conflict', 'failed'):
                budget.outcome(SOURCE_TYPE, key, result)
                frappe.db.commit()
            counts[result] = counts.get(result, 0) + 1
            if observe_identity:
                observe_identity(key, counts)
            if result != 'failed' and warnings:
                counts['pending_review'] = counts.get('pending_review', 0) + 1
        if window:
            state["offset_window"] = window.checkpoint
            offset = window.next_offset or 0
        else:
            offset += len(received)
        state["offset"] = offset
        if received:
            state['last_page_fingerprint'] = fingerprint
        if record_limit is not None and processed(counts) >= record_limit:
            state.update(done=True, testing_limit_reached=True)
            break
        if not received:
            state.update(done=True, offset=0)
            break
        save(state)
        if progress:
            counts["confirmed_offset"] = state.get("offset")
            progress(counts)
        if time.monotonic() > deadline:
            break
    save(state)
    return state
