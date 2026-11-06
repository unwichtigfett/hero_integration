"""Read-service metadata and variables validated by the operation materializer."""
from __future__ import annotations

from collections.abc import Mapping

from hero_integration.graphql_protocol.graphql_operation_materializer import (
    GraphQLOperationMaterializer, GraphQLOperationMaterializationError,
)
from hero_integration.services.graphql_schema_cache import compiled_schema
from hero_integration.services.localized_error import LocalizedError


class QueryDraftError(LocalizedError):
    pass


def _validated(schema, document):
    try:
        compiled = compiled_schema(schema)
        core = compiled.core
        tree = GraphQLOperationMaterializer.validate_document(core, document, allowed_kinds=('query',))
        return core, tree, compiled.fingerprint
    except (GraphQLOperationMaterializationError, TypeError, ValueError) as error:
        raise QueryDraftError('GraphQL query validation failed: {0}', str(error)) from error


def validate(schema: dict[str, object], document: str) -> dict[str, object]:
    """Describe one schema-validated Query, including root fragment selections."""
    from graphql import print_ast
    from graphql.language.ast import (FieldNode, FragmentDefinitionNode, FragmentSpreadNode,
                                      InlineFragmentNode, NonNullTypeNode, OperationDefinitionNode)
    _core, tree, _fingerprint = _validated(schema, document)
    operation = next(node for node in tree.definitions if isinstance(node, OperationDefinitionNode))
    fragments = {node.name.value: node for node in tree.definitions if isinstance(node, FragmentDefinitionNode)}
    roots = []
    def root_fields(selection):
        for node in selection.selections:
            if isinstance(node, FieldNode):
                roots.append(node.name.value)
            elif isinstance(node, InlineFragmentNode):
                root_fields(node.selection_set)
            elif isinstance(node, FragmentSpreadNode):
                root_fields(fragments[node.name.value].selection_set)
    root_fields(operation.selection_set)
    def count_fields(selection):
        return sum((1 if isinstance(node, FieldNode) else 0) +
                   (count_fields(node.selection_set) if getattr(node, 'selection_set', None) else 0)
                   for node in selection.selections)
    variables = operation.variable_definitions
    return {'operation_name': operation.name.value if operation.name else '', 'root_fields': roots,
            'variables': {node.variable.name.value: print_ast(node.type) for node in variables},
            'required_variables': sorted(node.variable.name.value for node in variables
                if isinstance(node.type, NonNullTypeNode) and node.default_value is None),
            'field_count': sum(count_fields(node.selection_set) for node in tree.definitions)}


def validate_variables(schema: dict[str, object], document: str, values: object) -> dict[str, object]:
    """Coerce the same detached, typed variable values used for transport."""
    if values is None:
        values = {}
    if not isinstance(values, Mapping):
        raise QueryDraftError('Query variables must be a JSON object.')
    core, _tree, fingerprint = _validated(schema, document)
    try:
        return GraphQLOperationMaterializer.materialize_document(
            core, fingerprint, document, values).variables
    except (GraphQLOperationMaterializationError, TypeError, ValueError) as error:
        raise QueryDraftError('GraphQL query validation failed: {0}', str(error)) from error
