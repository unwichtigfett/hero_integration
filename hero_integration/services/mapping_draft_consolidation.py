"""Move the reviewed union of old/new drafts onto the current active revisions."""
from copy import deepcopy
import json
from uuid import uuid5, NAMESPACE_URL

import frappe

from hero_integration.services.frappe_mapping_draft_storage import FrappeMappingDraftStorage
from hero_integration.services.mapping_draft_service import draft_key, _hash
from hero_integration.services.personal_mapping_content import RULE_FIELDS


def target_key(rule):
    return (rule["target_doctype"], rule["target_field"],
            rule.get("relation") or "" if rule["target_doctype"] == "Item Price" else "",
            rule["source_path"] if rule.get("purpose") == "Record Name" else "")


def assert_covered(source, merged):
    """Prove that archiving an input cannot silently lose a selected field rule."""
    def signature(rule):
        return (rule["source_path"], rule["target_doctype"], rule["target_field"],
                rule.get("transformation", "Identity"), rule.get("relation") or "", rule.get("priority", 5))
    wanted = {signature(rule): (identity, rule) for identity, rule in merged["field_rules"].items()}
    old = [(identity, source["field_rules"][identity]) for identity in source.get("mapping_definition_ids", [])]
    old += [(row["id"], {"source_path": row["source"]["field"], "target_doctype": row["target"]["doctype"],
             "target_field": row["target"]["field"], "transformation": row.get("transformation", "Identity"),
             "purpose": "Field"}) for row in source.get("additional_mappings", [])]
    for identity, rule in old:
        match = wanted.get(signature(rule))
        if match and all(rule.get(key) == match[1].get(key) for key in ("source_type", "purpose") if key in rule) and (
                source.get("query_adapter", {}).get("transform_options", {}).get(identity, {}) ==
                merged.get("query_adapter", {}).get("transform_options", {}).get(match[0], {})):
            continue
        # Explicitly reviewed replacement of a redundant custom category by
        # the native Item Group, with the same HERO category source.
        if (rule["target_doctype"], rule["target_field"], rule["source_path"]) == (
                "Item", "custom_hero_category", "base_data.category") and any(
                candidate[:4] == ("base_data.category", "Item", "item_group", "HERO Category Group")
                for candidate in wanted):
            continue
        raise ValueError("An old draft has a useful rule absent from the consolidated package: "
                         + rule["target_doctype"] + "." + rule["target_field"])


def consolidate(package):
    actor = frappe.session.user
    inputs = {}
    for item in package["merge_inputs"]:
        key = draft_key(frappe.local.site, item["definition_id"], actor)
        if not frappe.db.exists("HERO Mapping Draft", key):
            continue
        doc = frappe.get_doc("HERO Mapping Draft", key)
        if doc.state != "OPEN":
            continue
        if _hash(json.loads(doc.content_json)) != item["content_hash"]:
            raise ValueError("A draft changed after the reviewed merge; preserve it and rebuild the package: " + key)
        merged = next(row["content"] for row in package["mappings"]
                      if row["content"]["target_operation"] == item["operation"])
        assert_covered(json.loads(doc.content_json), merged)
        inputs[item["definition_id"]] = doc
    parents = {}
    for name in frappe.get_all("HERO GraphQL Mapping", filters={"status": "Active", "direction": "QUERY_TO_FRAPPE",
            "source_instance": package["source_instance"], "schema_fingerprint": package["schema_fingerprint"]},
            pluck="name", limit_page_length=0):
        parent = frappe.get_doc("HERO GraphQL Mapping", name)
        operation = json.loads(parent.mapping_json).get("target_operation")
        parents.setdefault(operation, []).append(parent)
    result, destinations = [], set()
    for item in package["mappings"]:
        proposed = item["content"]
        if set(proposed["mapping_definition_ids"]) != set(proposed["field_rules"]):
            raise ValueError("Consolidation package must contain exactly the selected rule snapshots.")
        operation = proposed["target_operation"]
        candidates = parents.get(operation, [])
        if len(candidates) != 1:
            raise ValueError("Consolidation requires one current active mapping for " + operation)
        parent = candidates[0]
        destinations.add(parent.definition_id)
        key = draft_key(frappe.local.site, parent.definition_id, actor)
        if frappe.db.get_value("HERO Mapping Draft", key, "state") == "OPEN" and parent.definition_id not in inputs:
            raise ValueError("An unreviewed current draft must be included before consolidation: " + key)
        storage = FrappeMappingDraftStorage()
        service = storage.service()
        old = inputs.get(parent.definition_id)
        if old:
            service.discard(parent.definition_id, actor, int(old.generation), int(old.content_revision), confirmed=True)
        # Reopening starts a new generation with the actual target revision and
        # freshly captured field-rule provenance, retaining the old generation.
        draft = service.open_draft(parent.definition_id, actor)
        assert_covered(draft.content, proposed)
        content = deepcopy(draft.content)
        available = frappe.get_all("HERO Frappe Mapping", filters={"status": "Active",
            "operation": operation, "schema_snapshot": parent.schema_snapshot},
            fields=["definition_id", "personal_graphql_mapping", *sorted(RULE_FIELDS)], limit_page_length=0)
        existing = {}
        selected = set(content["mapping_definition_ids"])
        for rule in sorted(available, key=lambda row: (row.definition_id not in selected, row.definition_id)):
            if rule.personal_graphql_mapping in (None, "", parent.name):
                existing.setdefault(target_key(rule), rule.definition_id)
        ids, remapped = [], {}
        for imported_id, rule in proposed["field_rules"].items():
            key = target_key(rule)
            identity = existing.get(key)
            if identity is None:
                identity = str(uuid5(NAMESPACE_URL, package["package_id"] + "/current/" + operation + "/" + json.dumps(key)))
                frappe.get_doc({"doctype": "HERO Frappe Mapping", "definition_id": identity,
                    "operation": operation, "schema_snapshot": parent.schema_snapshot, "status": "Active",
                    **rule}).insert()
            ids.append(identity)
            remapped[imported_id] = identity
            content["field_rules"][identity] = deepcopy(rule)
        if len(set(ids)) != len(ids):
            raise ValueError("Consolidation would select the same target rule twice.")
        content["mapping_definition_ids"] = ids
        for field in ("customer_contract_version", "project_contract_version"):
            if field in proposed:
                content[field] = proposed[field]
        if "additional_mappings" in content:
            content["additional_mappings"] = []  # these are now normal selected rules
        if proposed.get("query_adapter"):
            content["query_adapter"]["transform_options"] = {
                remapped[identity]: options for identity, options in proposed["query_adapter"].get("transform_options", {}).items()}
        saved = service.save(parent.definition_id, actor, draft.content_revision, int(parent.mapping_revision),
            content, expected_generation=draft.generation,
            expected_reconciliation_revision=draft.reconciliation_revision).draft
        assert saved.base_active_revision == int(parent.mapping_revision)
        result.append({"operation": operation, "draft": saved.key, "active_revision": saved.base_active_revision,
                       "draft_revision": saved.content_revision, "rules": len(ids)})
    # Every old/new input was reviewed into the package. Archive only after all
    # current-generation replacement drafts have been saved successfully.
    for identity, doc in inputs.items():
        if identity not in destinations:
            FrappeMappingDraftStorage().service().discard(identity, actor, int(doc.generation),
                                                        int(doc.content_revision), confirmed=True)
    return result
