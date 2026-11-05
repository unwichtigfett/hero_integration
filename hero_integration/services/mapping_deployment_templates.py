"""Install editable direct-area drafts and read definitions, never approvals."""
from copy import deepcopy
import json
from types import SimpleNamespace

import frappe

from hero_integration.services.frappe_mapping_draft_storage import FrappeMappingDraftStorage
from hero_integration.services.mapping_draft_service import draft_key


def install_templates(package):
    from hero_integration.api import _validated_snapshot
    from hero_integration.services.direct_mapping import MappingSpec
    from hero_integration.services.mapping_runtime import plan_from_definitions
    from hero_integration.services.query_mapping import seal_query

    snapshots = frappe.get_all("HERO GraphQL Schema Snapshot", filters={
        "source_instance": package["source_instance"], "schema_fingerprint": package["schema_fingerprint"],
        "analysis_status": "Validated"}, pluck="name", order_by="creation desc, name asc", limit_page_length=1)
    if not snapshots:
        raise ValueError("Editable mapping templates require the reviewed HERO schema snapshot.")
    snapshot, _ = _validated_snapshot(snapshots[0], for_update=False)
    active = set()
    for raw in frappe.get_all("HERO GraphQL Mapping", filters={"status": "Active", "direction": "QUERY_TO_FRAPPE",
            "source_instance": package["source_instance"], "schema_fingerprint": package["schema_fingerprint"]},
            pluck="mapping_json", limit_page_length=0):
        active.add(json.loads(raw).get("target_operation"))
    result = []
    for item in package.get("configurable_templates", []):
        operation = item["content"]["target_operation"]
        if operation in active:
            continue
        key = draft_key(frappe.local.site, item["definition_id"], frappe.session.user)
        if frappe.db.exists("HERO Mapping Draft", key):
            result.append({"operation": operation, "draft": key, "state": "PRESERVED"})
            continue
        content = deepcopy(item["content"])
        # Prove schema, pagination and all selected fields without sending a query.
        plan = plan_from_definitions(item["operation_name"], [MappingSpec(operation=operation,
            definition_id=identity, **rule) for identity, rule in content["field_rules"].items()],
            target_operation=operation, adapter_options=content["query_adapter"])
        seal_query(SimpleNamespace(operation_name=item["operation_name"], query_document=item["query_proposal"]),
                   snapshot, content, plan)
        candidates = frappe.get_all("HERO GraphQL Service", filters={"schema_snapshot": snapshot.name,
            "operation_name": item["operation_name"], "query_document": item["query_proposal"], "status": "Validated"},
            pluck="name", order_by="name asc", limit_page_length=1)
        if candidates:
            service_name = candidates[0]
        else:
            service = frappe.get_doc({"doctype": "HERO GraphQL Service", "service_name": operation + " mapping",
                "operation_name": item["operation_name"], "schema_snapshot": snapshot.name,
                "query_document": item["query_proposal"], "status": "Validated", "execution_status": "Pending Approval"})
            service.insert()
            service_name = service.name
        content["query_adapter"]["read_service"] = service_name
        definition = {"site_scope": frappe.local.site, "definition_id": item["definition_id"],
            "source_instance": package["source_instance"], "snapshot_name": snapshot.name,
            "schema_fingerprint": package["schema_fingerprint"], "direction": "QUERY_TO_FRAPPE",
            "operation_kind": "QUERY", "operation_name": item["operation_name"]}
        draft = FrappeMappingDraftStorage().service().open_new_draft(definition, frappe.session.user, content)
        result.append({"operation": operation, "draft": draft.key, "state": "CREATED"})
    return result
