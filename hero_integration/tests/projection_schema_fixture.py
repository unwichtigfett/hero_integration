"""Generate schema fixtures from the projection fields the tests exercise."""
import json
from uuid import uuid4

from graphql import build_schema, introspection_from_schema

from hero_integration.services import projection_contracts

_GRAPHQL = {'int': 'Int', 'string': 'String', 'boolean': 'Boolean'}
_NESTED = {
    'resources': ('CompanyResource', 'id: Int name: String type: String type_id: Int'),
    'partners': ('Partner', 'id: Int full_name: String'),
}


def projection_sdl(*, drop=(), retype=None):
    retype = retype or {}
    types, roots = [], []
    for contract in projection_contracts.CONTRACTS.values():
        fields = []
        for path, source_type in contract['paths'].items():
            if (contract['type'], path) in drop:
                continue
            if path in _NESTED:
                fields.append(f'{path}: [{_NESTED[path][0]}]')
            else:
                fields.append(f"{path}: {retype.get((contract['type'], path), _GRAPHQL[source_type.split('|')[0]])}")
        types.append(f"type {contract['type']} {{ {' '.join(fields)} }}")
        roots.append(f"{contract['root']}(first: Int, offset: Int, orderBy: String): [{contract['type']}]")
    types += [f'type {name} {{ {fields} }}' for name, fields in _NESTED.values()]
    return 'type Query { ' + ' '.join(roots) + ' } ' + ' '.join(types)


def insert_projection_snapshot(source_instance='default'):
    """Insert a validated, self-contained schema; caller owns the transaction."""
    import frappe
    from hero_integration.services.introspection import endpoint_fingerprint

    # Each persisted test snapshot must have its own fingerprint, even if prior
    # tests committed their otherwise equivalent schemas to the disposable site.
    sdl = projection_sdl() + f' type SyntheticFixture{uuid4().hex} {{ value: String }}'
    schema = introspection_from_schema(build_schema(sdl))
    return frappe.get_doc({
        'doctype': 'HERO GraphQL Schema Snapshot',
        'source_instance': source_instance,
        'endpoint_fingerprint': endpoint_fingerprint('https://synthetic.invalid/graphql', source_instance),
        'snapshot_json': json.dumps(schema),
    }).insert()
