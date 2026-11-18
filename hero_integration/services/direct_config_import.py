"""Install the complete CLI configuration without personal drafts or publication.

The authenticated JSON artifact is the desired state for one HERO source.
Document history and business bindings are retained; omitted configuration is
disabled. Schema DDL precedes the atomic configuration transition.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from hashlib import sha256
import json
from uuid import NAMESPACE_URL, uuid5

import frappe

from hero_integration.services import config_exchange as exchange
from hero_integration.services.doctype_permissions import configuration_metadata_review


MAPPING = "HERO GraphQL Mapping"
RULE = "HERO Frappe Mapping"
SERVICE = "HERO GraphQL Service"
CONTRACT = "HERO Mutation Contract"
SNAPSHOT = "HERO GraphQL Schema Snapshot"
_document_write = ContextVar("hero_direct_configuration_document", default=None)
_REVISION = {MAPPING: "mapping_revision", RULE: "mapping_revision",
             SERVICE: "service_revision", CONTRACT: "contract_revision"}
_SERVICE_SEMANTICS = {"schema_snapshot", "operation_name", "query_document", "read_mapping"}


def authorize():
    if frappe.session.user != "Administrator":
        frappe.only_for("System Manager")


def accepts_document(document):
    """An exact object capability, never a client-supplied Document flag."""
    admitted = _document_write.get()
    return admitted is not None and admitted == (
        id(document), frappe.local.site, frappe.session.user,
    )


@contextmanager
def _writing(document):
    authorize()
    if document.doctype not in _REVISION:
        raise exchange.ConfigExchangeError("Unsupported configuration document.")
    token = _document_write.set((id(document), frappe.local.site, frappe.session.user))
    try:
        yield
    finally:
        _document_write.reset(token)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _identity(source, kind, key):
    return str(uuid5(NAMESPACE_URL, _json(["hero-cli-config", frappe.local.site, source, kind, key])))


def _names(doctype, filters, *, lock=False):
    if lock:
        # A current read is required after waiting for the source lock. Ordinary
        # get_all reads may still see an earlier REPEATABLE READ snapshot.
        return frappe.db.get_values(doctype, filters, "name", pluck=True, for_update=True,
                                    order_by="creation asc, name asc")
    return frappe.get_all(doctype, filters=filters, pluck="name", order_by="creation asc, name asc",
                          limit_page_length=0)


def _documents(doctype, filters, *, lock=False):
    return [frappe.get_doc(doctype, name, for_update=True) if lock else frappe.get_doc(doctype, name)
            for name in _names(doctype, filters, lock=lock)]


def _inventory(source, *, lock=False):
    snapshots = _names(SNAPSHOT, {"source_instance": source}, lock=lock)
    mappings = _documents(MAPPING, {"source_instance": source}, lock=lock)
    filters = {"schema_snapshot": ["in", snapshots]}
    return {
        "mappings": mappings,
        "rules": _documents(RULE, filters, lock=lock) if snapshots else [],
        "services": _documents(SERVICE, filters, lock=lock) if snapshots else [],
        "contracts": _documents(CONTRACT, filters, lock=lock) if snapshots else [],
    }


def _choose(documents, predicate):
    matches = [doc for doc in documents if predicate(doc)]
    return next((doc for doc in matches if doc.get("status") in ("Active", "Validated")), next(iter(matches), None))


def _put(doctype, values, existing=None, *, force_revision=False, initial=False):
    """Keep stable object identities and bump runtime revisions only on change."""
    changed = existing is None or any(existing.get(key) != value for key, value in values.items())
    if not changed and not force_revision:
        return existing
    document = existing or frappe.get_doc({"doctype": doctype})
    revision_field = _REVISION[doctype]
    semantic = values if doctype != SERVICE else {key: value for key, value in values.items()
                                                if key in _SERVICE_SEMANTICS}
    revised = force_revision or existing is None or any(existing.get(k) != v for k, v in semantic.items())
    revision = max(1, int(document.get(revision_field) or 1))
    if existing is not None and revised and not initial:
        revision += 1
    document.update({**values, revision_field: revision})
    with _writing(document):
        document.insert() if existing is None else document.save()
    return document


def _prepare(snapshot_name, payload, source):
    rules = exchange._replacement_rules(payload)
    exchange._settings_changes(payload)
    snapshot, state = exchange._source_snapshot(snapshot_name, payload, source)
    # Rebuild runtime contracts against the selected target, not the old mapping.
    with configuration_metadata_review(frappe):
        schema = exchange._replacement_check(snapshot, payload, rules, replace_existing=True)
    mutations = payload.get("mutation_mappings", [])
    contracts = {item["operation_name"]: item for item in payload.get("mutation_contracts", [])}
    if any(contracts.get(item["operation_name"], {}).get("status") != "Active" for item in mutations):
        raise exchange.ConfigExchangeError("Every imported mutation requires its active contract in the same file.")
    return rules, snapshot, state, schema


def preview(snapshot_name, payload, source):
    authorize()
    rules, snapshot, state, schema = _prepare(snapshot_name, payload, source)
    current = _inventory(source)
    areas = [item["target_operation"] for item in payload["read_services"]]
    active = [doc for doc in current["mappings"] if doc.status == "Active"]
    existing_rules = {doc.definition_id: doc for doc in current["rules"]}
    planned_rules, planned_parents, services, new_rules = set(), set(), [], 0
    for item in payload["read_services"]:
        area = item["target_operation"]
        parent = _choose(current["mappings"], lambda doc: doc.direction == "QUERY_TO_FRAPPE"
                         and json.loads(doc.mapping_json).get("target_operation") == area)
        identity = parent.definition_id if parent else _identity(source, "query", area)
        desired = {_identity(source, "rule", [identity, value]) for value in item["mapping"]["mapping_definition_ids"]}
        planned_rules.update(desired)
        new_rules += len(desired - existing_rules.keys())
        state_name = "Replace"
        if parent:
            planned_parents.add(parent.name)
            reader = _choose(current["services"], lambda doc: doc.read_mapping == parent.name)
            content = json.loads(parent.mapping_json)
            ids = content.get("mapping_definition_ids", [])
            if (reader and parent.schema_snapshot == snapshot.name and parent.status == "Active"
                    and set(ids) <= existing_rules.keys()
                    and reader.query_document == item["query"] and reader.service_name == item["service_name"]
                    and reader.status == "Validated" and reader.execution_status == "Approved"
                    and reader.approved_mapping_revision == parent.mapping_revision
                    and reader.approved_service_revision == reader.service_revision
                    and exchange._semantic_mapping(content, existing_rules) == exchange._semantic_mapping(item["mapping"], rules)):
                state_name = "Unchanged"
        services.append({"target_operation": area, "state": state_name})
    return {
        "format_version": payload["format_version"], "snapshot": state,
        "settings": sorted(exchange._settings_changes(payload)), "replace_mappings": True,
        "mapping_package": {**schema["counts"], "New": schema["counts"].get("New", 0) + new_rules},
        "adopt": new_rules, "adoption": None, "rebased_from": None,
        "replacement": {
            "operations": areas,
            "field_mappings": sum(doc.status == "Active" and doc.definition_id not in planned_rules
                                  for doc in current["rules"]),
            "graphql_mappings": sum(doc.direction == "QUERY_TO_FRAPPE" and doc.name not in planned_parents for doc in active),
            "removed_operations": sorted({json.loads(doc.mapping_json).get("target_operation")
                                          for doc in active if doc.direction == "QUERY_TO_FRAPPE"} - set(areas)),
        },
        "read_services": services,
        "execution": False,
    }


def _resolve(value, readers, field=""):
    if isinstance(value, dict):
        return {key: _resolve(child, readers, key) for key, child in value.items()}
    if isinstance(value, list):
        return [_resolve(child, readers, field) for child in value]
    if field.endswith("_service") and isinstance(value, str) and value.startswith(exchange.SERVICE_REFERENCE):
        area = value[len(exchange.SERVICE_REFERENCE):]
        if area not in readers:
            raise exchange.ConfigExchangeError("Reader dependency is absent from the complete configuration: {0}", area)
        return readers[area]
    return value


def _query(snapshot, item, rules, source, schema, inventory, readers):
    from hero_integration.services.direct_mapping import MappingSpec
    from hero_integration.services.mapping_runtime import plan_from_definitions
    from hero_integration.services.query_mapping import seal_query

    area = item["target_operation"]
    parent = _choose(inventory["mappings"], lambda doc: doc.direction == "QUERY_TO_FRAPPE"
                     and json.loads(doc.mapping_json).get("target_operation") == area)
    initial = parent is None
    identity = parent.definition_id if parent else _identity(source, "query", area)
    common = {"schema_snapshot": snapshot.name, "schema_fingerprint": snapshot.schema_fingerprint,
              "source_instance": source, "operation_kind": "QUERY", "direction": "QUERY_TO_FRAPPE",
              "operation_name": item["mapping_operation_name"], "definition_id": identity,
              "portable_definition_id": parent.get("portable_definition_id") if parent else identity}
    if parent is None:
        parent = _put(MAPPING, {**common, "mapping_json": _json(item["mapping"]), "status": "Disabled"})
    reader = _choose(inventory["services"], lambda doc: doc.read_mapping == parent.name)
    reader = _put(SERVICE, {
        "schema_snapshot": snapshot.name, "schema_fingerprint": snapshot.schema_fingerprint,
        "service_name": item["service_name"], "operation_name": item["operation_name"],
        "query_document": item["query"], "read_mapping": parent.name, "status": "Validated",
        "provenance": "CLI complete configuration import",
    }, reader)
    readers[area] = reader.name
    content = _resolve(deepcopy(item["mapping"]), readers)
    for key in ("query_adapter_seal", "generated_task_seal", "field_rules", "field_rule_bases",
                "_portable_exchange_schema"):
        content.pop(key, None)
    remapping, specifications, changed = {}, [], False
    for incoming_id in content["mapping_definition_ids"]:
        rule = rules[incoming_id]
        local_id = _identity(source, "rule", [identity, incoming_id])
        old = _choose(inventory["rules"], lambda doc: doc.definition_id == local_id)
        values = {**exchange._rule_values(rule), "definition_id": local_id,
                  "portable_definition_id": local_id, "portable_lineage_json": "",
                  "personal_graphql_mapping": parent.name, "operation": area, "status": "Active",
                  "schema_snapshot": snapshot.name, "schema_fingerprint": snapshot.schema_fingerprint,
                  "schema_manifest": _json({"custom_doctypes": [], "custom_fields": [], "relations": []}),
                  "schema_dependencies": _json({"ids": rule["schema_dependencies"]})}
        changed |= old is None or any(old.get(k) != v for k, v in values.items())
        stored = _put(RULE, values, old)
        remapping[incoming_id] = stored.definition_id
        specifications.append(MappingSpec(operation=area, definition_id=stored.definition_id,
                                          **exchange._rule_values(rule)))
    content["mapping_definition_ids"] = list(remapping.values())
    if content.get("query_adapter"):
        options = content["query_adapter"]
        options["read_service"] = reader.name
        from hero_integration.services.personal_mapping_content import remap_query_options
        remap_query_options(content, remapping)
        plan = plan_from_definitions(item["mapping_operation_name"], specifications,
                                    target_operation=area, adapter_options=options)
        content["query_adapter_seal"] = seal_query(reader, snapshot, content, plan)
    else:
        plan_from_definitions(item["mapping_operation_name"], specifications, target_operation=area,
                              native_task=bool(content.get("native_task_onboarding")))
        if content.get("native_task_onboarding"):
            from hero_integration.services.read_contract import task_materialization
            plan = plan_from_definitions(item["mapping_operation_name"], specifications,
                                         target_operation=area, native_task=True)
            _, content["generated_task_seal"] = task_materialization(snapshot, content, plan)
    parent = _put(MAPPING, {**common, "mapping_json": _json(content),
                  "content_hash": sha256(_json(content).encode()).hexdigest(), "status": "Active",
                  "configuration_import_ids": _json({"rules": {local: incoming for incoming, local in remapping.items()}}),
                  "portable_schema_manifest": _json(schema),
                  "portable_schema_dependencies": _json([entry["id"] for group in schema.values() for entry in group])},
                  parent, force_revision=changed, initial=initial)
    reader = _put(SERVICE, {
        "execution_status": "Approved", "approved_service_revision": int(reader.service_revision),
        "approved_mapping_revision": int(parent.mapping_revision),
    }, reader)
    return parent, reader, remapping


def _mutations(snapshot, payload, source, inventory, readers, rule_ids):
    from hero_integration.services.mutation_binding_contract import parse_mutation
    from hero_integration.services.personal_mapping_content import configured_mutation_helpers

    definitions, mappings, services, contracts = {}, set(), set(), set()
    raw_schema = json.loads(snapshot.snapshot_json)
    for item in payload.get("mutation_mappings", []):
        content = deepcopy(item["content"])
        area, usage = content["area"], content["usage"]
        content["read_service"] = readers[area]
        for row in content.get("assignments", []) + content.get("required_readback", []):
            if row.get("query_rule_id"):
                row["query_rule_id"] = rule_ids[area][row["query_rule_id"]]
        normalise, _validate_sources = configured_mutation_helpers(usage)
        content = normalise(content, raw_schema)
        parent = _choose(inventory["mappings"], lambda doc: doc.direction == "FRAPPE_TO_MUTATION"
                         and (lambda value: (value.get("area"), value.get("usage")) == (area, usage))(
                             json.loads(doc.mapping_json)))
        identity = parent.definition_id if parent else _identity(source, "mutation", [area, usage])
        parent = _put(MAPPING, {
            "definition_id": identity,
            "portable_definition_id": parent.get("portable_definition_id") if parent else identity,
            "source_instance": source, "schema_snapshot": snapshot.name,
            "schema_fingerprint": snapshot.schema_fingerprint, "direction": "FRAPPE_TO_MUTATION",
            "operation_kind": "MUTATION", "operation_name": item["operation_name"],
            "mapping_json": _json(content), "content_hash": sha256(_json(content).encode()).hexdigest(),
            "configuration_import_ids": _json({"definition_id": item["definition_id"]}),
            "status": "Active",
        }, parent)
        definitions[item["definition_id"]] = parent.definition_id
        mappings.add(parent.name)
    for item in payload.get("mutation_contracts", []):
        operation = item["operation_name"]
        old = _choose(inventory["contracts"], lambda doc: doc.operation_name == operation)
        service = next((doc for doc in inventory["services"] if old and doc.name == old.graphql_service), None)
        service = service or _choose(inventory["services"], lambda doc: doc.operation_name == operation
                                     and not doc.read_mapping)
        service = _put(SERVICE, {
            "schema_snapshot": snapshot.name, "schema_fingerprint": snapshot.schema_fingerprint,
            "service_name": item["service_name"], "operation_name": operation, "query_document": item["query"],
            "read_mapping": None, "status": "Validated" if item["status"] == "Active" else "Draft",
            "execution_status": "Pending Approval", "approved_service_revision": 0, "approved_mapping_revision": 0,
            "provenance": "CLI complete configuration import",
        }, service)
        scope = deepcopy(item["mapping"])
        scope["mapping_definition_ids"] = [definitions[value] for value in scope["mapping_definition_ids"]]
        contract = _put(CONTRACT, {
            "graphql_service": service.name, "schema_snapshot": snapshot.name,
            "schema_fingerprint": snapshot.schema_fingerprint, "operation_name": operation,
            "mapping_json": _json(scope), "contract_fingerprint": parse_mutation(item["query"]).fingerprint,
            "status": item["status"],
        }, old)
        services.add(service.name)
        contracts.add(contract.name)
    return mappings, services, contracts


def _retire(inventory, kept_mappings, kept_services, kept_contracts, kept_rules):
    retired = {MAPPING: 0, RULE: 0, SERVICE: 0, CONTRACT: 0}
    # Shared field rules can still be referenced by a different source.
    selected_elsewhere = set()
    for doc in _documents(MAPPING, {"status": "Active", "direction": "QUERY_TO_FRAPPE"}, lock=True):
        if doc.name not in {row.name for row in inventory["mappings"]}:
            selected_elsewhere.update(json.loads(doc.mapping_json).get("mapping_definition_ids", []))
    for key, doctype, kept, identity in (
        ("mappings", MAPPING, kept_mappings, "name"),
        ("rules", RULE, kept_rules | selected_elsewhere, "definition_id"),
        ("contracts", CONTRACT, kept_contracts, "name"),
    ):
        for doc in inventory[key]:
            if doc.get(identity) not in kept and doc.status not in ("Disabled", "Rejected"):
                _put(doctype, {"status": "Disabled"}, doc)
                retired[doctype] += 1
    old_mappings = {doc.name for doc in inventory["mappings"]}
    old_contract_services = {doc.graphql_service for doc in inventory["contracts"]}
    for doc in inventory["services"]:
        if doc.name not in kept_services and (doc.read_mapping in old_mappings or doc.name in old_contract_services):
            if doc.status != "Disabled" or doc.execution_status == "Approved":
                _put(SERVICE, {"status": "Disabled", "execution_status": "Pending Approval",
                              "approved_service_revision": 0, "approved_mapping_revision": 0}, doc)
                retired[SERVICE] += 1
    return retired


def apply(snapshot_name, payload, source, *, before_mapping_write=None):
    authorize()
    rules, snapshot, state, _schema_preview = _prepare(snapshot_name, payload, source)
    from hero_integration.services.schema_provisioning import provision_schema
    provision_schema(payload["mapping_package"]["schema"])
    # DDL can commit implicitly. Acquire all configuration locks afterwards.
    if state == "New":
        snapshot, state = exchange._source_snapshot(snapshot_name, payload, source, create=True)
    from hero_integration.hero_integration.doctype.hero_graphql_mapping.hero_graphql_mapping import lock_exchange_context
    lock_exchange_context(source, snapshot.name, "QUERY_TO_FRAPPE")
    inventory = _inventory(source, lock=True)
    for name in sorted({doc.schema_snapshot for doc in inventory["mappings"]} | {snapshot.name}):
        for direction in ("QUERY_TO_FRAPPE", "FRAPPE_TO_MUTATION"):
            lock_exchange_context(source, name, direction)
    point = "hero_direct_config_import"
    frappe.db.savepoint(point)
    try:
        if before_mapping_write is not None:
            before_mapping_write(snapshot)
        selected, readers, rule_ids, field_ids, results = {}, {}, {}, set(), []
        schema = payload["mapping_package"]["schema"]
        for item in sorted(payload["read_services"], key=lambda row: exchange.AREA_ORDER.index(row["target_operation"])):
            parent, reader, identities = _query(snapshot, item, rules, source, schema, inventory, readers)
            selected[item["target_operation"]] = parent
            rule_ids[item["target_operation"]] = identities
            field_ids.update(identities.values())
            results.append({"target_operation": item["target_operation"], "state": "Approved", "service": reader.name})
        mutations, services, contracts = _mutations(snapshot, payload, source, inventory, readers, rule_ids)
        retired = _retire(inventory, {doc.name for doc in selected.values()} | mutations,
                          set(readers.values()) | services, contracts, field_ids)
        with configuration_metadata_review(frappe):
            exchange._verify_replacement_readers(snapshot, readers)
        changes = exchange._settings_changes(payload)
        exchange._apply_settings(changes)
    except Exception:
        frappe.db.rollback(save_point=point)
        raise
    return {
        "snapshot": state, "snapshot_name": snapshot.name, "settings": sorted(changes),
        "adopted": len(rules), "adoption": None, "read_services": results, "replace_mappings": True,
        "replacement": {"operations": list(selected), "field_mappings": retired[RULE],
                        "graphql_mappings": retired[MAPPING], "mutation_contracts": retired[CONTRACT]},
        "activation": True, "execution": False,
    }


def export_mutations(source, snapshot_name):
    result = []
    rule_ids = {}
    for parent in _documents(MAPPING, {"source_instance": source, "schema_snapshot": snapshot_name,
                                      "direction": "QUERY_TO_FRAPPE", "status": "Active"}):
        rule_ids.update(json.loads(parent.get("configuration_import_ids") or "{}").get("rules", {}))
    for doc in _documents(MAPPING, {"source_instance": source, "schema_snapshot": snapshot_name,
                                   "direction": "FRAPPE_TO_MUTATION", "status": "Active"}):
        content = json.loads(doc.mapping_json)
        if content.get("usage") in {"MAPPED_UPDATE_V2", "PROJECT_LOGBOOK_V1"}:
            for row in content.get("assignments", []) + content.get("required_readback", []):
                if row.get("query_rule_id"):
                    row["query_rule_id"] = rule_ids.get(row["query_rule_id"], row["query_rule_id"])
            identity = json.loads(doc.get("configuration_import_ids") or "{}").get("definition_id", doc.definition_id)
            result.append({"definition_id": identity, "direction": doc.direction,
                           "operation_kind": doc.operation_kind, "operation_name": doc.operation_name,
                           "dependencies": [], "content": content})
    return result


def export_contracts(source, snapshot_name, *, mutation_mappings):
    definitions = {}
    for parent in _documents(MAPPING, {"source_instance": source, "schema_snapshot": snapshot_name,
                                      "direction": "FRAPPE_TO_MUTATION", "status": "Active"}):
        definitions[parent.definition_id] = json.loads(parent.get("configuration_import_ids") or "{}").get(
            "definition_id", parent.definition_id)
    selected_ids = {item["definition_id"] for item in mutation_mappings}
    result = []
    for doc in _documents(CONTRACT, {"schema_snapshot": snapshot_name, "status": "Active"}):
        scope = json.loads(doc.mapping_json)
        identities = [definitions.get(value, value) for value in scope.get("mapping_definition_ids", [])]
        if identities and set(identities) <= selected_ids:
            scope["mapping_definition_ids"] = identities
            service = frappe.get_doc(SERVICE, doc.graphql_service)
            result.append({"operation_name": doc.operation_name, "service_name": service.service_name,
                           "query": service.query_document, "mapping": scope, "status": doc.status})
    return result
