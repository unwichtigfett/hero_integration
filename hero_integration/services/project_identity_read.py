"""Narrow approved selections using schema-proven identity arguments."""
from copy import deepcopy

from graphql import (parse, print_ast, validate, visit, Visitor, get_named_type, is_list_type,
                     is_non_null_type, build_client_schema)
from graphql.language.ast import ArgumentNode, NameNode, IntValueNode, OperationDefinitionNode, FieldNode


class ProjectIdentityReader:
    def __init__(self, client, raw_schema, hero_id):
        from hero_integration.services.graphql_graph import normalize_schema
        self.client, self.schema, self.hero_id = client, build_client_schema({'__schema': normalize_schema(raw_schema)}), str(hero_id)
        if not self.hero_id.isdigit() or int(self.hero_id) <= 0:
            raise ValueError('The bound external identity is invalid.')

    def request(self, document, variables):
        from hero_integration.services.mapped_update_v2 import MappedUpdateError
        tree = parse(document)
        operations = [node for node in tree.definitions if isinstance(node, OperationDefinitionNode)]
        root = self.schema.query_type
        many, one = root.fields.get('project_matches'), root.fields.get('project_match')
        if (len(operations) != 1 or operations[0].operation.value != 'query'
                or len(operations[0].selection_set.selections) != 1 or not many or not one
                or get_named_type(many.type) != get_named_type(one.type)
                or 'project_match_id' not in one.args or str(one.args['project_match_id'].type).rstrip('!') != 'Int'):
            raise MappedUpdateError('The approved Project reader cannot be narrowed to its bound identity.')
        kind = one.type.of_type if is_non_null_type(one.type) else one.type
        field = operations[0].selection_set.selections[0]
        if (is_list_type(kind) or not isinstance(field, FieldNode) or field.name.value != 'project_matches' or field.alias or field.directives
                or any(argument.name.value not in {'first', 'offset', 'orderBy'} for argument in field.arguments)):
            raise MappedUpdateError('The approved Project reader cannot be narrowed to its bound identity.')
        tree = deepcopy(tree); operation = next(node for node in tree.definitions if isinstance(node, OperationDefinitionNode))
        field = operation.selection_set.selections[0]
        field.name = NameNode(value='project_match')
        field.arguments = (ArgumentNode(name=NameNode(value='project_match_id'), value=IntValueNode(value=self.hero_id)),)
        used = set()
        class Variables(Visitor):
            def enter_variable_definition(self, *_): return False
            def enter_variable(self, node, *_): used.add(node.name.value)
        visit(tree, Variables())
        operation.variable_definitions = tuple(item for item in operation.variable_definitions if item.variable.name.value in used)
        if validate(self.schema, tree):
            raise MappedUpdateError('The approved Project reader cannot be narrowed to its bound identity.')
        return print_ast(tree), {key: value for key, value in variables.items() if key in used}

    def query(self, document, variables):
        from hero_integration.services.mapped_update_v2 import MappedUpdateError
        document, variables = self.request(document, variables)
        data = self.client.query(document, variables)
        if not isinstance(data, dict) or data.get('errors'):
            raise MappedUpdateError('HERO did not return this project.')
        payload = data.get('data', data)
        record = payload.get('project_match') if isinstance(payload, dict) else None
        if not isinstance(record, dict) or str(record.get('id')) != self.hero_id:
            raise MappedUpdateError('HERO did not return this project.')
        return {'project_matches': [record]}


class CollectionIdentityReader:
    """Restrict the approved selection using a verified root identity argument.

    The caller still exhausts the filtered result and rejects duplicate IDs.
    Unknown roots or existing identity filters retain the original bounded scan.
    """
    ROOTS = {
        ('HeroCustomers', 'contacts'): ('ids', 'id', 'Int'),
        ('HeroTasks', 'tasks'): ('ids', 'id', 'Int'),
        ('HeroCalendarEvents', 'calendar_events'): ('ids', 'id', 'Int'),
        ('HeroServices', 'supply_services'): ('service_ids', 'id', 'Int'),
        ('HeroProducts', 'supply_product_versions'): ('product_ids', 'product_id', 'String'),
    }

    def __init__(self, client, raw_schema, area, identity_path, hero_id, *, file_uuid=None):
        from hero_integration.services.graphql_graph import normalize_schema
        self.client = client
        self.schema = build_client_schema({'__schema': normalize_schema(raw_schema)})
        self.area, self.identity_path, self.hero_id, self.file_uuid = area, identity_path, str(hero_id), file_uuid

    def request(self, document):
        from graphql import GraphQLInputObjectType
        from graphql.language.ast import ListValueNode, StringValueNode, ObjectValueNode, ObjectFieldNode
        tree = parse(document)
        operations = [node for node in tree.definitions if isinstance(node, OperationDefinitionNode)]
        if len(operations) != 1 or operations[0].operation.value != 'query' or len(operations[0].selection_set.selections) != 1:
            return document
        field = operations[0].selection_set.selections[0]
        if not isinstance(field, FieldNode) or field.alias or field.directives:
            return document
        root = self.schema.query_type.fields.get(field.name.value)
        binding = self.ROOTS.get((self.area, field.name.value))
        argument = None
        if binding and root and self.identity_path == binding[1]:
            name, _, scalar = binding
            declared = root.args.get(name)
            kind = declared.type if declared else None
            if is_non_null_type(kind): kind = kind.of_type
            if (not is_list_type(kind) or get_named_type(kind).name != scalar
                    or any(arg.name.value == name for arg in field.arguments)):
                return document
            if scalar == 'Int':
                if not self.hero_id.isdigit() or not 0 < int(self.hero_id) <= 2147483647:
                    return document
                value = IntValueNode(value=self.hero_id)
            else:
                value = StringValueNode(value=self.hero_id)
            argument = ArgumentNode(name=NameNode(value=name), value=ListValueNode(values=(value,)))
        elif (root and self.area in {'HeroDocuments', 'HeroPictures'} and self.identity_path == 'id'
                and field.name.value == 'FileUpload_FileUploads' and isinstance(self.file_uuid, str) and self.file_uuid.strip()):
            declared = root.args.get('filters')
            filters = get_named_type(declared.type) if declared else None
            uuid = filters.fields.get('uuid') if isinstance(filters, GraphQLInputObjectType) else None
            uuid_filter = get_named_type(uuid.type) if uuid else None
            equals = uuid_filter.fields.get('equals') if isinstance(uuid_filter, GraphQLInputObjectType) else None
            if not equals or get_named_type(equals.type).name != 'String':
                return document
            existing = next((arg for arg in field.arguments if arg.name.value == 'filters'), None)
            if existing and (not isinstance(existing.value, ObjectValueNode) or any(f.name.value == 'uuid' for f in existing.value.fields)):
                return document
            uuid_value = ObjectFieldNode(name=NameNode(value='uuid'), value=ObjectValueNode(fields=(
                ObjectFieldNode(name=NameNode(value='equals'), value=StringValueNode(value=self.file_uuid)),)))
            values = tuple(existing.value.fields) if existing else ()
            argument = ArgumentNode(name=NameNode(value='filters'), value=ObjectValueNode(fields=(*values, uuid_value)))
            field.arguments = tuple(arg for arg in field.arguments if arg is not existing)
        if argument is None:
            return document
        field.arguments = (*field.arguments, argument)
        if validate(self.schema, tree):
            from hero_integration.services.mapped_update_v2 import MappedUpdateError
            raise MappedUpdateError('The approved readback query changed.')
        return print_ast(tree)

    def query(self, document, variables):
        return self.client.query(self.request(document), variables)
