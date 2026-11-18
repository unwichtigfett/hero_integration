"""Local mutation metadata and variables validated by the operation materializer."""
from __future__ import annotations

from hero_integration.graphql_protocol.graphql_operation_materializer import GraphQLOperationMaterializer
from hero_integration.services.graphql_schema_cache import compiled_schema
from hero_integration.services.localized_error import LocalizedError


class MutationDraftError(LocalizedError):
    pass


def operation_kind(document: str) -> str:
    from graphql.language.ast import OperationDefinitionNode
    try:
        tree = GraphQLOperationMaterializer.parse_document(document)
    except ValueError as error:
        raise MutationDraftError('GraphQL document validation failed: {0}', str(error)) from error
    return next(node.operation.value for node in tree.definitions if isinstance(node, OperationDefinitionNode))


def _validated(schema, document):
    try:
        return _validated_document(schema, document)
    except (TypeError, ValueError) as error:
        raise MutationDraftError('GraphQL mutation draft validation failed: {0}', str(error)) from error


def _validated_document(schema, document):
    from graphql import visit, Visitor
    # Persisted service documents use variables for string values. Inspect the
    # AST so quotes inside comments cannot be mistaken for persisted values.
    class NoPersistedStrings(Visitor):
        def enter_string_value(self, node, *_):
            raise ValueError('Mutation drafts must not contain literal values.')
    tree = GraphQLOperationMaterializer.parse_document(document, allowed_kinds=('mutation',))
    visit(tree, NoPersistedStrings())
    try:
        compiled = compiled_schema(schema)
        core = compiled.core
    except (TypeError, ValueError) as error:
        raise ValueError('The saved schema is invalid.') from error
    if core.mutation_type is None:
        raise ValueError('The saved schema has no mutation root.')
    return core, GraphQLOperationMaterializer.validate_document(core, document, allowed_kinds=('mutation',)), compiled.fingerprint


def validate(schema: dict[str, object], document: str) -> dict[str, object]:
    from graphql import get_named_type
    from graphql.language.ast import FieldNode, FragmentDefinitionNode, FragmentSpreadNode, InlineFragmentNode, OperationDefinitionNode
    core, tree, _fingerprint = _validated(schema, document)
    operation = next(node for node in tree.definitions if isinstance(node, OperationDefinitionNode))
    fragments = {node.name.value:node for node in tree.definitions if isinstance(node, FragmentDefinitionNode)}
    fields = []
    def collect(selection):
        for node in selection.selections:
            if isinstance(node, FieldNode):
                fields.append(node.name.value)
            elif isinstance(node, InlineFragmentNode):
                collect(node.selection_set)
            elif isinstance(node, FragmentSpreadNode):
                collect(fragments[node.name.value].selection_set)
    collect(operation.selection_set)
    field = fields[0]
    detail = {'operation':field, 'root':core.mutation_type.name,
              'return_type':('String' if field == '__typename' else get_named_type(core.mutation_type.fields[field].type).name)}
    if len(fields) > 1:
        detail['root_fields'] = fields
    return detail


def validate_variables(schema, document, values):
    """Validate local input only; the returned values carry no execution authority."""
    core, _tree, fingerprint = _validated(schema, document)
    try:
        return GraphQLOperationMaterializer.materialize_document(core, fingerprint,
            document, values, request_scope='local-mutation-draft-validation').variables
    except (TypeError, ValueError) as error:
        raise MutationDraftError('GraphQL mutation draft validation failed: {0}', str(error)) from error
