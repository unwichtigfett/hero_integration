"""Install the reviewed, site-scoped draft bundle during a code deployment.

This is a migration hook, not a public import endpoint. It uses the personal
draft service and never publishes mappings, approves services or contacts HERO.
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import frappe

from hero_integration.services.frappe_mapping_draft_storage import FrappeMappingDraftStorage
from hero_integration.services.mapping_draft_service import draft_key


PACKAGE_PATH = Path(__file__).resolve().parents[1] / "deployment" / "consolidated_mappings_20261103.json"


def after_migrate():
    result = install_package(json.loads(PACKAGE_PATH.read_text(encoding="utf-8")))
    if result["status"] != "OTHER_SITE":
        print("HERO draft deployment: " + json.dumps(result, sort_keys=True))
    return result


def install_package(package):
    """Create missing drafts; preserve existing, edited, discarded or published ones.

The outer migration owns the transaction. Missing Custom Fields use Frappe DDL
before draft writes; they can survive a later failure and are safe to retry.
"""
    if package.get("format_version") != 1:
        raise ValueError("Unsupported mapping draft deployment format.")
    if frappe.local.site != package["target_site"]:
        return {"status": "OTHER_SITE"}
    if package.get("owner") != "Administrator":
        raise ValueError("Bundled draft deployment requires the Administrator owner.")
    previous_user = frappe.session.user
    frappe.set_user(package["owner"])
    try:
        return _install(package)
    finally:
        frappe.set_user(previous_user)


def _install(package):
    pending, retained = [], []
    for item in package["mappings"]:
        identity = item["definition_id"]
        key = draft_key(frappe.local.site, identity, frappe.session.user)
        # Never reopen a discarded draft or undo edits/publication on a later migrate.
        if frappe.db.exists("HERO Mapping Draft", key):
            retained.append({"definition_id": identity, "draft": key, "state": "PRESERVED"})
        elif frappe.db.exists("HERO GraphQL Mapping", {"definition_id": identity}):
            retained.append({"definition_id": identity, "state": "ALREADY_DEFINED"})
        else:
            pending.append(item)
    if not pending:
        return {"status": "UNCHANGED", "package": package["package_id"], "drafts": retained}

    # Match by semantic schema identity; document names differ between sites.
    snapshots = frappe.get_all("HERO GraphQL Schema Snapshot", filters={
        "source_instance": package["source_instance"],
        "schema_fingerprint": package["schema_fingerprint"], "analysis_status": "Validated",
    }, pluck="name", order_by="creation desc, name asc", limit_page_length=1)
    if not snapshots:
        raise ValueError("Draft deployment requires a validated HERO schema matching the bundled fingerprint. "
                         "Existing drafts and active mappings were left unchanged.")
    snapshot_name = snapshots[0]
    from hero_integration.api import _validated_snapshot
    _validated_snapshot(snapshot_name, for_update=False)

    _ensure_fields(package["required_custom_fields"])
    prepared = []
    for item in pending:
        definition = {
            "site_scope": frappe.local.site, "definition_id": item["definition_id"],
            "source_instance": package["source_instance"], "snapshot_name": snapshot_name,
            "schema_fingerprint": package["schema_fingerprint"], "direction": "QUERY_TO_FRAPPE",
            "operation_kind": "QUERY", "operation_name": item["operation_name"],
        }
        content = deepcopy(item["content"])
        _bind_services(content, package, snapshot_name)
        # Validate all three domains before the first draft is written.
        storage = FrappeMappingDraftStorage()
        storage.validate_new_definition(definition, content)
        from hero_integration.services.direct_mapping import MappingSpec
        from hero_integration.services.mapping_runtime import plan_from_definitions
        specs = [MappingSpec(operation=content["target_operation"], definition_id=identity, **rule)
                 for identity, rule in content["field_rules"].items()]
        plan_from_definitions(item["operation_name"], specs, target_operation=content["target_operation"],
                              adapter_options=content.get("query_adapter"))
        prepared.append((definition, content))

    created = []
    frappe.db.savepoint("hero_deployment_drafts")
    try:
        for definition, content in prepared:
            draft = FrappeMappingDraftStorage().service().open_new_draft(
                definition, frappe.session.user, content)
            created.append({"definition_id": definition["definition_id"], "draft": draft.key,
                            "state": "CREATED"})
    except Exception:
        frappe.db.rollback(save_point="hero_deployment_drafts")
        raise
    return {"status": "DRAFTS_ONLY", "package": package["package_id"], "drafts": retained + created}


def _bind_services(content, package, snapshot_name):
    """Resolve optional local reader references without creating or approving services."""
    operation = "contacts" if content["target_operation"] == "HeroProjectMatches" else (
        content["records_path"] if content.get("query_adapter") else None)
    if operation is None:
        return
    services = frappe.get_all("HERO GraphQL Service", filters={
        # A service inherits its source from its schema snapshot.
        "schema_snapshot": snapshot_name,
        "operation_name": operation,
    }, pluck="name", limit_page_length=2)
    # Ambiguous/missing services remain unbound for review, never guessed.
    if len(services) == 1:
        if content.get("query_adapter"):
            content["query_adapter"]["read_service"] = services[0]
        else:
            content["customer_service"] = services[0]


def _ensure_fields(definitions):
    """Create only missing declared fields, checking every collision before DDL."""
    missing = {}
    for definition in definitions:
        doctype, fieldname = definition["dt"], definition["fieldname"]
        if doctype not in {"Customer", "Project", "Item"} or not fieldname.startswith("custom_hero_"):
            raise ValueError("Draft deployment field is outside the reviewed app-owned scope.")
        meta = frappe.get_meta(doctype)
        existing = meta.get_field(fieldname)
        if existing:
            expected = {key: definition[key] for key in ("fieldtype", "options", "reqd", "unique")}
            for key, value in expected.items():
                actual = getattr(existing, key, None)
                actual = int(actual or 0) if key in {"reqd", "unique"} else actual or ""
                if actual != value:
                    raise ValueError(f"Draft deployment field conflicts with {doctype}.{fieldname}: {key}.")
            continue
        missing.setdefault(doctype, []).append({key: value for key, value in definition.items() if key != "dt"})
    if missing:
        from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
        create_custom_fields(missing, update=False)
