"""Object-first, snapshot-only GraphQL field catalog for non-executable drafts."""
from __future__ import annotations

import json
from collections.abc import Mapping

from hero_integration.services.graphql_graph import normalize_schema
from hero_integration.services.schema_operations import type_reference, is_required

MAX_DEPTH = 4
AREAS = {'Customers': 'HeroCustomers', 'Projects': 'HeroProjectMatches',
         'Tasks': 'HeroTasks', 'Calendar': 'HeroCalendarEvents'}
# Direct migrations (services.product_migration, services.file_transfer): their
# query roots and the nested paths they read.
DIRECT_AREAS = {
    'Products': {'supply_product_versions': ('base_data.supply_catalog', 'sales_prices.supply_sales_price'),
                 'supply_products': ()},
    'Documents': {'FileUpload_FileUploads': ('edges.node',),
                  'file_uploads': ('file_upload_matches', 'file_upload_links', 'customer_document.document_type')},
    'Pictures': {'FileUpload_FileUploads': ('edges.node',),
                 'file_uploads': ('file_upload_matches', 'file_upload_links')},
}
AREA_NAMES = (*AREAS, *DIRECT_AREAS)


def _named(ref):
    while isinstance(ref, Mapping):
        if ref.get('name'):
            return str(ref['name']), str(ref.get('kind') or '')
        ref = ref.get('ofType')
    return '', ''


def _list(ref):
    while isinstance(ref, Mapping):
        if ref.get('kind') == 'LIST':
            return True
        ref = ref.get('ofType')
    return False


def _arguments(field):
    result = []
    for arg in field.get('args') or []:
        if not isinstance(arg, Mapping) or not isinstance(arg.get('name'), str):
            continue
        result.append({'name': arg['name'], 'type': type_reference(arg.get('type')),
                       'required': is_required(arg.get('type')), 'default_value': arg.get('defaultValue')})
    return sorted(result, key=lambda x: x['name'])


def _mapping_type(name, nullable):
    base = {'String': 'string', 'ID': 'string', 'Int': 'int', 'Float': 'float',
            'Boolean': 'boolean', 'DateTime': 'string', 'Date': 'string'}.get(name, '')
    return base + ('|null' if nullable else '') if base else ''


def _object_areas(schema, types):
    """Resolve areas only along existing reviewed source-contract paths."""
    from hero_integration.services.direct_mapping import SOURCE_CONTRACTS
    from hero_integration.services.mapping_suggestions import OPERATION_ROOTS
    query = types.get((schema.get('queryType') or {}).get('name'), {})
    root_fields = {field['name']: field for field in query.get('fields') or []
                   if isinstance(field, Mapping) and isinstance(field.get('name'), str)}
    result = {}

    def mark(area, root_name, paths):
        start, kind = _named(root_fields.get(root_name, {}).get('type'))
        if kind not in {'OBJECT', 'INTERFACE', 'UNION'} or start not in types:
            return
        result.setdefault(start, set()).add(area)
        for path in paths:
            current = start
            for segment in path.split('.'):
                member = next((field for field in types[current].get('fields') or []
                               if isinstance(field, Mapping) and field.get('name') == segment), None)
                if member is None:
                    break
                name, member_kind = _named(member.get('type'))
                if member_kind not in {'OBJECT', 'INTERFACE', 'UNION'} or name not in types:
                    break
                result.setdefault(name, set()).add(area)
                current = name

    for area, operation in AREAS.items():
        mark(area, OPERATION_ROOTS.get(operation), SOURCE_CONTRACTS.get(operation, {}).get('paths', {}))
    for area, roots in DIRECT_AREAS.items():
        for root_name, paths in roots.items():
            mark(area, root_name, paths)
    return result


def catalog(snapshot, object_type='', search=''):
    schema = normalize_schema(snapshot)
    types = {t['name']: t for t in schema.get('types', [])
             if isinstance(t, Mapping) and isinstance(t.get('name'), str)}
    roots = {str(root.get('name') or '') for key in ('queryType', 'mutationType', 'subscriptionType')
             if isinstance((root := schema.get(key)), Mapping)}
    objects = sorted((name for name, t in types.items()
                      if t.get('kind') == 'OBJECT' and name not in roots and not name.startswith('__')),
                     key=lambda name: (name.casefold(), name))
    memberships = _object_areas(schema, types)
    result = {'objects': [{'name': name, 'label': name,
                           'areas': [area for area in AREA_NAMES if area in memberships.get(name, set())]}
                          for name in objects], 'items': [], 'areas': list(AREA_NAMES)}
    if not object_type:
        return result
    if object_type not in objects:
        raise ValueError('The selected HERO object is not present in the validated snapshot.')
    found = []
    needle = search.strip().casefold()

    def walk(current, segments, relations, visited, depth):
        if depth >= MAX_DEPTH or current in visited:
            return
        typename = types[current]
        for field in typename.get('fields') or []:
            if not isinstance(field, Mapping) or not isinstance(field.get('name'), str):
                continue
            ref = field.get('type'); name, kind = _named(ref)
            args = _arguments(field)
            segment = {'name': field['name'], 'owner_type': current, 'type': type_reference(ref),
                       'arguments': args}
            chain = segments + [segment]
            path = '.'.join(x['name'] for x in chain)
            is_relation = kind in {'OBJECT', 'INTERFACE', 'UNION'}
            relation = {'name': field['name'], 'owner_type': current, 'target_type': name,
                        'type': type_reference(ref), 'is_list': _list(ref), 'arguments': args}
            if is_relation and name in types:
                walk(name, chain, relations + [relation], visited | {current}, depth + 1)
                continue
            if kind not in {'SCALAR', 'ENUM'}:
                continue
            nullable = any(not item['type'].endswith('!') for item in chain)
            requires_arguments = any(arg['required'] and arg['default_value'] is None
                                     for part in chain for arg in part['arguments'])
            context = json.dumps({'object_type': object_type, 'segments': chain}, ensure_ascii=False,
                                 sort_keys=True, separators=(',', ':'), allow_nan=False)
            within_limit = len(context) <= 4096
            item = {'object_type': object_type, 'path': path, 'field_name': field['name'],
                    'label': field['name'].replace('_', ' ').title(), 'fieldtype': type_reference(ref),
                    'nullable': nullable, 'relation': ' / '.join(r['name'] for r in relations),
                    'relations': relations, 'arguments': args, 'graphql_path': object_type + '.' + path,
                    'mapping_type': _mapping_type(name, nullable), 'selectable': within_limit,
                    'activation_allowed': False, 'schema_context': context, 'requires_argument_values': requires_arguments,
                    'reason': ('Draft only. Fetch inclusion is not verified and this selection does not authorize retrieval.'
                               if within_limit else 'Relationship context exceeds the supported draft size.')}
            if not needle or needle in (path + ' ' + item['label'] + ' ' + item['fieldtype']).casefold():
                found.append(item)
    walk(object_type, [], [], set(), 0)
    result['items'] = sorted(found, key=lambda item: item['path'])
    return result


def field(snapshot, object_type, path, schema_context):
    """Resolve the exact stored context; imported metadata never grants authority."""
    choices = [item for item in catalog(snapshot, object_type)['items'] if item['path'] == path]
    if len(choices) != 1 or choices[0]['schema_context'] != schema_context:
        raise ValueError('HERO object field or relationship context does not match the saved schema.')
    return choices[0]
