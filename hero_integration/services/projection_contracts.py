"""Closed read-only projections; configuration cannot add executable adapters."""
from __future__ import annotations

import json
from pathlib import Path


CONTRACTS = json.loads(Path(__file__).with_suffix('.json').read_text())
STATUS_PATHS = {
    'current_project_match_status.id': 'int|null',
    'current_project_match_status.status_code': 'int|null',
    'current_project_match_status.name': 'string|null',
    'current_project_match_status.short_name': 'string|null',
    'current_project_match_status.created': 'string|null',
    'current_project_match_status.modified': 'string|null',
    'current_project_match_status.step_id': 'int|null',
}


def validate_projection_spec(spec, metadata):
    contract = CONTRACTS[spec.operation]
    if spec.target_doctype != contract['target_doctype']:
        raise ValueError('Projection target is not its registered HERO DocType.')
    if spec.purpose == 'Project Relation':
        expected = (contract['project_path'], 'custom_hero_project', 'HERO Project Link', 'HERO Project Link')
        actual = (spec.source_path, spec.target_field, spec.transformation, spec.relation)
        if not contract['project_path'] or actual != expected:
            raise ValueError('Projection does not match its reviewed Project relation.')
        expected_type, expected_options = 'Link', 'Project'
    elif spec.purpose == 'Native Binding' and spec.target_field in contract.get('native_bindings', {}):
        binding = contract['native_bindings'][spec.target_field]
        if (spec.source_path, spec.transformation, spec.relation) != (binding['source_path'], 'Native Adapter', ''):
            raise ValueError('Native binding is not its registered source/target pair.')
        expected_type, expected_options = binding['fieldtype'], binding.get('options', '')
    else:
        binding = contract['bindings'].get(spec.source_path)
        if not binding or (spec.target_field, spec.purpose, spec.transformation, spec.relation) != (
            binding['field'], binding['purpose'], binding['transformation'], '',
        ):
            raise ValueError('Projection does not match its reviewed field binding.')
        expected_type, expected_options = binding['fieldtype'], binding.get('options', '')
    field = metadata.field(spec.target_doctype, spec.target_field)
    if field is None or field.fieldtype != expected_type or (field.options or '') != expected_options:
        raise ValueError('Projection field is absent or incompatible.')
    if getattr(field, 'read_only', 0):
        raise ValueError('Projection field is not writable.')


def validate_projection_record(operation, record):
    """Missing selections and wrong scalar types fail before target writes."""
    import math
    contract = CONTRACTS[operation]
    for path, declared in contract['paths'].items():
        if path not in record:
            raise ValueError('Projection response omits a selected field: ' + path)
        value = record[path]
        if value is None and '|null' in declared:
            continue
        kind = declared.split('|')[0]
        valid = {
            'int': isinstance(value, int) and not isinstance(value, bool),
            'float': isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value),
            'boolean': isinstance(value, bool),
            'string': isinstance(value, str),
            'json': True,
        }.get(kind, False)
        if not valid:
            raise ValueError('Projection response has an incompatible field: ' + path)
        binding = contract['bindings'][path]
        if 'object_selection' in binding and (not isinstance(value, dict) or set(value) != set(binding['object_selection'])):
            raise ValueError('Projection nested object has an invalid selection.')
        if 'selection' in binding:
            if value is None:
                continue
            if not isinstance(value, list) or not all(isinstance(row, dict) and set(row) == set(binding['selection']) for row in value):
                raise ValueError('Projection reference list has an invalid selection.')
            ids = [row.get('id') for row in value]
            if any(not isinstance(i, int) or isinstance(i, bool) or i <= 0 for i in ids) or len(ids) != len(set(ids)):
                raise ValueError('Projection reference list identities are missing or duplicated.')
        if binding.get('member_types'):
            rows = [value] if 'object_selection' in binding else value
            for row in rows:
                for name, declared_type in binding['member_types'].items():
                    member = row[name]
                    if member is None and '|null' in declared_type:
                        continue
                    scalar = declared_type.split('|')[0]
                    valid_member = ((scalar == 'string' and isinstance(member, str)) or
                                    (scalar == 'boolean' and type(member) is bool) or
                                    (scalar == 'int' and type(member) is int) or
                                    (scalar == 'float' and type(member) in (int, float) and math.isfinite(member)))
                    if not valid_member:
                        raise ValueError('Nested projection scalar has an incompatible type: ' + name)


def approved_query(operation):
    contract = CONTRACTS[operation]
    return ('query ' + operation + '($first: Int!, $offset: Int!) {\n  '
            + contract['root'] + '(first: $first, offset: $offset, orderBy: "id") {\n    '
            + '\n    '.join(contract['selection']) + '\n  }\n}\n')
