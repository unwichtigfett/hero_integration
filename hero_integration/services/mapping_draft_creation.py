"""Local editor seeds and first personal drafts; no publication or remote calls."""
from copy import deepcopy
from uuid import uuid4
from types import SimpleNamespace

from hero_integration.services import direct_operations
from hero_integration.services.mapping_draft_service import MappingDraftError
from hero_integration.services.query_mapping import ROOTS, validate_query_adapter
from hero_integration.services.schema_operations import catalog

DIRECTIONS = {"QUERY_TO_FRAPPE": "QUERY", "FRAPPE_TO_MUTATION": "MUTATION"}
NATIVE_LABELS = {"HeroCustomers": "Customers", "HeroProjectMatches": "Projects",
                 "HeroCalendarEvents": "Calendar", "HeroTasks": "Tasks"}


def _has_company_partners(schema):
    from graphql import build_client_schema, get_named_type
    from hero_integration.services.graphql_graph import normalize_schema
    parsed = build_client_schema({"__schema": normalize_schema(schema)})
    root = parsed.query_type.fields.get("company") if parsed.query_type else None
    company = get_named_type(root.type) if root else None
    return bool(company and "partners" in getattr(company, "fields", {}))


def seed(schema, direction, operation_name, target_operation=""):
    """Return an unsaved editor form, requiring an explicit choice when ambiguous."""
    kind = DIRECTIONS.get(direction)
    if kind is None:
        raise MappingDraftError("Choose Query or Mutation mapping direction.")
    if operation_name not in {item["operation_name"] for item in catalog(schema, kind)}:
        raise MappingDraftError("The selected operation is absent from this schema snapshot.")
    if kind == "MUTATION":
        if target_operation:
            raise MappingDraftError("Mutation mappings cannot select a Query adapter.")
        return {"query_targets": [], "query_target_labels": {}, "target_operation": "",
                "content": {"source": {}, "target": {"operation": operation_name}, "transformation": "Identity"}}
    targets = [name for name, (root, _owner) in ROOTS.items() if root == operation_name]
    if operation_name == "company" and _has_company_partners(schema):
        targets.append("HeroEmployees")
    labels = {name: direct_operations.OPERATIONS[name]["area"] if name in direct_operations.OPERATIONS
              else NATIVE_LABELS[name] for name in targets}
    if not targets:
        raise MappingDraftError("This Query has no supported document mapping adapter.")
    if target_operation and target_operation not in targets:
        raise MappingDraftError("The selected mapping area does not match this Query.")
    chosen = target_operation or (targets[0] if len(targets) == 1 else "")
    result = {"query_targets": targets, "query_target_labels": labels, "target_operation": chosen, "content": None}
    if not chosen:
        return result
    direct = direct_operations.OPERATIONS.get(chosen, {})
    path = direct_operations.records_path(chosen) if direct else operation_name
    if chosen == "HeroEmployees" and operation_name == "company":
        path = "company.partners"
    result["content"] = {"target_operation": chosen, "records_path": path,
        "identity_path": direct.get("identity", ("id",))[0], "page_size": direct.get("page_size", 100),
        "variables": {} if direct.get("unpaged") else {"first": "$page_size", "offset": "$offset"},
        "query_adapter": {"version": 2}, "mapping_definition_ids": [], "field_rules": {}}
    return result


def create(snapshot, schema, direction, operation_name, content, *, actor, site_scope, storage):
    """Use the ordinary parentless validator and actor-owned persistence service."""
    value = deepcopy(content)
    if not isinstance(value, dict):
        raise MappingDraftError("New mapping content must be an object.")
    target = value.get("target_operation", "") if direction == "QUERY_TO_FRAPPE" else ""
    proposed = seed(schema, direction, operation_name, target)
    if direction == "QUERY_TO_FRAPPE":
        if not target or target != proposed["target_operation"]:
            raise MappingDraftError("Choose a mapping area before saving this Query.")
        identities = value.get("mapping_definition_ids")
        if not isinstance(identities, list) or not identities or any(not isinstance(item, str) for item in identities):
            raise MappingDraftError("Add a field assignment before saving this Query mapping.")
        try:
            validate_query_adapter(value.get("query_adapter"),
                                   [SimpleNamespace(definition_id=identity) for identity in identities])
        except ValueError as error:
            raise MappingDraftError(str(error)) from error
        try:
            storage._complete_query_target_types(value)
        except (KeyError, TypeError, AttributeError) as error:
            raise MappingDraftError("New query field rule is incomplete.") from error
    definition = {"site_scope": site_scope, "definition_id": str(uuid4()), "source_instance": snapshot.source_instance,
        "snapshot_name": snapshot.name, "schema_fingerprint": snapshot.schema_fingerprint,
        "direction": direction, "operation_kind": DIRECTIONS[direction], "operation_name": operation_name}
    return storage.service().open_new_draft(definition, actor, value)
