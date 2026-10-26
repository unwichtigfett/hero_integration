"""Persist independent canonical identities for legacy GraphQL mappings."""
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
    if db.table_exists("HERO Mapping Draft"):
        drafts = db.sql("SELECT base_active_mapping, definition_id FROM `tabHERO Mapping Draft` FOR UPDATE", as_dict=True)
        if any(not persisted_by_name.get(row.base_active_mapping)
               or row.definition_id != persisted_by_name[row.base_active_mapping] for row in drafts):
            frappe.throw("Legacy mapping identity cannot be backfilled while ambiguous personal draft references exist.")
    if db.table_exists("HERO Mapping Revision"):
        revisions = db.sql("SELECT base_active_mapping FROM `tabHERO Mapping Revision` FOR UPDATE", as_dict=True)
        if any(not persisted_by_name.get(row.base_active_mapping) for row in revisions):
            frappe.throw("Legacy mapping identity cannot be backfilled while ambiguous personal revision references exist.")
    for doctype in ("HERO Field Binding", "HERO Object Binding"):
        if db.table_exists(doctype):
            bindings = db.sql(f"SELECT mapping_definition_id FROM `tab{doctype}` FOR UPDATE", as_dict=True)
            if any(identity_counts.get(row.mapping_definition_id, 0) != 1 for row in bindings):
                frappe.throw("Legacy mapping identity cannot be backfilled while ambiguous binding references exist.")
    # definition_id is a persisted, unique opaque Data identifier (API maximum
    # 140 characters). A UUIDv4 hex token is 32 characters, independent of the
    # document name, and becomes authoritative only after this transaction.
    allocations = []
    for name in missing:
        identity = uuid4().hex
        if identity in occupied:
            frappe.throw("A generated mapping identity collided; retry the migration.")
        allocations.append((identity, name))
        occupied.add(identity)
    for identity, name in allocations:
        db.sql("UPDATE `tabHERO GraphQL Mapping` SET definition_id = %s WHERE name = %s "
               "AND (definition_id IS NULL OR definition_id = '')", (identity, name))
