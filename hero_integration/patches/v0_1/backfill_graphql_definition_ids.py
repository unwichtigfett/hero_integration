"""Persist independent canonical identities for legacy GraphQL mappings."""
import json
from types import SimpleNamespace
from uuid import uuid4

import frappe


def execute():
    db = frappe.db
    # All rows lock before allocating identities. Nonempty persisted identities
    # remain authoritative; document names are never used as identities.
    rows = db.sql("SELECT name, definition_id FROM `tabHERO GraphQL Mapping` FOR UPDATE", as_dict=True)
    missing = [row.name for row in rows if row.definition_id is None or row.definition_id == ""]
    if not missing:
        return
    persisted_by_name = {row.name: row.definition_id for row in rows}
    occupied = {row.definition_id for row in rows if row.definition_id}
    identity_counts = {}
    for row in rows:
        if row.definition_id:
            identity_counts[row.definition_id] = identity_counts.get(row.definition_id, 0) + 1
    if db.table_exists("HERO Frappe Mapping"):
        for row in db.sql("SELECT definition_id FROM `tabHERO Frappe Mapping` FOR UPDATE", as_dict=True):
            if row.definition_id:
                occupied.add(row.definition_id)
                identity_counts[row.definition_id] = identity_counts.get(row.definition_id, 0) + 1
    registry_ids = _registry_definition_ids(db)
    occupied.update(registry_ids)
    if db.table_exists("HERO Mapping Draft"):
        drafts = db.sql("SELECT base_active_mapping, definition_id FROM `tabHERO Mapping Draft` FOR UPDATE", as_dict=True)
        if any(not persisted_by_name.get(row.base_active_mapping)
               or row.definition_id != persisted_by_name[row.base_active_mapping] for row in drafts):
            frappe.throw(frappe._("Legacy mapping identity cannot be backfilled while ambiguous personal draft references exist."))
    if db.table_exists("HERO Mapping Revision"):
        revisions = db.sql("SELECT base_active_mapping FROM `tabHERO Mapping Revision` FOR UPDATE", as_dict=True)
        if any(not persisted_by_name.get(row.base_active_mapping) for row in revisions):
            frappe.throw(frappe._("Legacy mapping identity cannot be backfilled while ambiguous personal revision references exist."))
    for doctype in ("HERO Field Binding", "HERO Object Binding"):
        if db.table_exists(doctype):
            bindings = db.sql(f"SELECT mapping_definition_id FROM `tab{doctype}` FOR UPDATE", as_dict=True)
            if any(identity_counts.get(row.mapping_definition_id, 0) > 1 or
                   (identity_counts.get(row.mapping_definition_id, 0) == 0 and row.mapping_definition_id not in registry_ids)
                   for row in bindings):
                frappe.throw(frappe._("Legacy mapping identity cannot be backfilled while ambiguous binding references exist."))
    # definition_id is a persisted, unique opaque Data identifier (API maximum
    # 140 characters). A UUIDv4 hex token is 32 characters, independent of the
    # document name, and becomes authoritative only after this transaction.
    allocations = []
    for name in missing:
        identity = uuid4().hex
        if identity in occupied:
            frappe.throw(frappe._("A generated mapping identity collided; retry the migration."))
        allocations.append((identity, name))
        occupied.add(identity)
    for identity, name in allocations:
        db.sql("UPDATE `tabHERO GraphQL Mapping` SET definition_id = %s WHERE name = %s "
               "AND (definition_id IS NULL OR definition_id = '')", (identity, name))


def _registry_definition_ids(db):
    if not db.table_exists("HERO Mapping Registry Version"):
        return set()
    if not all(db.has_column("HERO Mapping Registry Version", column)
               for column in ("mapping_payload_json", "integrity_hash")):
        if db.sql("SELECT name FROM `tabHERO Mapping Registry Version` LIMIT 1 FOR UPDATE"):
            raise ValueError("Legacy mapping registry has records without readable identity columns; reconciliation is required.")
        return set()
    from hero_integration.services.mapping_registry_service import MappingRegistryService
    from hero_integration.services.mapping_registry_storage import _decode_payload, _integrity

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate key")
            result[key] = value
        return result

    identities = set()
    versions = db.sql("SELECT mapping_payload_json, integrity_hash FROM `tabHERO Mapping Registry Version` FOR UPDATE", as_dict=True)
    for row in versions:
        try:
            raw = row.mapping_payload_json
            if not isinstance(raw, str) or _integrity(raw) != row.integrity_hash:
                raise ValueError("Registry integrity mismatch")
            json.loads(raw, object_pairs_hook=unique, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Invalid constant")))
            version = SimpleNamespace(payload=_decode_payload(raw))
            definitions = MappingRegistryService.__new__(MappingRegistryService)._definitions(version)
            ids = [item.definition_id for item in definitions]
            if len(ids) != len(set(ids)):
                raise ValueError("Duplicate registry identity")
            identities.update(ids)
        except Exception as error:
            raise ValueError("Stored mapping registry identity cannot be safely inspected.") from error
    return identities
