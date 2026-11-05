"""Replace an existing native mapping through its revisioned personal publisher."""
from copy import deepcopy
import json
from uuid import uuid4

import frappe

from hero_integration.services.frappe_mapping_draft_storage import FrappeMappingDraftStorage
from hero_integration.services.mapping_draft_service import draft_key
from hero_integration.services.personal_mapping_content import RULE_FIELDS, same


def _target(rule):
    return (rule['target_doctype'], rule['target_field'],
            rule.get('source_path') if rule.get('purpose') == 'Record Name' else '')


def replace_native_mapping(snapshot, item, rules, source_instance, created, reader, existing, schema=None):
    from hero_integration.services.config_exchange import ConfigExchangeError, _resolved_mapping
    from hero_integration.services.mapping_draft_exchange import enroll_portable_rule_identity

    operation = item['target_operation']
    parent = frappe.get_doc('HERO GraphQL Mapping', existing.name, for_update=True)
    if (parent.source_instance != source_instance or parent.schema_snapshot != snapshot.name
            or parent.status != 'Active' or parent.direction != 'QUERY_TO_FRAPPE'
            or json.loads(parent.mapping_json).get('target_operation') != operation
            or item['mapping'].get('query_adapter')):
        raise ConfigExchangeError('Native replacement requires its current source-bound mapping.')
    actor = frappe.session.user
    storage = FrappeMappingDraftStorage()
    service = storage.service()
    key = draft_key(frappe.local.site, parent.definition_id, actor)
    if frappe.db.get_value('HERO Mapping Draft', key, 'state') == 'OPEN':
        old = frappe.get_doc('HERO Mapping Draft', key, for_update=True)
        service.discard(parent.definition_id, actor, int(old.generation), int(old.content_revision), confirmed=True)
    draft = service.open_draft(parent.definition_id, actor)
    content = deepcopy(draft.content)
    incoming = _resolved_mapping(item, source_instance, created)
    selected = set(content['mapping_definition_ids'])
    available = frappe.get_all('HERO Frappe Mapping', filters={'operation': operation, 'status': 'Active',
        'schema_snapshot': snapshot.name}, fields=['name', 'definition_id', 'personal_graphql_mapping',
            'portable_definition_id', 'mapping_revision', *sorted(RULE_FIELDS)], limit_page_length=0)
    by_target = {}
    for row in sorted(available, key=lambda row: (row.definition_id not in selected, row.definition_id)):
        if row.personal_graphql_mapping in (None, '', parent.name):
            by_target.setdefault(_target(row), row)
    ids, changes = [], []
    for imported_id in incoming['mapping_definition_ids']:
        desired = {field: rules[imported_id].get(field) for field in RULE_FIELDS}
        current = by_target.get(_target(desired))
        if current is None:
            # These are new targets, not edits to active shared rules. Normal
            # controller validation and target uniqueness still apply.
            values = {'doctype': 'HERO Frappe Mapping', 'definition_id': str(uuid4()),
                'operation': operation, 'schema_snapshot': snapshot.name, 'status': 'Active', **desired}
            if schema:
                from hero_integration.services.mapping_draft_exchange import _portable_schema_graph
                dependencies = rules[imported_id].get('schema_dependencies', [])
                _, closure = _portable_schema_graph(schema)
                required = closure(dependencies)
                values['schema_manifest'] = json.dumps({group: [entry for entry in entries if entry['id'] in required]
                                                        for group, entries in schema.items()})
                values['schema_dependencies'] = json.dumps({'ids': dependencies})
            current = frappe.get_doc(values).insert()
        identity = current.definition_id
        if identity in ids:
            raise ConfigExchangeError('Native replacement contains a duplicate target field.')
        ids.append(identity)
        content['field_rules'][identity] = desired
        if not same({field: current.get(field) for field in RULE_FIELDS}, desired):
            changes.append(current)
    content['mapping_definition_ids'] = ids
    # Keep target-owned immutable reader/pagination metadata. Editable native
    # contract versions and relationships use the file's checked configuration.
    for field in ('customer_contract_version', 'project_contract_version', 'customer_service'):
        if field in incoming:
            content[field] = incoming[field]
    if 'additional_mappings' in content:
        content['additional_mappings'] = []
    for field in ('records_path', 'identity_path', 'variables', 'page_size', 'native_options', 'native_task_onboarding'):
        if content.get(field) != incoming.get(field):
            raise ConfigExchangeError('Native replacement changes a target-owned reader contract: {0}', field)
    saved = service.save(parent.definition_id, actor, draft.content_revision, int(parent.mapping_revision), content,
        expected_generation=draft.generation, expected_reconciliation_revision=draft.reconciliation_revision).draft
    for current in changes:
        if not current.get('portable_definition_id'):
            enroll_portable_rule_identity(parent.definition_id, current.definition_id, str(uuid4()),
                int(parent.mapping_revision), int(current.mapping_revision))
    published = storage.publish(parent.definition_id, actor, saved.generation, saved.content_revision,
                                int(parent.mapping_revision), saved.reconciliation_revision, confirmed=True)
    return frappe.get_doc('HERO GraphQL Mapping', published['name'])
