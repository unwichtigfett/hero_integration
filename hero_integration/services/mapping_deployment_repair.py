"""One-time reader repair and lossless consolidation onto current mapping revisions."""
from copy import deepcopy
import json
from uuid import uuid4

import frappe

from hero_integration.services.frappe_mapping_draft_storage import FrappeMappingDraftStorage


RECEIPT = "hero_integration.deployment.consolidated_readers_and_cleanup_20261103"
OPERATIONS = {"HeroCustomers", "HeroProjectMatches", "HeroCalendarEvents"}


def repair(package):
    if frappe.local.site != package["target_site"]:
        return {"status": "OTHER_SITE"}
    if frappe.db.exists("Patch Log", {"patch": RECEIPT}):
        return {"status": "ALREADY_REPAIRED"}
    actor = frappe.session.user
    frappe.set_user("Administrator")
    frappe.db.savepoint("hero_mapping_repair")
    try:
        readers = repair_readers(package)
        from hero_integration.services.mapping_draft_consolidation import consolidate
        consolidated = consolidate(package)
        from hero_integration.services.mapping_deployment_templates import install_templates
        templates = install_templates(package)
        frappe.get_doc({"doctype": "Patch Log", "patch": RECEIPT}).insert()
        return {"status": "REPAIRED", "consolidated": consolidated, "templates": templates, "readers": readers}
    except Exception:
        frappe.db.rollback(save_point="hero_mapping_repair")
        raise
    finally:
        frappe.set_user(actor)


def repair_readers(package):
    from hero_integration import api
    from hero_integration.services.mapping_runtime import active_mapping_plan
    from hero_integration.services.read_contract import from_documents
    from hero_integration.sources.hero import customer_query, project_query
    from hero_integration.services.projection_contracts import approved_query

    selected = {}
    for name in frappe.get_all("HERO GraphQL Service", filters={"status": "Validated"},
                               pluck="name", limit_page_length=0):
        service = frappe.get_doc("HERO GraphQL Service", name)
        if not service.read_mapping:
            continue
        mapping = frappe.get_doc("HERO GraphQL Mapping", service.read_mapping)
        if (mapping.status != "Active" or mapping.direction != "QUERY_TO_FRAPPE"
                or mapping.source_instance != package["source_instance"]
                or mapping.schema_fingerprint != package["schema_fingerprint"]
                or mapping.schema_snapshot != service.schema_snapshot):
            continue
        config = json.loads(mapping.mapping_json)
        operation = config.get("target_operation")
        if operation in OPERATIONS:
            selected.setdefault(operation, []).append((service, mapping, config))
    if set(selected) != OPERATIONS or any(len(rows) != 1 for rows in selected.values()):
        raise ValueError("Reader repair requires one unambiguous validated reader for Customers, Projects and Calendar.")
    result = {}
    for operation, [(service, mapping, config)] in sorted(selected.items()):
        if config.get("query_adapter"):
            raise ValueError("Reader repair cannot replace a custom query adapter.")
        query = (customer_query(config.get("customer_contract_version", "v1")) if operation == "HeroCustomers"
                 else project_query(config.get("project_contract_version", "v1")) if operation == "HeroProjectMatches"
                 else approved_query(operation))
        if service.query_document.strip() != query.strip():
            service.query_document = query
            service.save()
        snapshot = frappe.get_doc("HERO GraphQL Schema Snapshot", mapping.schema_snapshot)
        # Publication of the calendar correction requires the old read binding.
        # This provisional approval is transaction-local; only a fully validated
        # final contract can commit.
        api.approve_read_service(service.name, mapping.name, service.service_revision, mapping.mapping_revision)
        if operation == "HeroCalendarEvents":
            _calendar_rules(mapping)
            mapping.reload()
            config = json.loads(mapping.mapping_json)
        active_mapping_plan(service.operation_name, definition_ids=tuple(config["mapping_definition_ids"]),
                            target_operation=operation, mapping_name=mapping.name)
        service.reload()
        api.approve_read_service(service.name, mapping.name, service.service_revision, mapping.mapping_revision)
        service.reload()
        from_documents(service, mapping, snapshot)
        result[operation] = service.name
    return result


def calendar_content(content):
    """Remove the broken scalar partner resolver; use native color and status."""
    content = deepcopy(content)
    ids = content["mapping_definition_ids"]
    for identity in list(ids):
        rule = content["field_rules"][identity]
        if rule["source_path"] == "partner_id":
            ids.remove(identity)
        elif rule["target_field"] == "custom_hero_color":
            rule.update(target_field="color", transformation="Identity", purpose="Field")
        elif rule["target_field"] == "custom_hero_is_done":
            rule.update(target_field="status", transformation="Native Adapter", purpose="Native Binding")
    return content


def _calendar_rules(mapping):
    from hero_integration import api
    storage = FrappeMappingDraftStorage()
    with storage.transaction(mapping.definition_id, frappe.session.user):
        current = storage.initial_content(json.loads(mapping.mapping_json))
    if calendar_content(current) == current:
        return
    draft = api.mapping_draft_create(mapping.definition_id)["draft"]
    if draft["content"] != json.loads(draft["base_content_json"]):
        raise ValueError("An edited calendar draft needs reconciliation before reader repair.")
    revised = calendar_content(draft["content"])
    for identity, base in draft["content"].get("field_rule_bases", {}).items():
        if revised["field_rules"][identity] == draft["content"]["field_rules"][identity]:
            continue
        if not frappe.db.get_value("HERO Frappe Mapping", base["name"], "portable_definition_id"):
            api.mapping_draft_enroll_portable_rule(mapping.definition_id, identity, str(uuid4()),
                draft["base_active_revision"], int(base["revision"]))
    draft = api.mapping_draft_create(mapping.definition_id)["draft"]
    saved = api.mapping_draft_save(mapping.definition_id, draft["generation"], draft["content_revision"],
        draft["base_active_revision"], json.dumps(calendar_content(draft["content"])))["draft"]
    api.mapping_draft_publish(mapping.definition_id, saved["generation"], saved["content_revision"],
        saved["base_active_revision"], saved.get("reconciliation_revision") or 0,
        confirmed=True, warning_fingerprint=saved.get("warning_fingerprint") or "")
