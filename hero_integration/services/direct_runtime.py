"""Runtime of the configurable direct migrations (Products, Documents, Pictures).

``resolve_read`` decides which mapping applies to a run: the approved own
mapping of the source (its sealed read query and frozen rules) or, while none
is approved, the built-in mapping with a query generated from its rules. The
importers page through ``read_pages`` and turn each record into target values
with ``record_values``/``price_values``; everything else they do (identity
links, conflicts, masters, download, attachment) stays in their own code.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from hero_integration.services import direct_operations as direct
from hero_integration.services.direct_mapping import MISSING, MappingSpec, lookup

BUILTIN = "builtin"
CATEGORY_IMAGES = "images"


@dataclass(frozen=True)
class DirectRead:
    operation: str
    plan: object                 # mapping_runtime.MappingPlan
    document: str                # GraphQL query
    records_path: str
    variables: dict = field(default_factory=dict)   # variable name → "$page_size" | "$offset" | literal
    page_size: int = 100
    origin: str = BUILTIN        # "builtin" or the approved read service
    mapping: str = ""            # approved HERO GraphQL Mapping, "" for the built-in mapping

    @property
    def mapping_version(self) -> str:
        return getattr(self.plan, "mapping_version", "") or f"{self.operation}:{self.origin}"


# ---------------------------------------------------------------- plans and queries (pure)

def builtin_plan(operation: str):
    from hero_integration.services.mapping_runtime import plan_from_definitions

    rules = [MappingSpec(**rule, definition_id=f"builtin-{operation}-{index}")
             for index, rule in enumerate(direct.DEFAULT_RULES[operation])]
    return plan_from_definitions(operation, rules, target_operation=operation, adapter_options={"version": 2})


def selected_paths(plan) -> set[str]:
    """Every leaf path a read for ``plan`` must select."""
    paths = {spec.source_path for spec in plan.definitions if spec.transformation != "HERO Default Sales Price"}
    return paths | direct.required_paths(plan)


def selection(paths) -> str:
    """GraphQL selection set for dotted leaf paths (``a.b`` → ``a { b }``)."""
    tree: dict = {}
    for path in sorted(paths):
        node = tree
        for part in path.split("."):
            node = node.setdefault(part, {})

    def render(node) -> str:
        return " ".join(name + (" { " + render(child) + " }" if child else "") for name, child in sorted(node.items()))

    return render(tree)


def builtin_read(operation: str) -> DirectRead:
    contract = direct.OPERATIONS[operation]
    plan = builtin_plan(operation)
    fields = selection(selected_paths(plan))
    if contract.get("record_prefix"):  # connection: records are the edges' nodes, ordered by id
        images = ', filters: { category: { equals: "images" } }' if operation == "HeroPictures" else ""
        body = (f"{contract['root']}(first: $first, offset: $offset, sortings: [{contract['order_by']}]{images}) "
                f"{{ edges {{ node {{ {fields} }} }} }}")
    else:
        body = f"{contract['root']}(first: $first, offset: $offset, orderBy: \"{contract['order_by']}\") {{ {fields} }}"
    document = f"query Direct{operation}($first: Int!, $offset: Int!) {{ {body} }}"
    return DirectRead(operation, plan, document, direct.records_path(operation),
                      {"first": "$page_size", "offset": "$offset"}, contract["page_size"])


# ---------------------------------------------------------------- approved own mapping

def approved_read(operation: str, source_instance: str) -> DirectRead | None:
    """The approved own mapping of ``source_instance`` for ``operation``, validated like a native area."""
    import frappe

    from hero_integration import api
    from hero_integration.services.mapping_runtime import active_mapping_plan
    from hero_integration.services.read_contract import from_documents

    services = api._sync_area_read_services(source_instance).get(operation) or []
    if not services:
        return None
    service = frappe.get_doc("HERO GraphQL Service", services[0]["service"])
    mapping = frappe.get_doc("HERO GraphQL Mapping", services[0]["mapping"])
    snapshot = frappe.get_doc("HERO GraphQL Schema Snapshot", services[0]["snapshot"])
    contract = from_documents(service, mapping, snapshot)  # proves the sealed query and the frozen rules
    plan = active_mapping_plan(service.operation_name, definition_ids=contract.definition_ids,
                               target_operation=operation, mapping_name=mapping.name)
    return DirectRead(operation, plan, service.query_document, contract.records_path, dict(contract.variables),
                      contract.page_size, service.name, mapping.name)


def resolve_read(operation: str, source_instance: str) -> DirectRead:
    """The own approved mapping when there is one, otherwise the built-in mapping."""
    if operation not in direct.OPERATIONS:
        raise ValueError("Unknown direct operation: " + str(operation))
    return approved_read(operation, source_instance) or builtin_read(operation)


# ---------------------------------------------------------------- paging (client injected)

def page_variables(read: DirectRead, offset: int, size: int | None = None) -> dict:
    size = size or read.page_size
    return {name: size if value == "$page_size" else offset if value == "$offset" else value
            for name, value in read.variables.items()}


def records(read: DirectRead, data) -> list[dict]:
    """The records of one response; ``a.edges.node`` unwraps a connection's edge list."""
    path = read.records_path
    unwrap = ""
    if path.endswith(".edges.node"):
        path, unwrap = path[:-len(".node")], "node"
    value = lookup(data or {}, path)
    if value is MISSING or value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        raise ValueError("HERO returned an invalid record list for " + read.operation + ".")
    if unwrap:
        value = [row.get(unwrap) for row in value]
        if not all(isinstance(row, dict) for row in value):
            raise ValueError("HERO returned an invalid record list for " + read.operation + ".")
    return value


def fetch_page(client, read: DirectRead, offset: int, size: int | None = None) -> list[dict]:
    """One page; a page HERO rejects is read again in two halves (down to 50 records)."""
    size = size or read.page_size
    try:
        return records(read, client.query(read.document, page_variables(read, offset, size)))
    except ValueError:
        raise
    except Exception:
        if size <= 50:
            raise
        half = size // 2
        return fetch_page(client, read, offset, half) + fetch_page(client, read, offset + half, size - half)


def belongs(operation: str, record: dict) -> bool:
    """Documents and Pictures share file_uploads; the category decides."""
    if operation == "HeroPictures":
        return record.get("category") == CATEGORY_IMAGES
    if operation == "HeroDocuments":
        return record.get("category") != CATEGORY_IMAGES
    return True


# ---------------------------------------------------------------- values

def identity(read: DirectRead, record: dict) -> str:
    return read.plan.source_identity(record)


def record_values(plan, record: dict, target_doctype: str) -> dict:
    """Target values of one record for ``target_doctype``; None means the source gave no value.

    Fields whose source path is missing from the record are left out."""
    from hero_integration.services.query_mapping import _check_value, transform

    values = {}
    for spec in plan.definitions:
        if spec.target_doctype != target_doctype or spec.purpose == "Source Identity":
            continue
        raw = lookup(record, spec.source_path)
        if raw is MISSING:
            continue
        _check_value(spec, raw)
        if spec.transformation in direct.TRANSFORMS:
            value = direct.resolve(spec.transformation, raw, record)
        else:
            value = transform(spec, raw, plan.adapter_options)
        values[spec.target_field] = None if value is MISSING or value == "" else value
    return values


def price_values(plan, record: dict) -> dict:
    """Price per price list (``relation``) of one product record; None = no price."""
    result = {}
    for spec in plan.definitions:
        if spec.target_doctype != "Item Price":
            continue
        raw = lookup(record, spec.source_path)
        if raw is MISSING:
            raw = None
        result[spec.relation] = direct.resolve(spec.transformation, raw, record) \
            if spec.transformation in direct.TRANSFORMS else direct.price(raw)
    return result
