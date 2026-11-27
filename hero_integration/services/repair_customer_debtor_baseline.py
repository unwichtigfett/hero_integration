"""Repair the pre-readback baseline of HERO customers with an empty debtor number."""

import frappe

from hero_integration.api import _configured_source_instance
from hero_integration.services.binding_registry import matches_incarnation
from hero_integration.services.migration import decode_hashes, encode_hashes, field_hashes


def execute():
    field = "custom_hero_accounting_debitor_number"
    column = frappe.db.sql("SHOW COLUMNS FROM `tabCustomer` LIKE %s", (field,), as_dict=True)
    if not (len(column) == 1 and column[0].Type.startswith("int") and
            column[0].Null == "NO" and str(column[0].Default) == "0"):
        raise ValueError("Unexpected Customer debtor number schema")
    null_hash = field_hashes({field: None})[field]
    zero_hash = field_hashes({field: 0})[field]
    source = _configured_source_instance()
    names = frappe.get_all("HERO Integration Link", filters={
        "source_system": "HERO", "source_instance": source, "source_type": "customer",
        "target_doctype": "Customer", "sync_status": "Synchronized",
    }, pluck="name", order_by="name", limit_page_length=0)
    repaired = 0
    for name in names:
        link = frappe.get_doc("HERO Integration Link", name, for_update=True)
        previous = decode_hashes(link.target_field_hashes) or {}
        if (previous.get(field) != null_hash or (decode_hashes(link.source_field_hashes) or {}).get(field) != null_hash
                or link.sync_status != "Synchronized" or link.source_system != "HERO" or link.source_instance != source
                or link.source_type != "customer" or link.target_doctype != "Customer"):
            continue
        customer = frappe.db.get_value("Customer", link.target_name, ["creation", field], as_dict=True, for_update=True)
        bindings = frappe.db.sql("""
            SELECT name, target_incarnation, reconciliation FROM `tabHERO Object Binding`
            WHERE site_scope=%s AND source_instance=%s AND object_type='Customer'
              AND external_object_id=%s AND target_doctype='Customer' AND target_name=%s
              AND lifecycle='VALID' LIMIT 2 FOR UPDATE
        """, (frappe.local.site, source, link.source_id, link.target_name), as_dict=True)
        if (not customer or customer[field] != 0 or len(bindings) != 1 or bindings[0].reconciliation
                or not matches_incarnation(bindings[0].target_incarnation, customer.creation)):
            continue
        if frappe.db.exists("HERO Field Binding", {"target_doctype": "Customer", "target_name": link.target_name,
                                                  "target_field": field}):
            continue
        if frappe.db.exists("Version", {"ref_doctype": "Customer", "docname": link.target_name,
                                        "data": ["like", '%"' + field + '"%']}):
            continue
        if frappe.db.exists("HERO Integration Record", {"target_doctype": "Customer", "target_name": link.target_name,
                                                       "technical_action": "frappe_change_observed"}):
            continue
        previous[field] = zero_hash
        frappe.db.set_value("HERO Integration Link", name, "target_field_hashes", encode_hashes(previous),
                            update_modified=False)
        repaired += 1
    return repaired
