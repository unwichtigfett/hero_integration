"""Structural personal-draft content, never an executable mapping normalizer."""
from copy import deepcopy
import json
from uuid import UUID

from hero_integration.services.mapping_draft_service import MappingDraftError

RULE_FIELDS = frozenset({'source_path', 'source_type', 'target_doctype', 'target_field',
                         'purpose', 'transformation', 'relation', 'priority'})
# These are presentation/domain configuration paths, not document/actor metadata.
EDITABLE = {
    'source': frozenset({'doctype', 'field', 'path', 'source_path', 'type'}),
    'target': frozenset({'doctype', 'field', 'field_path', 'target_field', 'operation',
                         'argument', 'input_type', 'graphql_type', 'type'}),
}
TEXT_FIELDS = frozenset({'record_path', 'records_path', 'identity_path', 'target_operation',
                         'operation', 'operation_name', 'path', 'source_path', 'target_field',
                         'transformation', 'transform', 'purpose', 'relation'})


def same(left, right):
    return json.dumps(left, sort_keys=True, ensure_ascii=False, allow_nan=False) == json.dumps(right, sort_keys=True, ensure_ascii=False, allow_nan=False)


def references(content):
    values = content.get('mapping_definition_ids', [])
    if not isinstance(values, list) or len(values) > 100 or len(set(v for v in values if isinstance(v, str))) != len(values):
        raise MappingDraftError('Field references must be unique definition IDs.')
    if any(not isinstance(v, str) or not v or len(v) > 140 for v in values):
        raise MappingDraftError('Invalid field reference.')
    return values


def bounded(content):
    if len(json.dumps(content, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')) > 65536:
        raise MappingDraftError('Personal content exceeds its size limit.')
    remaining = 4096
    def walk(value, depth=0):
        nonlocal remaining
        remaining -= 1
        if depth > 16 or remaining < 0:
            raise MappingDraftError('Personal content exceeds its complexity limit.')
        for child in value.values() if isinstance(value, dict) else value if isinstance(value, list) else ():
            walk(child, depth + 1)
    walk(content)
    return content


def same_value(left, right):
    # JSON has one number kind. Browsers serialize 1.0 as 1 and -0.0 as 0;
    # booleans are never numbers, despite Python's bool/int equality.
    if type(left) in (int, float) and type(right) in (int, float):
        return left == right
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(same_value(left[k], right[k]) for k in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(same_value(a, b) for a, b in zip(left, right))
    return left == right


def validate_edits(prior, submitted):
    """Validate edits and retain exact stored values at immutable paths."""
    result = deepcopy(submitted)
    for key in set(prior) | set(submitted):
        if key == 'native_task_onboarding':
            if key not in submitted:
                continue
            scope = submitted[key]
            if (prior.get('target_operation') != 'HeroTasks' or submitted.get('target_operation') != 'HeroTasks'
                    or not isinstance(scope, dict)
                    or set(scope) != {'source_instance', 'source_type', 'confirmed'}
                    or scope.get('confirmed') is not True or scope.get('source_type') != 'Task'
                    or not isinstance(scope.get('source_instance'), str)
                    or not scope['source_instance'].strip() or scope['source_instance'] != scope['source_instance'].strip()):
                raise MappingDraftError('Native Task onboarding requires an explicit Task source scope.')
            continue
        if key == 'additional_mappings':
            validate_additional_mappings(submitted.get(key, []))
            continue
        if key in {'field_rules', 'field_rule_bases', 'mapping_definition_ids'}:
            continue
        if key in TEXT_FIELDS:
            if key in submitted and not isinstance(submitted[key], str):
                if key not in prior or not same_value(submitted[key], prior[key]):
                    raise MappingDraftError('Invalid text configuration.')
                result[key] = deepcopy(prior[key])
        elif key in EDITABLE:
            if key not in submitted:
                continue
            value, before = submitted[key], prior.get(key, {})
            if not isinstance(value, dict) or not isinstance(before, dict):
                if key not in prior or not same_value(value, before):
                    raise MappingDraftError('Unsupported assignment shape.')
                result[key] = deepcopy(prior[key])
                continue
            for child in set(before) | set(value):
                if child in EDITABLE[key]:
                    if child in value and not isinstance(value[child], str):
                        if child not in before or not same_value(value[child], before[child]):
                            raise MappingDraftError('Invalid assignment field.')
                        result[key][child] = deepcopy(before[child])
                else:
                    if child not in before or child not in value or not same_value(value[child], before[child]):
                        raise MappingDraftError('Additional assignment parameter is read-only.')
                    result[key][child] = deepcopy(before[child])
        else:
            if key not in prior or key not in submitted or not same_value(submitted[key], prior[key]):
                raise MappingDraftError('Additional mapping parameter is read-only.')
            result[key] = deepcopy(prior[key])
    references(submitted)
    return result


def validate_additional_mappings(items):
    if not isinstance(items, list) or len(items) > 100:
        raise MappingDraftError('Additional mappings must be a bounded list.')
    seen = set()
    for item in items:
        if not isinstance(item, dict) or set(item) != {'id', 'source', 'target', 'transformation'}:
            raise MappingDraftError('Invalid additional mapping shape.')
        try:
            identity = str(UUID(item['id']))
        except (ValueError, TypeError, AttributeError):
            raise MappingDraftError('Invalid additional mapping identity.') from None
        if identity != item['id'] or identity in seen:
            raise MappingDraftError('Additional mapping identities must be unique canonical UUIDs.')
        seen.add(identity)
        if not isinstance(item['transformation'], str) or item['transformation'] not in {'Identity', 'Trim'}:
            raise MappingDraftError('Unsupported additional mapping transformation.')
        for side in ('source', 'target'):
            value = item[side]
            if not isinstance(value, dict) or not value or set(value) - EDITABLE[side]:
                raise MappingDraftError('Invalid additional mapping assignment.')
            if any(not isinstance(v, str) or not v or len(v) > 4096 for v in value.values()):
                raise MappingDraftError('Additional mapping fields must be nonempty text.')
        if 'field' not in item['source'] or 'field' not in item['target']:
            raise MappingDraftError('Both additional mapping fields are required.')


def validate_rule(rule):
    if not isinstance(rule, dict) or set(rule) - RULE_FIELDS:
        raise MappingDraftError('Unsupported field-rule parameter.')
    for key, value in rule.items():
        if key == 'priority':
            if value is not None and (type(value) is not int or not -1000000 <= value <= 1000000):
                raise MappingDraftError('Invalid field-rule priority.')
        elif value is not None and (not isinstance(value, str) or len(value) > 4096):
            raise MappingDraftError('Invalid field-rule text.')
