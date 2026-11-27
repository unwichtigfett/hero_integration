"""Recognize a materialized local address decision without projecting it as a HERO field."""
import json
from hashlib import sha256

import frappe

from hero_integration.services.binding_registry import incarnation

DOCTYPE = 'HERO Address Correction'
LOCATION = ('id', 'street', 'zipcode', 'city', 'country')


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def key(instance, customer, address):
    return digest([instance, str(customer), str(address)])


def source_fingerprint(record):
    def location(address):
        return {field: address.get(field) for field in LOCATION} if isinstance(address, dict) else None

    relatives = [(item.get('is_deleted'), location(item.get('address'))) for item in record.get('customer_addresses') or []]
    return digest({'customer': [record.get(k) for k in ('id','nr','first_name','last_name','email','company_name','is_deleted','is_contact_person')],
                   'primary': [record.get('address_id'), record.get('address'), record.get('phone_fax')],
                   'collection': sorted(relatives, key=digest)})


def verified_address(record, address_id, role, *, expected_customer=None, locked=False):
    """Return the native Address only for the same source snapshot, owner, incarnation and decision."""
    source = record.get('_hero_source_instance')
    customer = record.get('id')
    if not source or type(customer) is not int or not getattr(getattr(frappe, 'local', None), 'site', None):
        return None
    correction = frappe.db.get_value(DOCTYPE, key(source, customer, address_id),
        ['active', 'role', 'customer', 'address', 'source_fingerprint', 'evidence_json'], as_dict=True, for_update=locked)
    if (not correction or not correction.active or correction.role != role or
            expected_customer and expected_customer != correction.customer or source_fingerprint(record) != correction.source_fingerprint):
        return None
    proof = json.loads(correction.evidence_json)
    if proof['site'] != frappe.local.site or int(frappe.db.get_single_value('HERO Integration Settings', 'source_binding_revision') or 0) != proof['source_revision']:
        return None
    owner = frappe.db.get_value('HERO Object Binding', proof['owner']['binding'],
        ['object_type','source_instance','external_object_id','target_doctype','target_name','target_incarnation','lifecycle','reconciliation'], as_dict=True, for_update=locked)
    if (not owner or owner.lifecycle != 'VALID' or owner.reconciliation or owner.source_instance != source or owner.external_object_id != str(customer)
            or owner.target_doctype != 'Customer' or owner.target_name != correction.customer or owner.target_incarnation != proof['owner']['incarnation']):
        return None
    actual_owner = frappe.db.get_value('Customer',owner.target_name,'creation',for_update=locked)
    if not actual_owner or incarnation(actual_owner) != owner.target_incarnation:
        return None
    for kind, identity, doctype, name in [('customer', customer, 'Customer', owner.target_name), ('customer_address', address_id, 'Address', correction.address)]:
        link = frappe.db.get_value('HERO Integration Link', {'source_system':'HERO','source_instance':source,'source_type':kind,'source_id':str(identity)},
                                   ['target_doctype','target_name','sync_status'], as_dict=True, for_update=locked)
        if not link or link.target_doctype != doctype or link.target_name != name or link.sync_status != 'Synchronized':
            return None
    if not frappe.db.exists('Address',correction.address):
        return None
    address = frappe.get_doc('Address',correction.address,for_update=locked)
    if (incarnation(address.creation) != proof['native']['creation'] or address.disabled or len(address.links) != 1 or
            (address.links[0].link_doctype,address.links[0].link_name) != ('Customer',owner.target_name) or any(
                (address.get(field) or '') != value for field,value in proof['native']['fields'].items())):
        return None
    return address.name
