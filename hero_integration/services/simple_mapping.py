"""Mapping page: list the active mapping rules; mappings are loaded as a JSON configuration file.

Owner decision 2026-11-04: mappings are not edited on the website. ``state`` feeds the
read-only list; ``publish_revision`` publishes a read mapping revision through the
personal-draft publisher for app patches and approves its read service again.
"""
from __future__ import annotations

import json

import frappe

LABELS = {"HeroCustomers": "Customers", "HeroProjectMatches": "Projects", "HeroCalendarEvents": "Events",
          "HeroTasks": "Tasks", "HeroProducts": "Products", "HeroDocuments": "Documents", "HeroPictures": "Pictures",
          "HeroEmployees": "Employees", "HeroServices": "Services"}
RULE_FIELDS = ["name", "definition_id", "operation", "source_path", "source_type", "target_doctype", "target_field",
               "purpose", "transformation", "relation", "mapping_revision", "status", "schema_snapshot",
               "personal_graphql_mapping"]


class SimpleMappingError(frappe.ValidationError):
    pass


# ---------------------------------------------------------------- reading

def _read_mappings() -> dict[str, dict]:
    """Active read mapping per area: {operation: {name, config, snapshot, revision, operation_name}}."""
    from hero_integration.api import _configured_source_instance
    result = {}
    for row in frappe.get_all("HERO GraphQL Mapping", filters={"status": "Active", "direction": "QUERY_TO_FRAPPE", "source_instance": _configured_source_instance()},
                              fields=["name", "mapping_json", "schema_snapshot", "mapping_revision", "operation_name"],
                              order_by="modified desc", limit_page_length=0):
        try:
            config = json.loads(row.mapping_json or "{}")
        except (TypeError, ValueError):
            continue
        operation = config.get("target_operation") if isinstance(config, dict) else None
        if operation and operation not in result:
            result[operation] = {"name": row.name, "config": config, "snapshot": row.schema_snapshot,
                                 "revision": int(row.mapping_revision or 0), "operation_name": row.operation_name}
    return result


def _services(mapping_name: str) -> list:
    return frappe.get_all("HERO GraphQL Service", filters={"read_mapping": mapping_name, "status": "Validated"},
                          fields=["name", "query_document", "service_revision", "execution_status"], limit_page_length=0)


def _rule_rows(operation: str, mapping: dict) -> list[dict]:
    ids = list(mapping["config"].get("mapping_definition_ids") or [])
    rows = frappe.get_all("HERO Frappe Mapping", filters={"definition_id": ["in", ids or [""]], "status": "Active"},
                          fields=RULE_FIELDS, limit_page_length=0)
    by_id = {row.definition_id: row for row in rows}
    return [_row(operation, by_id[identity], mapping) for identity in ids if identity in by_id]


def _row(operation: str, row, mapping: dict) -> dict:
    return {"status": "Active", "operation": operation, "operation_label": LABELS.get(operation, operation),
            "hero": row.source_path, "source_type": row.source_type, "app": _app(row.target_doctype),
            "target": f"{row.target_doctype}.{row.target_field}", "target_doctype": row.target_doctype,
            "target_field": row.target_field, "transformation": row.transformation, "purpose": row.purpose,
            "revision": int(row.mapping_revision or 0), "definition_id": row.definition_id, "mapping": mapping["name"]}


def _configured_relation_rows(operation, mapping, existing=()):
    """Project declared adapter fields into the same read-only list, without new rule records."""
    options = mapping['config'].get('query_adapter') or {}
    result = []
    seen = {(row.get('hero'), row.get('target_doctype'), row.get('target_field')) for row in existing}
    def add(path, doctype, field, transformation, suffix=''):
        key = (path, doctype, field)
        if not isinstance(path, str) or not path or key in seen: return
        seen.add(key)
        result.append({'status': 'Active', 'operation': operation, 'operation_label': LABELS.get(operation, operation),
            'hero': path, 'app': _app(doctype), 'target': f'{doctype}.{field}' + suffix,
            'target_doctype': doctype, 'target_field': field, 'transformation': transformation,
            'revision': mapping['revision'], 'definition_id': '', 'mapping': mapping['name']})
    for relation in options.get('native_relations', []):
        kind = relation.get('kind')
        if kind not in {'customer_bank_account', 'project_address', 'customer_addresses', 'customer_contacts', 'sales_prices'}:
            continue
        prefix = (relation.get('collection_path') + '.') if relation.get('collection_path') else ''
        for field, path in (relation.get('fields') or {}).items():
            doctype = ('Bank' if field in {'bank_name', 'swift_number'} else 'Bank Account') if kind == 'customer_bank_account' else (
                'Item Price' if kind == 'sales_prices' else 'Contact' if kind == 'customer_contacts' else 'Address')
            suffix = ''
            if kind == 'sales_prices':
                names = [binding['name'] for binding in relation.get('price_lists', {}).values()]
                suffix = ' (' + ', '.join(names) + ')' if names else ''
            add(prefix + path, doctype, field, kind, suffix)
        if relation.get('country_code_path'):
            add(prefix + relation['country_code_path'], 'Address', 'country', 'Country Code')
        if relation.get('parent_field') and relation.get('identity_path'):
            add(relation['identity_path'], 'Project', relation['parent_field'], 'Address Link')
    logbook = options.get('logbook') or {}
    for path in logbook.get('context_paths', []):
        key = path[len(logbook.get('path', '')) + 1:]
        add(path, 'Comment', 'custom_hero_logbook_context', 'JSON Value', ' (' + key + ')')
    return result


def _app(doctype: str) -> str:
    from frappe.modules.utils import get_module_app

    try:
        return get_module_app(frappe.db.get_value("DocType", doctype, "module")) or ""
    except Exception:
        return ""


def state() -> dict:
    """Everything the Mapping page shows: the areas and their active rules."""
    from hero_integration.api import _configured_source_instance
    mappings = _read_mappings()
    operations, rules = [], []
    for operation, mapping in sorted(mappings.items(), key=lambda item: LABELS.get(item[0], item[0])):
        area_rows = _rule_rows(operation, mapping)
        rules.extend(area_rows)
        rules.extend(_configured_relation_rows(operation, mapping, area_rows))
        operations.append({"operation": operation, "label": LABELS.get(operation, operation),
                           "mapping": mapping["name"], "snapshot": mapping["snapshot"], "revision": mapping["revision"]})
    sends = []
    active = frappe.get_all("HERO GraphQL Mapping", filters={"status": "Active", "direction": "FRAPPE_TO_MUTATION", "source_instance": _configured_source_instance()},
                            fields=["name", "definition_id", "mapping_json", "mapping_revision", "operation_name"], limit_page_length=0)
    drafts, replaced = _mutation_drafts(active, _configured_source_instance())
    for row in active:
        if row.name in replaced:
            continue
        try:
            config = json.loads(row.mapping_json or "{}")
        except (TypeError, ValueError):
            continue
        sends.extend(_send_rows(config, row.name, row.mapping_revision, row.operation_name, "Active"))
    sends.extend(drafts)
    snapshots = frappe.get_all("HERO GraphQL Schema Snapshot", filters={"analysis_status": "Validated", "source_instance": _configured_source_instance()},
                               fields=["name", "creation"], order_by="creation desc", limit_page_length=20)
    return {"operations": operations, "rules": rules, "sends": sends, "snapshots": [row.name for row in snapshots]}


def _send_rows(config, name, revision, operation, status):
    if not isinstance(config, dict): return []
    mapped = config.get('usage') == 'MAPPED_UPDATE_V2'
    items = config.get('assignments', []) if mapped else [config, *(config.get('additional_mappings') or [])]
    if not isinstance(items, list): return []
    result = []
    for item in items:
        if not isinstance(item, dict): continue
        source, target = item.get('source') or {}, item.get('target') or {}
        if not isinstance(source, dict) or not isinstance(target, dict): continue
        hero = item.get('input_path') if mapped else f"{target.get('argument') or ''}.{target.get('field_path') or target.get('field') or ''}".strip('.')
        if not source.get('doctype') or not source.get('field') or not hero: continue
        area = config.get('area') if mapped else None
        result.append({'status': status, 'operation': operation or target.get('operation') or '',
            'operation_label': LABELS.get(area, operation or target.get('operation') or ''),
            'hero': hero, 'app': source.get('app') or _app(source['doctype']),
            'target': f"{source['doctype']}.{source['field']}", 'target_doctype': source['doctype'],
            'target_field': source['field'], 'transformation': item.get('resolver') if mapped else item.get('transformation'),
            'revision': int(revision or 0), 'mapping': name, 'definition_id': item.get('id', '')})
    return result


def _mutation_drafts(active, source_instance):
    """Show only this actor's open, source-scoped updates; drafts never grant execution."""
    owner = getattr(getattr(frappe, 'session', None), 'user', None)
    site = getattr(getattr(frappe, 'local', None), 'site', None)
    if not owner or not site: return [], set()
    by_name = {row.name: row for row in active}
    rows = frappe.get_all('HERO Mapping Draft', filters={'owner_user': owner, 'site_scope': site, 'state': 'OPEN'},
        fields=['name', 'definition_id', 'owner_user', 'site_scope', 'new_definition_json', 'content_json',
                'base_active_mapping', 'content_revision'], order_by='modified desc', limit_page_length=0)
    result, replaced, seen = [], set(), set()
    for row in rows:
        if row.owner_user != owner or row.site_scope != site or row.definition_id in seen: continue
        try:
            config = json.loads(row.content_json or '{}')
            context = json.loads(row.new_definition_json or 'null')
        except (TypeError, ValueError): continue
        if not isinstance(config, dict) or config.get('usage') != 'MAPPED_UPDATE_V2': continue
        parent = by_name.get(row.base_active_mapping)
        if isinstance(context, dict):
            if (context.get('source_instance') != source_instance or context.get('site_scope') != site
                    or context.get('direction') != 'FRAPPE_TO_MUTATION'
                    or context.get('operation_kind') != 'MUTATION'
                    or context.get('definition_id') != row.definition_id): continue
            operation = context.get('operation_name')
        elif parent:
            operation = parent.operation_name
        else: continue
        target = config.get('target')
        if not isinstance(target, dict) or target.get('operation') != operation: continue
        entries = _send_rows(config, row.name, row.content_revision, operation, 'Draft')
        if entries:
            result.extend(entries); seen.add(row.definition_id)
            if parent: replaced.add(parent.name)
    return result, replaced


# ---------------------------------------------------------------- revision publication for installation patches

def publish_revision(operation: str, ids: list[str] | None = None, content_changes: dict | None = None,
                     query_document: str | None = None) -> dict:
    """Publish the area's read mapping as a new revision through the personal draft publisher and approve
    its read service again. ``ids`` replaces the rule list, ``content_changes`` sets contract keys and
    ``query_document`` replaces the service query (for a contract version change)."""
    from copy import deepcopy

    from hero_integration import api
    from hero_integration.services.frappe_mapping_draft_storage import FrappeMappingDraftStorage
    from hero_integration.services.mapping_draft_service import draft_key

    mapping = _read_mappings().get(operation)
    if not mapping:
        raise SimpleMappingError("This area has no active read mapping.")
    parent = frappe.get_doc("HERO GraphQL Mapping", mapping["name"], for_update=True)
    actor = frappe.session.user
    storage = FrappeMappingDraftStorage()
    service = storage.service()
    key = draft_key(frappe.local.site, parent.definition_id, actor)
    if frappe.db.get_value("HERO Mapping Draft", key, "state") == "OPEN":
        old = frappe.get_doc("HERO Mapping Draft", key, for_update=True)
        if int(old.content_revision or 0):
            raise SimpleMappingError("You have an unpublished draft of this mapping; reconcile it through JSON import first.")
        service.discard(parent.definition_id, actor, int(old.generation), int(old.content_revision), confirmed=True)
    readers = _services(parent.name)
    for reader in readers:  # publication needs a currently approved reader
        doc = frappe.get_doc("HERO GraphQL Service", reader.name)
        if doc.execution_status != "Approved" or int(doc.approved_mapping_revision or 0) != int(parent.mapping_revision or 0):
            api.approve_read_service(doc.name, parent.name, int(doc.service_revision or 0), int(parent.mapping_revision or 0))
    draft = service.open_draft(parent.definition_id, actor)
    content = deepcopy(draft.content)
    if ids is not None:
        content["mapping_definition_ids"] = list(ids)
    content.update(content_changes or {})
    saved = service.save(parent.definition_id, actor, draft.content_revision, int(parent.mapping_revision), content,
                         expected_generation=draft.generation,
                         expected_reconciliation_revision=draft.reconciliation_revision).draft
    if query_document is not None:
        for reader in readers:
            doc = frappe.get_doc("HERO GraphQL Service", reader.name)
            doc.query_document = query_document
            doc.save()
    published = storage.publish(parent.definition_id, actor, saved.generation, saved.content_revision,
                                int(parent.mapping_revision), saved.reconciliation_revision, confirmed=True)
    current = frappe.get_doc("HERO GraphQL Mapping", published["name"])
    # The helper draft is published; do not leave it open.
    if frappe.db.get_value("HERO Mapping Draft", key, "state") == "OPEN":
        left = frappe.get_doc("HERO Mapping Draft", key)
        service.discard(current.definition_id, actor, int(left.generation), int(left.content_revision), confirmed=True)
    approved = []
    for reader in readers:
        doc = frappe.get_doc("HERO GraphQL Service", reader.name)
        api.approve_read_service(doc.name, current.name, int(doc.service_revision or 0), int(current.mapping_revision or 0))
        approved.append(doc.name)
    return {"mapping": current.name, "revision": int(current.mapping_revision or 0), "approved": approved}
