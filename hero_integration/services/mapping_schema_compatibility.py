"""Pure dependency compatibility of saved mappings against two HERO schemas.

No snapshot identity, approval, schema fingerprint or mapping is modified.
Documents must validate on both schemas. Only used fields, input members,
arguments, defaults and types are compared; unrelated schema extensions do not
invalidate an existing mapping contract.
"""
from hashlib import sha256
import json

from graphql import (GraphQLError, TypeInfo, TypeInfoVisitor, Undefined, Visitor, ast_from_value, build_client_schema,
                     get_named_type, is_enum_type, is_input_object_type, is_non_null_type, is_union_type,
                     parse, print_ast, type_from_ast, validate, visit)
from graphql.language.ast import OperationDefinitionNode, ObjectValueNode, ListValueNode

from hero_integration.services.graphql_graph import normalize_schema
from hero_integration.services.localized_error import LocalizedError


class MappingSchemaCompatibilityError(LocalizedError):
    def __init__(self, paths):
        self.changed_paths = tuple(sorted(set(paths)))
        super().__init__('Used mapping schema dependencies changed: {0}', ', '.join(self.changed_paths))


def _fail(*paths):
    raise MappingSchemaCompatibilityError(paths)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _schema(raw, label):
    try:
        return build_client_schema({'__schema': normalize_schema(raw)})
    except (ValueError, TypeError, KeyError) as error:
        raise MappingSchemaCompatibilityError([label + ': invalid schema']) from error


def _type(kind):
    named = get_named_type(kind)
    result = {'type': str(kind), 'kind': type(named).__name__}
    if is_enum_type(named): result['values'] = sorted(named.values)
    if is_union_type(named): result['members'] = sorted(item.name for item in named.types)
    if is_input_object_type(named): result['one_of'] = bool(getattr(named, 'is_one_of', False))
    return result


def _default(field):
    if field.default_value is Undefined: return {'defined': False}
    node = ast_from_value(field.default_value, field.type)
    class CanonicalObjects(Visitor):
        def leave_object_value(self, value, *_):
            return ObjectValueNode(fields=tuple(sorted(value.fields, key=lambda item: item.name.value)))
    if node is not None: node = visit(node, CanonicalObjects())
    return {'defined': True, 'value': print_ast(node) if node is not None else None}


def _member(field):
    return {**_type(field.type), 'default': _default(field)}


def _opaque_input(kind, result, seen):
    """An opaque variable can use any member; no absent variable values are guessed."""
    named = get_named_type(kind)
    if not is_input_object_type(named) or named.name in seen: return
    seen.add(named.name)
    result['input-object:' + named.name] = sorted(named.fields)
    for name, field in named.fields.items():
        result['input:' + named.name + '.' + name] = _member(field)
        _opaque_input(field.type, result, seen)


def _literal_input(kind, node, result):
    named = get_named_type(kind)
    if isinstance(node, ListValueNode):
        for value in node.values: _literal_input(kind, value, result)
    elif isinstance(node, ObjectValueNode) and is_input_object_type(named):
        supplied = {value.name.value for value in node.fields}
        for value in node.fields:
            field = named.fields[value.name.value]
            result['input:' + named.name + '.' + value.name.value] = _member(field)
            _literal_input(field.type, value.value, result)
        for name, field in named.fields.items():
            if name not in supplied:
                result['input:' + named.name + '(omitted-default:' + name + ')'] = _omitted_default(field)


def _omitted_default(field):
    """Retain absence too: adding a default to an existing member changes behavior."""
    if field.default_value is Undefined: return {'defined': False}
    members = {}
    _literal_input(field.type, ast_from_value(field.default_value, field.type), members)
    members = {key: value for key, value in members.items()
               if not ('(omitted-default:' in key and value == {'defined': False})}
    return {'defined': True, 'member': _member(field), 'members': members}


def _document_dependencies(schema, document, kind, profiled_operations):
    try:
        tree = parse(document)
        operations = [node for node in tree.definitions if isinstance(node, OperationDefinitionNode)]
        errors = validate(schema, tree)
        if not operations or any(node.operation.value != kind for node in operations):
            _fail(kind + ': wrong operation direction')
        if errors:
            _fail(*[kind + ': ' + error.message for error in errors])
    except (ValueError, TypeError, GraphQLError) as error:
        if isinstance(error, MappingSchemaCompatibilityError): raise
        raise MappingSchemaCompatibilityError([kind + ': invalid GraphQL document']) from error
    result, info = {}, TypeInfo(schema)

    class Dependencies(Visitor):
        def enter_field(self, node, *_):
            field, owner = info.get_field_def(), info.get_parent_type()
            if field is None or owner is None: _fail(kind + ': missing selected field ' + node.name.value)
            key = 'field:' + owner.name + '.' + node.name.value
            result[key] = _type(field.type)
            for argument in node.arguments:
                selected = field.args[argument.name.value]
                result[key + '(' + argument.name.value + ')'] = _member(selected)
                _literal_input(selected.type, argument.value, result)
            supplied = {argument.name.value for argument in node.arguments}
            for name, argument in field.args.items():
                if name not in supplied:
                    result[key + '(omitted-default:' + name + ')'] = _omitted_default(argument)

        def enter_directive(self, node, *_):
            directive = schema.get_directive(node.name.value)
            result['directive:' + node.name.value] = {'repeatable': directive.is_repeatable}
            for argument in node.arguments:
                selected = directive.args[argument.name.value]
                result['directive:' + node.name.value + '(' + argument.name.value + ')'] = _member(selected)
                _literal_input(selected.type, argument.value, result)
            supplied = {argument.name.value for argument in node.arguments}
            for name, argument in directive.args.items():
                if name not in supplied:
                    result['directive:' + node.name.value + '(omitted-default:' + name + ')'] = _omitted_default(argument)

    visit(tree, TypeInfoVisitor(info, Dependencies()))
    for operation in operations:
        roots = {node.name.value for node in operation.selection_set.selections if hasattr(node, 'name')}
        profiled = kind == 'mutation' and roots and roots <= profiled_operations
        for definition in operation.variable_definitions:
            selected = type_from_ast(schema, definition.type)
            result['variable:' + (operation.name.value if operation.name else '') + ':' + definition.variable.name.value] = _type(selected)
            if not profiled: _opaque_input(selected, result, set())
    return result


def _profiles(profiles):
    result = []
    for item in profiles:
        content = item.get('content', item) if isinstance(item, dict) else None
        if not isinstance(content, dict): _fail('mutation profile: invalid configuration')
        usage = content.get('usage')
        operation = (content.get('target') or {}).get('operation')
        if not isinstance(operation, str) or not operation: _fail('mutation profile: missing operation')
        try:
            if usage == 'MAPPED_UPDATE_V2':
                paths = [content['identity']['input_path']]
                paths += [row['input_path'] for row in content['assignments'] + content['required_readback']]
            elif usage == 'PROJECT_LOGBOOK_V1' and operation == 'add_logbook_entry':
                paths = ['logbook_entry.target', 'logbook_entry.target_id', 'logbook_entry.custom_text']
                paths += [row['input_path'] for row in content['assignments']]
            else:
                _fail('mutation profile: unsupported usage')
        except (KeyError, TypeError) as error:
            raise MappingSchemaCompatibilityError(['mutation profile: missing configured input paths']) from error
        if not paths or any(not isinstance(path, str) or not path or any(not part.isidentifier() for part in path.split('.')) for path in paths):
            _fail('mutation profile: invalid input path')
        result.append((operation, sorted(set(paths))))
    return result


def _profile_dependencies(schema, operation, paths):
    root = schema.mutation_type
    field = root.fields.get(operation) if root else None
    if field is None: _fail('mutation:' + operation)
    result, objects = {'field:' + root.name + '.' + operation: _type(field.type)}, {}
    # Both supported runtimes verify the returned record identity even when
    # callers supplied only a profile and no saved mutation query document.
    response = get_named_type(field.type)
    identity = getattr(response, 'fields', {}).get('id')
    if identity is None: _fail('mutation:' + operation + '.response.id')
    result['field:' + response.name + '.id'] = _type(identity.type)
    for path in paths:
        parts = path.split('.')
        argument = field.args.get(parts[0])
        if argument is None: _fail('mutation:' + operation + '.' + path)
        result['argument:' + operation + '.' + parts[0]] = _member(argument)
        selected = argument.type
        prefix = parts[0]
        for name in parts[1:]:
            owner = get_named_type(selected)
            if not is_input_object_type(owner) or name not in owner.fields:
                _fail('mutation:' + operation + '.' + path)
            objects[prefix] = owner
            child = owner.fields[name]
            result['input:' + owner.name + '.' + name] = _member(child)
            selected, prefix = child.type, prefix + '.' + name
    supplied_args = {path.split('.')[0] for path in paths}
    for name, argument in field.args.items():
        if name not in supplied_args:
            result['argument:' + operation + '(omitted-default:' + name + ')'] = _omitted_default(argument)
        if is_non_null_type(argument.type) and argument.default_value is Undefined and name not in supplied_args:
            _fail('mutation:' + operation + '.' + name + ' (required input is not supplied)')
    for prefix, owner in objects.items():
        supplied = {path[len(prefix)+1:].split('.')[0] for path in paths if path.startswith(prefix + '.')}
        for name, child in owner.fields.items():
            if name not in supplied:
                result['input:' + owner.name + '(omitted-default:' + name + ')'] = _omitted_default(child)
            if is_non_null_type(child.type) and child.default_value is Undefined and name not in supplied:
                _fail('mutation:' + operation + '.' + prefix + '.' + name + ' (required input is not supplied)')
    return result


def check_compatibility(old_raw, new_raw, *, read_queries, mutation_profiles, mutation_queries):
    """Return a stable used-dependency proof, or raise with changed paths.

    The proof does not authorize execution or change either snapshot binding.
    Callers must separately validate profile semantics, source and permissions.
    """
    if any(not isinstance(value, list) for value in (read_queries, mutation_profiles, mutation_queries)):
        _fail('mapping dependencies: expected lists')
    if len(read_queries) + len(mutation_queries) > 200 or len(mutation_profiles) > 100:
        _fail('mapping dependencies: too many documents or profiles')
    schemas = [_schema(old_raw, 'old'), _schema(new_raw, 'new')]
    profiles = _profiles(mutation_profiles)
    operations = {operation for operation, _ in profiles}
    proofs = []
    for schema in schemas:
        dependencies = {}
        for kind, documents in (('query', read_queries), ('mutation', mutation_queries)):
            for index, document in enumerate(documents):
                if not isinstance(document, str) or not document.strip() or len(document) > 500000:
                    _fail(kind + ': invalid document')
                for key, value in _document_dependencies(schema, document, kind, operations).items():
                    dependencies[kind + ':' + str(index) + ':' + key] = value
        for index, (operation, paths) in enumerate(profiles):
            for key, value in _profile_dependencies(schema, operation, paths).items():
                dependencies['profile:' + str(index) + ':' + key] = value
        proofs.append(dependencies)
    before, after = proofs
    # Brand-new optional/defaulted members are allowed. Existing omitted
    # members retain their previous default semantics, including no default.
    for key in set(after) - set(before):
        if '(omitted-default:' in key: after.pop(key)
    for key in set(before) - set(after):
        if '(omitted-default:' in key and before[key] == {'defined': False}: before.pop(key)
    changed = [key for key in before.keys() | after.keys() if before.get(key) != after.get(key)]
    if changed: _fail(*changed)
    before = {key: value for key, value in before.items()
              if not ('(omitted-default:' in key and value == {'defined': False})}
    return {'compatible': True, 'dependencies': dict(sorted(before.items())),
            'dependency_fingerprint': sha256(_json(before).encode()).hexdigest()}
