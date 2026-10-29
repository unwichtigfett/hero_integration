"""Structural personal-draft content, never an executable mapping normalizer."""
from copy import deepcopy
import json
from uuid import UUID

from hero_integration.services.mapping_draft_service import MappingDraftError

RULE_FIELDS = frozenset({'source_path', 'source_type', 'target_doctype', 'target_field',
                         'purpose', 'transformation', 'relation', 'priority'})
# These are presentation/domain configuration paths, not document/actor metadata.
EDITABLE = {
    'source': frozenset({'doctype', 'field', 'path', 'source_path', 'type', 'object_type', 'schema_context'}),
    'target': frozenset({'doctype', 'field', 'field_path', 'target_field', 'operation',
                         'argument', 'input_type', 'graphql_type', 'type'}),
}
TEXT_FIELDS = frozenset({'record_path', 'records_path', 'identity_path', 'target_operation',
                         'operation', 'operation_name', 'path', 'source_path', 'target_field',
                         'transformation', 'transform', 'purpose', 'relation'})


def unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Mapping JSON contains duplicate object keys.')
        result[key] = value
    return result


def same(left, right):
    return json.dumps(left, sort_keys=True, ensure_ascii=False, allow_nan=False) == json.dumps(right, sort_keys=True, ensure_ascii=False, allow_nan=False)


def references(content):
    values = content.get('mapping_definition_ids', [])
    if not isinstance(values, list) or len(values) > 100 or len(set(v for v in values if isinstance(v, str))) != len(values):
        raise MappingDraftError('Field references must be unique definition IDs.')
    if any(not isinstance(v, str) or not v or len(v) > 140 for v in values):
        raise MappingDraftError('Invalid field reference.')
    return values


def identity_sets(content, direction):
    """Return local rule identities and child-definition identities for one direction."""
    if direction not in {'QUERY_TO_FRAPPE', 'FRAPPE_TO_MUTATION'}:
        raise MappingDraftError('Draft direction is required to resolve mapping identities.')
    rules = set(references(content))
    snapshots = content.get('field_rules', {})
    if not isinstance(snapshots, dict) or any(not isinstance(key, str) or not key for key in snapshots):
        raise MappingDraftError('Personal Draft field-rule snapshots are invalid.')
    rules.update(snapshots)
    additions = content.get('additional_mappings') or []
    validate_additional_mappings(additions)
    additional_ids = {item['id'] for item in additions}
    if direction == 'QUERY_TO_FRAPPE':
        canonical_rules = set()
        for identity in rules:
            try:
                canonical_rules.add(str(UUID(identity)))
            except (TypeError, ValueError, AttributeError):
                continue
        if canonical_rules & additional_ids:
            raise MappingDraftError('Additional mapping identity duplicates a selected field rule.')
        rules.update(additional_ids)
        return rules, set()
    return rules, additional_ids


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
        if key == 'field_contexts':
            contexts = submitted.get(key, {})
            if not isinstance(contexts, dict) or len(contexts) > 100:
                raise MappingDraftError('Invalid object field contexts.')
            for identity, context in contexts.items():
                if (not isinstance(identity, str) or not identity or len(identity) > 140
                        or not isinstance(context, dict) or set(context) != {'object_type', 'schema_context'}
                        or any(not isinstance(value, str) or not value or len(value) > 4096
                               for value in context.values())):
                    raise MappingDraftError('Invalid object field context.')
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


def validate_exchange_action(content, schema, operation_name):
    """Project a primary mutation Draft onto the existing reviewed action contract.

    The projection contains configuration only. Runtime identities, release
    decisions and acknowledgements remain exclusively target-owned.
    """
    from hero_integration.services.frappe_writeback import (
        WritebackContractError, normalise_mapping, reviewed_input_target,
    )
    try:
        if not isinstance(content, dict):
            raise WritebackContractError('Action configuration must be an object.')
        incoming_source, incoming_target = content.get('source'), content.get('target')
        if (not isinstance(incoming_source, dict) or set(incoming_source) != {'doctype', 'field'}
                or not isinstance(incoming_target, dict)
                or not {'operation', 'argument', 'input_type', 'field'} <= set(incoming_target)
                or set(incoming_target) - {'operation', 'argument', 'input_type', 'field', 'field_path', 'graphql_type'}):
            raise WritebackContractError('Action contains an unsupported parameter.')
        if 'version' in content and (type(content['version']) is not int or content['version'] != 1):
            raise WritebackContractError('Unsupported action version.')
        action = normalise_mapping(content, schema)
        source, target = action['source'], action['target']
        reviewed = reviewed_input_target(source['doctype'], source['field'], schema)
        if target['operation'] != operation_name:
            raise WritebackContractError('Action operation does not match the selected definition.')
        if 'field_path' in incoming_target and incoming_target['field_path'] != target['field']:
            raise WritebackContractError('Action input path conflicts with the reviewed field.')
        if 'graphql_type' in incoming_target and incoming_target['graphql_type'] != reviewed['graphql_type']:
            raise WritebackContractError('Action input type conflicts with the saved schema.')
    except (WritebackContractError, KeyError, TypeError) as error:
        raise MappingDraftError('Imported action does not match the reviewed target contract.') from error
    return {'mapping': action, 'graphql_type': reviewed['graphql_type']}


def prepare_exchange_content(incoming, local, resolve_rule):
    """Prepare imported draft configuration using only target-side rule provenance.

    ``resolve_rule`` must read the current, scoped rule and return its rule and
    provenance snapshots.  The caller must bind ``references`` to its preview
    and repeat this operation before saving; these snapshots are evidence for
    optimistic concurrency, not grants of execution or identity authority.
    """
    if not isinstance(incoming, dict) or not isinstance(local, dict):
        raise MappingDraftError('Draft content must be an object.')
    proposed = deepcopy(incoming)
    ids = references(proposed)
    rules = proposed.get('field_rules', {})
    imported_bases = proposed.get('field_rule_bases', {})
    local_rules = local.get('field_rules', {})
    local_bases = local.get('field_rule_bases', {})
    if not all(isinstance(x, dict) for x in (rules, imported_bases, local_rules, local_bases)):
        raise MappingDraftError('Invalid field-rule snapshots.')
    if set(rules) != set(imported_bases) or set(local_rules) != set(local_bases):
        raise MappingDraftError('Field-rule snapshots require matching provenance.')
    if (any(not isinstance(identity, str) or not identity for identity in rules)
            or set(ids) - set(rules)):
        raise MappingDraftError('Every selected rule requires a named snapshot.')
    if set(local_rules) - set(rules) or set(rules) - (set(ids) | set(local_rules)):
        raise MappingDraftError('Field-rule snapshots have unexpected identities.')
    # Onboarding is an explicit local confirmation, never an import grant.
    if not same_value(proposed.get('native_task_onboarding'), local.get('native_task_onboarding')):
        raise MappingDraftError('Native Task onboarding must be confirmed locally.')
    evidence, conflicts = [], []
    for identity in sorted(rules):
        validate_rule(rules[identity])
        try:
            current_rule, current_base = resolve_rule(identity)
            if not isinstance(current_rule, dict) or not isinstance(current_base, dict):
                raise ValueError('Invalid target rule snapshot.')
        except (ValueError, LookupError) as error:
            conflicts.append({'identity': identity, 'state': 'UNRESOLVED', 'detail': str(error)})
            continue
        entry = {'identity': identity, 'base': deepcopy(current_base), 'rule': deepcopy(current_rule)}
        evidence.append(entry)
        if not same(imported_bases[identity], current_base):
            conflicts.append({'identity': identity, 'state': 'CONFLICT', 'detail': 'Target provenance differs.'})
        if identity in local_bases and not same(local_bases[identity], current_base):
            conflicts.append({'identity': identity, 'state': 'CONFLICT', 'detail': 'Local provenance is stale.'})
    # New reference bases are filled by canonicalize from _rule_snapshot;
    # existing local bases are retained verbatim and never taken from the file.
    proposed['field_rule_bases'] = deepcopy(local_bases)
    if 'field_rules' not in incoming and 'field_rules' not in local:
        proposed.pop('field_rules', None)
        proposed.pop('field_rule_bases', None)
    validate_edits(local, proposed)
    return {'content': bounded(proposed), 'references': evidence, 'conflicts': conflicts,
            'can_save': not conflicts}
