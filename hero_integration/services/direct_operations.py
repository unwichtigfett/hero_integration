"""Configurable mappings for the direct migrations: HERO articles and files.

Products (``supply_product_versions`` → Item) and Documents/Pictures
(``file_uploads`` → File) are query-adapter v2 operations like the native
areas: a saved read query, a mapping with free field rules and an approval.
This module is the single registry of what is configurable for them and what
stays fixed in code (identity, deletion, download, storage, attachment to the
Project/Customer, source binding, price conflict protection).

``DEFAULT_RULES`` supplies editable mapping templates. Runtime execution requires
an explicitly saved and approved mapping and read service; these defaults never
substitute for missing or unapproved configuration.
"""
from __future__ import annotations

import math

from hero_integration.services.service_schema import SCALARS as SERVICE_SCALARS

OPERATIONS = {
    "HeroEmployees": {
        "root": "partners", "owner": "Employee", "area": "Employees", "unpaged": True,
        "identity": ("id", "Positive Int To Text", "int"),
        "required_paths": frozenset({"id", "user_id", "user.id"}),
        "targets": frozenset({"Employee", "User", "HERO Integration Link"}),
        "record_name": ("Employee", "employee_name"), "page_size": 100,
    },
    "HeroServices": {
        "root": "supply_services", "owner": "Item", "area": "Services",
        "identity": ("id", "Positive Int To Text", "int"),
        "required_paths": frozenset({"id", "company_id", "is_deleted", "modified"}),
        "targets": frozenset({"Item", "Item Price", "HERO Integration Link"}),
        "record_name": ("Item", "item_name"), "order_by": "id", "page_size": 100,
    },
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
    "Employee": frozenset({"first_name", "last_name", "employee_name", "company_email", "personal_email",
                           "cell_number", "status", "gender", "date_of_birth", "current_address"}),
    "User": frozenset({"email", "phone", "mobile_no", "language", "email_signature", "custom_hero_role",
                       "custom_hero_last_login", "custom_hero_created", "custom_hero_modified"}),
    "Item": frozenset({"item_code", "item_name", "description", "item_group", "stock_uom", "brand", "disabled", "barcodes",
                       "supplier_items", "min_order_qty", "lead_time_days"}),
    "Item Price": frozenset({"price_list_rate"}),
    "File": frozenset({"file_name", "file_type"}),
}
# Coded resolvers; each may only feed its one native field.
TRANSFORM_TARGETS = {
    "Employee Status": ("Employee", "status"),
    "Employee Language": ("User", "language"),
    "Service Price": ("Item Price", "price_list_rate"),
    "HERO Item Group": ("Item", "item_group"),       # catalog name → Item Group below "HERO Artikel"
    "HERO Category Group": ("Item", "item_group"),   # category → Item Group; without category the catalog
    "HERO Supplier": ("Item", "supplier_items"),     # supplier name (+ its article number) → Item Supplier row
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
CATALOG_PATH = "base_data.supply_catalog.name"
SUPPLIER_PART_PATH = "base_data.supplier_id"


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
    if spec.operation == "HeroServices" and spec.target_doctype == "Item" and spec.target_field in {
            "item_group", "supplier_items", "custom_hero_service_calculation"}:
        raise ValueError("Query target is protected or outside the supported adapter fields.")
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
    coded = {target for name, target in TRANSFORM_TARGETS.items() if not name.startswith("Employee ")}
    if (spec.target_doctype, spec.target_field) in coded and not expected:
        raise ValueError("{0}.{1} requires its coded HERO resolver.".format(spec.target_doctype, spec.target_field))
    if spec.target_doctype == "Item Price":
        if spec.operation == "HeroServices" and spec.relation != "Standard Selling":
            raise ValueError("Service prices require Standard Selling.")
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
    if (plan.target_operation or plan.operation) == "HeroEmployees":
        required = {("Employee", "first_name"), ("Employee", "status"), ("User", "email")}
        if not required.issubset({(d.target_doctype, d.target_field) for d in plan.definitions}):
            raise ValueError("Employee mappings require first name, status and user login email.")
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
        if spec.transformation == "HERO Category Group":
            paths.add(CATALOG_PATH)
        if spec.transformation == "HERO Supplier":
            paths.add(SUPPLIER_PART_PATH)
    return paths


# ---------------------------------------------------------------- coded resolvers (pure)

def resolve(transformation: str, value, record: dict | None = None):
    """Value of a coded resolver; None means "no value" (nothing is written)."""
    if transformation == "Employee Status":
        states = {"active": "Active", "inactive": "Inactive", "suspended": "Suspended", "left": "Left"}
        if value is not None and value not in states:
            raise ValueError("Employee status requires an explicit mapping.")
        return states.get(value)
    if transformation == "Employee Language":
        return value.strip() if isinstance(value, str) else None
    if transformation in {"HERO Item Group", "HERO Brand"}:
        text = value.strip()[:140] if isinstance(value, str) else ""
        if transformation == "HERO Item Group":
            return text or UNCATALOGUED_GROUP
        return text or None
    if transformation == "HERO Category Group":
        text = value.strip()[:140] if isinstance(value, str) else ""
        return text or resolve("HERO Item Group", _path(record, CATALOG_PATH))
    if transformation == "HERO Supplier":
        name = value.strip()[:140] if isinstance(value, str) else ""
        if not name:
            return None
        from hero_integration.services.direct_mapping import MISSING, lookup
        part = lookup(record or {}, SUPPLIER_PART_PATH)
        if part is MISSING:
            return {"supplier": name}
        if part is not None and not isinstance(part, str):
            raise ValueError("HERO supplier article number must be text or null.")
        return {"supplier": name, "supplier_part_no": part.strip()[:140] if isinstance(part, str) and part.strip() else None}
    if transformation == "HERO UOM":
        text = value.strip()[:140] if isinstance(value, str) else ""
        return text or DEFAULT_UOM
    if transformation == "HERO EAN Barcode":
        text = "".join(value.split())[:140] if isinstance(value, str) else ""
        return text or None
    if transformation == "Service Price":
        return price(value, allow_zero=True)
    if transformation == "HERO Price":
        return price(value)
    if transformation == "HERO Default Sales Price":
        for row in value if isinstance(value, list) else []:
            if isinstance(row, dict) and (row.get("supply_sales_price") or {}).get("is_default"):
                return price(row.get("net_price_per_unit"))
        return price((record or {}).get("list_price"))
    raise ValueError("Unknown HERO resolver: " + transformation)


def _path(record, path: str):
    value = record or {}
    for part in path.split("."):
        value = value.get(part) if isinstance(value, dict) else None
    return value


def price(value, *, allow_zero=False) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = round(float(value), 2)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and (number >= 0 if allow_zero else number > 0) else None


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
    "HeroEmployees": (
        _rule("HeroEmployees", "id", "HERO Integration Link", "source_id", "int", "Source Identity", "Positive Int To Text"),
        _rule("HeroEmployees", "first_name", "Employee", "first_name", "string|null"),
        _rule("HeroEmployees", "last_name", "Employee", "last_name", "string|null"),
        _rule("HeroEmployees", "full_name", "Employee", "employee_name", "string|null", "Record Name"),
        _rule("HeroEmployees", "email", "Employee", "company_email", "string|null"),
        _rule("HeroEmployees", "mobile", "Employee", "cell_number", "string|null"),
        _rule("HeroEmployees", "role", "Employee", "custom_hero_partner_role", "string|null"),
        _rule("HeroEmployees", "account_type", "Employee", "custom_hero_account_type", "string|null"),
        _rule("HeroEmployees", "status", "Employee", "status", "string|null", "Field", "Employee Status"),
        _rule("HeroEmployees", "user.email", "User", "email", "string|null"),
        _rule("HeroEmployees", "phone", "User", "phone", "string|null"),
        _rule("HeroEmployees", "user.locale", "User", "language", "string|null", "Field", "Employee Language"),
        *(_rule("HeroEmployees", "user." + name, "User", "custom_hero_" + name, "string|null")
          for name in ("role", "last_login", "created", "modified")),
    ),
    "HeroServices": (
        _rule("HeroServices", "id", "HERO Integration Link", "source_id", "int", "Source Identity", "Positive Int To Text"),
        _rule("HeroServices", "nr", "Item", "item_code", "string|null"),
        _rule("HeroServices", "name", "Item", "item_name", "string|null", "Record Name"),
        _rule("HeroServices", "description", "Item", "description", "string|null", "Field", "Safe HTML"),
        _rule("HeroServices", "unit_type", "Item", "stock_uom", "string|null", "Field", "HERO UOM"),
        _rule("HeroServices", "manufacturer", "Item", "brand", "string|null", "Field", "HERO Brand"),
        _rule("HeroServices", "ean", "Item", "barcodes", "string|null", "Field", "HERO EAN Barcode"),
        _rule("HeroServices", "is_deleted", "Item", "disabled", "boolean|null", "Field", "Identity"),
        _rule("HeroServices", "net_price_per_unit", "Item Price", "price_list_rate", "float|null", "Field", "Service Price", "Standard Selling"),
        *(_rule("HeroServices", source, "Item", "custom_hero_service_" + source,
                {"Data": "string|null", "Float": "float|null", "Check": "boolean|null"}[kind], "Field", "Identity")
          for source, (kind, _label) in SERVICE_SCALARS.items()),
    ),
    "HeroProducts": (
        _rule("HeroProducts", "product_id", "HERO Integration Link", "source_id", "string", "Source Identity",
              "Nonempty String ID"),
        _rule("HeroProducts", "base_data.name", "Item", "item_name", "string|null", "Record Name", "Trim"),
        _rule("HeroProducts", "base_data.description", "Item", "description", "string|null", "Field", "Safe HTML"),
        _rule("HeroProducts", "base_data.category", "Item", "item_group", "string|null", "Field",
              "HERO Category Group"),
        _rule("HeroProducts", "base_data.unit_type", "Item", "stock_uom", "string|null", "Field", "HERO UOM"),
        _rule("HeroProducts", "base_data.manufacturer", "Item", "brand", "string|null", "Field", "HERO Brand"),
        _rule("HeroProducts", "is_deleted", "Item", "disabled", "boolean|null", "Field", "Identity"),
        _rule("HeroProducts", "base_data.ean", "Item", "barcodes", "string|null", "Field", "HERO EAN Barcode"),
        _rule("HeroProducts", "supply_operator.name", "Item", "supplier_items", "string|null", "Field", "HERO Supplier"),
        _rule("HeroProducts", "base_data.manufacturer_nr", "Item", "custom_hero_manufacturer_nr", "string|null",
              "Field", "Trim"),
        _rule("HeroProducts", "nr", "Item", "item_code", "string|null", "Field", "Trim"),
        # Native order data: minimum order quantity and delivery time in days.
        _rule("HeroProducts", "base_data.quantity_min", "Item", "min_order_qty", "float|null", "Field", "Number"),
        _rule("HeroProducts", "base_data.delivery_time", "Item", "lead_time_days", "int|null", "Field", "Integer"),
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
