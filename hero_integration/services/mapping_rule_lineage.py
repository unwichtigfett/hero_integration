"""Canonical, portable provenance for an immutable published field-rule revision."""
from __future__ import annotations

import hashlib
import json
from uuid import UUID

from hero_integration.services.personal_mapping_content import RULE_FIELDS, unique_json_object


def canonical_uuid(value):
    try:
        result = str(UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise ValueError('Rule identity must be an enrolled canonical UUID.') from None
    if result != value:
        raise ValueError('Rule identity must be an enrolled canonical UUID.')
    return result


def semantic_fingerprint(operation, schema_fingerprint, fields, *, schema_manifest=None, schema_dependencies=None):
    if not isinstance(operation, str) or not operation or not isinstance(schema_fingerprint, str) or not schema_fingerprint:
        raise ValueError('Rule operation and schema fingerprint are required.')
    if not isinstance(fields, dict) or set(fields) != set(RULE_FIELDS):
        raise ValueError('Complete canonical field-rule content is required.')
    def declaration(value, default):
        if value is None or value == '':
            return default
        if isinstance(value, str):
            try:
                value = json.loads(value, object_pairs_hook=unique_json_object,
                                   parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()))
            except (ValueError, TypeError):
                raise ValueError('Rule schema provenance is invalid.') from None
        if isinstance(value, (list, dict)):
            return value
        raise ValueError('Rule schema provenance is invalid.')
    encoded = json.dumps({'operation':operation, 'schema_fingerprint':schema_fingerprint, 'fields':fields,
                          'schema_manifest':declaration(schema_manifest, {}),
                          'schema_dependencies':declaration(schema_dependencies, [])},
                         ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def parse_lineage(raw):
    try:
        value = json.loads(raw, object_pairs_hook=unique_json_object)
    except (ValueError, TypeError):
        raise ValueError('Portable rule lineage is invalid.') from None
    if not isinstance(value, dict) or set(value) != {'version', 'logical_id', 'revision_id', 'predecessor_id', 'content_hash'}:
        raise ValueError('Portable rule lineage is invalid.')
    if type(value['version']) is not int or value['version'] != 1:
        raise ValueError('Portable rule lineage version is unsupported.')
    for key in ('logical_id', 'revision_id', 'predecessor_id'):
        canonical_uuid(value[key])
    digest = value['content_hash']
    if not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
        raise ValueError('Portable rule lineage fingerprint is invalid.')
    if value['revision_id'] == value['predecessor_id']:
        raise ValueError('A rule revision cannot precede itself.')
    return value


def next_lineage(original, revision_id, fields):
    predecessor = canonical_uuid(original.get('portable_definition_id') or '')
    revision_id = canonical_uuid(revision_id)
    prior = original.get('portable_lineage_json') or ''
    logical_id = predecessor
    if prior:
        prior_value = parse_lineage(prior)
        if prior_value['revision_id'] != predecessor:
            raise ValueError('Stored rule lineage does not identify the preceding revision.')
        current = {key: original.get(key) for key in RULE_FIELDS}
        if prior_value['content_hash'] != semantic_fingerprint(original.operation, original.schema_fingerprint, current,
                schema_manifest=original.get('schema_manifest'), schema_dependencies=original.get('schema_dependencies')):
            raise ValueError('Stored rule revision content no longer matches its fingerprint.')
        logical_id = prior_value['logical_id']
    value = {'version':1, 'logical_id':logical_id, 'revision_id':revision_id, 'predecessor_id':predecessor,
             'content_hash':semantic_fingerprint(original.operation, original.schema_fingerprint, fields,
                schema_manifest=original.get('schema_manifest'), schema_dependencies=original.get('schema_dependencies'))}
    parse_lineage(json.dumps(value))
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


_CUSTOMER_LEGACY = {
    'customer-primary-address-line2': ('address.line_2', 'string|null', 'Address', 'address_line2',
                                       'Native Binding', 'Trim', 'Customer Primary Address'),
    'customer-company-legal-form': ('company_legal_form', 'string|null', 'Customer', 'custom_hero_company_legal_form',
                                    'Field', 'Trim', ''),
}


def reserved_customer_semantics(row):
    # Contract versions restrict source selection, regardless of where a rule writes it.
    return (row.get('source_path') or '') in {spec[0] for spec in _CUSTOMER_LEGACY.values()}


def validate_customer_scope(row, mapping):
    if (row.get('status') != 'Active' or row.get('operation') != 'HeroCustomers'
            or row.get('schema_snapshot') != mapping.schema_snapshot
            or row.get('schema_fingerprint') != mapping.schema_fingerprint
            or (row.get('personal_graphql_mapping') or '') not in ('', mapping.name)):
        raise ValueError('Customer rule is outside the approved mapping context.')


def customer_legacy_literals(selected_ids, mapping, lookup_definition, lookup_portable):
    """Resolve special Customer IDs only through a verified, scoped predecessor chain."""
    found = set()
    for selected in selected_ids:
        matches = lookup_definition(selected)
        if len(matches) != 1:
            raise ValueError('Selected Customer rule revision is missing or ambiguous.')
        row = matches[0]
        validate_customer_scope(row, mapping)
        if selected in _CUSTOMER_LEGACY:
            spec = tuple((row.get(key) or '') for key in ('source_path','source_type','target_doctype',
                         'target_field','purpose','transformation','relation'))
            if spec != _CUSTOMER_LEGACY[selected] or selected in found:
                raise ValueError('Customer literal rule is incompatible or ambiguous.')
            found.add(selected)
            continue
        if not row.get('portable_lineage_json'):
            if row.get('personal_graphql_mapping'):
                raise ValueError('Scoped Customer rule lineage is missing; reconcile the legacy revision.')
            if reserved_customer_semantics(row):
                raise ValueError('Customer legacy rule identity is ambiguous; reconcile before approval.')
            continue  # Unscoped legacy roots keep their explicitly selected identities.
        leaf = row
        visited, logical_id = set(), None
        for _ in range(100):
            validate_customer_scope(row, mapping)
            version = canonical_uuid(row.get('portable_definition_id'))
            if version in visited:
                raise ValueError('Customer rule lineage contains a cycle.')
            visited.add(version)
            raw = row.get('portable_lineage_json') or ''
            if not raw:
                if logical_id != version:
                    raise ValueError('Customer rule lineage does not reach its enrolled root.')
                literal = row.get('definition_id')
                if literal in _CUSTOMER_LEGACY:
                    expected = _CUSTOMER_LEGACY[literal]
                    def spec(value):
                        return tuple((value.get(key) or '') for key in ('source_path','source_type','target_doctype',
                                     'target_field','purpose','transformation','relation'))
                    if spec(row) != expected or spec(leaf) != expected or literal in found:
                        raise ValueError('Customer literal lineage is incompatible or ambiguous.')
                    found.add(literal)
                elif reserved_customer_semantics(leaf):
                    raise ValueError('Reserved Customer semantics require the authorized literal root.')
                break
            lineage = parse_lineage(raw)
            if lineage['revision_id'] != version or (logical_id and lineage['logical_id'] != logical_id):
                raise ValueError('Customer rule lineage identity is inconsistent.')
            logical_id = lineage['logical_id']
            fields = {key:row.get(key) for key in RULE_FIELDS}
            if lineage['content_hash'] != semantic_fingerprint(row['operation'], row['schema_fingerprint'], fields,
                    schema_manifest=row.get('schema_manifest'), schema_dependencies=row.get('schema_dependencies')):
                raise ValueError('Customer rule lineage fingerprint changed.')
            previous = lookup_portable(lineage['predecessor_id'])
            if len(previous) != 1:
                raise ValueError('Customer rule predecessor is missing or ambiguous.')
            row = previous[0]
        else:
            raise ValueError('Customer rule lineage is too deep.')
    return found
