"""Explicit mapping-owned native bank accounts and Project addresses.

No accounting entries, balances, default-account changes, commits or remote writes.
Existing unbound accounts/addresses are never silently adopted.
"""
from __future__ import annotations

from hashlib import sha256
import json
import re

import frappe

from hero_integration.services.direct_mapping import MISSING, lookup
from hero_integration.services import mapped_collection_relations as collections

PATH = re.compile(r'^[_A-Za-z][_0-9A-Za-z]*(?:\.[_A-Za-z][_0-9A-Za-z]*)*$')
FIELDS = {
    'customer_bank_account': {'iban', 'bank_name', 'swift_number'},
    'project_address': {'address_line1', 'address_line2', 'city', 'pincode', 'state', 'county'},
}
PARENTS = {'customer_bank_account': 'Customer', 'project_address': 'Project', **collections.PARENTS}


def validate_config(config):
    if not isinstance(config, list) or len(config) > 5:
        raise ValueError('Native relations require a bounded explicit list.')
    seen = set()
    for relation in config:
        if not isinstance(relation, dict):
            raise ValueError('Native relation must be an object.')
        kind = relation.get('kind')
        if kind not in set(FIELDS) | collections.KINDS or kind in seen:
            raise ValueError('Native relation kinds must be supported and unique.')
        seen.add(kind)
        if kind in collections.KINDS:
            collections.validate_relation(relation)
            continue
        allowed = {'kind', 'identity_path', 'fields'}
        if kind == 'project_address': allowed |= {'country_code_path', 'parent_field', 'address_type'}
        if set(relation) - allowed or not _path(relation.get('identity_path')):
            raise ValueError('Native relation identity or options are invalid.')
        fields = relation.get('fields')
        if (not isinstance(fields, dict) or not fields or set(fields) - FIELDS[kind]
                or any(not _path(path) for path in fields.values())):
            raise ValueError('Native relation fields must explicitly select supported source paths.')
        if kind == 'customer_bank_account' and not {'iban', 'bank_name', 'swift_number'} <= set(fields):
            raise ValueError('Bank account relations require explicit IBAN, bank name and BIC paths.')
        if kind == 'project_address' and (
                not {'address_line1', 'city'} <= set(fields) or not _path(relation.get('country_code_path'))
                or relation.get('parent_field') not in {'custom_hero_project_address', 'site_address'}
                or relation.get('address_type') not in {'Billing', 'Shipping', 'Office', 'Other'}):
            raise ValueError('Project address requires native address fields, country code and an explicit Project link.')
    return json.loads(json.dumps(config, sort_keys=True))


def required_paths(config):
    paths = set()
    for relation in validate_config(config):
        if relation['kind'] in collections.KINDS:
            paths.update(collections.required_paths(relation))
            continue
        paths.add(relation['identity_path'])
        paths.update(relation['fields'].values())
        if relation.get('country_code_path'): paths.add(relation['country_code_path'])
    return paths


def _path(value):
    return isinstance(value, str) and bool(PATH.fullmatch(value))


def _hash(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def _value(record, path):
    value = lookup(record, path)
    if value is MISSING:
        raise ValueError('An approved native relation source field is absent.')
    if value is not None and not isinstance(value, str):
        raise ValueError('A native relation text field has an incompatible source value.')
    return value.strip() if value else ''


def prepare(plan, record, parent_doctype, parent_name):
    """Pure source validation; no implicit paths and no external/native writes."""
    config = validate_config((getattr(plan, 'adapter_options', None) or {}).get('native_relations', []))
    instance = str(record.get('_hero_source_instance') or '').strip()
    if config and not instance:
        raise ValueError('Native relations require the approved source instance.')
    result = []
    for relation in config:
        kind = relation['kind']
        if kind in collections.KINDS:
            result.extend(collections.prepare(relation, record, parent_doctype, parent_name))
            continue
        if PARENTS[kind] != parent_doctype:
            raise ValueError('Native relation crosses its approved parent type.')
        identity = lookup(record, relation['identity_path'])
        if identity is MISSING:
            raise ValueError('An approved native relation identity is absent.')
        values = {field: _value(record, path) for field, path in relation['fields'].items()}
        if not any(values.values()):
            continue  # Explicit null/empty source relation never deletes a local record.
        if type(identity) not in {str, int} or not str(identity).strip() or type(identity) is int and identity <= 0:
            raise ValueError('A populated native relation needs a stable source identity.')
        if kind == 'customer_bank_account':
            values['iban'] = re.sub(r'\s+', '', values['iban']).upper()
            values['swift_number'] = re.sub(r'\s+', '', values['swift_number']).upper()
            if not _valid_iban(values['iban']) or not values['bank_name']:
                raise ValueError('Bank account requires a valid IBAN and an explicit bank name.')
            if values['swift_number'] and not re.fullmatch(r'[A-Z]{6}[A-Z0-9]{2}(?:[A-Z0-9]{3})?', values['swift_number']):
                raise ValueError('Bank BIC has an invalid format.')
        else:
            values['country_code'] = _value(record, relation['country_code_path']).lower()
            if not values['address_line1'] or not values['city'] or not re.fullmatch('[a-z]{2}', values['country_code']):
                raise ValueError('Project address requires street, city and an explicit country code.')
        key = _hash([instance, kind, str(identity)])
        result.append({'config': relation, 'values': values, 'source_id': key,
                       'source_instance': instance, 'identity': str(identity)})
    return result


def _valid_iban(value):
    if not re.fullmatch(r'[A-Z]{2}[0-9]{2}[A-Z0-9]{11,30}', value): return False
    numeric = ''.join(str(ord(char) - 55) if char.isalpha() else char for char in value[4:] + value[:4])
    return int(numeric) % 97 == 1


def sync(plan, record, parent_doctype, parent_name, *, preview=False):
    """Apply configured child relations inside the caller's import transaction."""
    items = prepare(plan, record, parent_doctype, parent_name)
    counts = {'created': 0, 'updated': 0, 'unchanged': 0, 'conflicts': 0, 'planned': len(items)}
    if not items: return counts
    parent = frappe.get_doc(parent_doctype, parent_name) if frappe.db.exists(parent_doctype, parent_name) else None
    frappe.has_permission(parent_doctype, 'write' if parent else 'create', doc=parent, throw=True)
    if preview:
        # Still validate deterministic references and ownership; never provision documents.
        for item in items:
            prepared = _preflight(item, parent_doctype, parent_name, parent=parent)
            if _local_conflict(prepared): counts['conflicts'] += 1
        return counts
    if parent is None:
        raise ValueError('Native relations require the imported parent record before writes.')
    savepoint = 'hero_native_relations'
    frappe.db.savepoint(savepoint)
    try:
        for item in items:
            prepared = _preflight(item, parent_doctype, parent_name, parent=parent)
            outcome = _apply(plan, item, prepared, parent_doctype, parent_name, parent)
            counts[outcome] += 1
    except Exception:
        frappe.db.rollback(save_point=savepoint)
        raise
    return counts


def _preflight(item, parent_doctype, parent_name, parent=None):
    if item['config']['kind'] in collections.KINDS:
        return collections.preflight(item, parent_doctype, parent_name, parent=parent)
    kind = item['config']['kind']
    links = frappe.get_all('HERO Integration Link', filters={'source_system': 'HERO', 'source_type': kind,
        'source_instance': item['source_instance'], 'source_id': item['source_id']}, fields=['*'], limit_page_length=2)
    if len(links) > 1: raise ValueError('Native relation has ambiguous source bindings.')
    target = 'Bank Account' if kind == 'customer_bank_account' else 'Address'
    link = links[0] if links else None
    document = None
    if link:
        if link.target_doctype != target or not frappe.db.exists(target, link.target_name):
            raise ValueError('Native relation binding has a missing or incompatible target.')
        document = frappe.get_doc(target, link.target_name)
        frappe.has_permission(target, 'write', doc=document, throw=True)
    else:
        frappe.has_permission(target, 'create', throw=True)
    values = dict(item['values'])
    bank = None
    if kind == 'customer_bank_account':
        bank = _bank(values, owned_bank=document.get('bank') if document else None)
        if document and (document.party_type != 'Customer' or document.party != parent_name
                         or document.is_company_account or document.account or document.company):
            raise ValueError('Bank account ownership differs from the mapped Customer.')
        duplicates = frappe.get_all('Bank Account', filters={'iban': values['iban']}, fields=['name'], limit_page_length=2)
        if any(row.name != (document.name if document else None) for row in duplicates):
            raise ValueError('An existing IBAN requires explicit account reconciliation; no account was adopted.')
        values = {'iban': values['iban'], 'party_type': 'Customer', 'party': parent_name,
                  'is_company_account': 0, 'is_default': 0}
        values['bank'] = bank.name if bank else item['values']['bank_name']
    else:
        countries = frappe.get_all('Country', filters={'code': values.pop('country_code')}, fields=['name'], limit_page_length=2)
        if len(countries) != 1: raise ValueError('Address country code must resolve to exactly one ERPNext Country.')
        values.update(country=countries[0].name, address_type=item['config']['address_type'])
        if parent is None and frappe.db.exists(parent_doctype, parent_name): parent = frappe.get_doc(parent_doctype, parent_name)
        if parent:
            field = item['config']['parent_field']
            meta = frappe.get_meta(parent_doctype).get_field(field)
            if not meta or meta.fieldtype != 'Link' or meta.options != 'Address':
                raise ValueError('Configured Project address field must be a native Address link.')
            existing = parent.get(field)
            if existing and existing != (document.name if document else None):
                raise ValueError('Existing Project address is locally owned and requires reconciliation.')
    return {'link': link, 'document': document, 'values': values, 'bank': bank}


def _bank(values, *, owned_bank=None):
    """Only an exact BIC proves an existing institution; names alone never adopt one."""
    bic = values['swift_number']
    matched = frappe.get_all('Bank', filters={'swift_number': bic}, fields=['name', 'bank_name', 'swift_number'], limit_page_length=2) if bic else []
    named = frappe.get_all('Bank', filters={'bank_name': values['bank_name']}, fields=['name', 'bank_name', 'swift_number'], limit_page_length=2)
    if not bic and len(named) == 1 and named[0].name == owned_bank and not named[0].swift_number:
        frappe.has_permission('Bank', 'read', doc=frappe.get_doc('Bank', owned_bank), throw=True)
        return named[0]
    if len(matched) > 1 or len(named) > 1 or (named and (not matched or named[0].name != matched[0].name)):
        raise ValueError('Bank name and BIC do not prove one existing institution.')
    if matched:
        frappe.has_permission('Bank', 'read', doc=frappe.get_doc('Bank', matched[0].name), throw=True)
        return matched[0]
    frappe.has_permission('Bank', 'create', throw=True)
    return None


def _local_conflict(prepared):
    if prepared.get('collection_kind'):
        return collections.local_conflict(prepared)
    document, link = prepared['document'], prepared['link']
    return bool(document and _hash({field: document.get(field) for field in prepared['values']}) != link.target_payload_hash)


def _apply(plan, item, prepared, parent_doctype, parent_name, parent):
    if prepared.get('collection_kind'):
        return collections.apply(plan, item, prepared, parent_doctype, parent_name, parent)
    kind = item['config']['kind']; target = 'Bank Account' if kind == 'customer_bank_account' else 'Address'
    values = prepared['values']; document = prepared['document']; link = prepared['link']
    if document:
        observed = {field: document.get(field) for field in values}
        if _local_conflict(prepared):
            frappe.db.set_value('HERO Integration Link', link.name, {'sync_status': 'Conflict',
                'conflict_detail': 'Native relation changed locally; review before replacing mapped fields.',
                'observed_source_hash': _hash(values), 'observed_target_hash': _hash(observed),
                'last_observed_on': frappe.utils.now_datetime()}, update_modified=False)
            return 'conflicts'
    before = {field: document.get(field) for field in values} if document else None
    if document and target == 'Address':
        before['links'] = sorted((row.link_doctype, row.link_name) for row in document.get('links', []))
    if kind == 'customer_bank_account' and prepared['bank'] is None:
        bank = frappe.get_doc({'doctype': 'Bank', 'bank_name': item['values']['bank_name'],
                              'swift_number': item['values']['swift_number'] or None}).insert()
        values['bank'] = bank.name
    if document:
        document.update(values)
    else:
        title = f'{parent_name} / {item["identity"]}'
        document = frappe.get_doc({'doctype': target, **values,
            **({'account_name': title} if target == 'Bank Account' else {'address_title': title})})
    if target == 'Address' and not any(row.link_doctype == parent_doctype and row.link_name == parent_name for row in document.get('links', [])):
        document.append('links', {'link_doctype': parent_doctype, 'link_name': parent_name})
    from hero_integration.services.change_detection import native_values_changed
    after = {field: document.get(field) for field in values}
    if target == 'Address':
        after['links'] = sorted((row.link_doctype, row.link_name) for row in document.get('links', []))
    changed = before is None or native_values_changed(before, after)
    if link and changed: document.save()
    elif not link: document.insert()
    observed = {field: document.get(field) for field in values}
    evidence = {'sync_status': 'Synchronized', 'conflict_detail': '', 'mapping_version': plan.mapping_version,
                'source_payload_hash': _hash(item['values']), 'target_payload_hash': _hash(observed),
                'last_synced_on': frappe.utils.now_datetime()}
    if link: frappe.db.set_value('HERO Integration Link', link.name, evidence, update_modified=False)
    else:
        frappe.get_doc({'doctype': 'HERO Integration Link', 'source_system': 'HERO', 'source_type': kind,
            'source_instance': item['source_instance'], 'source_id': item['source_id'],
            'target_doctype': target, 'target_name': document.name, **evidence}).insert(ignore_permissions=True)
    if target == 'Address' and parent.get(item['config']['parent_field']) != document.name:
        changed = True
        parent.set(item['config']['parent_field'], document.name)
        from hero_integration.services.change_detection import managed_target_update
        with managed_target_update(parent_doctype, parent_name, None):
            parent.save()
    return ('updated' if changed else 'unchanged') if link else 'created'
