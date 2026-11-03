"""Mapping-owned native read contracts; adapters retain native document safety."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, time, timezone
from hashlib import sha256
import json
import math
import re
from zoneinfo import ZoneInfo

from hero_integration.services import direct_operations as direct
from hero_integration.services.direct_mapping import MISSING, lookup, transformed_value

ROOTS = {'HeroCustomers': ('contacts', 'Customer'), 'HeroProjectMatches': ('project_matches', 'Project'),
         'HeroCalendarEvents': ('calendar_events', 'Event'), 'HeroTasks': ('tasks', 'Task'),
         **{name: (item['root'], item['owner']) for name, item in direct.OPERATIONS.items()}}
TRANSFORMS = {'Identity', 'Trim', 'Positive Int To Text', 'Nonempty String ID', 'Scalar Text',
              'JSON Value', 'Salutation Table', 'Customer Type From Company Name', 'HERO Customer Link',
              'Confirmed ERPNext Company', 'HERO Project Link', 'Native Adapter', 'Value Map', 'Link Map',
              'Local Date', 'Local Datetime', 'Safe HTML', 'Table Rows', 'Number', 'Integer'} | direct.TRANSFORMS
FIELDS = {
    'Customer': {'customer_name', 'customer_type', 'first_name', 'last_name', 'email_id', 'mobile_no', 'salutation'},
    'Contact': {'first_name', 'last_name', 'email_id', 'mobile_no', 'salutation'},
    'Project': {'project_name', 'customer', 'company', 'project_type', 'department'},
    'Task': {'subject', 'description', 'project', 'exp_start_date', 'exp_end_date', 'completed_on'},
    'Event': {'subject', 'description', 'location', 'starts_on', 'ends_on', 'all_day'},
    'Address': {'address_line1', 'address_line2', 'pincode', 'city', 'country', 'address_title', 'address_type'},
    'ToDo': {'allocated_to'}, 'Comment': {'content', 'creation'}, 'HERO Integration Link': {'source_id'},
    **{doctype: set(fields) for doctype, fields in direct.FIELDS.items()},
}
PATH = re.compile(r'^[_A-Za-z][_0-9A-Za-z]*(?:\.[_A-Za-z][_0-9A-Za-z]*)*$')

TEXT_TYPES = {'Data', 'Small Text', 'Text', 'Long Text', 'Text Editor', 'Select', 'Link'}
FLOAT_TYPES = {'Float', 'Currency', 'Percent'}
INT_TYPES = {'Int', 'Long Int'}
TARGET_TYPES = TEXT_TYPES | FLOAT_TYPES | INT_TYPES | {'Check', 'Date', 'Datetime', 'Time', 'Table'}
CHILD_TYPES = {'Data', 'Small Text', 'Int', 'Float', 'Currency', 'Percent', 'Check'}
CHILD_TRANSFORMS = {'Identity', 'Trim', 'Scalar Text', 'Number', 'Integer'}


def _hash(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def validate_query_adapter(value, definitions=()):
    if not isinstance(value, Mapping) or value.get('version') != 2 or type(value['version']) is not int:
        raise ValueError('Query adapter configuration requires version 2.')
    if set(value) - {'version', 'transform_options', 'logbook', 'read_service', 'role', 'max_records'}:
        raise ValueError('Query adapter configuration has unsupported parameters.')
    if 'read_service' in value and (not isinstance(value['read_service'], str) or not value['read_service'].strip()):
        raise ValueError('Query adapter read service must be an explicit document name.')
    if 'role' in value and value['role'] != 'LOOKUP':
        raise ValueError('Query adapter role is unsupported.')
    if 'max_records' in value and (type(value['max_records']) is not int or not 1 <= value['max_records'] <= 1000):
        raise ValueError('Lookup result bound must be explicit.')
    options = value.get('transform_options', {})
    if not isinstance(options, dict) or len(options) > 100:
        raise ValueError('Query transform options must identify bounded field rules.')
    ids = {d.definition_id for d in definitions}
    for identity, settings in options.items():
        if not isinstance(identity, str) or not identity or (ids and identity not in ids):
            raise ValueError('Query transform options refer to an unselected field rule.')
        if not isinstance(settings, dict) or set(settings) - {'map', 'default', 'casefold', 'link_doctype', 'timezone', 'child_fields', 'target_type', 'child_types', 'child_transforms'}:
            raise ValueError('Query field transformation has unsupported options.')
        if 'target_type' in settings and settings['target_type'] not in TARGET_TYPES:
            raise ValueError('Mapped target type is unsupported.')
        if 'map' in settings:
            table = settings['map']
            if (not isinstance(table, dict) or len(table) > 1000
                    or any(not isinstance(k, str) or not _scalar(v) for k, v in table.items())):
                raise ValueError('Value Map requires a bounded scalar lookup table.')
        if 'default' in settings and not _scalar(settings['default']):
            raise ValueError('A mapped default must be a scalar value.')
        if 'casefold' in settings and type(settings['casefold']) is not bool:
            raise ValueError('Value Map case folding must be explicit.')
        if settings.get('casefold') and len({key.casefold() for key in settings.get('map', {})}) != len(settings.get('map', {})):
            raise ValueError('Case-folded Value Map keys must remain unique.')
        if 'link_doctype' in settings and settings['link_doctype'] not in {'Project Type', 'Department', 'Country', 'User', 'Project', 'Customer', 'Salutation'}:
            raise ValueError('Link Map target is not a supported native reference.')
        if 'timezone' in settings:
            if not isinstance(settings['timezone'], str) or not settings['timezone']:
                raise ValueError('Mapped timezone must be an explicit IANA identifier.')
            ZoneInfo(settings['timezone'])
        if 'child_fields' in settings:
            children = settings['child_fields']
            if not isinstance(children, dict) or not children or len(children) > 20 or any(
                    not isinstance(k, str) or not PATH.fullmatch(k) or not isinstance(v, str)
                    or '.' in v or not v.startswith('custom_hero_') for k, v in children.items()):
                raise ValueError('Table Rows requires explicit app-owned child-field bindings.')
            if len(set(children.values())) != len(children):
                raise ValueError('Table child targets must be unique.')
            child_types = settings.get('child_types')
            if not isinstance(child_types, dict) or set(child_types) != set(children.values()) or any(
                    value not in CHILD_TYPES for value in child_types.values()):
                raise ValueError('Table Rows requires explicit native types for every child target.')
            child_transforms = settings.get('child_transforms', {})
            if not isinstance(child_transforms, dict) or set(child_transforms) - set(children.values()) or any(
                    value not in CHILD_TRANSFORMS for value in child_transforms.values()):
                raise ValueError('Table child transformations are unsupported.')
    if value.get('role') == 'LOOKUP' and (options or value.get('logbook')):
        raise ValueError('Lookup services cannot contain unused target transformations or logbook configuration.')
    logbook = value.get('logbook')
    if logbook is not None:
        if (not isinstance(logbook, dict) or set(logbook) - {'path', 'identity_path', 'limit', 'heading_paths'}
                or not PATH.fullmatch(logbook.get('path', '')) or not PATH.fullmatch(logbook.get('identity_path', ''))
                or type(logbook.get('limit')) is not int or not 1 <= logbook['limit'] <= 500):
            raise ValueError('Logbook requires explicit collection, identity and bounded first limit.')
        headings = logbook.get('heading_paths', [])
        if not isinstance(headings, list) or len(headings) > 5 or any(
                not isinstance(path, str) or not PATH.fullmatch(path) or not path.startswith(logbook['path'] + '.') for path in headings):
            raise ValueError('Logbook headings must be selected child paths.')
    canonical = json.loads(json.dumps(value, sort_keys=True, allow_nan=False))
    if len(json.dumps(canonical).encode()) > 65536:
        raise ValueError('Query adapter configuration exceeds its size limit.')
    return canonical


def _scalar(value):
    return value is None or type(value) in {str, bool, int} or (type(value) is float and math.isfinite(value))


def validate_v2_spec(spec, metadata, adapter_options=None):
    """Validate selected fields; source paths are proven against the query's schema."""
    if spec.operation not in ROOTS or not isinstance(spec.source_path, str) or not PATH.fullmatch(spec.source_path):
        raise ValueError('Query rule has an unsupported adapter or source path.')
    if spec.target_doctype not in FIELDS or spec.transformation not in TRANSFORMS:
        raise ValueError('Query target or transformation is unsupported.')
    if type(spec.priority) is not int or not 0 <= spec.priority <= 10:
        raise ValueError('Query rule priority must be an integer from 0 through 10.')
    if spec.operation in direct.OPERATIONS:
        direct.validate_spec(spec)
    else:
        _validate_native_spec(spec, adapter_options)
    if not isinstance(spec.source_type, str) or spec.source_type.split('|')[0] not in {'string', 'int', 'float', 'boolean', 'json'}:
        raise ValueError('Query source type is unsupported.')
    _validate_option_keys(spec, (adapter_options or {}).get('transform_options', {}).get(spec.definition_id, {}))
    if metadata is None:
        return
    if (spec.target_doctype, spec.target_field) == ('HERO Integration Link', 'source_id'):
        return  # the technical identity is written by the integration itself, never through a form
    metadata.ensure_write('Project' if spec.target_doctype == 'Comment' else spec.target_doctype)
    if (spec.target_doctype, spec.target_field) == ('Comment', 'creation'):
        _validate_output_compatibility(spec, (adapter_options or {}).get('transform_options', {}).get(spec.definition_id, {}), 'Datetime')
        return  # Frappe's standard Datetime column is absent from Meta.fields.
    contact_proxy = (spec.purpose == 'Customer Primary Contact'
        and spec.target_doctype in {'Customer', 'Contact'}
        and spec.target_field in {'first_name', 'last_name', 'email_id', 'mobile_no', 'salutation'})
    field = metadata.field(spec.target_doctype, spec.target_field)
    if (field is None or getattr(field, 'is_virtual', False) or (getattr(field, 'fetch_from', None) and not contact_proxy)
            or getattr(field, 'hidden', False) or getattr(field, 'permlevel', 0)):
        raise ValueError('Mapped native field is absent or computed.')
    if (getattr(field, 'read_only', 0) and not spec.target_field.startswith('custom_hero_') and not contact_proxy
            and spec.target_doctype not in {'Customer', 'Comment'}
            and spec.target_field not in direct.FIELDS.get(spec.target_doctype, ())):
        # Explicitly listed direct-migration fields (e.g. File.file_name) are set by their importer.
        raise ValueError('Mapped native field is read-only.')
    if field.fieldtype == 'Text Editor' and spec.transformation not in {'Safe HTML', 'Native Adapter'}:
        raise ValueError('Rich text fields require the HTML safety adapter.')
    if field.fieldtype in {'Password', 'Attach', 'Attach Image', 'Code', 'Signature', 'Button', 'HTML'}:
        raise ValueError('Mapped native field type is unsupported.')
    opts = (adapter_options or {}).get('transform_options', {}).get(spec.definition_id, {})
    _validate_output_compatibility(spec, opts, field.fieldtype)
    if spec.transformation in direct.TRANSFORMS:
        return  # a coded HERO resolver writes exactly its registered field
    if spec.transformation in {'Local Date', 'Local Datetime'} and field.fieldtype != {'Local Date': 'Date', 'Local Datetime': 'Datetime'}[spec.transformation]:
        raise ValueError('Mapped date transformation differs from the native target type.')
    if spec.transformation == 'Link Map' and (field.fieldtype != 'Link' or getattr(field, 'options', '') != opts.get('link_doctype')):
        raise ValueError('Link Map must name the native field reference type.')
    if field.fieldtype == 'Select' and spec.transformation in {'Value Map', 'Link Map'}:
        choices = set((getattr(field, 'options', '') or '').splitlines())
        mapped = list(opts.get('map', {}).values()) + ([opts['default']] if 'default' in opts else [])
        if any(value is not None and value not in choices for value in mapped):
            raise ValueError('Value Map contains an unsupported native Select value.')
    if field.fieldtype == 'Link' and spec.transformation not in {'Link Map', 'HERO Customer Link', 'HERO Project Link', 'Confirmed ERPNext Company', 'Salutation Table'}:
        raise ValueError('Native links require an explicit approved resolver.')
    if field.fieldtype == 'Table':
        if spec.transformation != 'Table Rows' or not opts.get('child_fields'):
            raise ValueError('Native tables require explicit child mappings.')
        for child in opts['child_fields'].values():
            entry = metadata.field(field.options, child)
            if (entry is None or entry.fieldtype not in CHILD_TYPES
                    or any(getattr(entry, name, False) for name in ('hidden', 'permlevel', 'read_only', 'is_virtual', 'fetch_from'))):
                raise ValueError('Native table child is absent, protected or incompatible.')
            if opts.get('child_types', {}).get(child) != entry.fieldtype:
                raise ValueError('Configured table child type differs from native metadata.')


def validate_plan(plan):
    options = validate_query_adapter(plan.adapter_options, plan.definitions)
    operation = plan.target_operation or plan.operation
    if options.get('role') == 'LOOKUP':
        raise ValueError('Lookup mappings cannot authorize a native document writer.')
    if operation not in ROOTS:
        raise ValueError('Query adapter is unsupported.')
    if not 1 <= len(plan.definitions) <= 100:
        raise ValueError('Query adapter requires a bounded nonempty field-rule selection.')
    identities_by_rule = [d.definition_id for d in plan.definitions]
    if any(not identity for identity in identities_by_rule) or len(set(identities_by_rule)) != len(identities_by_rule):
        raise ValueError('Query field-rule identities must be nonempty and unique.')
    identities = [d for d in plan.definitions if d.purpose == 'Source Identity']
    if len(identities) != 1:
        raise ValueError('Query adapter requires exactly one source identity.')
    if operation in direct.OPERATIONS:
        direct.validate_plan(plan)
    elif identities[0].source_path != 'id':
        raise ValueError('Native query identity must use the canonical source id.')
    if identities[0].transformation not in {'Positive Int To Text', 'Nonempty String ID'}:
        raise ValueError('Query source identity requires a lossless identity transformation.')
    contact_fields = FIELDS['Contact']
    targets = [direct.target_key(d) if operation in direct.OPERATIONS else
               ('Contact' if d.target_doctype == 'Customer' and d.target_field in contact_fields
                else d.target_doctype, d.target_field) for d in plan.definitions]
    if len(set(targets)) != len(targets):
        raise ValueError('Query adapter cannot write one target field twice.')
    for spec in plan.definitions:
        validate_v2_spec(spec, None, options)
        opts = options.get('transform_options', {}).get(spec.definition_id, {})
        if spec.transformation in {'Value Map', 'Link Map'} and not isinstance(opts.get('map'), dict):
            raise ValueError('Mapped value conversion requires an explicit table.')
        if spec.transformation in {'Local Date', 'Local Datetime'} and not opts.get('timezone'):
            raise ValueError('Mapped dates require an explicit source timezone.')
    if operation in {'HeroCustomers', 'HeroProjectMatches', 'HeroTasks'}:
        if len([d for d in plan.definitions if d.purpose == 'Record Name']) != 1:
            raise ValueError('Native intake requires a record-name binding.')
    if options.get('logbook') and not any(d.target_doctype == 'Comment' and d.target_field == 'content' for d in plan.definitions):
        raise ValueError('Logbook collection requires an explicit Comment content binding.')
    if operation == 'HeroCustomers' and not any(d.target_doctype == 'Customer' and d.target_field == 'customer_type' for d in plan.definitions):
        raise ValueError('Customer intake requires an explicit customer-type binding.')
    if operation == 'HeroProjectMatches':
        if not {'Customer Relation', 'Confirmed Company'} <= {d.purpose for d in plan.definitions}:
            raise ValueError('Project intake requires customer and confirmed company bindings.')
    if operation == 'HeroCalendarEvents':
        names = {d.target_field for d in plan.definitions if d.target_doctype == 'Event'}
        if not {'subject', 'starts_on', 'all_day'} <= names:
            raise ValueError('Event intake requires subject, start and all-day bindings.')
    return options


def _transform(spec, value, options):
    opts = options.get('transform_options', {}).get(spec.definition_id, {})
    if value is MISSING:
        return MISSING
    if spec.transformation in {'Number', 'Integer'}:
        return _number(value) if spec.transformation == 'Number' else _integer(value)
    if spec.transformation in {'Value Map', 'Link Map'}:
        key = str(value) if value is not None else 'null'
        if opts.get('casefold'): key = key.casefold()
        table = {k.casefold() if opts.get('casefold') else k: v for k, v in opts.get('map', {}).items()}
        if key in table: return table[key]
        if 'default' in opts: return opts['default']
        raise ValueError('The selected source value has no explicit mapping.')
    if spec.transformation in {'Local Date', 'Local Datetime'}:
        if value is None: return None
        if not isinstance(value, str): raise ValueError('Mapped date requires an ISO datetime.')
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        zone = ZoneInfo(opts['timezone'])
        if parsed.tzinfo is None:
            first, second = parsed.replace(tzinfo=zone, fold=0), parsed.replace(tzinfo=zone, fold=1)
            if (first.utcoffset() != second.utcoffset()
                    or first.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) != parsed):
                raise ValueError('Mapped local time is ambiguous or nonexistent.')
            parsed = first
        parsed = parsed.astimezone(zone)
        return parsed.date().isoformat() if spec.transformation == 'Local Date' else str(parsed.replace(tzinfo=None))
    if spec.transformation == 'Safe HTML':
        if value is None: return None
        from frappe.utils.html_utils import sanitize_html
        return sanitize_html(str(value))
    if spec.transformation == 'Table Rows':
        if value is None: return None
        if not isinstance(value, list) or len(value) > 1000 or not all(isinstance(row, dict) for row in value):
            raise ValueError('Mapped collection is invalid or too large.')
        result = []
        for row in value:
            mapped = {}
            for source, target in opts['child_fields'].items():
                raw = lookup(row, source)
                if raw is MISSING: continue
                converted = _child_transform(raw, opts.get('child_transforms', {}).get(target, 'Identity'))
                mapped[target] = _canonical(converted, opts['child_types'][target])
            result.append(mapped)
        return result
    return transformed_value(spec, value)


def _validate_native_spec(spec, adapter_options):
    """Target rules of the native adapters (Customers, Projects, Tasks, Events)."""
    owner = ROOTS[spec.operation][1]
    allowed = {owner, 'HERO Integration Link'}
    if owner == 'Customer': allowed |= {'Address', 'Contact'}
    if owner == 'Project': allowed |= {'Comment'}
    if owner == 'Task': allowed |= {'ToDo'}
    if spec.target_doctype not in allowed:
        raise ValueError('Query rule crosses an unsupported native relationship.')
    if (spec.target_field not in FIELDS[spec.target_doctype]
            and (spec.target_doctype not in {owner} or not spec.target_field.startswith('custom_hero_'))):
        raise ValueError('Query target is protected or outside the supported adapter fields.')
    if spec.target_doctype == 'HERO Integration Link' and spec.purpose != 'Source Identity':
        raise ValueError('Only identity may target the integration link.')
    if spec.purpose == 'Source Identity' and (spec.target_doctype, spec.target_field) not in {
            ('HERO Integration Link', 'source_id'), (owner, 'custom_hero_source_id')}:
        raise ValueError('Source identity may only bind its technical identity field.')
    record_name = {'Customer': 'customer_name', 'Project': 'project_name', 'Task': 'subject', 'Event': 'subject'}[owner]
    if spec.purpose == 'Record Name' and (spec.target_doctype, spec.target_field) != (owner, record_name):
        raise ValueError('Record Name must bind the native record title.')
    if (owner == 'Project' and spec.target_field == 'project_name' and spec.purpose != 'Record Name'):
        raise ValueError('Project title must retain its coded title builder.')
    if spec.target_doctype == 'Event' and spec.target_field in {'starts_on', 'ends_on', 'all_day'} and spec.transformation != 'Native Adapter':
        raise ValueError('Event time fields require the native timezone and recurrence adapter.')

    if spec.target_doctype == 'Contact' and spec.purpose != 'Customer Primary Contact':
        raise ValueError('Contact fields require the exclusive primary-contact adapter.')
    if spec.target_doctype == 'Address' and (spec.relation != 'Customer Primary Address' or not spec.source_path.startswith('address.')):
        raise ValueError('Address fields require the direct Customer.address primary relationship.')
    if spec.target_doctype == 'Comment':
        options = validate_query_adapter(adapter_options or {'version': 2})
        collection = options.get('logbook')
        if not collection or not spec.source_path.startswith(collection['path'] + '.'):
            raise ValueError('Comment fields require the configured logbook collection.')
        if spec.target_field == 'creation' and spec.transformation != 'Local Datetime':
            raise ValueError('Logbook dates require an explicit timezone conversion.')
    if spec.target_field == 'company' and (spec.purpose != 'Confirmed Company' or spec.transformation != 'Confirmed ERPNext Company'):
        raise ValueError('Company requires the independently confirmed company resolver.')
    if spec.purpose == 'Confirmed Company' and (spec.target_doctype, spec.target_field, spec.transformation) != ('Project', 'company', 'Confirmed ERPNext Company'):
        raise ValueError('Confirmed Company must bind the native Project company through its confirmed resolver.')
    if spec.purpose == 'Project Relation' and spec.transformation != 'HERO Project Link':
        raise ValueError('Project Relation requires the native Project identity resolver.')
    if spec.purpose == 'Customer Primary Contact' and (spec.target_doctype not in {'Customer', 'Contact'} or spec.target_field not in FIELDS['Contact']):
        raise ValueError('Primary Contact purpose must bind a supported primary Contact field.')
    if spec.transformation == 'HERO Customer Link' and (spec.target_doctype, spec.target_field) != ('Project', 'customer'):
        raise ValueError('Customer identity resolution may only bind the Project customer.')
    if spec.purpose == 'Customer Relation' and (spec.target_doctype, spec.target_field, spec.transformation) != ('Project', 'customer', 'HERO Customer Link'):
        raise ValueError('Project customer requires the native Customer identity resolver.')
    if spec.transformation == 'HERO Project Link' and not (
            (spec.target_doctype, spec.target_field) == ('Task', 'project')
            or (spec.target_doctype == 'Event' and spec.target_field.startswith('custom_hero_'))):
        raise ValueError('Project identity resolution must bind an approved native Project link.')
    if spec.transformation == 'Native Adapter' and spec.target_doctype != 'Event':
        raise ValueError('Native Adapter is reserved for mapped Event value groups.')
    if spec.transformation == 'Salutation Table' and (spec.target_doctype not in {'Customer', 'Contact'} or spec.target_field != 'salutation'):
        raise ValueError('Salutation conversion requires a primary Contact salutation binding.')
    if spec.transformation == 'Customer Type From Company Name' and (spec.target_doctype, spec.target_field) != ('Customer', 'customer_type'):
        raise ValueError('Customer classification may only bind the native Customer type.')
    if spec.target_doctype == 'ToDo' and spec.transformation not in {'Value Map', 'Link Map'}:
        raise ValueError('Assignment requires an explicit source-user mapping.')


def seal_query(service, snapshot, config, plan):
    """Prove a saved query selects its mapping and bind its exact approved document."""
    from graphql import (build_client_schema, get_named_type, parse, validate,
                         FieldNode, OperationDefinitionNode, VariableNode, IntValueNode)
    from hero_integration.services.graphql_graph import normalize_schema
    options = validate_plan(plan)
    if options.get('read_service') and getattr(service, 'name', None) != options['read_service']:
        raise ValueError('Mapped query differs from its explicitly selected read service.')
    from hero_integration.services.mapping_contract_v1 import SnapshotAdapter, StoredSnapshot
    SnapshotAdapter().adapt(StoredSnapshot(snapshot.name, snapshot.snapshot_json, snapshot.schema_fingerprint, snapshot.source_instance, snapshot.analysis_status))
    raw = json.loads(snapshot.snapshot_json) if isinstance(snapshot.snapshot_json, str) else snapshot.snapshot_json
    schema = build_client_schema({'__schema': normalize_schema(raw)})
    document = parse(service.query_document)
    errors = validate(schema, document)
    if errors: raise ValueError('Mapped query does not validate against its saved schema.')
    definitions = document.definitions
    if len(definitions) != 1 or not isinstance(definitions[0], OperationDefinitionNode) or definitions[0].operation.value != 'query':
        raise ValueError('Mapped read must contain one Query and no fragments.')
    fields = definitions[0].selection_set.selections
    if len(fields) != 1 or not isinstance(fields[0], FieldNode):
        raise ValueError('Mapped read must contain exactly one collection root.')
    root = fields[0]
    if root.directives or definitions[0].directives:
        raise ValueError('Mapped queries cannot conditionally omit their root.')
    if definitions[0].name and getattr(service, 'operation_name', definitions[0].name.value) not in {definitions[0].name.value, root.name.value}:
        raise ValueError('Mapped service operation does not match its saved Query.')
    operation = plan.target_operation or plan.operation
    expected, _target = ROOTS[operation]
    if root.name.value != expected or config.get('records_path') != (root.alias.value if root.alias else expected):
        raise ValueError('Mapped read root differs from its native adapter.')
    variables = config.get('variables', {})
    args = {arg.name.value: arg.value for arg in root.arguments}
    for name, marker in (('first', '$page_size'), ('offset', '$offset')):
        node = args.get(name)
        if not isinstance(node, VariableNode) or variables.get(node.name.value) != marker:
            raise ValueError('Mapped reads require explicit bounded first/offset variables.')
    if type(config.get('page_size', 100)) is not int or not 1 <= config.get('page_size', 100) <= 1000:
        raise ValueError('Mapped query page size is invalid.')
    from graphql.execution.values import get_variable_values
    supplied = {key: (config.get('page_size', 100) if value == '$page_size' else 0 if value == '$offset' else value) for key, value in variables.items()}
    coerced = get_variable_values(schema, definitions[0].variable_definitions or (), supplied)
    if isinstance(coerced, list): raise ValueError('Mapped query variables are incompatible with their schema.')
    selected, list_paths, nodes, leaf_types = set(), set(), {}, {}
    def walk(selection_set, current, prefix=''):
        from graphql import GraphQLList, GraphQLNonNull
        for field in selection_set.selections:
            if not isinstance(field, FieldNode) or field.alias or field.directives:
                raise ValueError('Mapped child selections cannot contain fragments, aliases or directives.')
            path = prefix + field.name.value
            definition = current.fields.get(field.name.value)
            if definition is None: raise ValueError('Mapped query field is absent from its schema.')
            nodes[path] = field
            kind = definition.type
            while isinstance(kind, GraphQLNonNull): kind = kind.of_type
            if isinstance(kind, GraphQLList): list_paths.add(path)
            if field.selection_set: walk(field.selection_set, get_named_type(definition.type), path + '.')
            else:
                selected.add(path)
                leaf_types[path] = get_named_type(definition.type).name
    walk(root.selection_set, get_named_type(schema.query_type.fields[expected].type))
    required = {d.source_path for d in plan.definitions if d.purpose != 'Confirmed Company'}
    for d in plan.definitions:
        if d.target_doctype == 'Address':
            required.add(d.source_path.split('.', 1)[0] + '.id')
        if d.transformation == 'Table Rows':
            required.discard(d.source_path)
            required |= {d.source_path + '.' + child for child in options.get('transform_options', {}).get(d.definition_id, {}).get('child_fields', {})}
    if operation == 'HeroCustomers':
        required |= {'is_deleted', 'is_contact_person'}
    if operation == 'HeroTasks':
        required.add('is_deleted')
        if any(spec.target_doctype == 'ToDo' for spec in plan.definitions):
            required.add('author_user_id')
    if operation == 'HeroCalendarEvents':
        required |= {'is_recurring', 'deleted'}
    if operation == 'HeroProjectMatches':
        required |= {'project_nr', 'project_type', 'customer.title', 'customer.full_name', 'address.full_address'}
    if operation in direct.OPERATIONS:
        required = (required - {d.source_path for d in plan.definitions
                                if d.transformation == 'HERO Default Sales Price'}) | direct.required_paths(plan)
    logbook = options.get('logbook')
    if logbook:
        path = logbook['path']
        required.add(path + '.' + logbook['identity_path'])
        required.update(logbook.get('heading_paths', []))
        node = nodes.get(path)
        first = next((a.value for a in node.arguments if a.name.value == 'first'), None) if node else None
        amount = int(first.value) if isinstance(first, IntValueNode) else variables.get(first.name.value) if isinstance(first, VariableNode) else None
        if path not in list_paths or type(amount) is not int or amount != logbook['limit']:
            raise ValueError('Logbook collection first must match its explicitly approved limit.')
    families = {'int': {'Int'}, 'string': {'String', 'ID', 'Date', 'DateTime'}, 'float': {'Float', 'Int'}, 'boolean': {'Boolean'}}
    for spec in plan.definitions:
        if spec.purpose == 'Confirmed Company': continue
        if spec.transformation == 'Table Rows':
            from dataclasses import replace
            child_options = options.get('transform_options', {}).get(spec.definition_id, {})
            for source, target in child_options.get('child_fields', {}).items():
                child_type = source_type(raw, operation, spec.source_path + '.' + source)
                child_transform = child_options.get('child_transforms', {}).get(target, 'Identity')
                child_spec = replace(spec, source_type=child_type, transformation=child_transform)
                _validate_output_compatibility(child_spec, {'target_type': child_options['child_types'][target]}, child_options['child_types'][target])
            continue
        actual = leaf_types.get(spec.source_path)
        kind = spec.source_type.split('|')[0]
        if actual and actual not in families.get(kind, set()):
            from graphql import GraphQLEnumType
            if not (kind == 'string' and isinstance(schema.get_type(actual), GraphQLEnumType)):
                raise ValueError('Mapped source type differs from its saved schema.')
    if not required <= selected:
        raise ValueError('Mapped query omits selected field or title dependencies: ' + ', '.join(sorted(required - selected)))
    identity = next(d.source_path for d in plan.definitions if d.purpose == 'Source Identity')
    if config.get('identity_path') != identity:
        raise ValueError('Query identity path differs from its selected rule.')
    return {'version': 2, 'mapping_version': plan.mapping_version, 'source_instance': snapshot.source_instance,
            'schema_fingerprint': snapshot.schema_fingerprint, 'document_sha256': sha256(service.query_document.encode()).hexdigest(),
            'variables_sha256': _hash(variables), 'adapter_sha256': _hash(options),
            'read_shape_sha256': _hash({key: config.get(key) for key in ('records_path', 'identity_path', 'page_size', 'native_options', 'native_task_onboarding')})}


def _check_value(spec, value):
    if value is MISSING:
        return
    if value is None:
        if '|null' not in spec.source_type:
            raise ValueError('Mapped required source field is null: ' + spec.source_path)
        return
    kind = spec.source_type.split('|')[0]
    valid = {'string': isinstance(value, str), 'int': type(value) is int,
             'float': type(value) in {int, float} and math.isfinite(value),
             'boolean': type(value) is bool, 'json': isinstance(value, (list, dict))}.get(kind, False)
    if not valid:
        raise ValueError('Mapped source field has an incompatible value: ' + spec.source_path)


def _reference(doctype, name):
    import frappe
    if not isinstance(name, str) or not name or not frappe.db.exists(doctype, name):
        raise ValueError('Mapped native reference does not exist.')
    document = frappe.get_doc(doctype, name)
    frappe.has_permission(doctype, 'read', doc=document, throw=True)
    return document.name


def mapped_values(plan, record, *, customer_link=None, confirmed_company=None, project_title=None,
                  project_link=None, native_value=None, allow_partial=False, target_doctype=None):
    from hero_integration.services.mapping_runtime import MappedValues
    target = target_doctype or plan.target_doctype
    values = MappedValues()
    for spec in plan.definitions:
        if spec.target_doctype != target and not (target == 'Customer' and spec.target_doctype == 'Contact'):
            continue
        if spec.purpose == 'Record Name' and plan.target_doctype == 'Project' and project_title is not None:
            values[spec.target_field] = project_title(record)
            continue
        if spec.purpose == 'Confirmed Company':
            if confirmed_company is None: raise ValueError('Confirmed company resolver is unavailable.')
            values[spec.target_field] = confirmed_company()
            continue
        raw = lookup(record, spec.source_path)
        _check_value(spec, raw)
        if raw is MISSING:
            values.missing.add(spec.target_field)
            continue
        if spec.purpose == 'Customer Relation':
            if customer_link is None: raise ValueError('Customer identity resolver is unavailable.')
            value = customer_link(raw)
        elif spec.purpose == 'Project Relation' or spec.transformation == 'HERO Project Link':
            if project_link is None: raise ValueError('Project identity resolver is unavailable.')
            value = project_link(raw) if raw is not None else None
        elif spec.transformation == 'Native Adapter':
            if native_value is None: raise ValueError('Selected native value resolver is unavailable.')
            try: value = native_value(spec.target_field)
            except KeyError:
                if allow_partial:
                    values.missing.add(spec.target_field)
                    continue
                raise ValueError('Selected native field could not be resolved.') from None
        else:
            value = transform(spec, raw, plan.adapter_options)
        opts = plan.adapter_options.get('transform_options', {}).get(spec.definition_id, {})
        expected_type = opts.get('target_type') or _inferred_target(spec)
        if expected_type:
            value = _canonical(value, expected_type)
        if value is None or value == '':
            values.empty.add(spec.target_field)
            continue
        if spec.transformation == 'Link Map':
            value = _reference(plan.adapter_options['transform_options'][spec.definition_id]['link_doctype'], value)
        values[spec.target_field] = value
    return values


def address_values(plan, record):
    """Resolve only declared Address fields; never invent a missing country."""
    selected = [d for d in plan.definitions if d.target_doctype == 'Address']
    if not selected:
        return None
    if any(not spec.source_path.startswith('address.') for spec in selected):
        raise ValueError('Primary Address values must belong to the direct Customer.address relation.')
    root = {spec.source_path.split('.', 1)[0] for spec in selected}
    if len(root) != 1:
        raise ValueError('Primary address bindings require one explicit source relation.')
    parent = lookup(record, next(iter(root)))
    if parent is MISSING or parent is None:
        return None
    if not isinstance(parent, dict) or type(parent.get('id')) is not int or parent['id'] <= 0:
        raise ValueError('Mapped primary address requires a stable source identity.')
    values = mapped_values(plan, record, target_doctype='Address')
    if not {'address_line1', 'pincode', 'city', 'country'} <= set(values):
        raise ValueError('Mapped primary address is incomplete; no country is assumed.')
    # The existing Address writer owns linking, exclusive ownership and baselines.
    return {'id': str(parent['id']), 'street': values['address_line1'], 'zipcode': values['pincode'],
            'city': values['city'], 'country_name': values['country'],
            **({'line_2': values['address_line2']} if 'address_line2' in values else {}),
            'mapped_values': dict(values)}


def seal_lookup_query(service, snapshot, config):
    """Approve a lookup service without adding any target writer."""
    from graphql import build_client_schema, get_named_type, parse, validate, OperationDefinitionNode, FieldNode
    from hero_integration.services.graphql_graph import normalize_schema
    from hero_integration.services.mapping_contract_v1 import SnapshotAdapter, StoredSnapshot
    options = validate_query_adapter(config.get('query_adapter'))
    if (config.get('target_operation') != 'HeroLookup' or config.get('mapping_definition_ids')
            or config.get('field_rules') or config.get('additional_mappings')):
        raise ValueError('Lookup mappings cannot authorize a native document writer.')
    if options.get('read_service') and getattr(service, 'name', None) != options['read_service']:
        raise ValueError('Lookup query differs from its explicitly selected read service.')
    if options.get('role') != 'LOOKUP' or not options.get('max_records'):
        raise ValueError('Lookup query requires an explicit read-only role and record bound.')
    SnapshotAdapter().adapt(StoredSnapshot(snapshot.name, snapshot.snapshot_json, snapshot.schema_fingerprint,
        snapshot.source_instance, snapshot.analysis_status))
    raw = json.loads(snapshot.snapshot_json) if isinstance(snapshot.snapshot_json, str) else snapshot.snapshot_json
    schema = build_client_schema({'__schema': normalize_schema(raw)})
    document = parse(service.query_document)
    if validate(schema, document): raise ValueError('Lookup query does not validate against its saved schema.')
    if (len(document.definitions) != 1 or not isinstance(document.definitions[0], OperationDefinitionNode)
            or document.definitions[0].operation.value != 'query'):
        raise ValueError('Lookup service must contain exactly one read-only Query.')
    if not PATH.fullmatch(config.get('records_path', '')) or not PATH.fullmatch(config.get('identity_path', '')):
        raise ValueError('Lookup service requires explicit record and identity paths.')
    operation = document.definitions[0]
    if operation.directives or len(operation.selection_set.selections) != 1:
        raise ValueError('Lookup query must select one unconditional root.')
    selected = set()
    list_paths = set()
    def walk(selection_set, current, prefix=''):
        from graphql import GraphQLList, GraphQLNonNull
        for field in selection_set.selections:
            if not isinstance(field, FieldNode) or field.directives:
                raise ValueError('Lookup selections must be unconditional explicit fields.')
            definition = current.fields.get(field.name.value)
            if definition is None: raise ValueError('Lookup selection is absent from the saved schema.')
            path = prefix + (field.alias.value if field.alias else field.name.value)
            kind = definition.type
            while isinstance(kind, GraphQLNonNull): kind = kind.of_type
            if isinstance(kind, GraphQLList): list_paths.add(path)
            if field.selection_set: walk(field.selection_set, get_named_type(definition.type), path + '.')
            else: selected.add(path)
    walk(operation.selection_set, schema.query_type)
    records_path = config['records_path']
    if records_path not in list_paths or records_path + '.' + config['identity_path'] not in selected:
        raise ValueError('Lookup records and identity must be selected from an explicit collection.')
    from graphql.execution.values import get_variable_values
    supplied = {key: (config.get('page_size', 100) if value == '$page_size' else 0 if value == '$offset' else value)
                for key, value in config.get('variables', {}).items()}
    if isinstance(get_variable_values(schema, operation.variable_definitions or (), supplied), list):
        raise ValueError('Lookup query variables are incompatible with its saved schema.')
    return {'version': 2, 'role': 'LOOKUP', 'source_instance': snapshot.source_instance,
            'schema_fingerprint': snapshot.schema_fingerprint, 'document_sha256': sha256(service.query_document.encode()).hexdigest(),
            'variables_sha256': _hash(config.get('variables', {})), 'adapter_sha256': _hash(options),
            'read_shape_sha256': _hash({key: config.get(key) for key in ('records_path', 'identity_path', 'page_size')})}


def event_record(plan, record):
    """Provide the time adapter's semantic inputs from selected field bindings."""
    names = {'subject': 'title', 'description': 'description', 'location': 'full_address',
             'starts_on': 'start', 'ends_on': 'end', 'all_day': 'all_day',
             'custom_hero_resources': 'resources', 'custom_hero_partners': 'partners'}
    result = {'is_recurring': lookup(record, 'is_recurring')}
    for spec in plan.definitions:
        if spec.target_doctype != 'Event' or spec.target_field not in names:
            continue
        value = lookup(record, spec.source_path)
        if value is not MISSING: result[names[spec.target_field]] = value
    return result


def source_type(schema, operation_name, path):
    """Derive a mapping type from the stored schema, including nullable parents."""
    from hero_integration.services.graphql_graph import normalize_schema
    raw = json.loads(schema) if isinstance(schema, str) else schema
    normalized = normalize_schema(raw)
    types = {item['name']: item for item in normalized['types']}
    current = types[(normalized.get('queryType') or {})['name']]
    root_name = ROOTS.get(operation_name, (operation_name, None))[0]
    segments = [root_name, *path.split('.')]
    nullable = False
    named = None
    for index, segment in enumerate(segments):
        field = next((item for item in current.get('fields', []) if item.get('name') == segment), None)
        if field is None: raise ValueError('Selected source path is absent from the stored Query schema.')
        ref = field['type']
        if index:
            nullable |= ref.get('kind') != 'NON_NULL'
        has_list = False
        while ref.get('kind') in {'LIST', 'NON_NULL'}:
            has_list |= ref.get('kind') == 'LIST'
            ref = ref['ofType']
        named = ref
        if index == len(segments) - 1 and has_list:
            return 'json' + ('|null' if nullable else '')
        current = types.get(ref.get('name'), {})
    kind = {'Int': 'int', 'Float': 'float', 'Boolean': 'boolean'}.get(named.get('name'))
    if kind is None and named.get('kind') in {'SCALAR', 'ENUM'}: kind = 'string'
    if kind is None: raise ValueError('Selected source path must identify a scalar or an explicit collection.')
    return kind + ('|null' if nullable else '')


def _number(value):
    if value is None: return None
    if type(value) not in {str, int, float} or (isinstance(value, str) and not value.strip()):
        raise ValueError('Number requires an explicit finite numeric value.')
    try:
        result = float(value)
    except (ValueError, OverflowError) as error:
        raise ValueError('Number requires an explicit finite numeric value.') from error
    if not math.isfinite(result): raise ValueError('Number must be finite.')
    return 0.0 if result == 0 else result


def _integer(value):
    if value is None: return None
    if type(value) is int: return value
    if type(value) is float and math.isfinite(value) and value.is_integer(): return int(value)
    if type(value) is str and re.fullmatch(r'[+-]?[0-9]+', value.strip()): return int(value.strip())
    raise ValueError('Integer requires a whole numeric value without truncation.')


def _canonical(value, target_type):
    if value is None or value is MISSING: return value
    if target_type in TEXT_TYPES:
        if not isinstance(value, str): raise ValueError('Mapped text target requires a string; choose an explicit text transform.')
        return value
    if target_type in FLOAT_TYPES:
        if type(value) not in {int, float}: raise ValueError('Mapped numeric target requires a number; choose Number explicitly.')
        return _number(value)
    if target_type in INT_TYPES:
        if type(value) is not int: raise ValueError('Mapped integer target requires an integer; choose Integer explicitly.')
        return value
    if target_type == 'Check':
        if type(value) is bool or (type(value) is int and value in {0, 1}): return int(value)
        raise ValueError('Mapped Check target requires a boolean or zero/one.')
    if target_type == 'Date':
        if not isinstance(value, str): raise ValueError('Mapped Date target requires an ISO date.')
        try: return date.fromisoformat(value).isoformat()
        except ValueError as error: raise ValueError('Mapped Date target requires an ISO date.') from error
    if target_type == 'Datetime':
        if not isinstance(value, str): raise ValueError('Mapped Datetime target requires an ISO datetime.')
        try: parsed = datetime.fromisoformat(value)
        except ValueError as error: raise ValueError('Mapped Datetime target requires an ISO datetime.') from error
        if parsed.tzinfo is not None: raise ValueError('Timezone-aware source instants require Local Datetime conversion.')
        return str(parsed)
    if target_type == 'Time':
        if not isinstance(value, str): raise ValueError('Mapped Time target requires an ISO time.')
        try: parsed = time.fromisoformat(value)
        except ValueError as error: raise ValueError('Mapped Time target requires an ISO time.') from error
        if parsed.tzinfo is not None: raise ValueError('Mapped Time cannot discard a timezone offset.')
        return parsed.isoformat()
    if target_type == 'Table':
        if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
            raise ValueError('Mapped Table target requires explicitly typed rows.')
        return value
    raise ValueError('Mapped target type is unsupported.')


def _inferred_target(spec):
    if spec.transformation == 'Local Date': return 'Date'
    if spec.transformation == 'Local Datetime': return 'Datetime'
    if spec.transformation == 'Number': return 'Float'
    if spec.transformation == 'Integer': return 'Int'
    if spec.transformation == 'Table Rows': return 'Table'
    if spec.transformation == 'Native Adapter':
        return {'starts_on': 'Datetime', 'ends_on': 'Datetime', 'all_day': 'Check',
                'subject': 'Small Text', 'description': 'Text Editor', 'location': 'Data',
                'custom_hero_original_start': 'Data', 'custom_hero_original_end': 'Data',
                'custom_hero_resources': 'Table', 'custom_hero_partners': 'Table'}.get(spec.target_field)
    return None


def transform(spec, value, options):
    """Apply declared output defaults only to explicit null, never to missing data."""
    opts = options.get('transform_options', {}).get(spec.definition_id, {})
    _validate_option_keys(spec, opts)
    if value is MISSING:
        return MISSING
    if value is None and 'default' in opts and spec.transformation not in {'Value Map', 'Link Map'}:
        result = opts['default']
        if spec.transformation == 'Safe HTML' and result is not None:
            if not isinstance(result, str): raise ValueError('Safe HTML default must be text.')
            from frappe.utils.html_utils import sanitize_html
            result = sanitize_html(result)
    else:
        result = _transform(spec, value, options)
    target_type = opts.get('target_type') or _inferred_target(spec)
    return _canonical(result, target_type) if target_type else result


def _child_transform(value, name):
    if value is None: return None
    if name == 'Number': return _number(value)
    if name == 'Integer': return _integer(value)
    if name == 'Scalar Text':
        if type(value) not in {int, float, bool} or (type(value) is float and not math.isfinite(value)):
            raise ValueError('Child Scalar Text requires a finite scalar number or boolean.')
        return json.dumps(value, allow_nan=False)
    if name == 'Trim':
        if not isinstance(value, str): raise ValueError('Child Trim requires a string.')
        return value.strip()
    if name != 'Identity': raise ValueError('Table child transformation is unsupported.')
    return value


def _output_family(source_type, transformation):
    kind = source_type.split('|')[0]
    if transformation in {'Identity', 'Native Adapter'}: return kind
    if transformation in {'Trim', 'Nonempty String ID', 'Salutation Table', 'Customer Type From Company Name'}:
        if kind != 'string': raise ValueError('Selected text transformation requires a string source.')
        return 'string'
    if transformation == 'Positive Int To Text':
        if kind != 'int': raise ValueError('Positive Int To Text requires an integer source.')
        return 'string'
    if transformation == 'Scalar Text':
        if kind not in {'int', 'float', 'boolean'}: raise ValueError('Scalar Text requires a numeric or boolean source.')
        return 'string'
    if transformation == 'JSON Value': return 'string'
    if transformation == 'Safe HTML':
        if kind != 'string': raise ValueError('Safe HTML requires a string source.')
        return 'string'
    if transformation == 'Number':
        if kind not in {'string', 'int', 'float'}: raise ValueError('Number source is not numeric or numeric text.')
        return 'float'
    if transformation == 'Integer':
        if kind not in {'string', 'int', 'float'}: raise ValueError('Integer source is not numeric or numeric text.')
        return 'int'
    if transformation in {'Local Date', 'Local Datetime'}:
        if kind != 'string': raise ValueError('Date conversion requires a string source.')
        return 'string'
    if transformation in {'Link Map', 'HERO Customer Link', 'HERO Project Link', 'Confirmed ERPNext Company'}: return 'string'
    if transformation == 'Table Rows':
        if kind != 'json': raise ValueError('Table Rows requires a collection source.')
        return 'json'
    if transformation == 'Value Map': return 'configured'
    raise ValueError('Query transformation is unsupported.')


def _validate_output_compatibility(spec, options, fieldtype):
    """Reject incompatible mappings before Frappe can coerce source values."""
    if spec.transformation in direct.TRANSFORMS:
        kind = spec.source_type.split('|')[0]
        allowed = {'HERO Price': {'int', 'float'}, 'HERO Default Sales Price': {'json'}}.get(spec.transformation, {'string'})
        if kind not in allowed:
            raise ValueError(spec.transformation + ' requires a ' + '/'.join(sorted(allowed)) + ' source.')
        if options:
            raise ValueError('Coded HERO resolvers take no transform options.')
        return
    if fieldtype not in TARGET_TYPES:
        raise ValueError('Mapped native field type is unsupported.')
    declared = options.get('target_type')
    if declared and declared != fieldtype:
        raise ValueError('Configured mapped target type differs from native metadata.')
    inferred = _inferred_target(spec)
    if 'default' in options:
        _canonical(options['default'], fieldtype)
    if fieldtype in FLOAT_TYPES and declared is None:
        raise ValueError('Numeric targets require an explicit metadata-bound target_type.')
    if fieldtype in {'Date', 'Datetime', 'Time', 'Int', 'Long Int', 'Check'} and declared is None:
        compatible_inference = inferred == fieldtype or (fieldtype in INT_TYPES and inferred in INT_TYPES)
        scalar_identity = ((fieldtype in INT_TYPES and spec.source_type.split('|')[0] == 'int' and spec.transformation == 'Identity')
                           or (fieldtype == 'Check' and spec.source_type.split('|')[0] == 'boolean' and spec.transformation == 'Identity'))
        if not compatible_inference and not scalar_identity:
            raise ValueError('Typed native targets require an explicit target_type or a matching typed transform.')
    if spec.transformation in {'Value Map', 'Link Map'}:
        for value in list(options.get('map', {}).values()) + ([options['default']] if 'default' in options else []):
            _canonical(value, fieldtype)
        return
    if spec.transformation == 'Native Adapter':
        if inferred != fieldtype and not (inferred in TEXT_TYPES and fieldtype in TEXT_TYPES - {'Link', 'Select'}):
            raise ValueError('Native adapter output type differs from the native target.')
        return
    family = _output_family(spec.source_type, spec.transformation)
    if ((fieldtype in TEXT_TYPES or fieldtype in {'Date', 'Datetime', 'Time'}) and family != 'string'
            or fieldtype in FLOAT_TYPES and family not in {'int', 'float'}
            or fieldtype in INT_TYPES and family != 'int'
            or fieldtype == 'Check' and family not in {'boolean', 'int'}
            or fieldtype == 'Table' and family != 'json'):
        raise ValueError('Mapped source transformation is incompatible with the native field type.')


def _validate_option_keys(spec, options):
    allowed = {'target_type'}
    if spec.transformation in {'Value Map', 'Link Map'}:
        allowed |= {'map', 'default', 'casefold'}
        if spec.transformation == 'Link Map': allowed.add('link_doctype')
    elif spec.transformation in {'Local Date', 'Local Datetime'}:
        allowed |= {'timezone', 'default'}
    elif spec.transformation == 'Table Rows':
        allowed |= {'child_fields', 'child_types', 'child_transforms'}
    elif spec.transformation not in {'Confirmed ERPNext Company', 'HERO Customer Link', 'HERO Project Link', 'Native Adapter'}:
        allowed.add('default')
    if spec.purpose in {'Source Identity', 'Confirmed Company', 'Customer Relation', 'Project Relation'}:
        allowed.discard('default')
    if spec.operation == 'HeroProjectMatches' and spec.purpose == 'Record Name':
        allowed = {'target_type'}
    if set(options) - allowed:
        raise ValueError('Query transform options do not apply to the selected transformation or technical role.')
