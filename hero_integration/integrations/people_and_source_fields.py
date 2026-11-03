"""Read the lead source, the project designation and the HERO people (one-time, idempotent).

Owner decision 2026-11-03:
- Customer: source ("Quelle") -> Customer Source (read contract v5).
- Project: name ("Bezeichnung"), partner_source ("Quelle"), the responsible partner
  (partner_id, matched by e-mail) -> Project Manager, and the assigned users
  ("Bearbeiter") -> native assignments (read contract v4).
- Task: the author ("Ersteller") -> Created By; the approved task query already reads it.

The Customer and Project read mappings get the new rules and contract version;
their read services get the matching query. Both then need a new approval in the UI.
"""
from __future__ import annotations

import json

import frappe

NEW_RULES = {
    "HeroCustomers": (
        {"definition_id": "customer-lead-source", "source_path": "source", "target_doctype": "Customer",
         "target_field": "custom_hero_lead_source", "transformation": "Trim"},
    ),
    "HeroProjectMatches": (
        {"definition_id": "project-designation", "source_path": "name", "target_doctype": "Project",
         "target_field": "custom_hero_designation", "transformation": "Trim"},
        {"definition_id": "project-lead-source", "source_path": "partner_source", "target_doctype": "Project",
         "target_field": "custom_hero_lead_source", "transformation": "Trim"},
        {"definition_id": "project-manager", "source_path": "partner.email", "target_doctype": "Project",
         "target_field": "custom_hero_project_manager", "transformation": "User By Email"},
    ),
}
CONTRACTS = {"HeroCustomers": ("customer_contract_version", "v5"), "HeroProjectMatches": ("project_contract_version", "v4")}


def _query(operation: str, version: str) -> str:
    from hero_integration.sources.hero import customer_query, project_query

    return customer_query(version) if operation == "HeroCustomers" else project_query(version)


def _rule(mapping, spec: dict) -> str:
    """The active rule of ``spec`` (created once in the mapping's schema context); returns its definition_id."""
    if not frappe.db.exists("HERO Frappe Mapping", {"definition_id": spec["definition_id"], "status": "Active"}):
        frappe.get_doc({"doctype": "HERO Frappe Mapping", "operation": mapping["operation"], "source_type": "string|null",
                        "purpose": "Field", "priority": 5, "status": "Active", "schema_snapshot": mapping["snapshot"],
                        **spec}).insert()
    return spec["definition_id"]


def extend_mapping(operation: str) -> str:
    """Add the new rules and contract version to the area's active read mapping and service."""
    from hero_integration import api

    services = api._sync_area_read_services("default").get(operation) or []
    rows = frappe.get_all("HERO GraphQL Mapping", filters={"status": "Active", "direction": "QUERY_TO_FRAPPE"},
                          fields=["name", "mapping_json", "schema_snapshot", "operation_name", "mapping_revision"],
                          limit_page_length=0)
    mapping = next((row for row in rows if json.loads(row.mapping_json or "{}").get("target_operation") == operation), None)
    if mapping is None:
        return "no active read mapping"
    config = json.loads(mapping.mapping_json)
    key, version = CONTRACTS[operation]
    ids = list(config.get("mapping_definition_ids") or [])
    context = {"operation": operation, "snapshot": mapping.schema_snapshot}
    wanted = [_rule(context, spec) for spec in NEW_RULES[operation]]
    if config.get(key) == version and set(wanted) <= set(ids):
        return "already extended"
    config[key] = version
    config["mapping_definition_ids"] = [*ids, *(rule for rule in wanted if rule not in ids)]
    draft = api.graphql_mapping_save(mapping.schema_snapshot, "QUERY", mapping.operation_name, "QUERY_TO_FRAPPE",
                                     json.dumps(config), mapping_name=mapping.name,
                                     expected_revision=mapping.mapping_revision, activate=0)
    saved = api.graphql_mapping_save(mapping.schema_snapshot, "QUERY", mapping.operation_name, "QUERY_TO_FRAPPE",
                                     json.dumps(config), mapping_name=draft["name"],
                                     expected_revision=draft["revision"], activate=1)
    service_names = [row["service"] for row in services] or frappe.get_all(
        "HERO GraphQL Service", filters={"read_mapping": mapping.name}, pluck="name", limit_page_length=0)
    for name in service_names:
        service = frappe.get_doc("HERO GraphQL Service", name)
        service.query_document = _query(operation, version)
        service.read_mapping = saved["name"]
        service.save()
    return f"{saved['name']} {key}={version}; services {', '.join(service_names) or '-'} need approval"


def run() -> dict:
    from hero_integration.install import ensure_hero_source_fields
    from hero_integration.integrations.project_form import setup_project_form
    from hero_integration.integrations.task_form import setup_task_form

    previous = frappe.session.user
    frappe.set_user("Administrator")
    report = {}
    try:
        ensure_hero_source_fields()
        setup_task_form()
        setup_project_form()
        for operation in NEW_RULES:
            savepoint = "hero_people_" + operation.lower()
            frappe.db.savepoint(savepoint)
            try:
                report[operation] = extend_mapping(operation)
            except Exception as error:
                frappe.db.rollback(save_point=savepoint)
                frappe.log_error(title="HERO source/people fields: mapping not extended", message=f"{operation}: {error!r}")
                report[operation] = f"not extended: {error}"
    finally:
        frappe.set_user(previous)
    return report


# Contract v5: people by HERO ID (owner decision 2026-11-03). The responsible person comes from partner_id.
RESPONSIBLE_BY_ID = {"definition_id": "project-responsible", "source_path": "partner_id", "source_type": "int|null",
                     "target_doctype": "Project", "target_field": "custom_hero_project_manager",
                     "transformation": "User By Partner ID"}
RESPONSIBLE_BY_EMAIL = "project-manager"


def upgrade_projects_to_ids() -> str:
    """Switch the Project read mapping and service to contract v5 (partner_id, assignee user_id)."""
    from hero_integration import api

    rows = frappe.get_all("HERO GraphQL Mapping", filters={"status": "Active", "direction": "QUERY_TO_FRAPPE"},
                          fields=["name", "mapping_json", "schema_snapshot", "operation_name", "mapping_revision"],
                          limit_page_length=0)
    mapping = next((row for row in rows
                    if json.loads(row.mapping_json or "{}").get("target_operation") == "HeroProjectMatches"), None)
    if mapping is None:
        return "no active project read mapping"
    config = json.loads(mapping.mapping_json)
    context = {"operation": "HeroProjectMatches", "snapshot": mapping.schema_snapshot}
    wanted = [_rule(context, spec) for spec in NEW_RULES["HeroProjectMatches"] if spec["definition_id"] != RESPONSIBLE_BY_EMAIL]
    # One active rule per target: the e-mail rule of contract v4 makes way for the partner-ID rule.
    for name in frappe.get_all("HERO Frappe Mapping", filters={"definition_id": RESPONSIBLE_BY_EMAIL, "status": "Active"},
                               pluck="name"):
        frappe.db.set_value("HERO Frappe Mapping", name, "status", "Disabled")
    wanted.append(_rule(context, RESPONSIBLE_BY_ID))
    ids = [rule for rule in config.get("mapping_definition_ids") or [] if rule != RESPONSIBLE_BY_EMAIL]
    if config.get("project_contract_version") == "v5" and set(wanted) <= set(ids):
        return "already on v5"
    config["project_contract_version"] = "v5"
    config["mapping_definition_ids"] = [*ids, *(rule for rule in wanted if rule not in ids)]
    services = [row["service"] for row in api._sync_area_read_services("default").get("HeroProjectMatches") or []] \
        or frappe.get_all("HERO GraphQL Service", filters={"read_mapping": mapping.name}, pluck="name", limit_page_length=0)
    draft = api.graphql_mapping_save(mapping.schema_snapshot, "QUERY", mapping.operation_name, "QUERY_TO_FRAPPE",
                                     json.dumps(config), mapping_name=mapping.name,
                                     expected_revision=mapping.mapping_revision, activate=0)
    saved = api.graphql_mapping_save(mapping.schema_snapshot, "QUERY", mapping.operation_name, "QUERY_TO_FRAPPE",
                                     json.dumps(config), mapping_name=draft["name"],
                                     expected_revision=draft["revision"], activate=1)
    for name in services:
        service = frappe.get_doc("HERO GraphQL Service", name)
        service.query_document = _query("HeroProjectMatches", "v5")
        service.read_mapping = saved["name"]
        service.save()
    return f"{saved['name']} project_contract_version=v5; services {', '.join(services) or '-'} need approval"


def run_partner_ids() -> dict:
    from hero_integration.install import ensure_hero_source_fields
    from hero_integration.integrations.hero_people import _client, sync_users

    previous = frappe.session.user
    frappe.set_user("Administrator")
    report = {}
    try:
        ensure_hero_source_fields()
        try:
            report["people"] = sync_users(_client())
        except Exception as error:  # HERO unreachable: the daily job fills the IDs later
            report["people"] = f"not synchronised: {error}"
        frappe.db.savepoint("hero_partner_ids")
        try:
            report["projects"] = upgrade_projects_to_ids()
        except Exception as error:
            frappe.db.rollback(save_point="hero_partner_ids")
            report["projects"] = f"not upgraded: {error}"
    finally:
        frappe.set_user(previous)
    return report
