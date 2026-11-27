"""Publish per-mapping compatibility evidence without rewriting an approved definition or binding."""
from contextlib import contextmanager
from contextvars import ContextVar
from hashlib import sha256
import json

from hero_integration.services.mapping_schema_compatibility import check_compatibility


_read_schemas = ContextVar("hero_compatible_reader_schemas", default=None)


def _digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


@contextmanager
def read_scope(frappe, selection):
    """Also check actual dynamically narrowed and auxiliary queries before transport."""
    schemas = None
    if getattr(selection, "validated_snapshot", ""):
        schemas = tuple(frappe.get_doc("HERO GraphQL Schema Snapshot", name).snapshot_json
                        for name in (selection.schema_snapshot, selection.validated_snapshot))
    token = _read_schemas.set((schemas, set()) if schemas else None)
    try:
        yield
    finally:
        _read_schemas.reset(token)


def validate_read_document(document):
    scope = _read_schemas.get()
    if scope is None:
        return
    schemas, checked = scope
    key = sha256(document.encode()).digest()
    if key not in checked:
        check_compatibility(*(json.loads(raw) for raw in schemas), read_queries=[document],
                            mutation_queries=[], mutation_profiles=[])
        checked.add(key)


def _readers(content):
    if isinstance(content, dict):
        for key, value in content.items():
            if (key.endswith("_service") or key == "read_service") and isinstance(value, str) and value:
                yield value
            else:
                yield from _readers(value)
    elif isinstance(content, list):
        for value in content:
            yield from _readers(value)


def _graph(frappe, mapping, child):
    from hero_integration.services.mapping_snapshot_rebase import _binding, MAPPING, SERVICE, CONTRACT
    from hero_integration.services.mapping_runtime import effective_rule_fingerprint

    content = json.loads(mapping.mapping_json)
    maps = {mapping.name: mapping}
    services, rules = {}, {}
    pending = set(_readers(content))
    if mapping.direction == "QUERY_TO_FRAPPE":
        names = frappe.get_all(SERVICE, filters={"read_mapping": mapping.name, "status": "Validated",
            "execution_status": "Approved", "schema_snapshot": mapping.schema_snapshot}, pluck="name")
        if len(names) != 1:
            raise ValueError("Expected one approved reader for " + mapping.name)
        pending.add(names[0])
    while pending:
        name = pending.pop()
        if name in services:
            continue
        service = frappe.get_doc(SERVICE, name)
        parent = frappe.get_doc(MAPPING, service.read_mapping)
        if (parent.status != "Active" or parent.direction != "QUERY_TO_FRAPPE"
                or parent.schema_snapshot != mapping.schema_snapshot or parent.source_instance != mapping.source_instance
                or service.status != "Validated" or service.execution_status != "Approved"
                or service.schema_snapshot != parent.schema_snapshot
                or service.approved_mapping_revision != parent.mapping_revision
                or service.approved_service_revision != service.service_revision):
            raise ValueError("Reader approval differs for " + name)
        maps[parent.name] = parent
        services[name] = service
        config = json.loads(parent.mapping_json)
        rules[parent.name] = effective_rule_fingerprint(config["mapping_definition_ids"], parent.name, frappe_module=frappe)
        pending.update(_readers(config))
    contracts = []
    mutation_services = []
    if mapping.direction == "FRAPPE_TO_MUTATION":
        rows = frappe.get_all(CONTRACT, filters={"schema_snapshot": mapping.schema_snapshot,
                              "status": "Active", "operation_name": mapping.operation_name}, pluck="name")
        if len(rows) != 1 or not services:
            raise ValueError("Missing unique mutation contract or approved readback for " + mapping.name)
        contract = frappe.get_doc(CONTRACT, rows[0])
        contracts.append(contract)
        mutation_services.append(frappe.get_doc(SERVICE, contract.graphql_service))
    docs = [*maps.values(), *services.values(), *contracts, *mutation_services]
    anchors = sorted((_binding(doc, child)["state_hash"], doc.name) for doc in docs)
    inputs = (mapping.name, rules, anchors)
    key = _receipt_key(child.name, child.schema_fingerprint, inputs)
    return key, list(maps.values()), list(services.values()), contracts, mutation_services, inputs


def _receipt_key(snapshot_name, fingerprint, inputs):
    return _digest(["contract-adoption-v1", snapshot_name, fingerprint, *inputs])


def _require_same_lineage(frappe, mapping, inputs):
    from hero_integration.services.snapshot_inheritance import INHERITANCE_DOCTYPE
    previous = frappe.get_all(INHERITANCE_DOCTYPE, filters={"parent_mapping": mapping.name,
        "parent_snapshot": mapping.schema_snapshot, "status": "Compatible Contract"},
        fields=["name", "child_snapshot"], order_by="creation desc", limit_page_length=1)
    if previous:
        prior = previous[0]
        fingerprint = frappe.db.get_value("HERO GraphQL Schema Snapshot", prior.child_snapshot, "schema_fingerprint")
        if _receipt_key(prior.child_snapshot, fingerprint, inputs) != prior.name:
            raise ValueError("Approved mapping graph changed since its last compatibility review: " + mapping.name)


def _targets(mapping):
    import frappe
    from hero_integration.services.personal_mapping_content import RULE_FIELDS
    from hero_integration.services.direct_mapping import MappingSpec, validate_spec
    from hero_integration.services.query_mapping import validate_v2_spec
    from hero_integration.hero_integration.doctype.hero_frappe_mapping.hero_frappe_mapping import _FrappeMetadata

    config = json.loads(mapping.mapping_json)
    ids = config["mapping_definition_ids"]
    rows = frappe.get_all("HERO Frappe Mapping", filters={"definition_id": ["in", ids], "status": "Active",
        "schema_snapshot": mapping.schema_snapshot}, fields=["definition_id", *sorted(RULE_FIELDS)], limit_page_length=0)
    if len(rows) != len(ids):
        raise ValueError("Missing active field rules for " + mapping.name)
    for row in rows:
        spec = MappingSpec(operation=config["target_operation"], **row)
        try:
            if config.get("query_adapter"):
                validate_v2_spec(spec, _FrappeMetadata(), config["query_adapter"])
            else:
                validate_spec(spec, _FrappeMetadata())
        except ValueError as error:
            raise ValueError(f"{row.definition_id} {row.target_doctype}.{row.target_field}: {error}") from error


def _prove(mapping, old, child, graph):
    from hero_integration.services.mapping_snapshot_rebase import _validate_group
    from hero_integration.services.personal_mapping_content import configured_mutation_helpers
    _, mappings, readers, contracts, mutations, _ = graph
    old_raw, new_raw = json.loads(old.snapshot_json), json.loads(child.snapshot_json)
    _validate_group(mappings, readers, contracts, readers + mutations, {old.name: old})
    for parent in mappings:
        if parent.direction == "QUERY_TO_FRAPPE":
            _targets(parent)
    profiles = []
    if mutations:
        content = json.loads(mapping.mapping_json)
        normalise, validate = configured_mutation_helpers(content.get("usage"))
        normal = normalise(content, new_raw)
        if normal != normalise(content, old_raw):
            raise ValueError("Mutation input semantics changed for " + mapping.name)
        validate(normal)
        profiles.append(content)
    return check_compatibility(old_raw, new_raw, read_queries=[r.query_document for r in sorted(readers,key=lambda r:r.name)],
        mutation_profiles=profiles, mutation_queries=[s.query_document for s in mutations])["dependency_fingerprint"]


def is_confirmed(frappe, latest, selection):
    """A current schema alone cannot transfer approval. Bind the receipt to every live contract input."""
    from hero_integration.services.snapshot_inheritance import INHERITANCE_DOCTYPE
    name = getattr(selection, "graphql_mapping", None)
    if not isinstance(name, str) or not name:
        return False
    if hasattr(selection, "validated_snapshot") and selection.validated_snapshot != latest["name"]:
        return False
    child = frappe.get_doc("HERO GraphQL Schema Snapshot", latest["name"])
    mapping = frappe.get_doc("HERO GraphQL Mapping", name)
    if (mapping.status != "Active" or mapping.schema_snapshot != selection.schema_snapshot
            or mapping.schema_fingerprint != selection.schema_fingerprint or child.source_instance != mapping.source_instance):
        return False
    key, mappings, _, _, _, _ = _graph(frappe, mapping, child)
    if frappe.db.get_value(INHERITANCE_DOCTYPE, key, "status") != "Compatible Contract":
        return False
    for parent in mappings:
        if parent.direction == "QUERY_TO_FRAPPE":
            _targets(parent)
    return True


def adopt(snapshot_name, actor):
    """One transaction publishes immutable receipts and the new generation of the existing plan."""
    import frappe
    from hero_integration.services.mapping_snapshot_rebase import _snapshot, MAPPING
    from hero_integration.services.snapshot_inheritance import INHERITANCE_DOCTYPE
    from hero_integration.services.sync_runtime_adapter import _runtime_plan_service
    from hero_integration.hero_integration.doctype.hero_graphql_mapping.hero_graphql_mapping import lock_exchange_context
    frappe.only_for("System Manager")
    if actor != frappe.session.user:
        raise PermissionError("Schema adoption requires the current session actor.")
    child, _, _ = _snapshot(snapshot_name, lock=True, require_compatible=False)
    cfg = frappe.get_single("HERO Integration Settings")
    from hero_integration.services.sync_runtime_adapter import _require_confirmed_source_binding
    _require_confirmed_source_binding(cfg)
    from hero_integration.client.settings import get_configuration
    from hero_integration.services.introspection import endpoint_fingerprint
    conn = get_configuration(fresh=True)
    if (not conn.source_binding_confirmed or conn.source_instance != child.source_instance
            or child.endpoint_fingerprint != endpoint_fingerprint(conn.endpoint.url, conn.source_instance)):
        raise ValueError("Snapshot must belong to the confirmed endpoint.")
    latest = frappe.get_all(child.doctype, filters={"source_instance":child.source_instance,
        "endpoint_fingerprint":child.endpoint_fingerprint,"analysis_status":"Validated"},pluck="name",
        order_by="creation desc, name desc",limit_page_length=1)
    if latest != [child.name]:
        raise ValueError("Only the current validated snapshot can receive compatibility evidence.")
    parents = frappe.get_all(MAPPING, filters={"source_instance":child.source_instance,"status":"Active"},
                             fields=["name","schema_snapshot","direction"],order_by="name",limit_page_length=0)
    for scope in sorted({(r.schema_snapshot,r.direction) for r in parents}):
        lock_exchange_context(child.source_instance,*scope)
    result, originals = {}, {}
    frappe.db.savepoint("hero_schema_adoption")
    try:
        for row in parents:
            if row.schema_snapshot == child.name:
                continue
            mapping = frappe.get_doc(MAPPING,row.name,for_update=True)
            if mapping.status != "Active" or mapping.schema_snapshot != row.schema_snapshot:
                raise ValueError("Mapping graph changed during adoption.")
            if row.schema_snapshot not in originals:
                originals[row.schema_snapshot] = _snapshot(row.schema_snapshot, child.source_instance, lock=True)[0]
            old = originals[row.schema_snapshot]
            if child.endpoint_fingerprint != old.endpoint_fingerprint:
                result[row.name] = "Endpoint changed"
                continue
            key = _digest(["blocked",child.name,mapping.name,mapping.mapping_revision])
            try:
                graph = _graph(frappe,mapping,child)
                key = graph[0]
                _require_same_lineage(frappe, mapping, graph[-1])
                signature = _prove(mapping,old,child,graph)
                status,reason = "Compatible Contract",signature
            except (ValueError, TypeError, KeyError, frappe.DoesNotExistError, frappe.ValidationError) as error:
                status,reason = "Blocked Contract",str(error)[:500]
                key = _digest([key,status,reason])
            if not frappe.db.exists(INHERITANCE_DOCTYPE,key):
                frappe.get_doc({"doctype":INHERITANCE_DOCTYPE,"inheritance_key":key,"child_snapshot":child.name,
                    "parent_snapshot":old.name,"direction":mapping.direction,"operation_name":mapping.operation_name,
                    "parent_mapping":mapping.name,"parent_definition":mapping.definition_id,"status":status,
                    "reason":reason,"actor":actor}).insert(ignore_permissions=True)
            result[row.name] = status if status == "Compatible Contract" else reason
        plans = _runtime_plan_service(frappe)
        plan = plans.current_approved(frappe.local.site,child.source_instance)
        if plan:
            confirmed = {name for name, status in result.items() if status == "Compatible Contract"}
            plan = plans.adopt_validated_schemas(plan, confirmed, child.name, actor=actor)
        return {"mappings":result,"plan_generation":plan.generation if plan else None}
    except Exception:
        frappe.db.rollback(save_point="hero_schema_adoption")
        raise
