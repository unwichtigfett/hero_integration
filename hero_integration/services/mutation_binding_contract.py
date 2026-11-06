"""Single-root write binding metadata; GraphQL belongs to the materializer.

The fingerprint format remains stable for existing approved contracts. This
adapter checks the binding boundary; it performs no transport or publication.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field

from hero_integration.graphql_protocol.graphql_operation_materializer import GraphQLOperationMaterializer
from hero_integration.services.graphql_schema_cache import compiled_schema
from hero_integration.services.localized_error import LocalizedError


class GraphQLContractError(LocalizedError):
    pass


@dataclass(frozen=True)
class MutationContract:
    operation_name: str
    root_field: str
    variable_types: dict[str, dict[str, object]]
    fingerprint: str
    argument_variables: dict[str, str] = field(default_factory=dict)
    document: str = ''


def _type_ref(node):
    from graphql.language.ast import ListTypeNode, NonNullTypeNode
    if isinstance(node, NonNullTypeNode):
        return {'kind':'NON_NULL', 'name':None, 'ofType':_type_ref(node.type)}
    if isinstance(node, ListTypeNode):
        return {'kind':'LIST', 'name':None, 'ofType':_type_ref(node.type)}
    return {'kind':'NAMED', 'name':node.name.value, 'ofType':None}


def parse_mutation(document: str) -> MutationContract:
    from graphql.language.ast import (FieldNode, FragmentDefinitionNode, FragmentSpreadNode,
                                      InlineFragmentNode, OperationDefinitionNode, VariableNode)
    try:
        tree = GraphQLOperationMaterializer.parse_document(document, allowed_kinds=('mutation',))
    except ValueError as error:
        raise GraphQLContractError('A mutation contract must contain exactly one operation and root field.') from error
    operation = next(node for node in tree.definitions if isinstance(node, OperationDefinitionNode))
    fragments = {node.name.value:node for node in tree.definitions if isinstance(node, FragmentDefinitionNode)}
    if len(fragments) != sum(isinstance(node, FragmentDefinitionNode) for node in tree.definitions):
        raise GraphQLContractError('Mutation contract fragments must have unique names.')
    roots = []
    def collect(selection, visited=frozenset()):
        for node in selection.selections:
            if isinstance(node, FieldNode):
                roots.append(node)
            elif isinstance(node, InlineFragmentNode):
                collect(node.selection_set, visited)
            elif isinstance(node, FragmentSpreadNode):
                name = node.name.value
                if name in visited or name not in fragments:
                    raise GraphQLContractError('Mutation contract fragments must be defined and acyclic.')
                collect(fragments[name].selection_set, visited | {name})
    collect(operation.selection_set)
    if len(roots) != 1:
        raise GraphQLContractError('A mutation contract must contain exactly one root field.')
    root = roots[0]
    if root.alias is not None and root.alias.value != root.name.value:
        raise GraphQLContractError('A write binding requires an unaliased mutation root field.')
    argument_variables = {}
    for argument in root.arguments:
        name = argument.name.value
        if name in argument_variables:
            raise GraphQLContractError('Duplicate mutation argument {0}.', name)
        if not isinstance(argument.value, VariableNode):
            raise GraphQLContractError('Mutation contract arguments must be declared variables.')
        argument_variables[name] = argument.value.name.value
    variables = {}
    for declaration in operation.variable_definitions:
        name = declaration.variable.name.value
        if name in variables:
            raise GraphQLContractError('Duplicate GraphQL variable ${0}.', name)
        variables[name] = _type_ref(declaration.type)
    operation_name = operation.name.value if operation.name else ''
    canonical = json.dumps({'document':document.strip(), 'operation_name':operation_name,
        'root_field':root.name.value, 'variables':variables}, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return MutationContract(operation_name, root.name.value, variables, hashlib.sha256(canonical.encode()).hexdigest(),
                            argument_variables, document)


def validate_mutation(schema_snapshot: object, contract: MutationContract, variables: object) -> dict[str, object]:
    """Validate the entire bound document and coerce its variables once."""
    if not isinstance(variables, Mapping):
        raise GraphQLContractError('Mutation variables must be a JSON object.')
    try:
        if not isinstance(contract, MutationContract) or parse_mutation(contract.document) != contract:
            raise ValueError('Mutation binding metadata no longer matches its document.')
        compiled = compiled_schema(schema_snapshot)
        request = GraphQLOperationMaterializer.materialize_document(compiled.core, compiled.fingerprint,
            contract.document, variables, request_scope='local-mutation-binding-validation')
        return request.variables
    except (TypeError, ValueError) as error:
        raise GraphQLContractError('Mutation contract validation failed: {0}', str(error)) from error
