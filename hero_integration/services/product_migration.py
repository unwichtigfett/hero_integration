"""Import every HERO article (own articles and supplier catalogs) as an ERPNext Item.

Read-only towards HERO. Articles come from ``supply_product_versions`` in pages
of 500; HERO ignores the offset unless the list is ordered, so the order is
fixed to ``product_id``. One HERO Integration Link per article (source type
``supply_product``) skips unchanged articles, updates changed ones and marks
an Item that was edited in ERPNext as Conflict instead of overwriting it.

Mapping: nr → Item Code (``nr-product_id`` when the code is taken), name,
description, unit → UOM, catalog → Item Group under "HERO Artikel",
manufacturer → Brand, EAN → barcode (only when unique), purchase price →
Standard Buying, default sales price → Standard Selling, deleted → disabled.
Items are non-stock items.
"""

from __future__ import annotations

import hashlib
import json
import time

import frappe

SOURCE_TYPE = "supply_product"
MAPPING_VERSION = "product-migration-v2"  # v2: managed Item Price baseline in target_field_hashes
PAGE_SIZE = 500  # HERO returns at most 500 articles per request
STATE_KEY = "hero_product_migration"
ROOT_GROUP = "HERO Artikel"
BUYING, SELLING = "Standard Buying", "Standard Selling"
TIME_BUDGET_SECONDS = 50 * 60
QUERY = (
    "query HeroProducts($first: Int!, $offset: Int!) { supply_product_versions(first: $first, offset: $offset, "
    "orderBy: \"product_id\") { product_id nr base_price list_price is_deleted modified "
    "base_data { name description ean manufacturer unit_type supply_catalog { name } } "
    "sales_prices { net_price_per_unit supply_sales_price { is_default } } } }"
)
# Item fields this migration owns; a local change to one of them is a conflict.
OWNED = ("item_name", "description", "item_group", "stock_uom", "brand", "disabled")


# ---------------------------------------------------------------- mapping (pure)

def item_values(row: dict) -> dict:
    data = row.get("base_data") or {}
    catalog = ((data.get("supply_catalog") or {}).get("name") or "Ohne Katalog").strip()
    name = (data.get("name") or row.get("nr") or row.get("product_id") or "").strip()
    return {
        "item_code": (row.get("nr") or "").strip()[:140] or str(row["product_id"]),
        "item_name": name[:140],
        "description": (data.get("description") or "").strip() or name,
        "item_group": catalog[:140],
        "stock_uom": (data.get("unit_type") or "Stk").strip()[:140] or "Stk",
        "brand": (data.get("manufacturer") or "").strip()[:140] or None,
        "disabled": 1 if row.get("is_deleted") else 0,
        "barcode": "".join((data.get("ean") or "").split())[:140] or None,
        "buying": _price(row.get("base_price")),
        "selling": selling_price(row),
    }


def selling_price(row: dict) -> float | None:
    for price in row.get("sales_prices") or []:
        if (price.get("supply_sales_price") or {}).get("is_default"):
            return _price(price.get("net_price_per_unit"))
    return _price(row.get("list_price"))


def _price(value) -> float | None:
    try:
        number = round(float(value), 2)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


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


def owned_digest(doc) -> str:
    return digest({field: doc.get(field) or None for field in OWNED})


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
        self.ensure("Item Group", values["item_group"], item_group_name=values["item_group"], parent_item_group=ROOT_GROUP)
        self.ensure("UOM", values["stock_uom"], uom_name=values["stock_uom"])
        self.ensure("Brand", values["brand"], brand=values["brand"])


def _links(source_instance: str) -> dict:
    rows = frappe.get_all("HERO Integration Link", filters={"source_type": SOURCE_TYPE, "source_instance": source_instance},
                          fields=["name", "source_id", "target_name", "source_payload_hash", "target_payload_hash",
                                  "target_field_hashes"],
                          limit_page_length=0)
    return {row.source_id: row for row in rows}


class PriceConflict(Exception):
    """An imported price cannot be identified safely or was changed in ERPNext."""


PRICE_LISTS = ((BUYING, "buying"), (SELLING, "selling"))


def _managed_prices(link) -> dict:
    try:
        data = json.loads(getattr(link, "target_field_hashes", "") or "{}")
    except (TypeError, ValueError):
        return {}
    prices = data.get("prices") if isinstance(data, dict) else None
    return prices if isinstance(prices, dict) else {}


def _conflict(link, source: str, detail: str) -> str:
    frappe.db.set_value("HERO Integration Link", link.name, {
        "sync_status": "Conflict", "observed_source_hash": source, "last_observed_on": frappe.utils.now_datetime(),
        "conflict_detail": detail})
    return "conflict"


def upsert(row: dict, link, masters: _Masters, source_instance: str) -> str:
    """Create or update one Item; returns added, updated, unchanged or conflict."""
    values = item_values(row)
    source = digest(values)
    if link and link.source_payload_hash == source:
        return "unchanged"
    item = frappe.get_doc("Item", link.target_name) if link and frappe.db.exists("Item", link.target_name) else None
    if item is not None and link.target_payload_hash and owned_digest(item) != link.target_payload_hash:
        return _conflict(link, source, "The Item was changed in ERPNext; the newer HERO article was not applied.")
    managed = _managed_prices(link) if item is not None else {}
    plans = {}
    if item is not None:
        try:  # decide every price before anything is written
            for price_list, key in PRICE_LISTS:
                plans[price_list] = plan_price(item.name, price_list, values[key], values["stock_uom"],
                                               managed.get(price_list))
        except PriceConflict as error:
            return _conflict(link, source, f"{error} The newer HERO article was not applied.")
    masters.for_item(values)
    fields = {key: values[key] for key in OWNED}
    if item is None:
        code = values["item_code"]
        if frappe.db.exists("Item", code):
            code = f"{code}-{row['product_id']}"[:140]
        item = frappe.get_doc({"doctype": "Item", "item_code": code, "is_stock_item": 0, **fields})
        _set_barcode(item, values["barcode"])
        item.insert(ignore_permissions=True)
        result = "added"
    else:
        item.update(fields)
        if not item.get("barcodes"):
            _set_barcode(item, values["barcode"])
        item.save(ignore_permissions=True)
        result = "updated"
    prices = dict(managed)
    for price_list, key in PRICE_LISTS:
        plan = plans[price_list] if price_list in plans else plan_price(
            item.name, price_list, values[key], item.stock_uom, None)
        if plan is not None:
            prices[price_list] = apply_price(item.name, price_list, plan)
    link_values = {"sync_status": "Synchronized", "target_name": item.name, "source_payload_hash": source,
                   "target_payload_hash": owned_digest(item), "conflict_detail": "",
                   "target_field_hashes": json.dumps({"prices": prices}, sort_keys=True),
                   "mapping_version": MAPPING_VERSION, "last_synced_on": frappe.utils.now_datetime()}
    if link:
        frappe.db.set_value("HERO Integration Link", link.name, link_values)
    else:
        frappe.get_doc({"doctype": "HERO Integration Link", "source_system": "HERO", "source_type": SOURCE_TYPE,
                        "source_id": str(row["product_id"]), "source_instance": source_instance,
                        "target_doctype": "Item", **link_values}).insert(ignore_permissions=True)
    return result


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


def fetch_page(client, offset: int, size: int = PAGE_SIZE) -> list[dict]:
    """One page; a page HERO rejects is read again in smaller parts."""
    try:
        data = client.query(QUERY, {"first": size, "offset": offset})
        return list((data or {}).get("supply_product_versions") or [])
    except Exception:
        if size <= 50:
            raise
        half = size // 2
        return fetch_page(client, offset, half) + fetch_page(client, offset + half, size - half)


def sync_products(client=None, *, source_instance: str | None = None, time_budget: int = TIME_BUDGET_SECONDS,
                  should_stop=None, max_pages: int = 2000) -> dict:
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
    state = _state()
    if state.get("done") or state.get("source_instance") != source_instance:
        state = {}
    state["source_instance"] = source_instance
    counts = state.setdefault("counts", {})
    state.update(done=False, stopped=False, last_error=None)
    links, masters = _links(source_instance), _Masters()
    deadline = time.monotonic() + time_budget
    offset = int(state.get("offset") or 0)
    for _ in range(max_pages):
        if should_stop and should_stop():
            state["stopped"] = True
            break
        rows = fetch_page(client, offset)
        counts["requests"] = counts.get("requests", 0) + 1
        for row in rows:
            key = str(row.get("product_id") or "")
            if not key:
                counts["skipped_no_id"] = counts.get("skipped_no_id", 0) + 1
                continue
            try:
                result = upsert(row, links.get(key), masters, source_instance)
                frappe.db.commit()
            except Exception as error:
                frappe.db.rollback()
                masters.seen.clear()  # a rollback may undo masters created in this article
                result = "failed"
                frappe.log_error(title="HERO product migration failed",
                                 message=f"HERO article {key} ({row.get('nr')}): {error}")
            counts[result] = counts.get(result, 0) + 1
        offset += len(rows)
        state["offset"] = offset
        if len(rows) < PAGE_SIZE:
            state.update(done=True, offset=0)
            break
        _save(state)
        if time.monotonic() > deadline:
            break
    _save(state)
    return state
