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
MAPPING_VERSION = "product-migration-v1"
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


def _links() -> dict:
    rows = frappe.get_all("HERO Integration Link", filters={"source_type": SOURCE_TYPE},
                          fields=["name", "source_id", "target_name", "source_payload_hash", "target_payload_hash"],
                          limit_page_length=0)
    return {row.source_id: row for row in rows}


def upsert(row: dict, link, masters: _Masters) -> str:
    """Create or update one Item; returns added, updated, unchanged or conflict."""
    values = item_values(row)
    source = digest(values)
    if link and link.source_payload_hash == source:
        return "unchanged"
    item = frappe.get_doc("Item", link.target_name) if link and frappe.db.exists("Item", link.target_name) else None
    if item is not None and link.target_payload_hash and owned_digest(item) != link.target_payload_hash:
        frappe.db.set_value("HERO Integration Link", link.name, {
            "sync_status": "Conflict", "observed_source_hash": source, "last_observed_on": frappe.utils.now_datetime(),
            "conflict_detail": "The Item was changed in ERPNext; the newer HERO article was not applied."})
        return "conflict"
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
    _set_price(item.name, BUYING, values["buying"], item.stock_uom)
    _set_price(item.name, SELLING, values["selling"], item.stock_uom)
    link_values = {"sync_status": "Synchronized", "target_name": item.name, "source_payload_hash": source,
                   "target_payload_hash": owned_digest(item), "conflict_detail": "",
                   "last_synced_on": frappe.utils.now_datetime()}
    if link:
        frappe.db.set_value("HERO Integration Link", link.name, link_values)
    else:
        frappe.get_doc({"doctype": "HERO Integration Link", "source_system": "HERO", "source_type": SOURCE_TYPE,
                        "source_id": str(row["product_id"]), "source_instance": "default", "target_doctype": "Item",
                        "mapping_version": MAPPING_VERSION, **link_values}).insert(ignore_permissions=True)
    return result


def _set_barcode(item, code: str | None) -> None:
    if code and not frappe.db.exists("Item Barcode", {"barcode": code}):
        item.append("barcodes", {"barcode": code, "barcode_type": barcode_type(code)})


def _set_price(item_code: str, price_list: str, rate: float | None, uom: str) -> None:
    if rate is None or not frappe.db.exists("Price List", price_list):
        return
    name = frappe.db.get_value("Item Price", {"item_code": item_code, "price_list": price_list}, "name")
    if name:
        if frappe.db.get_value("Item Price", name, "price_list_rate") != rate:
            frappe.db.set_value("Item Price", name, "price_list_rate", rate)
        return
    frappe.get_doc({"doctype": "Item Price", "item_code": item_code, "price_list": price_list,
                    "price_list_rate": rate, "uom": uom}).insert(ignore_permissions=True)


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
    return HeroGraphQLClient(config.endpoint, lambda: config.api_token, config.timeout_seconds, max_retries=2)


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


def sync_products(client=None, *, time_budget: int = TIME_BUDGET_SECONDS, should_stop=None, max_pages: int = 2000) -> dict:
    """Process articles from the saved offset until done, stopped or the time budget ends."""
    client = client or _client()
    state = _state()
    if state.get("done"):
        state = {}
    counts = state.setdefault("counts", {})
    state.update(done=False, stopped=False, last_error=None)
    links, masters = _links(), _Masters()
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
                result = upsert(row, links.get(key), masters)
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
