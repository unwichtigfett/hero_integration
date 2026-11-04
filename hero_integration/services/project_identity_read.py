"""Narrow an approved Project list selection to its schema-proven bound identity."""
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
