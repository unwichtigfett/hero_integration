"""Fill an empty native primary-address relation from one proven imported address."""
from __future__ import annotations

import frappe


def fill_primary_address(customer_name: str, source_instance: str) -> bool:
    customer = frappe.get_doc('Customer', customer_name, for_update=True)
    if customer.customer_primary_address:
        return False
    rows = frappe.db.sql("""SELECT DISTINCT a.name, a.address_type, a.is_primary_address
        FROM `tabAddress` a JOIN `tabDynamic Link` d ON d.parent=a.name AND d.parenttype='Address'
        WHERE d.link_doctype='Customer' AND d.link_name=%s""", (customer_name,), as_dict=True)
    billing = [row for row in rows if row.address_type == 'Billing']
    if len(billing) != 1 or any(row.is_primary_address and row.name != billing[0].name for row in rows):
        return False
    name = billing[0].name
    bindings = frappe.get_all('HERO Integration Link', filters={'source_system':'HERO',
        'source_type':'customer_address', 'source_instance':source_instance,
        'target_doctype':'Address', 'target_name':name}, pluck='name', limit_page_length=2)
    if len(bindings) != 1:
        return False
    address = frappe.get_doc('Address', name, for_update=True)
    if any(row.link_doctype != 'Customer' or row.link_name != customer_name for row in address.links):
        return False
    from hero_integration.services.change_detection import managed_target_update

    with managed_target_update('Address', name, bindings[0]):
        address.is_primary_address = 1
        address.save(ignore_permissions=True)
    # Native display formatting, without changing the source-controlled address fields.
    frappe.db.set_value('Customer', customer_name, {
        'customer_primary_address': name, 'primary_address': address.get_display(),
    }, update_modified=False)
    return True
