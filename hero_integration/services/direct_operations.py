"""Configurable mappings for the direct migrations: HERO articles and files.

Products (``supply_product_versions`` → Item) and Documents/Pictures
(``file_uploads`` → File) are query-adapter v2 operations like the native
areas: a saved read query, a mapping with free field rules and an approval.
This module is the single registry of what is configurable for them and what
stays fixed in code (identity, deletion, download, storage, attachment to the
Project/Customer, source binding, price conflict protection).

``DEFAULT_RULES`` is the built-in mapping: it reproduces the coded behaviour
and is used while no approved mapping exists. It is shown read-only under
Mapping → Active Mappings.
"""
from __future__ import annotations

import math

OPERATIONS = {
    "HeroProducts": {
        "root": "supply_product_versions", "owner": "Item", "area": "Products",
        "identity": ("product_id", "Nonempty String ID", "string"),
        # Selected by every product read: identity, deletion and the price fallback.
        "required_paths": frozenset({"product_id", "is_deleted"}),
        "targets": frozenset({"Item", "Item Price", "HERO Integration Link"}),
        "record_name": ("Item", "item_name"),
        "order_by": "product_id",
        "page_size": 500,  # HERO returns at most 500 article versions per request
    },
    "HeroDocuments": {
        # file_uploads answers only for given uuids; the list comes from the connection,
        # records are its edges' nodes. Project/Customer attachment details are read by
        # the importer's fixed detail query (file_uploads(uuids)).
        "root": "FileUpload_FileUploads", "record_prefix": "edges.node", "owner": "File", "area": "Documents",
        "identity": ("id", "Positive Int To Text", "int"),
        # Download, change detection, deletion check and the Documents/Pictures split.
        "required_paths": frozenset({"id", "uuid", "category", "downloadUrl", "size", "modified", "created"}),
        "targets": frozenset({"File", "HERO Integration Link"}),
        "record_name": ("File", "file_name"),
        "order_by": "ID_ASC",
        "page_size": 200,
    },
}
OPERATIONS["HeroPictures"] = {**OPERATIONS["HeroDocuments"], "area": "Pictures"}


def records_path(name: str) -> str:
    """Where the records of an operation sit in its response (root, or root plus record prefix)."""
    contract = OPERATIONS[name]
    return contract["root"] + ("." + contract["record_prefix"] if contract.get("record_prefix") else "")

# Configurable target fields per DocType (besides app-owned custom_hero_* fields of the owner).
FIELDS = {
    "Item": frozenset({"item_name", "description", "item_group", "stock_uom", "brand", "disabled", "barcodes"}),
    "Item Price": frozenset({"price_list_rate"}),
    "File": frozenset({"file_name"}),
}
# Coded resolvers; each may only feed its one native field.
TRANSFORM_TARGETS = {
    "HERO Item Group": ("Item", "item_group"),       # catalog name → Item Group below "HERO Artikel"
    "HERO UOM": ("Item", "stock_uom"),               # unit text → UOM (created when missing)
    "HERO Brand": ("Item", "brand"),                 # manufacturer → Brand (created when missing)
    "HERO EAN Barcode": ("Item", "barcodes"),        # EAN → one Item Barcode row, only when unique
    "HERO Price": ("Item Price", "price_list_rate"),
    "HERO Default Sales Price": ("Item Price", "price_list_rate"),  # default sales price, else list_price
}
TRANSFORMS = frozenset(TRANSFORM_TARGETS)
# Item Price rules name their price list in ``relation``; the import keeps its conflict protection.
PRICE_LISTS = frozenset({"Standard Buying", "Standard Selling"})
DEFAULT_UOM = "Stk"
UNCATALOGUED_GROUP = "Ohne Katalog"


def operation(name: str) -> dict | None:
    return OPERATIONS.get(name)


def owner(name: str) -> str:
    return OPERATIONS[name]["owner"]


def validate_spec(spec) -> None:
    """Rules of a direct operation; raises ValueError like ``validate_v2_spec``."""
    contract = OPERATIONS[spec.operation]
    if spec.target_doctype not in contract["targets"]:
        raise ValueError("Query rule crosses an unsupported native relationship.")
    if spec.target_doctype == "HERO Integration Link":
        identity_path, identity_transform, _ = contract["identity"]
        if (spec.purpose, spec.target_field, spec.source_path, spec.transformation) != (
                "Source Identity", "source_id", identity_path, identity_transform):
            raise ValueError("The identity of this HERO object is fixed: {0} → HERO Integration Link.source_id.".format(
                identity_path))
        return
    if spec.purpose == "Source Identity":
        raise ValueError("Source identity may only bind its technical identity field.")
    if spec.target_field not in FIELDS[spec.target_doctype] and not (
            spec.target_doctype == contract["owner"] and spec.target_field.startswith("custom_hero_")):
        raise ValueError("Query target is protected or outside the supported adapter fields.")
    if spec.purpose == "Record Name" and (spec.target_doctype, spec.target_field) != contract["record_name"]:
        raise ValueError("Record Name must bind the native record title.")
    expected = TRANSFORM_TARGETS.get(spec.transformation)
    if expected and expected != (spec.target_doctype, spec.target_field):
        raise ValueError("{0} may only feed {1}.{2}.".format(spec.transformation, *expected))
    coded = {target for target in TRANSFORM_TARGETS.values()}
    if (spec.target_doctype, spec.target_field) in coded and not expected:
        raise ValueError("{0}.{1} requires its coded HERO resolver.".format(spec.target_doctype, spec.target_field))
    if spec.target_doctype == "Item Price":
        if spec.relation not in PRICE_LISTS:
            raise ValueError("An Item Price rule must name its price list (Standard Buying or Standard Selling).")
    elif spec.relation:
        raise ValueError("Only Item Price rules name a relation.")


def validate_plan(plan) -> None:
    """Plan-level rules of a direct operation (called from query_mapping.validate_plan)."""
    contract = OPERATIONS[plan.target_operation or plan.operation]
    identities = [d for d in plan.definitions if d.purpose == "Source Identity"]
    path, transformation, _ = contract["identity"]
    if len(identities) != 1 or (identities[0].source_path, identities[0].transformation) != (path, transformation):
        raise ValueError("The identity of this HERO object is fixed: {0}.".format(path))
    if len([d for d in plan.definitions if d.purpose == "Record Name"]) != 1:
        raise ValueError("Native intake requires a record-name binding.")
    prices = [d.relation for d in plan.definitions if d.target_doctype == "Item Price"]
    if len(set(prices)) != len(prices):
        raise ValueError("Each price list may be fed by one rule only.")


def target_key(spec) -> tuple:
    """Uniqueness key of a rule target; price rules differ by their price list."""
    return (spec.target_doctype, spec.target_field, spec.relation if spec.target_doctype == "Item Price" else "")


def required_paths(plan) -> set[str]:
    paths = set(OPERATIONS[plan.target_operation or plan.operation]["required_paths"])
    for spec in plan.definitions:
        if spec.transformation == "HERO Default Sales Price":
            paths |= {"sales_prices.net_price_per_unit", "sales_prices.supply_sales_price.is_default", "list_price"}
            paths.discard(spec.source_path)
    return paths


# ---------------------------------------------------------------- coded resolvers (pure)

def resolve(transformation: str, value, record: dict | None = None):
    """Value of a coded resolver; None means "no value" (nothing is written)."""
    if transformation in {"HERO Item Group", "HERO Brand"}:
        text = value.strip()[:140] if isinstance(value, str) else ""
        if transformation == "HERO Item Group":
            return text or UNCATALOGUED_GROUP
        return text or None
    if transformation == "HERO UOM":
        text = value.strip()[:140] if isinstance(value, str) else ""
        return text or DEFAULT_UOM
    if transformation == "HERO EAN Barcode":
        text = "".join(value.split())[:140] if isinstance(value, str) else ""
        return text or None
    if transformation == "HERO Price":
        return price(value)
    if transformation == "HERO Default Sales Price":
        for row in value if isinstance(value, list) else []:
            if isinstance(row, dict) and (row.get("supply_sales_price") or {}).get("is_default"):
                return price(row.get("net_price_per_unit"))
        return price((record or {}).get("list_price"))
    raise ValueError("Unknown HERO resolver: " + transformation)


def price(value) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = round(float(value), 2)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


# ---------------------------------------------------------------- built-in default mapping

def _rule(operation, source_path, target_doctype, target_field, source_type, purpose="Field",
          transformation="Trim", relation=""):
    return {"operation": operation, "source_path": source_path, "target_doctype": target_doctype,
            "target_field": target_field, "source_type": source_type, "purpose": purpose,
            "transformation": transformation, "relation": relation}


_FILE_RULES = lambda op: (  # noqa: E731 - same rules for Documents and Pictures
    _rule(op, "id", "HERO Integration Link", "source_id", "int", "Source Identity", "Positive Int To Text"),
    _rule(op, "filename", "File", "file_name", "string", "Record Name", "Trim"),
)

DEFAULT_RULES = {
    "HeroProducts": (
        _rule("HeroProducts", "product_id", "HERO Integration Link", "source_id", "string", "Source Identity",
              "Nonempty String ID"),
        _rule("HeroProducts", "base_data.name", "Item", "item_name", "string|null", "Record Name", "Trim"),
        _rule("HeroProducts", "base_data.description", "Item", "description", "string|null", "Field", "Safe HTML"),
        _rule("HeroProducts", "base_data.supply_catalog.name", "Item", "item_group", "string|null", "Field",
              "HERO Item Group"),
        _rule("HeroProducts", "base_data.unit_type", "Item", "stock_uom", "string|null", "Field", "HERO UOM"),
        _rule("HeroProducts", "base_data.manufacturer", "Item", "brand", "string|null", "Field", "HERO Brand"),
        _rule("HeroProducts", "is_deleted", "Item", "disabled", "boolean|null", "Field", "Identity"),
        _rule("HeroProducts", "base_data.ean", "Item", "barcodes", "string|null", "Field", "HERO EAN Barcode"),
        _rule("HeroProducts", "base_price", "Item Price", "price_list_rate", "float|null", "Field", "HERO Price",
              "Standard Buying"),
        _rule("HeroProducts", "sales_prices", "Item Price", "price_list_rate", "json", "Field",
              "HERO Default Sales Price", "Standard Selling"),
    ),
    "HeroDocuments": _FILE_RULES("HeroDocuments"),
    "HeroPictures": _FILE_RULES("HeroPictures"),
}


def default_rows() -> list[dict]:
    """The built-in mapping as read-only rows for Mapping → Active Mappings."""
    return [{**rule, "status": "Built-in", "builtin": 1, "definition_id": f"builtin:{rule['operation']}:{index}"}
            for name, rules in DEFAULT_RULES.items() for index, rule in enumerate(rules)]
