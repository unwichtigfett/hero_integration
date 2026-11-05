"""Import every HERO article (own articles and supplier catalogs) as an ERPNext Item.

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
deleted → disabled, configured article number → Item Code on insert. Fixed in code:
collision protection (``code-product_id`` when the code is taken), the name/group/unit fallbacks, master records, non-stock
Items and the price conflict protection.
"""

from __future__ import annotations

import hashlib
import json
import time

import frappe

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


def _price(value) -> float | None:
    return direct.price(value)


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


def owned_digest(doc, owned=OWNED) -> str:
    return digest({field: doc.get(field) or None for field in owned})


def owned_field_digests(doc, owned=OWNED) -> dict[str, str]:
    """Per-field baseline, so a conflict report can name the field changed in ERPNext."""
    return {field: digest(doc.get(field) or None) for field in owned}


# ---------------------------------------------------------------- Frappe side

class _Masters:
    """Create Item Groups, UOMs and Brands once per run."""

    def __init__(self):
        self.seen = set()
        self.names = {}

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
        key = ("Supplier", name)
        if key not in self.names:
            existing = frappe.db.get_value("Supplier", {"supplier_name": name}, "name")
            if not existing:
                doc = frappe.get_doc({"doctype": "Supplier", "supplier_name": name, "supplier_type": "Company",
                                      **({"supplier_group": SUPPLIER_GROUP} if frappe.db.exists("Supplier Group", SUPPLIER_GROUP) else {})})
                doc.insert(ignore_permissions=True)
                existing = doc.name
            self.names[key] = existing
        return self.names[key]


def _links(source_instance: str) -> dict:
    rows = frappe.get_all("HERO Integration Link", filters={"source_type": SOURCE_TYPE, "source_instance": source_instance},
                          fields=["name", "source_id", "target_name", "source_payload_hash", "target_payload_hash",
                                  "target_field_hashes", "sync_status", "conflict_detail"],
                          limit_page_length=0)
    return {row.source_id: row for row in rows}


class PriceConflict(Exception):
    """An imported price cannot be identified safely or was changed in ERPNext."""


PRICE_LISTS = ((BUYING, "buying"), (SELLING, "selling"))


def _link_baseline(link) -> dict:
    try:
        data = json.loads(getattr(link, "target_field_hashes", "") or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _managed_prices(link) -> dict:
    prices = _link_baseline(link).get("prices")
    return prices if isinstance(prices, dict) else {}


def _conflict(link, source: str, detail: str) -> str:
    frappe.db.set_value("HERO Integration Link", link.name, {
        "sync_status": "Conflict", "observed_source_hash": source, "last_observed_on": frappe.utils.now_datetime(),
        "conflict_detail": detail})
    return "conflict"


def upsert(row: dict, link, masters: _Masters, source_instance: str, read=None) -> str:
    """Create or update one Item; returns added, updated, unchanged or conflict."""
    read = read or runtime.resolve_read(OPERATION, source_instance)
    values = item_values(row, read)
    owned = values.pop("owned")
    source = digest({**values, "mapping": read.mapping_version})
    relations = (getattr(read.plan, 'adapter_options', None) or {}).get('native_relations')
    status, detail = (getattr(link, "sync_status", "") or "", getattr(link, "conflict_detail", "") or "") if link else ("", "")
    if status == "Rejected":
        return "skipped_rejected"  # declined: HERO changes to this article are not imported
    if status == "Conflict" and detail.startswith(CODE_TAKEN_PREFIX):
        return "conflict"  # the taken item code waits for approve (link that Item) or decline
    if link and (not link.target_name or not frappe.db.exists("Item", link.target_name)):
        return _conflict(link, source, "The linked Item no longer exists in ERPNext; review the binding before importing.")
    if link and link.source_payload_hash == source and not relations:
        return "unchanged"
    item = frappe.get_doc("Item", link.target_name) if link else None
    if item is None and frappe.db.exists("Item", values["item_code"]):
        # The HERO article number is the Item's identity; it is never suffixed or reassigned.
        return _code_taken(link, row, values["item_code"], source_instance, read)
    # The baseline covers the fields mapped when it was taken; legacy links used the built-in set.
    baseline_fields = _link_baseline(link).get("fields") or {}
    if item is not None:
        # A partial Send advances only its confirmed fields. An old aggregate
        # cannot acknowledge the remaining fields, nor veto a valid partial update.
        changed = (owned_field_digests(item, tuple(baseline_fields)) != baseline_fields
                   if baseline_fields else bool(link.target_payload_hash)
                   and owned_digest(item, OWNED) != link.target_payload_hash)
        if changed:
            return _conflict(link, source, "The Item was changed in ERPNext; the newer HERO article was not applied.")
    supplier_baseline = _link_baseline(link).get("supplier")
    if item is not None and not _supplier_unchanged(item, values["supplier"], supplier_baseline):
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
            try:
                checks = sync(read.plan, {**row, '_hero_source_instance': source_instance}, 'Item', item.name, preview=True)
            except ValueError as error:
                return _conflict(link, source, str(error))
            if checks.get('conflicts'):
                return _conflict(link, source, 'Mapped item prices changed locally; review before updating.')
    fields = {key: values[key] for key in owned}
    if item is None:
        fields = {**{key: value for key, value in REQUIRED_FALLBACKS.items() if not fields.get(key)}, **{
            key: value for key, value in fields.items() if value is not None or key not in REQUIRED_FALLBACKS}}
    masters.for_item(fields)
    if item is None:
        item = frappe.get_doc({"doctype": "Item", "item_code": values["item_code"], "is_stock_item": 0, **fields})
        _set_barcode(item, values["barcode"])
        supplier_baseline = _set_supplier(item, values["supplier"], masters) or supplier_baseline
        item.insert(ignore_permissions=True)
        result = "added"
    else:
        item.update({key: value for key, value in fields.items()
                     if value is not None or key not in REQUIRED_FALLBACKS})  # never blank a required field
        if not item.get("barcodes"):
            _set_barcode(item, values["barcode"])
        supplier_baseline = _set_supplier(item, values["supplier"], masters) or supplier_baseline
        item.save(ignore_permissions=True)
        result = "updated"
    prices = dict(managed)
    for price_list, key in PRICE_LISTS:
        plan = plans[price_list] if price_list in plans else plan_price(
            item.name, price_list, values[key], item.stock_uom, None)
        if plan is not None:
            prices[price_list] = apply_price(item.name, price_list, plan)
    if relations:
        from hero_integration.services.mapped_relations import sync
        counts = sync(read.plan, {**row, '_hero_source_instance': source_instance}, 'Item', item.name)
        if counts.get('conflicts'):
            return _conflict(link, source, 'Mapped item prices changed locally; review before updating.') if link else 'conflict'
    link_values = {"sync_status": "Synchronized", "target_name": item.name, "source_payload_hash": source,
                   "target_payload_hash": owned_digest(item, owned), "conflict_detail": "",
                   "target_field_hashes": json.dumps({"prices": prices, "fields": owned_field_digests(item, owned),
                                                     **({"supplier": supplier_baseline} if supplier_baseline else {})},
                                                     sort_keys=True),
                   "mapping_version": f"{MAPPING_VERSION}:{read.mapping_version}"[:140],
                   "last_synced_on": frappe.utils.now_datetime()}
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
    if not supplier or not supplier.get("supplier"):
        return True
    rows = item.get("supplier_items") or []
    if baseline:
        if not isinstance(baseline, dict) or not baseline.get("supplier"):
            return False
        managed = [row for row in rows if row.get("supplier") == baseline["supplier"]]
        if len(managed) != 1 or (managed[0].get("supplier_part_no") or None) != baseline.get("supplier_part_no"):
            return False
    name = frappe.db.get_value("Supplier", {"supplier_name": supplier["supplier"]}, "name")
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
            if part and row.get("supplier_part_no") != part:
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
    frappe.db.set_default(STATE_KEY, "")
    frappe.db.commit()


def _state() -> dict:
    raw = frappe.db.get_default(STATE_KEY)
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


def sync_products(client=None, *, source_instance: str | None = None, time_budget: int = TIME_BUDGET_SECONDS,
                  should_stop=None, max_pages: int = 2000, read=None, progress=None, record_limit=None, run_name=None) -> dict:
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
    from hero_integration.services.migration_testing import bounded_rows, processed, test_state, save_test_state, validate_checkpoint
    state = test_state(run_name, OPERATION) if record_limit is not None else _state()
    save = (lambda value: save_test_state(run_name, OPERATION, value)) if record_limit is not None else _save
    if record_limit is not None:
        validate_checkpoint(state, record_limit, source_instance=source_instance, mapping=read.mapping_version)
        if state.get('done'):
            return state
    # A finished pass, another source or a changed mapping starts over (unchanged articles are skipped).
    if state.get("done") or state.get("source_instance") != source_instance or state.get("mapping") != read.mapping_version:
        state = {}
    state.update(source_instance=source_instance, mapping=read.mapping_version, mapping_origin=read.origin)
    counts = state.setdefault("counts", {})
    state.update(done=False, stopped=False, last_error=None)
    links, masters = _links(source_instance), _Masters()
    deadline = time.monotonic() + time_budget
    offset = int(state.get("offset") or 0)
    for _ in range(max_pages):
        if record_limit is not None and processed(counts) >= record_limit:
            state.update(done=True, testing_limit_reached=True)
            break
        if should_stop and should_stop():
            state["stopped"] = True
            break
        received = fetch_page(client, offset, read.page_size, read)
        rows = bounded_rows(state, received, record_limit)
        counts["requests"] = counts.get("requests", 0) + 1
        for row in rows:
            key = str(row.get("product_id") or "")
            if not key:
                counts["skipped_no_id"] = counts.get("skipped_no_id", 0) + 1
                continue
            try:
                result = upsert(row, links.get(key), masters, source_instance, read=read)
                frappe.db.commit()
            except Exception as error:
                frappe.db.rollback()
                masters = _Masters()  # rollback invalidates both master existence and Supplier-name caches
                result = "failed"
                frappe.log_error(title="HERO product migration failed",
                                 message=f"HERO article {key} ({row.get('nr')}): {error}")
            counts[result] = counts.get(result, 0) + 1
        offset += len(rows)
        state["offset"] = offset
        if record_limit is not None and processed(counts) >= record_limit:
            state.update(done=True, testing_limit_reached=True)
            break
        if len(received) < read.page_size:
            state.update(done=True, offset=0)
            break
        save(state)
        if progress:
            progress(counts)
        if time.monotonic() > deadline:
            break
    save(state)
    return state
