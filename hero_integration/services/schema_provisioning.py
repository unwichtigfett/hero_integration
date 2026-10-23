"""Preview and provision declared HERO-owned Frappe schema without SQL writes."""
from __future__ import annotations

from dataclasses import asdict
import json
import re
from types import SimpleNamespace

import frappe

from hero_integration.services import mapping_exchange
from hero_integration.services.direct_mapping import MappingSpec, validate_spec


OWNED_DOCTYPE_PREFIX = "HERO "
OWNED_FIELD_PREFIX = "custom_hero_"
FIELD_TYPES = {"Data", "Small Text", "Int", "Float", "Check", "Date", "Datetime", "Link", "Table"}
FIELD_TYPES.add('JSON')
SCHEMA_STATES = ("New", "Unchanged", "Changed", "Conflict", "Invalid")


class SchemaProvisioningError(ValueError):
    pass


def preview_package(snapshot_name: str, payload_json: str) -> dict[str, object]:
    snapshot = mapping_exchange._validated_snapshot(snapshot_name)
    package = mapping_exchange._parse_payload(payload_json, snapshot.schema_fingerprint)
    definitions = _definitions(package["schema"])
    items = _schema_items(definitions)
    items.extend(_mapping_items(package, definitions, items))
    counts = {state: sum(item["state"] == state for item in items) for state in SCHEMA_STATES}
    return {
        "format_version": mapping_exchange.FORMAT_VERSION,
        "schema_fingerprint": snapshot.schema_fingerprint,
        "items": items,
        "counts": counts,
        "selection_requires_confirmation": True,
    }


def adopt_package(
    snapshot_name: str, payload_json: str, selected_ids: list[str], confirmed: bool,
) -> dict[str, object]:
    """Apply one checked package. DDL may persist, but mappings stay inactive on failure."""
    if confirmed is not True:
        raise SchemaProvisioningError("Administrator confirmation is required before adoption.")
    preview = preview_package(snapshot_name, payload_json)
    selected = set(selected_ids)
    items = {item["id"]: item for item in preview["items"]}
    if not selected or not selected <= set(items):
        raise SchemaProvisioningError("The selection is empty or contains an unknown checklist item.")
    _validate_selection(items, selected)
    snapshot = mapping_exchange._validated_snapshot(snapshot_name)
    package = mapping_exchange._parse_payload(payload_json, snapshot.schema_fingerprint)
    definitions = _definitions(package["schema"])
    _require_schema_permissions(definitions, selected)
    created = {"custom_doctypes": [], "custom_fields": [], "mappings": []}
    relation_fields = _relation_fields(definitions)
    selected_relation_fields = {
        relation["field_id"] for relation in definitions["relations"] if relation["id"] in selected
    }
    savepoint = "hero_mapping_adoption"
    mapping_savepoint = False
    try:
        for definition in _ordered_doctypes(definitions, selected):
            if _doctype_is_new(definition):
                _create_custom_doctype(definition, selected_relation_fields)
                created["custom_doctypes"].append(definition["name"])
        for definition in definitions["custom_fields"]:
            if definition["id"] in selected and _custom_field_is_new(definition):
                _create_custom_field(definition)
                created["custom_fields"].append(f"{definition['doctype']}.{definition['fieldname']}")
        for relation in definitions["relations"]:
            if relation["id"] in selected:
                _provision_relation(relation, relation_fields, created)
        _validate_selected_relations(definitions, selected)
        # MariaDB DDL may commit and invalidate earlier savepoints. All mapping
        # activation belongs to a fresh DML transaction after provisioning.
        frappe.db.savepoint(savepoint)
        mapping_savepoint = True
        _activate_selected_mappings(snapshot_name, package, selected, created)
    except Exception:
        if mapping_savepoint:
            frappe.db.rollback(save_point=savepoint)
        raise
    return {
        "status": "COMPLETED",
        "created": created,
        "schema_note": "Schema changes are documented by this result. Frappe DDL rollback is not promised.",
        "data_migration_started": False,
    }


def _definitions(schema: object) -> dict[str, list[dict[str, object]]]:
    if not isinstance(schema, dict):
        raise SchemaProvisioningError("Schema must be an object.")
    result = {key: list(schema[key]) for key in ("custom_doctypes", "custom_fields", "relations")}
    ids: set[str] = set()
    fields: dict[str, dict[str, object]] = {}
    child_names: set[str] = set()
    proposed_names: set[str] = set()
    for definition in result["custom_doctypes"]:
        _require_keys(definition, {"id", "name", "label", "is_child", "fields"}, "custom DocType")
        _claim_id(definition, ids)
        if not _owned_doctype(definition["name"]) or not isinstance(definition["is_child"], bool):
            raise SchemaProvisioningError("Custom DocTypes must use the HERO prefix and a boolean child flag.")
        if definition['name'] in proposed_names:
            raise SchemaProvisioningError('Duplicate Custom DocType name.')
        proposed_names.add(str(definition["name"]))
        if definition["is_child"]:
            child_names.add(str(definition["name"]))
        if not isinstance(definition["fields"], list):
            raise SchemaProvisioningError("Custom DocType fields must be a list.")
        for field in definition["fields"]:
            _validate_field(field, ids, fields, custom_doctype=str(definition["name"]))
    for definition in result["custom_fields"]:
        required = {"id", "doctype", "fieldname", "label", "fieldtype", "options", "insert_after"}
        if not isinstance(definition, dict) or not required <= set(definition) or set(definition) - required - {'reqd', 'unique', 'read_only'}:
            raise SchemaProvisioningError('Custom field has an unknown or missing field.')
        _validate_field({key: definition[key] for key in ("id", "fieldname", "label", "fieldtype", "options", 'reqd', 'unique', 'read_only') if key in definition}, ids, fields, custom_doctype=None)
        if not _owned_field(definition["fieldname"]):
            raise SchemaProvisioningError("Custom Fields must use the custom_hero_ prefix.")
    for relation in result["relations"]:
        _require_keys(relation, {"id", "doctype", "field_id", "fieldname", "relation_type", "target_doctype", "dependencies"}, "relation")
        _claim_id(relation, ids)
        field = fields.get(str(relation["field_id"]))
        if not field or relation["relation_type"] not in {"Link", "Table"}:
            raise SchemaProvisioningError("A relation must reference a declared Link or Table field.")
        if field["fieldname"] != relation["fieldname"] or field["fieldtype"] != relation["relation_type"] or field["options"] != relation["target_doctype"]:
            raise SchemaProvisioningError("Relation type and target must match its declared field.")
        owner = next((doc['name'] for doc in result['custom_doctypes'] if any(f['id'] == relation['field_id'] for f in doc['fields'])), None)
        owner = owner or next((f['doctype'] for f in result['custom_fields'] if f['id'] == relation['field_id']), None)
        if owner != relation['doctype']:
            raise SchemaProvisioningError('Relation owner must match its declared field owner.')
        if not isinstance(relation["dependencies"], list) or not all(isinstance(value, str) for value in relation["dependencies"]):
            raise SchemaProvisioningError("Relation dependencies must be a list of checklist IDs.")
        if relation["relation_type"] == "Table" and str(relation["target_doctype"]) not in child_names:
            raise SchemaProvisioningError("Table relations require a declared HERO child DocType.")
        if relation["relation_type"] == "Link" and not _doctype_exists(str(relation["target_doctype"])) and str(relation["target_doctype"]) not in proposed_names:
            raise SchemaProvisioningError("Link relations require an installed or declared target DocType.")
    declared_locations = [(doc['name'], f['fieldname']) for doc in result['custom_doctypes'] for f in doc['fields']]
    declared_locations.extend((f['doctype'], f['fieldname']) for f in result['custom_fields'])
    if len(declared_locations) != len(set(declared_locations)):
        raise SchemaProvisioningError('Duplicate schema target field.')
    relation_ids = [r['field_id'] for r in result['relations']]
    if len(relation_ids) != len(set(relation_ids)):
        raise SchemaProvisioningError('A field must not have duplicate relation definitions.')
    if any(f['fieldtype'] in {'Link', 'Table'} and fid not in relation_ids for fid, f in fields.items()):
        raise SchemaProvisioningError('Every Link or Table field requires an explicit relation.')
    checklist_ids = {d['id'] for group in result.values() for d in group}
    if any(dep not in checklist_ids for r in result['relations'] for dep in r['dependencies']):
        raise SchemaProvisioningError('Unknown relation dependency.')
    return result


def _schema_items(definitions: dict[str, list[dict[str, object]]]) -> list[dict[str, object]]:
    items = []
    relation_field_ids = {relation["field_id"] for relation in definitions["relations"]}
    for definition in definitions["custom_doctypes"]:
        state = "New" if _doctype_is_new(definition) else _doctype_state(definition, relation_field_ids)
        items.append(_item(definition, "New Custom DocTypes", state, [], definition["name"]))
    for definition in definitions["custom_fields"]:
        state = "New" if _custom_field_is_new(definition) else _custom_field_state(definition)
        deps = [doc["id"] for doc in definitions["custom_doctypes"] if doc["name"] == definition["doctype"]]
        items.append(_item(definition, "New Custom Fields", state, deps, f"{definition['doctype']}.{definition['fieldname']}"))
    for definition in definitions["relations"]:
        state = "Unchanged" if _relation_exists(definition) else "New"
        try:
            field = frappe.get_meta(definition['doctype']).get_field(definition['fieldname'])
        except Exception:
            field = None
        if field and state == 'New':
            state = 'Conflict'
        deps = list(definition["dependencies"])
        items.append(_item(definition, "Relations / Link Fields", state, deps, f"{definition['relation_type']} → {definition['target_doctype']}"))
    return items


def _mapping_items(package: dict[str, object], definitions: dict[str, list[dict[str, object]]], items: list[dict[str, object]]) -> list[dict[str, object]]:
    existing = {item["id"]: item for item in items}
    mapping_preview = mapping_exchange.preview_import_from_package(package, metadata=_PlannedMetadata(definitions))
    result = []
    for row, mapping in zip(mapping_preview["items"], package["mappings"], strict=True):
        dependencies = list(mapping["schema_dependencies"])
        unknown = [value for value in dependencies if value not in existing]
        state = "Invalid" if unknown else row["state"]
        detail = "Unknown schema dependency." if unknown else row["detail"]
        declared = _target_dependency(definitions, mapping)
        if declared and declared not in dependencies:
            state, detail = 'Invalid', 'Declared mapping target requires its schema dependency.'
        if any(existing.get(dep, {}).get('state') in {'Invalid', 'Conflict'} for dep in dependencies):
            state, detail = 'Conflict', 'A required schema definition conflicts with the target.'
        group = "Changed Field Mappings" if state == "Changed" else "New Field Mappings"
        result.append({
            "id": f"mapping:{mapping['definition_id']}", "kind": "mapping", "group": group,
            "state": state, "selectable": state in {"New", "Changed"}, "dependencies": dependencies,
            "detail": detail, "label": f"{mapping['source_path']} → {mapping['target_doctype']}.{mapping['target_field']}",
            "definition": mapping,
        })
    return result


def _declares_target(definitions, mapping):
    return any(
        field["doctype"] == mapping["target_doctype"] and field["fieldname"] == mapping["target_field"]
        for field in definitions["custom_fields"]
    )


def _target_dependency(definitions, mapping):
    for field in definitions['custom_fields']:
        if field['doctype'] == mapping['target_doctype'] and field['fieldname'] == mapping['target_field']:
            return field['id']
    for definition in definitions['custom_doctypes']:
        if definition['name'] == mapping['target_doctype'] and any(f['fieldname'] == mapping['target_field'] for f in definition['fields']):
            return definition['id']
    return None


def _item(definition, group, state, dependencies, label):
    return {
        "id": definition["id"], "kind": "schema", "group": group, "state": state,
        "selectable": state in {"New", "Changed"}, "dependencies": dependencies,
        "detail": "Validated schema definition." if state == "New" else "Target already exists.",
        "label": label, "definition": definition,
    }


def _validate_selection(items, selected):
    for item_id in selected:
        item = items[item_id]
        if item["state"] == "Unchanged":
            continue
        if not item["selectable"]:
            raise SchemaProvisioningError(f"{item_id} cannot be selected in state {item['state']}.")
        missing = [dependency for dependency in item["dependencies"] if dependency not in selected and items.get(dependency, {}).get("state") != "Unchanged"]
        if missing:
            raise SchemaProvisioningError(f"{item_id} requires: {', '.join(missing)}.")


def _require_schema_permissions(definitions, selected):
    new_doctypes = {d['name'] for d in definitions['custom_doctypes'] if d['id'] in selected and _doctype_is_new(d)}
    for definition in definitions["custom_doctypes"]:
        if definition["id"] in selected:
            frappe.has_permission("DocType", "create", throw=True)
    for definition in definitions["custom_fields"]:
        if definition["id"] in selected:
            frappe.has_permission("Custom Field", "create", throw=True)
            frappe.has_permission(definition["doctype"], "write", throw=True)
    for relation in definitions["relations"]:
        if relation["id"] in selected:
            frappe.has_permission("Custom Field", "create", throw=True)
            if relation['doctype'] not in new_doctypes:
                frappe.has_permission(relation["doctype"], "write", throw=True)


def _validate_selected_relations(definitions, selected):
    for relation in definitions["relations"]:
        if relation["id"] in selected and not _relation_exists(relation):
            raise SchemaProvisioningError("A selected relation is not available after schema provisioning.")


def _ordered_doctypes(definitions, selected):
    """Create declared relation targets before the DocTypes that use them."""
    requested = [definition for definition in definitions["custom_doctypes"] if definition["id"] in selected]
    by_name = {definition["name"]: definition for definition in requested}
    dependencies = {name: set() for name in by_name}
    for definition in requested:
        for field in definition["fields"]:
            target = field["options"]
            if field["fieldtype"] in {"Link", "Table"} and target in by_name and target != definition["name"]:
                dependencies[definition["name"]].add(target)
    for relation in definitions["relations"]:
        if relation["id"] not in selected or relation["doctype"] not in dependencies:
            continue
        target = relation["target_doctype"]
        if target in by_name and target != relation["doctype"]:
            dependencies[relation["doctype"]].add(target)
    ordered = []
    while dependencies:
        ready = [name for name, requires in dependencies.items() if not requires]
        if not ready:
            raise SchemaProvisioningError("Selected Custom DocType relations contain a cycle.")
        for name in sorted(ready):
            ordered.append(by_name[name])
            del dependencies[name]
        for requires in dependencies.values():
            requires.difference_update(ready)
    return ordered


def _activate_selected_mappings(snapshot_name, package, selected, created):
    for mapping in package["mappings"]:
        item_id = f"mapping:{mapping['definition_id']}"
        if item_id not in selected:
            continue
        values = {key: mapping[key] for key in mapping_exchange.EXPORTED_FIELDS if key != "mapping_revision"}
        values["schema_dependencies"] = json.dumps({"ids": mapping["schema_dependencies"]}, sort_keys=True, separators=(",", ":"))
        values.update({
            "doctype": "HERO Frappe Mapping", "schema_snapshot": snapshot_name,
            "schema_manifest": mapping_exchange._canonical_json(package["schema"]), "status": "Draft",
        })
        local = _mapping_by_definition(mapping["definition_id"])
        if local and mapping_exchange._definition_hash(mapping_exchange._exportable(local)) == mapping_exchange._definition_hash(mapping):
            continue
        document = frappe.get_doc("HERO Frappe Mapping", local.name) if local else frappe.get_doc(values)
        if local:
            if local.status == "Active":
                raise SchemaProvisioningError("An active mapping cannot be replaced by bulk adoption.")
            document.update(values)
        validate_spec(MappingSpec(**{key: mapping[key] for key in asdict(MappingSpec("", "", "", "", "")).keys()}), _FrappeMetadata())
        document.status = "Active"
        if local:
            document.save()
        else:
            document.mapping_revision = max(1, int(mapping["mapping_revision"] or 1))
            document.insert()
        created["mappings"].append(mapping["definition_id"])


def _mapping_by_definition(definition_id):
    rows = frappe.get_all("HERO Frappe Mapping", filters={"definition_id": definition_id}, fields=["name", "status", *mapping_exchange.EXPORTED_FIELDS])
    return rows[0] if rows else None


def _create_custom_doctype(definition, selected_relation_fields):
    fields = [
        {key: field[key] for key in ("fieldname", "label", "fieldtype", "options", 'reqd', 'unique') if key in field}
        for field in definition["fields"]
        if field["fieldtype"] not in {"Link", "Table"} or field["id"] in selected_relation_fields
    ]
    frappe.get_doc({
        "doctype": "DocType", "name": definition["name"], "module": "HERO Integration", "custom": 1,
        "istable": int(definition["is_child"]), "fields": fields,
        'permissions': [] if definition['is_child'] else [{'role': 'System Manager', 'read': 1, 'write': 1, 'create': 1}],
    }).insert()


def _create_custom_field(definition):
    frappe.get_doc({
        "doctype": "Custom Field", "dt": definition["doctype"], "fieldname": definition["fieldname"],
        "label": definition["label"], "fieldtype": definition["fieldtype"], "options": definition["options"],
        "insert_after": definition["insert_after"],
        **{flag: definition[flag] for flag in ('reqd', 'unique', 'read_only') if flag in definition},
    }).insert()


def _provision_relation(relation, fields, created):
    if _relation_exists(relation):
        return
    field = fields[relation["field_id"]]
    frappe.get_doc({
        "doctype": "Custom Field", "dt": relation["doctype"], "fieldname": field["fieldname"],
        "label": field["label"], "fieldtype": field["fieldtype"], "options": field["options"], "insert_after": "",
    }).insert()
    created["custom_fields"].append(f"{relation['doctype']}.{field['fieldname']}")


def _doctype_is_new(definition):
    return not _doctype_exists(str(definition["name"]))


def _relation_fields(definitions):
    result = {}
    for definition in definitions["custom_doctypes"]:
        result.update({field["id"]: field for field in definition["fields"]})
    result.update({field["id"]: field for field in definitions["custom_fields"]})
    return result


def _doctype_state(definition, relation_field_ids):
    document = frappe.get_doc("DocType", definition["name"])
    if not getattr(document, "custom", 0) or getattr(document, "module", "") != "HERO Integration":
        return "Conflict"
    if bool(getattr(document, "istable", 0)) != bool(definition["is_child"]):
        return "Conflict"
    permissions = getattr(document, 'permissions', None)
    if not definition['is_child'] and permissions is not None and any(getattr(p, 'role', '') != 'System Manager' for p in permissions):
        return 'Conflict'
    metadata = frappe.get_meta(definition["name"])
    for field in definition["fields"]:
        if field["id"] in relation_field_ids:
            continue
        local = metadata.get_field(field["fieldname"])
        if not local or local.fieldtype != field["fieldtype"] or (local.options or '') != field["options"]:
            return "Conflict"
        if any(int(getattr(local, flag, 0) or 0) != field[flag] for flag in ('reqd', 'unique') if flag in field):
            return 'Conflict'
    return "Unchanged"


def _custom_field_is_new(definition):
    if frappe.db.exists("Custom Field", {"dt": definition["doctype"], "fieldname": definition["fieldname"]}):
        return False
    try:
        return frappe.get_meta(definition["doctype"]).get_field(definition["fieldname"]) is None
    except Exception:
        return False


def _custom_field_state(definition):
    rows = frappe.get_all("Custom Field", filters={"dt": definition["doctype"], "fieldname": definition["fieldname"]}, fields=["fieldtype", "options", "label", 'reqd', 'unique', 'read_only'])
    if not rows:
        return "Conflict"
    row = rows[0]
    if any(int(row.get(flag) or 0) != definition[flag] for flag in ('reqd', 'unique', 'read_only') if flag in definition):
        return 'Conflict'
    return "Unchanged" if all((row.get(key) or '') == definition[key] for key in ("fieldtype", "options", "label")) else "Conflict"


def _relation_exists(relation):
    # The field is the relation. Metadata is the post-provisioning source of truth.
    try:
        field = frappe.get_meta(relation["doctype"]).get_field(relation["fieldname"])
    except Exception:
        return False
    return bool(field and field.fieldtype == relation["relation_type"] and field.options == relation["target_doctype"])


def _doctype_exists(name):
    return bool(frappe.db.exists("DocType", name))


def _owned_doctype(value):
    return isinstance(value, str) and value.startswith(OWNED_DOCTYPE_PREFIX)


def _owned_field(value):
    return isinstance(value, str) and value.startswith(OWNED_FIELD_PREFIX)


def _validate_field(field, ids, fields, custom_doctype):
    expected = {"id", "fieldname", "label", "fieldtype", "options"}
    if not isinstance(field, dict) or not expected <= set(field) or set(field) - expected - {'reqd', 'unique', 'read_only'}:
        raise SchemaProvisioningError('field has an unknown or missing field.')
    if any(type(field[flag]) is not int or field[flag] not in (0, 1) for flag in ('reqd', 'unique', 'read_only') if flag in field):
        raise SchemaProvisioningError('Field constraints must be 0 or 1.')
    if not re.fullmatch(r'custom_hero_[a-z][a-z0-9_]*', str(field['fieldname'])) or len(field['fieldname']) > 64:
        raise SchemaProvisioningError('Schema field name is invalid.')
    _claim_id(field, ids)
    if not _owned_field(field["fieldname"]) or field["fieldtype"] not in FIELD_TYPES or not isinstance(field["options"], str):
        raise SchemaProvisioningError("Schema fields must use the custom_hero_ prefix and a supported type.")
    if field["fieldtype"] not in {"Link", "Table"} and field["options"]:
        raise SchemaProvisioningError("Only Link and Table fields may declare options.")
    fields[str(field["id"])] = field


def _claim_id(definition, ids):
    value = definition.get("id") if isinstance(definition, dict) else None
    if not isinstance(value, str) or not value or value in ids:
        raise SchemaProvisioningError("Every schema definition needs one unique ID.")
    ids.add(value)


def _require_keys(value, expected, kind):
    if not isinstance(value, dict) or set(value) != expected:
        raise SchemaProvisioningError(f"{kind} has an unknown or missing field.")


class _FrappeMetadata:
    def ensure_write(self, doctype):
        frappe.has_permission(doctype, "write", throw=True)

    def field(self, doctype, fieldname):
        return frappe.get_meta(doctype).get_field(fieldname)


class _PlannedMetadata(_FrappeMetadata):
    """Validate missing targets against declarations without bypassing other rules."""
    def __init__(self, definitions):
        self.proposed = {d['name']: d for d in definitions['custom_doctypes'] if _doctype_is_new(d)}
        self.fields = {(d['name'], f['fieldname']): f for d in definitions['custom_doctypes'] for f in d['fields']}
        self.fields.update({(f['doctype'], f['fieldname']): f for f in definitions['custom_fields']})

    def ensure_write(self, doctype):
        if doctype in self.proposed:
            frappe.has_permission('DocType', 'create', throw=True)
        else:
            super().ensure_write(doctype)

    def field(self, doctype, fieldname):
        if doctype not in self.proposed:
            field = super().field(doctype, fieldname)
            if field is not None:
                return field
        field = self.fields.get((doctype, fieldname))
        if not field:
            return None
        values = {key: value for key, value in field.items() if key not in {'doctype', 'read_only'}}
        return SimpleNamespace(**values, read_only=int(field.get('read_only', 0)), is_custom_field=True)
