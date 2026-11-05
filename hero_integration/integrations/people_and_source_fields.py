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
    """The active rule of ``spec`` (created once in the mapping's schema context); returns its definition_id.

    An active shared rule for the same target (e.g. from a configuration import) is reused."""
    if frappe.db.exists("HERO Frappe Mapping", {"definition_id": spec["definition_id"], "status": "Active"}):
        return spec["definition_id"]
    # One active shared rule per target: reuse the one that already writes this field.
    same = frappe.get_all("HERO Frappe Mapping", filters={
        "operation": mapping["operation"], "status": "Active",
        "target_doctype": spec["target_doctype"], "target_field": spec["target_field"]},
        fields=["definition_id", "personal_graphql_mapping"], limit_page_length=0)
    shared = [row.definition_id for row in same if not row.personal_graphql_mapping]
    if shared:
        return shared[0]
    frappe.get_doc({"doctype": "HERO Frappe Mapping", "operation": mapping["operation"], "source_type": "string|null",
                    "purpose": "Field", "priority": 5, "status": "Active", "schema_snapshot": mapping["snapshot"],
                    **spec}).insert()
    return spec["definition_id"]


def extend_mapping(operation: str) -> str:
    """Add the new rules and contract version to the area's active read mapping and service."""
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
    from hero_integration.services.simple_mapping import publish_revision

    result = publish_revision(operation, [*ids, *(rule for rule in wanted if rule not in ids)], {key: version},
                              query_document=_query(operation, version))
    return f"{result['mapping']} {key}={version}; services {', '.join(result['approved']) or '-'} approved"


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
    by_email = set(frappe.get_all("HERO Frappe Mapping", filters={"operation": "HeroProjectMatches",
        "target_field": "custom_hero_project_manager", "source_path": "partner.email"}, pluck="definition_id"))
    ids = [rule for rule in config.get("mapping_definition_ids") or [] if rule != RESPONSIBLE_BY_EMAIL and rule not in by_email]
    if config.get("project_contract_version") == "v5" and set(wanted) <= set(ids):
        return "already on v5"
    from hero_integration.services.simple_mapping import publish_revision

    result = publish_revision("HeroProjectMatches", [*ids, *(rule for rule in wanted if rule not in ids)],
                              {"project_contract_version": "v5"}, query_document=_query("HeroProjectMatches", "v5"))
    return f"{result['mapping']} project_contract_version=v5; services {', '.join(result['approved']) or '-'} approved"


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
