"""Normalize absent legacy identities before the unique index is synchronized."""
import frappe


def execute():
    db = frappe.db
    if db.table_exists("HERO GraphQL Mapping") and db.has_column("HERO GraphQL Mapping", "definition_id"):
        if db.sql("SELECT name FROM `tabHERO GraphQL Mapping` WHERE OCTET_LENGTH(definition_id) = 0 FOR UPDATE"):
            from hero_integration.patches.v0_1.backfill_graphql_definition_ids import _registry_definition_ids
            _registry_definition_ids(db)
            db.sql("UPDATE `tabHERO GraphQL Mapping` SET definition_id = NULL WHERE OCTET_LENGTH(definition_id) = 0")
