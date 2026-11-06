from __future__ import annotations

from collections.abc import Mapping
from collections import OrderedDict
from dataclasses import dataclass, replace
import hashlib
import json
import re
from threading import RLock
from weakref import WeakKeyDictionary

from .schema import (
    GraphQLField,
    GraphQLSchema,
    GraphQLTypeRef,
)
from .schema_snapshot import (
    schema_fingerprint,
)

from .graphql_argument_values import (
    GraphQLArgumentValueError,
    validate_typed_argument_values,
)
from .graphql_query_composer import (
    GraphQLQueryComposerError,
    GraphQLQueryComposerModel,
    GraphQLArgumentSpec,
    GraphQLSelectionField,
    build_mutation_composer_model,
    build_query_composer_model,
)
from .graphql_selection_set import (
    GraphQLSelectionPathSegment,
    GraphQLSelectionSetError,
    GraphQLSelectionTree,
    build_stable_selection_tree,
)
from .graphql_write_safety import (
    GraphQLWriteRequest,
    detect_operation_kind,
)
from .operation_kind import GraphQLOperationKind
from .source_proposals import (
    SourceApiProposalModel,
    SourceOperationProposal,
)


_GRAPHQL_NAME = re.compile(r"^[_A-Za-z][_0-9A-Za-z]*$")
_QUERY_PROJECTION_CACHE = WeakKeyDictionary()
_QUERY_PROJECTION_LOCK = RLock()
_MAX_PROJECTION_PLANS = 16
_MAX_CACHED_PROJECTION_CHARS = 65536


class GraphQLOperationMaterializationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class GraphQLOperationBinding:
    binding_id: str
    schema_fingerprint: str
    operation_node_id: str
    operation_kind: GraphQLOperationKind
    operation_name: str
    root_alias: str
    selected_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        binding_id = self.binding_id.strip()
        fingerprint = self.schema_fingerprint.strip().lower()
        operation_node_id = self.operation_node_id.strip()
        operation_name = self.operation_name.strip()
        root_alias = self.root_alias.strip()

        if not binding_id:
            raise GraphQLOperationMaterializationError(
                "binding_id must not be empty."
            )

        if (
            len(fingerprint) != 64
            or any(
                char not in "0123456789abcdef"
                for char in fingerprint
            )
        ):
            raise GraphQLOperationMaterializationError(
                "schema_fingerprint must be a SHA-256 hex digest."
            )

        if not operation_node_id:
            raise GraphQLOperationMaterializationError(
                "operation_node_id must not be empty."
            )

        if self.operation_kind not in {
            GraphQLOperationKind.QUERY,
            GraphQLOperationKind.MUTATION,
        }:
            raise GraphQLOperationMaterializationError(
                "Only GraphQL query and mutation bindings are supported."
            )

        if not _GRAPHQL_NAME.fullmatch(operation_name):
            raise GraphQLOperationMaterializationError(
                "operation_name must be a valid GraphQL name."
            )

        if not _GRAPHQL_NAME.fullmatch(root_alias):
            raise GraphQLOperationMaterializationError(
                "root_alias must be a valid GraphQL name."
            )

        normalized_paths = tuple(
            path.strip()
            for path in self.selected_paths
        )

        if any(
            not path
            for path in normalized_paths
        ):
            raise GraphQLOperationMaterializationError(
                "selected_paths must not contain empty paths."
            )

        if len(normalized_paths) != len(
            set(normalized_paths)
        ):
            raise GraphQLOperationMaterializationError(
                "selected_paths contains duplicates."
            )

        object.__setattr__(
            self,
            "binding_id",
            binding_id,
        )
        object.__setattr__(
            self,
            "schema_fingerprint",
            fingerprint,
        )
        object.__setattr__(
            self,
            "operation_node_id",
            operation_node_id,
        )
        object.__setattr__(
            self,
            "operation_name",
            operation_name,
        )
        object.__setattr__(
            self,
            "root_alias",
            root_alias,
        )
        object.__setattr__(
            self,
            "selected_paths",
            normalized_paths,
        )


@dataclass(frozen=True, slots=True)
class GraphQLArgumentBinding:
    argument_name: str
    graphql_type: str
    variable_name: str


@dataclass(frozen=True, slots=True)
class GraphQLResultBinding:
    selection_path: tuple[str, ...]
    protocol_field_node_ids: tuple[str, ...]
    response_path: tuple[str, ...]
    graphql_type: str


@dataclass(frozen=True, slots=True)
class GraphQLOperationRequest:
    binding_id: str
    schema_fingerprint: str
    operation_node_id: str
    operation_kind: GraphQLOperationKind
    protocol_field_name: str
    operation_name: str
    root_alias: str
    document: str
    argument_bindings: tuple[GraphQLArgumentBinding, ...]
    result_bindings: tuple[GraphQLResultBinding, ...]
    request_fingerprint: str
    mutation_safety_request: GraphQLWriteRequest | None
    _canonical_variables: str

    @property
    def variables(self) -> dict[str, object]:
        # Detached copy: callers cannot mutate the sealed request.
        return json.loads(self._canonical_variables)


@dataclass(frozen=True, slots=True)
class GraphQLDocumentRequest:
    document: str
    schema_fingerprint: str
    operation_kind: GraphQLOperationKind
    request_fingerprint: str
    mutation_safety_request: GraphQLWriteRequest | None
    _canonical_variables: str

    @property
    def variables(self):
        return json.loads(self._canonical_variables)


@dataclass(frozen=True, slots=True)
class GraphQLQueryBatchRequest:
    document: str
    schema_fingerprint: str
    request_fingerprint: str
    result_bindings: tuple[GraphQLResultBinding, ...]
    _canonical_variables: str
    requests: tuple[GraphQLOperationRequest, ...]

    @property
    def variables(self):
        return json.loads(self._canonical_variables)


class GraphQLOperationMaterializer:
    """Pure single-operation GraphQL materializer.

    Query and Mutation share one protocol contract. This component performs
    no HTTP I/O, no Frappe persistence and no Process-Core publication.
    """

    @staticmethod
    def operation_template(schema, operation_kind, root_field_name):
        """Build a schema-validated draft without inventing runtime input values."""
        from graphql import GraphQLSchema as CoreSchema, GraphQLError, get_named_type
        from graphql import is_composite_type, is_required_argument, parse_type, print_ast
        from graphql.language import OperationType
        from graphql.language.ast import (
            ArgumentNode, DocumentNode, FieldNode, NameNode, OperationDefinitionNode,
            SelectionSetNode, VariableDefinitionNode, VariableNode,
        )
        if not isinstance(schema, CoreSchema) or not isinstance(root_field_name, str) or not _GRAPHQL_NAME.fullmatch(root_field_name):
            raise GraphQLOperationMaterializationError('A GraphQL schema and root field are required.')
        try:
            kind = OperationType(operation_kind)
            root = schema.get_root_type(kind)
            if root is None or root_field_name not in root.fields:
                raise GraphQLOperationMaterializationError('The operation does not exist in the saved schema.')
            field = root.fields[root_field_name]
            required = [(name, argument) for name, argument in field.args.items() if is_required_argument(argument)]
            operation_name = ''.join(part[:1].upper() + part[1:] for part in root_field_name.split('_') if part) or 'Operation'
            if not _GRAPHQL_NAME.fullmatch(operation_name):
                operation_name = 'Operation' + operation_name
            variables = tuple(VariableDefinitionNode(variable=VariableNode(name=NameNode(value=name)),
                              type=parse_type(str(argument.type))) for name, argument in required)
            arguments = tuple(ArgumentNode(name=NameNode(value=name),value=VariableNode(name=NameNode(value=name)))
                              for name, _ in required)
            selection = SelectionSetNode(selections=(FieldNode(name=NameNode(value='__typename')),)) \
                if is_composite_type(get_named_type(field.type)) else None
            tree = DocumentNode(definitions=(OperationDefinitionNode(operation=kind,name=NameNode(value=operation_name),
                variable_definitions=variables,selection_set=SelectionSetNode(selections=(FieldNode(
                    name=NameNode(value=root_field_name),arguments=arguments,selection_set=selection),))),))
            document = print_ast(tree)
            GraphQLOperationMaterializer.validate_document(schema, document, allowed_kinds=(kind.value,))
            return document
        except GraphQLOperationMaterializationError:
            raise
        except (TypeError, ValueError, GraphQLError) as error:
            raise GraphQLOperationMaterializationError('The GraphQL operation template cannot be materialized.') from error

    @staticmethod
    def materialize_introspection(document):
        """Compile schema discovery before a remote schema snapshot exists.

        graphql-core supplies the standard introspection types. The placeholder
        root field is never available to callers; business fields require the
        captured remote schema and the regular document materializer.
        """
        from graphql import GraphQLError, build_schema, parse, print_schema
        from graphql import visit, Visitor

        schema = build_schema('type Query { _schemaBootstrap: Boolean }')
        class IntrospectionOnly(Visitor):
            def enter_field(self, node, *_):
                if node.name.value == '_schemaBootstrap':
                    raise GraphQLOperationMaterializationError('Only schema introspection is allowed during discovery.')
        try:
            visit(parse(document), IntrospectionOnly())
        except GraphQLOperationMaterializationError:
            raise
        except (TypeError, ValueError, GraphQLError) as error:
            raise GraphQLOperationMaterializationError('Invalid schema introspection document.') from error
        fingerprint = hashlib.sha256(print_schema(schema).encode()).hexdigest()
        return GraphQLOperationMaterializer.materialize_document(schema, fingerprint, document, {})

    @staticmethod
    def parse_document(document, *, allowed_kinds=('query', 'mutation', 'subscription'), operation_name=None):
        """Parse executable definitions and require an unambiguous operation."""
        from graphql import GraphQLError, parse
        from graphql.language.ast import OperationDefinitionNode, FragmentDefinitionNode
        try:
            tree = parse(document)
        except (TypeError, ValueError, GraphQLError) as error:
            raise GraphQLOperationMaterializationError(str(error)) from error
        if any(not isinstance(node, (OperationDefinitionNode, FragmentDefinitionNode)) for node in tree.definitions):
            raise GraphQLOperationMaterializationError('GraphQL request documents may contain only operations and fragments.')
        operations = [node for node in tree.definitions if isinstance(node, OperationDefinitionNode)]
        if operation_name is not None:
            if not isinstance(operation_name, str) or not _GRAPHQL_NAME.fullmatch(operation_name):
                raise GraphQLOperationMaterializationError('A valid GraphQL operation name is required.')
            operations = [node for node in operations if node.name and node.name.value == operation_name]
        if len(operations) != 1 or operations[0].operation.value not in allowed_kinds:
            raise GraphQLOperationMaterializationError('A draft must contain exactly one permitted GraphQL operation.')
        return tree

    @staticmethod
    def validate_document(schema, document, *, allowed_kinds=('query', 'mutation'), operation_name=None):
        """Validate the complete document without requiring runtime input values."""
        from graphql import GraphQLSchema as CoreSchema, GraphQLError, validate
        if not isinstance(schema, CoreSchema):
            raise GraphQLOperationMaterializationError('A GraphQL schema is required.')
        try:
            tree = GraphQLOperationMaterializer.parse_document(document, allowed_kinds=allowed_kinds,
                                                              operation_name=operation_name)
            errors = validate(schema, tree)
            if errors:
                raise GraphQLOperationMaterializationError(' '.join(error.message for error in errors))
            return tree
        except GraphQLOperationMaterializationError:
            raise
        except (TypeError, ValueError, GraphQLError) as error:
            raise GraphQLOperationMaterializationError(str(error)) from error

    @staticmethod
    def materialize_document(schema, fingerprint, document, variables, *, request_scope=None, operation_name=None):
        """Validate and seal a complete operation, including fragments and directives."""
        from graphql import GraphQLSchema as CoreSchema, GraphQLError, print_ast
        from graphql.execution.values import get_variable_values
        from graphql.language.ast import OperationDefinitionNode
        from graphql.utilities import separate_operations
        if not isinstance(schema, CoreSchema) or not isinstance(fingerprint, str) or not re.fullmatch('[0-9a-f]{64}', fingerprint):
            raise GraphQLOperationMaterializationError('A validated schema fingerprint is required.')
        if not isinstance(variables, Mapping) or any(not isinstance(key, str) for key in variables):
            raise GraphQLOperationMaterializationError('Document variables must be a mapping of names to values.')
        try:
            tree = GraphQLOperationMaterializer.validate_document(schema, document, operation_name=operation_name)
            if operation_name is not None:
                # Validate the entire submitted document first, then transport
                # only the selected operation and its transitive fragments.
                # Existing transports need no separate operationName channel.
                tree = separate_operations(tree)[operation_name]
            operation = next(node for node in tree.definitions if isinstance(node, OperationDefinitionNode))
            declared = {item.variable.name.value for item in operation.variable_definitions}
            if set(variables) - declared:
                raise GraphQLOperationMaterializationError('Document variables contain undeclared names: ' + ', '.join(sorted(set(variables) - declared)) + '.')
            # JSON normalization detaches caller-owned containers before coercion.
            detached = json.loads(GraphQLOperationMaterializer._canonical_variables(variables))
            values = get_variable_values(schema, operation.variable_definitions, detached)
            if isinstance(values, list):
                raise GraphQLOperationMaterializationError('Document variables do not match their declared types: ' +
                    ', '.join('$' + item.variable.name.value + ': ' + print_ast(item.type)
                              for item in operation.variable_definitions) + '.')
            canonical = GraphQLOperationMaterializer._canonical_variables(values)
            rendered = print_ast(tree)
            kind = GraphQLOperationKind(operation.operation.value)
            if kind is GraphQLOperationKind.MUTATION and not isinstance(request_scope, str):
                raise GraphQLOperationMaterializationError('Mutation materialization requires a request scope.')
            if kind is GraphQLOperationKind.QUERY and request_scope is not None:
                raise GraphQLOperationMaterializationError('Request scope is mutation-only.')
            safety = GraphQLWriteRequest.create(rendered, variables=values,
                operation_name=operation.name.value if operation.name else None, request_scope=request_scope)
            sealed = hashlib.sha256((fingerprint + safety.fingerprint).encode()).hexdigest()
            return GraphQLDocumentRequest(rendered, fingerprint, kind, sealed,
                safety if kind is GraphQLOperationKind.MUTATION else None, canonical)
        except GraphQLOperationMaterializationError:
            raise
        except (TypeError, ValueError, GraphQLError) as error:
            raise GraphQLOperationMaterializationError('The GraphQL document cannot be materialized.') from error

    @staticmethod
    def materialize_projected_query(schema, fingerprint, document, variables, selected_paths, *, preserve_paths=()):
        """Narrow an approved read without changing arguments or collection shape.

        Paths use response keys (including aliases). Selecting a composite path
        retains its complete approved subtree. No fields are added implicitly.
        """
        from copy import copy
        from graphql import print_ast, visit, Visitor, GraphQLSchema as CoreSchema
        from graphql.language.ast import (DocumentNode, FieldNode, FragmentDefinitionNode,
            FragmentSpreadNode, InlineFragmentNode, NameNode, OperationDefinitionNode, SelectionSetNode)
        if not isinstance(schema,CoreSchema) or not isinstance(document,str):
            raise GraphQLOperationMaterializationError('A GraphQL schema and query document are required for projection.')
        if (not isinstance(selected_paths,(tuple,list,set,frozenset)) or
                not isinstance(preserve_paths,(tuple,list,set,frozenset)) or
                any(not isinstance(path,str) or not path for path in (*selected_paths,*preserve_paths))):
            raise GraphQLOperationMaterializationError('Query projection requires valid response paths.')
        required = {tuple(path.split('.')) for path in selected_paths if isinstance(path,str) and path}
        optional = {tuple(path.split('.')) for path in preserve_paths if isinstance(path,str) and path}
        if not required or any(not all(_GRAPHQL_NAME.fullmatch(part) for part in path) for path in required | optional):
            raise GraphQLOperationMaterializationError('Query projection requires valid response paths.')
        cacheable=(len(document)<=_MAX_CACHED_PROJECTION_CHARS and
                   sum(len(path) for path in (*selected_paths,*preserve_paths))<=_MAX_CACHED_PROJECTION_CHARS)
        key=(document,tuple(sorted(required)),tuple(sorted(optional)))
        if cacheable:
            with _QUERY_PROJECTION_LOCK:
                bucket=_QUERY_PROJECTION_CACHE.get(schema)
                cached=bucket.get(key) if bucket is not None else None
                if cached is not None: bucket.move_to_end(key)
            if cached is not None:
                rendered,declared,used=cached
                if not isinstance(variables,Mapping) or set(variables)-declared:
                    raise GraphQLOperationMaterializationError('Query projection variables must match the approved document.')
                return GraphQLOperationMaterializer.materialize_document(schema,fingerprint,rendered,
                    {name:value for name,value in variables.items() if name in used})
        tree = GraphQLOperationMaterializer.validate_document(schema, document, allowed_kinds=('query',))
        operation = next(node for node in tree.definitions if isinstance(node,OperationDefinitionNode))
        declared = {node.variable.name.value for node in operation.variable_definitions}
        if not isinstance(variables,Mapping) or set(variables)-declared:
            raise GraphQLOperationMaterializationError('Query projection variables must match the approved document.')
        fragments = {node.name.value:node for node in tree.definitions if isinstance(node,FragmentDefinitionNode)}
        emitted, available = {}, set()
        wanted = required | optional

        def inventory(selection,prefix=()):
            for node in selection.selections:
                if isinstance(node,FieldNode):
                    path=(*prefix,node.alias.value if node.alias else node.name.value)
                    available.add(path)
                    if node.selection_set: inventory(node.selection_set,path)
                elif isinstance(node,InlineFragmentNode): inventory(node.selection_set,prefix)
                elif isinstance(node,FragmentSpreadNode): inventory(fragments[node.name.value].selection_set,prefix)
        inventory(operation.selection_set)
        if required-available:
            raise GraphQLOperationMaterializationError('Approved query omits projection paths: '+
                ', '.join('.'.join(path) for path in sorted(required-available)))

        def trim(selection, prefix=()):
            selections = []
            for node in selection.selections:
                if isinstance(node,FieldNode):
                    path = (*prefix,node.alias.value if node.alias else node.name.value)
                    if any(path[:len(need)]==need for need in wanted):
                        selections.append(node)
                    elif any(need[:len(path)]==path for need in wanted) and node.selection_set:
                        child = trim(node.selection_set,path)
                        if child.selections:
                            narrowed = copy(node);narrowed.selection_set = child;selections.append(narrowed)
                    elif node.name.value=='__typename':
                        selections.append(node)
                elif isinstance(node,InlineFragmentNode):
                    child = trim(node.selection_set,prefix)
                    if child.selections:
                        narrowed = copy(node);narrowed.selection_set = child;selections.append(narrowed)
                elif isinstance(node,FragmentSpreadNode):
                    fragment = fragments[node.name.value]
                    child = trim(fragment.selection_set,prefix)
                    if child.selections:
                        name = fragment.name.value+'_projection_'+str(len(emitted)+1)
                        while name in fragments or name in emitted: name += '_'
                        definition = copy(fragment);definition.name=NameNode(value=name);definition.selection_set=child
                        emitted[name] = definition
                        spread = copy(node);spread.name=NameNode(value=name);selections.append(spread)
            return SelectionSetNode(selections=tuple(selections))

        projected = copy(operation);projected.selection_set=trim(operation.selection_set)
        reachable = {}
        def include_fragments(selection):
            for node in selection.selections:
                if isinstance(node,FragmentSpreadNode) and node.name.value not in reachable:
                    definition=emitted.get(node.name.value) or fragments[node.name.value]
                    reachable[node.name.value]=definition
                    include_fragments(definition.selection_set)
                elif getattr(node,'selection_set',None): include_fragments(node.selection_set)
        include_fragments(projected.selection_set)
        result = DocumentNode(definitions=(projected,*reachable.values()))
        used = set()
        class Uses(Visitor):
            def enter_variable_definition(self,*_): return False
            def enter_variable(self,node,*_): used.add(node.name.value)
        visit(result,Uses())
        projected.variable_definitions=tuple(node for node in operation.variable_definitions if node.variable.name.value in used)
        rendered=print_ast(result)
        if cacheable:
            # Weak schema keys do not keep evicted compiled schemas alive.
            # Values contain only validated projection text and variable names.
            with _QUERY_PROJECTION_LOCK:
                bucket=_QUERY_PROJECTION_CACHE.setdefault(schema,OrderedDict())
                bucket[key]=(rendered,frozenset(declared),frozenset(used))
                bucket.move_to_end(key)
                while len(bucket)>_MAX_PROJECTION_PLANS: bucket.popitem(last=False)
        return GraphQLOperationMaterializer.materialize_document(schema,fingerprint,rendered,
            {key:value for key,value in variables.items() if key in used})

    @staticmethod
    def combine_queries(requests, operation_name):
        """Combine generated reads while keeping each root and its variable scope."""
        from graphql import parse, print_ast, visit, Visitor
        from graphql.language.ast import DocumentNode, OperationDefinitionNode, SelectionSetNode, NameNode, VariableNode
        from graphql.language import OperationType
        requests = tuple(requests)
        if not requests or not isinstance(operation_name, str) or not _GRAPHQL_NAME.fullmatch(operation_name):
            raise GraphQLOperationMaterializationError('A named nonempty query batch is required.')
        if any(not isinstance(item, GraphQLOperationRequest) or item.operation_kind is not GraphQLOperationKind.QUERY for item in requests):
            raise GraphQLOperationMaterializationError('Query batches may contain only materialized queries.')
        if len({item.schema_fingerprint for item in requests}) != 1 or len({item.root_alias for item in requests}) != len(requests):
            raise GraphQLOperationMaterializationError('Query batch schema and root aliases must be unambiguous.')
        declarations, selections, variables, bindings = [], [], {}, []
        for index, request in enumerate(requests, 1):
            names = {name: f'q{index}_{name}' for name in request.variables}
            class Rename(Visitor):
                def enter_variable(self, node, *_):
                    return VariableNode(name=NameNode(value=names[node.name.value]))
            tree = visit(parse(request.document), Rename())
            operation, = tree.definitions
            if not isinstance(operation, OperationDefinitionNode) or operation.operation is not OperationType.QUERY:
                raise GraphQLOperationMaterializationError('Invalid materialized query in batch.')
            declarations.extend(operation.variable_definitions)
            selections.extend(operation.selection_set.selections)
            variables.update({names[name]: value for name, value in request.variables.items()})
            bindings.extend(request.result_bindings)
        document = print_ast(DocumentNode(definitions=(OperationDefinitionNode(operation=OperationType.QUERY,
            name=NameNode(value=operation_name), variable_definitions=tuple(declarations),
            selection_set=SelectionSetNode(selections=tuple(selections))),)))
        canonical = GraphQLOperationMaterializer._canonical_variables(variables)
        fingerprint = hashlib.sha256(json.dumps([document, json.loads(canonical),
            [item.request_fingerprint for item in requests]], sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        return GraphQLQueryBatchRequest(document, requests[0].schema_fingerprint, fingerprint, tuple(bindings), canonical, requests)

    def materialize(
        self,
        schema: GraphQLSchema,
        proposals: SourceApiProposalModel,
        binding: GraphQLOperationBinding,
        argument_values: Mapping[str, object],
        *,
        request_scope: str | None = None,
        nested_arguments: Mapping[str, Mapping[str, object]] | None = None,
    ) -> GraphQLOperationRequest:
        if not isinstance(
            schema,
            GraphQLSchema,
        ):
            raise TypeError(
                "schema must be a GraphQLSchema."
            )

        if not isinstance(
            proposals,
            SourceApiProposalModel,
        ):
            raise TypeError(
                "proposals must be a SourceApiProposalModel."
            )

        if not isinstance(
            binding,
            GraphQLOperationBinding,
        ):
            raise TypeError(
                "binding must be a GraphQLOperationBinding."
            )

        if not isinstance(
            argument_values,
            Mapping,
        ):
            raise TypeError(
                "argument_values must be a mapping."
            )

        current_fingerprint = schema_fingerprint(
            schema
        )

        if (
            current_fingerprint
            != proposals.schema_fingerprint
            or current_fingerprint
            != binding.schema_fingerprint
        ):
            raise GraphQLOperationMaterializationError(
                "Schema, proposals and operation binding "
                "use different fingerprints."
            )

        operation = self._operation(
            proposals,
            binding,
        )

        root_field = self._root_field(
            schema,
            operation,
        )

        model = self._composer_model(
            schema,
            root_field,
            binding.operation_kind,
        )
        model = replace(model, selectable_fields=self._resolve_selected_fields(schema, model, binding.selected_paths))

        try:
            validated = validate_typed_argument_values(
                model,
                dict(argument_values),
                schema=schema,
            )
        except GraphQLArgumentValueError as exc:
            raise GraphQLOperationMaterializationError(
                str(exc)
            ) from exc

        argument_bindings, variables = (
            self._arguments(
                model,
                validated,
            )
        )

        nested_bindings, nested_variables, nested_by_path = self._nested_arguments(
            schema, model, binding, nested_arguments, len(argument_bindings)
        )
        variables.update(nested_variables)
        selection_tree, result_bindings = (
            self._selection(
                schema,
                model,
                binding,
                nested_by_path,
            )
        )

        document = self._render(
            binding,
            root_field,
            argument_bindings,
            selection_tree,
            nested_bindings,
        )

        detected = detect_operation_kind(
            document,
            operation_name=binding.operation_name,
        )

        if detected is not binding.operation_kind:
            raise GraphQLOperationMaterializationError(
                "Rendered GraphQL operation kind does not "
                "match the sealed binding."
            )

        canonical_variables = self._canonical_variables(
            variables
        )

        mutation_safety_request = None

        if (
            binding.operation_kind
            is GraphQLOperationKind.MUTATION
        ):
            normalized_scope = (
                request_scope.strip()
                if isinstance(
                    request_scope,
                    str,
                )
                else ""
            )

            if not normalized_scope:
                raise GraphQLOperationMaterializationError(
                    "Mutation materialization requires "
                    "a non-empty request_scope."
                )

            mutation_safety_request = (
                GraphQLWriteRequest.create(
                    document,
                    variables=json.loads(
                        canonical_variables
                    ),
                    operation_name=(
                        binding.operation_name
                    ),
                    request_scope=normalized_scope,
                )
            )

            if (
                mutation_safety_request.operation_kind
                is not GraphQLOperationKind.MUTATION
            ):
                raise GraphQLOperationMaterializationError(
                    "Mutation safety request resolved to "
                    "a non-mutation operation."
                )

        elif request_scope is not None:
            raise GraphQLOperationMaterializationError(
                "request_scope is mutation-only."
            )

        request_fingerprint = (
            self._request_fingerprint(
                binding,
                document,
                canonical_variables,
                result_bindings,
                (
                    mutation_safety_request.request_scope
                    if mutation_safety_request
                    is not None
                    else None
                ),
            )
        )

        return GraphQLOperationRequest(
            binding_id=binding.binding_id,
            schema_fingerprint=(
                binding.schema_fingerprint
            ),
            operation_node_id=(
                binding.operation_node_id
            ),
            operation_kind=(
                binding.operation_kind
            ),
            protocol_field_name=(
                root_field.name
            ),
            operation_name=(
                binding.operation_name
            ),
            root_alias=binding.root_alias,
            document=document,
            argument_bindings=(
                argument_bindings
            ),
            result_bindings=(
                result_bindings
            ),
            request_fingerprint=(
                request_fingerprint
            ),
            mutation_safety_request=(
                mutation_safety_request
            ),
            _canonical_variables=(
                canonical_variables
            ),
        )

    @staticmethod
    def _operation(
        proposals: SourceApiProposalModel,
        binding: GraphQLOperationBinding,
    ) -> SourceOperationProposal:
        operation = next(
            (
                item
                for item in proposals.operations
                if (
                    item.operation_node_id
                    == binding.operation_node_id
                )
            ),
            None,
        )

        if operation is None:
            raise GraphQLOperationMaterializationError(
                "Operation binding references an "
                "unknown source operation."
            )

        if (
            operation.operation_kind
            is not binding.operation_kind
        ):
            raise GraphQLOperationMaterializationError(
                "Operation kind differs from the "
                "source proposal."
            )

        return operation

    @staticmethod
    def _root_field(
        schema: GraphQLSchema,
        operation: SourceOperationProposal,
    ) -> GraphQLField:
        if (
            operation.operation_kind
            is GraphQLOperationKind.QUERY
        ):
            fields = schema.query_fields

        elif (
            operation.operation_kind
            is GraphQLOperationKind.MUTATION
        ):
            fields = schema.mutation_fields

        else:
            raise GraphQLOperationMaterializationError(
                "Unsupported GraphQL operation kind."
            )

        field = next(
            (
                item
                for item in fields
                if (
                    item.name
                    == operation.protocol_name
                )
            ),
            None,
        )

        if field is None:
            raise GraphQLOperationMaterializationError(
                "Source operation root field is "
                "missing from schema."
            )

        return field

    @staticmethod
    def _composer_model(
        schema: GraphQLSchema,
        root_field: GraphQLField,
        kind: GraphQLOperationKind,
    ) -> GraphQLQueryComposerModel:
        try:
            if kind is GraphQLOperationKind.QUERY:
                return build_query_composer_model(
                    schema,
                    root_field,
                    max_depth=1,
                )

            return build_mutation_composer_model(
                schema,
                root_field,
                max_depth=1,
            )

        except GraphQLQueryComposerError as exc:
            raise GraphQLOperationMaterializationError(
                str(exc)
            ) from exc

    @staticmethod
    def _resolve_selected_fields(schema, model, selected_paths):
        """Resolve only explicitly requested paths, including recursive type paths."""
        resolved = {}
        for raw_path in selected_paths:
            parts = raw_path.split('.')
            if any(not _GRAPHQL_NAME.fullmatch(part[1:] if part.startswith('@') else part) for part in parts):
                raise GraphQLOperationMaterializationError(f"Invalid selected GraphQL path: {raw_path}")
            current = model.root_return_type
            for index, name in enumerate(parts):
                if name.startswith('@'):
                    target_name = name[1:]
                    if (current.kind not in {'UNION', 'INTERFACE'}
                            or target_name not in {item.named_type() for item in current.possible_types}):
                        raise GraphQLOperationMaterializationError(f"Invalid GraphQL type condition: {raw_path}")
                    target = schema.type_named(target_name)
                    if target is None or target.kind != 'OBJECT' or index == len(parts) - 1:
                        raise GraphQLOperationMaterializationError(f"Invalid GraphQL type condition: {raw_path}")
                    path = tuple(parts[:index + 1])
                    synthetic = GraphQLField(name, None, (), GraphQLTypeRef('OBJECT', target_name), False, None)
                    resolved[path] = GraphQLSelectionField(path, synthetic, current.name)
                    current = target
                    continue
                if name == '__typename' and current.kind in {'OBJECT', 'INTERFACE', 'UNION'}:
                    path = tuple(parts[:index + 1])
                    synthetic = GraphQLField(name, None, (), GraphQLTypeRef('SCALAR', 'String'), False, None)
                    resolved[path] = GraphQLSelectionField(path, synthetic, current.name)
                    current = schema.type_named('String')
                    if current is None:
                        raise GraphQLOperationMaterializationError('GraphQL String type is missing.')
                    continue
                if current.kind not in {'OBJECT', 'INTERFACE'}:
                    raise GraphQLOperationMaterializationError(f"Selected GraphQL path is not available: {raw_path}")
                field = next((item for item in current.fields if item.name == name), None)
                if field is None:
                    raise GraphQLOperationMaterializationError(f"Selected GraphQL path is not available: {raw_path}")
                path = tuple(parts[:index + 1])
                resolved[path] = GraphQLSelectionField(path, field, current.name)
                current = schema.type_named(field.type_ref.named_type())
                if current is None:
                    raise GraphQLOperationMaterializationError(f"Selected GraphQL path has an unknown type: {raw_path}")
        return tuple(resolved[path] for path in sorted(resolved))

    @staticmethod
    def _arguments(
        model: GraphQLQueryComposerModel,
        validated: Mapping[str, object],
    ) -> tuple[
        tuple[GraphQLArgumentBinding, ...],
        dict[str, object],
    ]:
        included = sorted(
            (
                spec
                for spec in model.arguments
                if spec.name in validated
            ),
            key=lambda item: item.name,
        )

        bindings: list[
            GraphQLArgumentBinding
        ] = []

        variables: dict[
            str,
            object,
        ] = {}

        for index, spec in enumerate(
            included,
            start=1,
        ):
            variable_name = f"v{index}"

            bindings.append(
                GraphQLArgumentBinding(
                    argument_name=spec.name,
                    graphql_type=spec.type_name,
                    variable_name=variable_name,
                )
            )

            variables[variable_name] = (
                validated[spec.name]
            )

        return (
            tuple(bindings),
            variables,
        )

    @staticmethod
    def _nested_arguments(schema, model, binding, supplied, offset):
        if supplied is None:
            supplied = {}
        if not isinstance(supplied, Mapping) or any(not isinstance(k, str) or not isinstance(v, Mapping) for k, v in supplied.items()):
            raise GraphQLOperationMaterializationError("Nested arguments must map selected field paths to argument mappings.")
        selections = {".".join(item.path): item for item in model.selectable_fields}
        selected = {".".join(path.split(".")[:depth]) for path in binding.selected_paths
                    for depth in range(1, len(path.split(".")) + 1)}
        if set(supplied) - selected or set(supplied) - set(selections):
            raise GraphQLOperationMaterializationError("Nested arguments must refer to selected field paths.")
        bindings, variables, by_path = [], {}, {}
        for path in sorted(selected):
            selection = selections.get(path)
            if selection is None:
                continue
            nested_model = replace(model, arguments=tuple(GraphQLArgumentSpec(arg, schema) for arg in selection.field.arguments))
            try:
                values = validate_typed_argument_values(nested_model, dict(supplied.get(path, {})), schema=schema)
            except GraphQLArgumentValueError as exc:
                raise GraphQLOperationMaterializationError(f"Nested field {path} requires unbound arguments or has invalid arguments: {exc}") from exc
            items = []
            for spec in sorted(nested_model.arguments, key=lambda item: item.name):
                if spec.name in values:
                    variable = f"v{offset + len(bindings) + 1}"
                    bindings.append(GraphQLArgumentBinding(spec.name, spec.type_name, variable))
                    variables[variable] = values[spec.name]
                    items.append((spec.name, variable))
            by_path[path] = tuple(items)
        return tuple(bindings), variables, by_path

    @staticmethod
    def _selection(
        schema: GraphQLSchema,
        model: GraphQLQueryComposerModel,
        binding: GraphQLOperationBinding,
        nested_by_path: Mapping[str, tuple[tuple[str, str], ...]],
    ) -> tuple[
        GraphQLSelectionTree,
        tuple[GraphQLResultBinding, ...],
    ]:
        root_type = model.root_return_type

        if root_type.kind in {
            "SCALAR",
            "ENUM",
        }:
            if binding.selected_paths:
                raise GraphQLOperationMaterializationError(
                    "Scalar/enum root must not contain "
                    "selected child paths."
                )

            return (
                GraphQLSelectionTree(),
                (
                    GraphQLResultBinding(
                        selection_path=(),
                        protocol_field_node_ids=(),
                        response_path=(
                            "data",
                            binding.root_alias,
                        ),
                        graphql_type=(
                            model.root_field.type_ref.render()
                        ),
                    ),
                ),
            )

        if root_type.kind not in {
            "OBJECT",
            "INTERFACE",
            "UNION",
        }:
            raise GraphQLOperationMaterializationError(
                "Unsupported GraphQL operation return kind: "
                f"{root_type.kind}."
            )

        if not binding.selected_paths:
            raise GraphQLOperationMaterializationError(
                "Object/interface operation requires "
                "explicit selected_paths."
            )

        by_path = {
            item.path: item
            for item in model.selectable_fields
        }

        stable_paths = []
        result_bindings = []

        for raw_path in sorted(
            binding.selected_paths
        ):
            path = tuple(
                part
                for part in raw_path.split(".")
                if part
            )

            leaf = by_path.get(
                path
            )

            if leaf is None:
                raise GraphQLOperationMaterializationError(
                    "Selected GraphQL path is not available: "
                    f"{raw_path}"
                )

            leaf_type_name = (
                leaf.field.type_ref.named_type()
            )

            leaf_type = (
                schema.type_named(
                    leaf_type_name
                )
                if leaf_type_name is not None
                else None
            )

            if (
                leaf_type is None
                or leaf_type.kind
                not in {
                    "SCALAR",
                    "ENUM",
                }
            ):
                raise GraphQLOperationMaterializationError(
                    "Selected GraphQL result path must "
                    "terminate in a scalar or enum field: "
                    f"{raw_path}"
                )

            segments = []
            protocol_ids = []

            for depth in range(
                1,
                len(path) + 1,
            ):
                prefix = path[:depth]
                selection = by_path.get(
                    prefix
                )

                if selection is None:
                    raise GraphQLOperationMaterializationError(
                        "Selected GraphQL path has an "
                        "unresolvable prefix: "
                        f"{'.'.join(prefix)}"
                    )

                protocol_node_id = (
                    f"{'fragment' if selection.field.name.startswith('@') else 'field'}:"
                    f"{selection.parent_type_name}."
                    f"{selection.field.name}"
                )

                protocol_ids.append(
                    protocol_node_id
                )

                segments.append(
                    GraphQLSelectionPathSegment(
                        protocol_node_id=(
                            protocol_node_id
                        ),
                        field_name=(
                            '... on ' + selection.field.name[1:] if selection.field.name.startswith('@') else selection.field.name
                        ),
                        arguments=nested_by_path.get('.'.join(prefix), ()),
                    )
                )

                if selection.field.name.startswith('@'):
                    stable_paths.append(tuple(segments[:-1]) + (GraphQLSelectionPathSegment(
                        protocol_node_id=f'field:{selection.parent_type_name}.__typename', field_name='__typename'),))

            stable_paths.append(
                tuple(segments)
            )

            result_bindings.append(
                GraphQLResultBinding(
                    selection_path=path,
                    protocol_field_node_ids=(
                        tuple(protocol_ids)
                    ),
                    response_path=(
                        "data",
                        binding.root_alias,
                        *(part for part in path if not part.startswith('@')),
                    ),
                    graphql_type=(
                        leaf.field.type_ref.render()
                    ),
                )
            )

        try:
            tree = build_stable_selection_tree(
                stable_paths
            )
        except GraphQLSelectionSetError as exc:
            raise GraphQLOperationMaterializationError(
                str(exc)
            ) from exc

        return (
            tree,
            tuple(
                sorted(
                    result_bindings,
                    key=lambda item: (
                        item.selection_path
                    ),
                )
            ),
        )

    @staticmethod
    def _render(
        binding: GraphQLOperationBinding,
        root_field: GraphQLField,
        arguments: tuple[
            GraphQLArgumentBinding,
            ...,
        ],
        selection_tree: GraphQLSelectionTree,
        nested_arguments: tuple[GraphQLArgumentBinding, ...] = (),
    ) -> str:
        declarations = ", ".join(
            (
                f"${item.variable_name}: "
                f"{item.graphql_type}"
            )
            for item in (*arguments, *nested_arguments)
        )

        argument_values = ", ".join(
            (
                f"{item.argument_name}: "
                f"${item.variable_name}"
            )
            for item in arguments
        )

        signature = (
            f"({declarations})"
            if declarations
            else ""
        )

        call_arguments = (
            f"({argument_values})"
            if argument_values
            else ""
        )

        selection = (
            selection_tree.render()
        )

        selection_suffix = (
            f" {selection}"
            if selection
            else ""
        )

        keyword = (
            binding.operation_kind.value
        )

        return (
            f"{keyword} "
            f"{binding.operation_name}"
            f"{signature} {{\n"
            f"  {binding.root_alias}: "
            f"{root_field.name}"
            f"{call_arguments}"
            f"{selection_suffix}\n"
            "}"
        )

    @staticmethod
    def _canonical_variables(
        variables: Mapping[str, object],
    ) -> str:
        try:
            return json.dumps(
                dict(variables),
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except (
            TypeError,
            ValueError,
            RecursionError,
        ) as exc:
            raise GraphQLOperationMaterializationError(
                "GraphQL variables must be "
                "JSON serializable."
            ) from exc

    @staticmethod
    def _request_fingerprint(
        binding: GraphQLOperationBinding,
        document: str,
        canonical_variables: str,
        result_bindings: tuple[
            GraphQLResultBinding,
            ...,
        ],
        request_scope: str | None,
    ) -> str:
        canonical = json.dumps(
            {
                "binding_id": binding.binding_id,
                "schema_fingerprint": (
                    binding.schema_fingerprint
                ),
                "operation_node_id": (
                    binding.operation_node_id
                ),
                "operation_kind": (
                    binding.operation_kind.value
                ),
                "operation_name": (
                    binding.operation_name
                ),
                "root_alias": binding.root_alias,
                "document": document,
                "variables": json.loads(
                    canonical_variables
                ),
                "result_paths": [
                    {
                        "selection_path": (
                            list(
                                item.selection_path
                            )
                        ),
                        "protocol_field_node_ids": (
                            list(
                                item.protocol_field_node_ids
                            )
                        ),
                        "response_path": (
                            list(
                                item.response_path
                            )
                        ),
                        "graphql_type": (
                            item.graphql_type
                        ),
                    }
                    for item
                    in result_bindings
                ],
                "request_scope": request_scope,
            },
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )

        return hashlib.sha256(
            canonical.encode("utf-8")
        ).hexdigest()


__all__ = [
    "GraphQLOperationMaterializationError",
    "GraphQLOperationBinding",
    "GraphQLArgumentBinding",
    "GraphQLResultBinding",
    "GraphQLOperationRequest",
    "GraphQLDocumentRequest",
    "GraphQLQueryBatchRequest",
    "GraphQLOperationMaterializer",
]
