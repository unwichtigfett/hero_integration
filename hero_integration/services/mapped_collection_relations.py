"""Explicit native collection bindings; no implicit price adoption or stock writes."""
from __future__ import annotations

from hashlib import sha256
import json
import math
import re

import frappe

from hero_integration.services.direct_mapping import MISSING, lookup

KINDS = {'customer_addresses', 'customer_contacts', 'sales_prices'}
PARENTS = {'customer_addresses': 'Customer', 'customer_contacts': 'Customer', 'sales_prices': 'Item'}
PATH = re.compile(r'^[_A-Za-z][_0-9A-Za-z]*(?:\.[_A-Za-z][_0-9A-Za-z]*)*$')
ADDRESS_FIELDS = {'address_line1', 'address_line2', 'city', 'pincode', 'state', 'county'}
CONTACT_FIELDS = {'first_name','last_name','email_id','mobile_no','phone','salutation','designation'}
PRICE_DIMENSIONS = ('customer', 'supplier', 'batch_no', 'packing_unit', 'valid_from', 'valid_upto')
MAX_ROWS = 1000


def _path(value):
    return isinstance(value, str) and bool(PATH.fullmatch(value))


def _identity(value):
    if type(value) not in {str, int} or not str(value).strip() or type(value) is int and value <= 0:
        raise ValueError('Native collection rows require a stable source identity.')
    return str(value).strip()


def _hash(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def validate_relation(relation):
    kind = relation.get('kind')
    allowed = {'kind', 'collection_path', 'identity_path', 'fields'}
    if kind == 'customer_addresses':
        allowed |= {'deleted_path', 'primary_identity_path', 'country_code_path', 'address_type'}
    elif kind == 'customer_contacts':
        allowed |= {'deleted_path', 'contact_person_path', 'parent_identity_path', 'parent_source_identity_path', 'salutation_map'}
    elif kind == 'sales_prices':
        allowed |= {'price_list_identity_path', 'price_lists'}
    else:
        raise ValueError('Native collection kind is unsupported.')
    if set(relation) - allowed or not all(_path(relation.get(key)) for key in ('collection_path', 'identity_path')):
        raise ValueError('Native collection paths or options are invalid.')
    fields = relation.get('fields')
    supported = ADDRESS_FIELDS if kind == 'customer_addresses' else CONTACT_FIELDS if kind == 'customer_contacts' else {'price_list_rate'}
    if not isinstance(fields, dict) or not fields or set(fields) - supported or any(not _path(path) for path in fields.values()):
        raise ValueError('Native collection fields must explicitly select supported source paths.')
    if kind == 'customer_addresses':
        if (not {'address_line1', 'city'} <= set(fields)
                or not all(_path(relation.get(key)) for key in ('deleted_path', 'primary_identity_path', 'country_code_path'))
                or relation.get('address_type') not in {'Billing', 'Shipping', 'Office', 'Other'}):
            raise ValueError('Customer addresses require explicit identity, deletion, country and primary-address paths.')
    elif kind == 'customer_contacts':
        if not {'first_name','last_name'} <= set(fields) or not all(_path(relation.get(key)) for key in ('deleted_path','contact_person_path','parent_identity_path','parent_source_identity_path')):
            raise ValueError('Customer contacts require explicit parent identity, person and deletion paths.')
        salutations=relation.get('salutation_map',{})
        if not isinstance(salutations,dict) or len(salutations)>100 or any(not isinstance(k,str) or not isinstance(v,str) or not k.strip() or not v.strip() for k,v in salutations.items()):
            raise ValueError('Contact salutations require an explicit text-to-Salutation table.')
    else:
        bindings = relation.get('price_lists')
        if not _path(relation.get('price_list_identity_path')) or not isinstance(bindings, dict) or not 1 <= len(bindings) <= 100:
            raise ValueError('Sales prices require explicit source price-list bindings.')
        names = set()
        for source_id, binding in bindings.items():
            _identity(source_id)
            if (not isinstance(binding, dict) or set(binding) != {'name', 'currency', 'create'}
                    or not isinstance(binding['name'], str) or not binding['name'].strip()
                    or not isinstance(binding['currency'], str) or not re.fullmatch('[A-Z]{3}', binding['currency'])
                    or type(binding['create']) is not bool):
                raise ValueError('Price-list bindings require an explicit name, currency and creation decision.')
            if binding['name'] in names:
                raise ValueError('Two source price lists cannot write the same ERPNext price list.')
            names.add(binding['name'])
    return relation


def required_paths(relation):
    validate_relation(relation)
    prefix = relation['collection_path'] + '.'
    result = {prefix + relation['identity_path'], *(prefix + path for path in relation['fields'].values())}
    if relation['kind'] == 'customer_addresses':
        result |= {prefix + relation['deleted_path'], prefix + relation['country_code_path'], relation['primary_identity_path']}
    elif relation['kind'] == 'customer_contacts':
        result |= {prefix+relation[key] for key in ('deleted_path','contact_person_path','parent_identity_path')}
        result.add(relation['parent_source_identity_path'])
    else:
        result.add(prefix + relation['price_list_identity_path'])
    return result


def _read(record, path):
    value = lookup(record, path)
    if value is MISSING:
        raise ValueError('An approved native collection source field is absent.')
    return value


def prepare(relation, record, parent_doctype, parent_name):
    """Validate every row before writes; missing fields differ from explicit null."""
    validate_relation(relation)
    kind = relation['kind']
    if parent_doctype != PARENTS[kind]:
        raise ValueError('Native collection crosses its approved parent type.')
    instance = str(record.get('_hero_source_instance') or '').strip()
    if not instance:
        raise ValueError('Native collections require the approved source instance.')
    rows = _read(record, relation['collection_path'])
    if rows is None:
        return []
    if not isinstance(rows, list) or len(rows) > MAX_ROWS or any(not isinstance(row, dict) for row in rows):
        raise ValueError('Native collection must be a bounded list of records.')
    primary = _read(record, relation['primary_identity_path']) if kind == 'customer_addresses' else None
    primary = _identity(primary) if primary is not None else None
    parent_identity = _identity(_read(record,relation['parent_source_identity_path'])) if kind=='customer_contacts' else None
    result, seen = [], {}
    for row in rows:
        if kind in {'customer_addresses','customer_contacts'}:
            deleted = _read(row, relation['deleted_path'])
            if type(deleted) is not bool:
                raise ValueError('Customer address deletion state must be an explicit Boolean.')
            if deleted:
                continue  # Remote deletion never deletes or disables a local address.
        raw_identity = _read(row, relation['identity_path'])
        if kind == 'sales_prices' and _read(row, relation['fields']['price_list_rate']) is None:
            continue  # An absent source price needs no row identity and writes nothing.
        identity = _identity(raw_identity)
        if kind == 'customer_addresses' and identity == primary:
            continue  # Primary address is already handled by the canonical customer importer.
        values = {field: _read(row, path) for field, path in relation['fields'].items()}
        extra = {}
        if kind == 'customer_addresses':
            if any(value is not None and not isinstance(value, str) for value in values.values()):
                raise ValueError('Customer address fields must contain text or null.')
            values = {field: value.strip() if value else '' for field, value in values.items()}
            country = _read(row, relation['country_code_path'])
            if not isinstance(country, str) or not re.fullmatch('[a-zA-Z]{2}', country):
                raise ValueError('Customer addresses require an explicit country code.')
            if not values['address_line1'] or not values['city']:
                raise ValueError('Customer addresses require street and city.')
            values['country_code'] = country.lower()
            source_type, source_id = 'customer_address', identity
        elif kind=='customer_contacts':
            if _read(row,relation['contact_person_path']) is not True:
                raise ValueError('A nested customer contact must explicitly be a contact person.')
            if _identity(_read(row,relation['parent_identity_path'])) != parent_identity:
                raise ValueError('Nested contact parent identity differs from the mapped Customer.')
            if any(value is not None and not isinstance(value,str) for value in values.values()):
                raise ValueError('Customer contact fields must contain text or null.')
            values={field:value.strip() if value else '' for field,value in values.items()}
            if not values['first_name'] and not values['last_name']:
                raise ValueError('Customer contacts require at least one native name component.')
            salutation=values.get('salutation')
            if salutation:
                mapping=relation.get('salutation_map',{})
                if salutation.casefold() not in mapping:
                    raise ValueError('Source contact salutation has no explicit native binding.')
                values['salutation']=mapping[salutation.casefold()]
            source_type,source_id='customer_contact',identity
            extra={'parent_identity':parent_identity}
        else:
            list_id = _identity(_read(row, relation['price_list_identity_path']))
            if list_id not in relation['price_lists']:
                raise ValueError('A source sales price list has no explicit ERPNext binding.')
            rate = values['price_list_rate']
            if rate is None:
                continue  # No source price is not a zero-price instruction.
            if type(rate) not in {int, float} or not math.isfinite(rate) or rate < 0:
                raise ValueError('Sales price must be a finite nonnegative number.')
            values['price_list_rate'] = float(rate)
            extra = {'price_list_identity': list_id, 'price_list': relation['price_lists'][list_id]}
            source_type = 'product_sales_price'
            source_id = identity
        item = {'config': relation, 'values': values, 'identity': identity, 'source_type': source_type,
                'source_id': source_id, 'source_instance': instance, **extra}
        if identity in seen:
            if seen[identity] != item:
                raise ValueError('Duplicate native collection identities contain conflicting values.')
            continue
        if kind == 'sales_prices' and any(previous['price_list_identity'] == extra['price_list_identity'] for previous in result):
            raise ValueError('One product contains multiple prices for the same bound price list.')
        seen[identity] = item
        result.append(item)
    return result


def _rows(doctype, filters, fields=('name',), limit=2):
    return frappe.get_all(doctype, filters=filters, fields=list(fields), limit_page_length=limit)


def _source_link(item, source_type=None, source_id=None):
    rows = _rows('HERO Integration Link', {'source_system': 'HERO',
        'source_type': source_type or item['source_type'], 'source_instance': item['source_instance'],
        'source_id': source_id or item['source_id']}, ('*',))
    if len(rows) > 1:
        raise ValueError('Native collection has ambiguous source bindings.')
    return frappe.get_doc('HERO Integration Link', rows[0].name, for_update=True) if rows else None


def _price_list(item):
    binding = item['price_list']; name = binding['name']
    link = _source_link(item, 'sales_price_list', item['price_list_identity'])
    exists = frappe.db.exists('Price List', name)
    if link and (link.target_doctype != 'Price List' or link.target_name != name or not exists):
        raise ValueError('Source price-list binding differs from the configured ERPNext list.')
    if exists:
        document = frappe.get_doc('Price List', name)
        frappe.has_permission('Price List', 'read', doc=document, throw=True)
        if document.currency != binding['currency'] or not document.enabled or not document.selling or document.buying:
            raise ValueError('Configured price list must be enabled, selling-only and use the approved currency.')
        return document
    if not binding['create']:
        raise ValueError('The explicitly bound ERPNext price list does not exist.')
    if not frappe.db.exists('Currency', binding['currency']):
        raise ValueError('The approved price-list currency is absent from ERPNext.')
    frappe.has_permission('Price List', 'create', throw=True)
    return None


def preflight(item, parent_doctype, parent_name, parent=None):
    kind = item['config']['kind']; target = 'Address' if kind == 'customer_addresses' else 'Contact' if kind == 'customer_contacts' else 'Item Price'
    link = _source_link(item); document = None
    if link:
        if link.target_doctype != target or not frappe.db.exists(target, link.target_name):
            raise ValueError('Native collection binding has a missing or incompatible target.')
        document = frappe.get_doc(target, link.target_name, for_update=True)
        frappe.has_permission(target, 'write', doc=document, throw=True)
    else:
        frappe.has_permission(target, 'create', throw=True)
    values = dict(item['values']); price_list = None
    if kind == 'customer_addresses':
        countries = _rows('Country', {'code': values.pop('country_code')})
        if len(countries) != 1:
            raise ValueError('Address country code must resolve to exactly one ERPNext Country.')
        values.update(country=countries[0].name, address_type=item['config']['address_type'])
        if document and not any(row.link_doctype == 'Customer' and row.link_name == parent_name for row in document.get('links', [])):
            raise ValueError('Bound customer address does not belong to the mapped Customer.')
        # An unbound existing address is never silently adopted or recreated.
        if not document:
            candidates = _rows('Address', {'address_line1': values['address_line1'], 'city': values['city'], 'country': values['country']}, limit=0)
            for candidate in candidates:
                address = frappe.get_doc('Address', candidate.name)
                if any(row.link_doctype == 'Customer' and row.link_name == parent_name for row in address.get('links', [])):
                    raise ValueError('An existing customer address requires explicit source reconciliation.')
    elif kind=='customer_contacts':
        if values.get('salutation') and not frappe.db.exists('Salutation',values['salutation']):
            raise ValueError('The explicitly bound native Salutation does not exist.')
        if document:
            links=document.get('links',[])
            if not links or any(row.link_doctype!='Customer' or row.link_name!=parent_name for row in links):
                raise ValueError('Bound contact is shared or does not belong to the mapped Customer.')
        else:
            primary=parent.get('customer_primary_contact') if parent else None
            if primary and item['identity']==item['parent_identity']:
                roots=_rows('HERO Integration Link',{'source_system':'HERO','source_type':'customer','source_instance':item['source_instance'],
                    'source_id':item['parent_identity'],'target_doctype':'Customer','target_name':parent_name},('name',))
                if len(roots)!=1:
                    raise ValueError('Primary contact reuse requires the exact imported Customer identity.')
                document=frappe.get_doc('Contact',primary,for_update=True)
                frappe.has_permission('Contact','write',doc=document,throw=True)
                links=document.get('links',[])
                if not links or any(row.link_doctype!='Customer' or row.link_name!=parent_name for row in links):
                    raise ValueError('Bound contact is shared or does not belong to the mapped Customer.')
                if any(_contact_value(document,field)!=value for field,value in values.items()):
                    raise ValueError('Existing primary contact differs from the verified source person.')
            else:
                # Equal email/name only detects a collision; it never proves source identity.
                linked=_rows('Dynamic Link',{'parenttype':'Contact','link_doctype':'Customer','link_name':parent_name},('parent',),limit=0)
                for row in linked:
                    candidate=frappe.get_doc('Contact',row.parent)
                    same_email=values.get('email_id') and _contact_value(candidate,'email_id')==values['email_id']
                    same_name=all(_contact_value(candidate,field)==values.get(field,'') for field in ('first_name','last_name'))
                    if same_email or same_name:
                        raise ValueError('An existing customer contact requires explicit source reconciliation.')
    else:
        if parent is None:
            if not frappe.db.exists('Item', parent_name):
                raise ValueError('Sales-price preview requires the mapped Item and its native unit.')
            parent = frappe.get_doc('Item', parent_name)
        if not parent.stock_uom:
            raise ValueError('Sales prices require the mapped Item stock unit.')
        price_list = _price_list(item)
        values.update(item_code=parent_name, price_list=item['price_list']['name'], currency=item['price_list']['currency'], uom=parent.stock_uom)
        if document and (document.item_code != parent_name or document.price_list != values['price_list']):
            raise ValueError('Bound Item Price belongs to another item or price list.')
        candidates = _rows('Item Price', {'item_code': parent_name, 'price_list': values['price_list'], 'uom': parent.stock_uom}, ('name', *PRICE_DIMENSIONS), limit=0)
        if any(row.name != (document.name if document else None) and not any(row.get(field) for field in PRICE_DIMENSIONS) for row in candidates):
            raise ValueError('An existing ERPNext item price requires explicit source reconciliation.')
    return {'collection_kind': kind, 'document': document, 'link': link, 'values': values, 'price_list': price_list}


def _observed(prepared):
    document = prepared['document']
    result = {field: _contact_value(document,field) if prepared['collection_kind']=='customer_contacts' else document.get(field) for field in prepared['values']}
    if prepared['collection_kind'] == 'sales_prices':
        if result.get('price_list_rate') is not None:
            result['price_list_rate'] = float(result['price_list_rate'])
        result.update({field: document.get(field) or None for field in PRICE_DIMENSIONS})
    else:
        if prepared['collection_kind']=='customer_contacts':
            result['email_rows']=[(getattr(row,'email_id',None),bool(getattr(row,'is_primary',False))) for row in document.get('email_ids',[])]
            result['phone_rows']=[(getattr(row,'phone',None),bool(getattr(row,'is_primary_phone',False)),bool(getattr(row,'is_primary_mobile_no',False))) for row in document.get('phone_nos',[])]
        result['links'] = sorted((row.link_doctype, row.link_name) for row in document.get('links', []))
        result['is_primary_address'] = bool(document.get('is_primary_address'))
    return result


def local_conflict(prepared):
    return bool(prepared['document'] and prepared['link'] and _hash(_observed(prepared)) != prepared['link'].target_payload_hash)


def _evidence(plan, values, observed):
    return {'sync_status': 'Synchronized', 'conflict_detail': '', 'mapping_version': plan.mapping_version,
        'source_payload_hash': _hash(values), 'target_payload_hash': _hash(observed), 'last_synced_on': frappe.utils.now_datetime()}


def apply(plan, item, prepared, parent_doctype, parent_name, parent):
    document, link = prepared['document'], prepared['link']
    if local_conflict(prepared):
        frappe.db.set_value('HERO Integration Link', link.name, {'sync_status': 'Conflict',
            'conflict_detail': 'Native collection changed locally; review before replacing mapped fields.',
            'observed_source_hash': _hash(item['values']), 'observed_target_hash': _hash(_observed(prepared)),
            'last_observed_on': frappe.utils.now_datetime()}, update_modified=False)
        return 'conflicts'
    if prepared['collection_kind'] == 'sales_prices' and prepared['price_list'] is None:
        binding = item['price_list']
        price_list = frappe.get_doc({'doctype': 'Price List', 'price_list_name': binding['name'],
            'currency': binding['currency'], 'selling': 1, 'buying': 0, 'enabled': 1}).insert()
        if price_list.name != binding['name']:
            raise ValueError('Created price-list identity differs from the explicit binding.')
        frappe.get_doc({'doctype': 'HERO Integration Link', 'source_system': 'HERO', 'source_type': 'sales_price_list',
            'source_instance': item['source_instance'], 'source_id': item['price_list_identity'],
            'target_doctype': 'Price List', 'target_name': price_list.name,
            **_evidence(plan, binding, binding)}).insert(ignore_permissions=True)
    target = 'Address' if prepared['collection_kind'] == 'customer_addresses' else 'Contact' if prepared['collection_kind']=='customer_contacts' else 'Item Price'
    existed=document is not None
    before = _observed(prepared) if existed else None
    writable={field:value for field,value in prepared['values'].items() if target!='Contact' or field not in {'email_id','phone','mobile_no'} and value not in (None,'')}
    if document:
        document.update(writable)
    else:
        document = frappe.get_doc({'doctype': target, **writable})
        if target == 'Address':
            document.set('address_title', f'{parent_name} / {item["identity"]}')
            document.append('links', {'link_doctype': 'Customer', 'link_name': parent_name})
        elif target=='Contact':
            document.append('links', {'link_doctype':'Customer','link_name':parent_name})
    if target=='Contact':
        for field in ('email_id','phone','mobile_no'):
            if field in prepared['values']:_set_contact_value(document,field,prepared['values'][field])
    prepared['document'] = document
    from hero_integration.services.change_detection import native_values_changed
    changed = not existed or native_values_changed(before, _observed(prepared))
    if existed and changed: document.save()
    elif not existed: document.insert()
    if target == 'Contact':
        for field in ('email_id', 'phone', 'mobile_no'):
            expected = prepared['values'].get(field)
            if expected and _contact_value(document, field) != expected:
                raise ValueError('Saved contact communication differs from the mapped source value.')
    prepared['document'] = document
    evidence = _evidence(plan, item['values'], _observed(prepared))
    if link:
        frappe.db.set_value('HERO Integration Link', link.name, evidence, update_modified=False)
    else:
        frappe.get_doc({'doctype': 'HERO Integration Link', 'source_system': 'HERO', 'source_type': item['source_type'],
            'source_instance': item['source_instance'], 'source_id': item['source_id'],
            'target_doctype': target, 'target_name': document.name, **evidence}).insert(ignore_permissions=True)
    return ('updated' if changed else 'unchanged') if link else 'created'


def lookup_managed_price(item_name, price_list, source_instance, *, allow_rate_change=False):
    """Return one owned price. With allow_rate_change, caller must prove its old
    baseline by replacing observed.price_list_rate with the verified remote rate
    and comparing the resulting hash to target_payload_hash before any write.
    """
    if not all(isinstance(value, str) and value.strip() for value in (item_name, price_list, source_instance)):
        raise ValueError('Managed price lookup requires item, price list and source instance.')
    if type(allow_rate_change) is not bool:
        raise ValueError('Managed price rate-change policy must be an explicit Boolean.')
    candidates = _rows('Item Price', {'item_code': item_name, 'price_list': price_list}, ('name',), limit=0)
    owned = []
    for candidate in candidates:
        links = _rows('HERO Integration Link', {'source_system': 'HERO', 'source_type': 'product_sales_price',
            'source_instance': source_instance, 'target_doctype': 'Item Price', 'target_name': candidate.name}, ('name',), limit=2)
        if len(links) > 1:
            raise ValueError('Native collection has ambiguous source bindings.')
        if not links:
            continue
        document = frappe.get_doc('Item Price', candidate.name, for_update=True)
        frappe.has_permission('Item Price', 'read', doc=document, throw=True)
        link = frappe.get_doc('HERO Integration Link', links[0].name, for_update=True)
        values = {field: document.get(field) for field in ('price_list_rate', 'item_code', 'price_list', 'currency', 'uom')}
        prepared = {'collection_kind': 'sales_prices', 'document': document, 'link': link, 'values': values}
        if any(document.get(field) for field in PRICE_DIMENSIONS) or (not allow_rate_change and local_conflict(prepared)):
            raise ValueError('Managed sales price changed locally or has additional dimensions.')
        owned.append({'document': document, 'link': link, 'source_id': _identity(link.source_id),
            'observed': _observed(prepared), 'target_payload_hash': link.target_payload_hash})
    if len(owned) != 1:
        raise ValueError('Managed price lookup must resolve to exactly one source-bound price.')
    return owned[0]


def _contact_value(document,field):
    child={'email_id':('email_ids','email_id','is_primary'),'phone':('phone_nos','phone','is_primary_phone'),'mobile_no':('phone_nos','phone','is_primary_mobile_no')}.get(field)
    if not child:return document.get(field) or ''
    table,value_key,primary_key=child
    primary=[row for row in document.get(table,[]) if getattr(row,primary_key,False)]
    if len(primary)>1:raise ValueError('A customer Contact has ambiguous primary communication rows.')
    return (getattr(primary[0],value_key,None) or '') if primary else ''


def _set_contact_value(document,field,value):
    table,value_key,primary_key={'email_id':('email_ids','email_id','is_primary'),'phone':('phone_nos','phone','is_primary_phone'),'mobile_no':('phone_nos','phone','is_primary_mobile_no')}[field]
    if not value:return  # Empty remote data does not erase native communication rows.
    rows=[row for row in document.get(table,[]) if getattr(row,primary_key,False)]
    if len(rows)>1:raise ValueError('A customer Contact has ambiguous primary communication rows.')
    if rows:
        row = rows[0]
        other_primary = {'phone': 'is_primary_mobile_no', 'mobile_no': 'is_primary_phone'}.get(field)
        if other_primary and getattr(row, other_primary, False) and getattr(row, value_key, None) != value:
            # A shared row represents two independently mapped numbers. Keep
            # the other number and move only this field's primary flag.
            setattr(row, primary_key, 0)
            document.append(table, {value_key: value, primary_key: 1})
        else:
            setattr(row, value_key, value)
    else:
        document.append(table, {value_key: value, primary_key: 1})
