"""Saved-schema operation catalog and materializer-backed service drafts."""
from __future__ import annotations

import re
from collections.abc import Mapping

from hero_integration.services.graphql_graph import GraphSnapshotError, normalize_schema


class SchemaOperationError(ValueError):
    pass


MAX_INPUT_FIELD_DEPTH = 4


def catalog(snapshot: object, operation_kind: str) -> list[dict[str, object]]:
    kind = operation_kind.upper()
    root_key = {"QUERY": "queryType", "MUTATION": "mutationType"}.get(kind)
    if not root_key:
        raise SchemaOperationError("Operation kind must be QUERY or MUTATION.")
    try:
        schema = normalize_schema(snapshot)
    except GraphSnapshotError as error:
        raise SchemaOperationError("Invalid saved GraphQL schema.") from error
    root = schema.get(root_key)
    root_name = root.get("name") if isinstance(root, Mapping) else ""
    types = {item.get("name"): item for item in schema.get("types") or [] if isinstance(item, Mapping)}
    root_type = types.get(root_name)
    if not isinstance(root_type, Mapping):
        return []
    return [
        _operation(kind, root_name, field, types)
        for field in root_type.get("fields") or []
        if isinstance(field, Mapping) and isinstance(field.get("name"), str)
    ]


_OPERATION_NODE = re.compile(r"^operation:(query|mutation):([_A-Za-z][_0-9A-Za-z]*)$")


def operation_service_draft(snapshot: object, node_id: str) -> dict[str, object]:
    """Build an unsaved service Draft for one saved-schema operation node.

    Only required arguments become variables; their values stay outside the
    query document. Composite results select the standard __typename field.
    """
    match = _OPERATION_NODE.match(node_id or "")
    if not match:
        raise SchemaOperationError("Select a Query or Mutation operation.")
    kind, field_name = match.groups()
    operations = {item["operation_name"]: item for item in catalog(snapshot, kind)}
    if field_name not in operations:
        raise SchemaOperationError("The operation does not exist in the saved schema.")
    from graphql import parse, print_ast
    from hero_integration.services.graphql_schema_cache import compiled_schema
    from hero_integration.graphql_protocol.graphql_operation_materializer import GraphQLOperationMaterializer
    try:
        schema = compiled_schema(snapshot).core
        document = GraphQLOperationMaterializer.operation_template(schema, kind, field_name)
    except (TypeError, ValueError) as error:
        raise SchemaOperationError('The operation cannot be materialized from the saved schema.') from error
    operation = parse(document).definitions[0]
    return {
        "operation_kind": kind,
        "operation_name": field_name,
        "return_type": operations[field_name]['return_type'],
        "required_variables": [{"name": variable.variable.name.value, "type": print_ast(variable.type)}
                               for variable in operation.variable_definitions],
        "payload": {"name": field_name, "operation_name": operation.name.value, "query": document, "variables": {}},
    }


def _operation(kind: str, root_name: str, field: Mapping, types: dict[object, Mapping]) -> dict[str, object]:
    argument_details = [_argument_details(arg) for arg in field.get("args") or [] if isinstance(arg, Mapping)]
    arguments = [{key: item[key] for key in ("name", "type", "required")} for item in argument_details]
    return {
        "operation_kind": kind,
        "operation_name": field["name"],
        "root_type": root_name,
        "return_type": type_reference(field.get("type")),
        "arguments": arguments,
        "input_fields": {
            str(argument["name"]): _input_fields(types, argument.get("input_type", ""), argument.get("reference"))
            for argument in argument_details
            if isinstance(argument.get("name"), str)
        },
    }


def _argument_details(argument: Mapping) -> dict[str, object]:
    reference = argument.get("type")
    named_type, named_kind = _named_type(reference)
    return {
        "name": argument.get("name"),
        "type": type_reference(reference),
        "required": is_required(reference),
        "input_type": named_type if named_kind == "INPUT_OBJECT" else "",
        "reference": reference,
    }


def _input_fields(types: dict[object, Mapping], input_type: str, reference: object) -> list[dict[str, object]]:
    """Expose saved input leaves for inspection without implying execution support."""
    if _has_list(reference):
        return [{
            "path": "", "type": type_reference(reference), "required": is_required(reference),
            "kind": "LIST", "input_type": input_type, "selectable": False,
            "reason": "List input requires an approved collection resolver before it can be mapped.",
        }]
    root = types.get(input_type)
    if not isinstance(root, Mapping) or root.get("kind") != "INPUT_OBJECT":
        named_type, named_kind = _named_type(reference)
        return [{
            "path": "", "type": type_reference(reference), "required": is_required(reference),
            "kind": named_kind, "input_type": named_type, "selectable": named_kind in {"SCALAR", "ENUM"},
            "reason": "Direct scalar or enum argument." if named_kind in {"SCALAR", "ENUM"} else "Unsupported argument shape.",
        }]
    return _walk_input_fields(types, root, "", set(), 0)


def _walk_input_fields(
    types: dict[object, Mapping], current: Mapping, prefix: str, visited: set[str], depth: int,
) -> list[dict[str, object]]:
    name = str(current.get("name") or "")
    if depth >= MAX_INPUT_FIELD_DEPTH or name in visited:
        return []
    result: list[dict[str, object]] = []
    for field in current.get("inputFields") or []:
        if not isinstance(field, Mapping) or not isinstance(field.get("name"), str):
            continue
        reference = field.get("type")
        path = f"{prefix}.{field['name']}" if prefix else field["name"]
        named_type, named_kind = _named_type(reference)
        is_list = _has_list(reference)
        item = {
            "path": path,
            "type": type_reference(reference),
            "required": is_required(reference),
            "kind": named_kind,
            "input_type": named_type if named_kind == "INPUT_OBJECT" else "",
            "selectable": named_kind in {"SCALAR", "ENUM"} and not is_list,
            "reason": "Direct scalar or enum input field." if named_kind in {"SCALAR", "ENUM"} and not is_list else "",
        }
        if is_list:
            item["selectable"] = False
            item["reason"] = "List input requires an approved collection resolver before it can be mapped."
        elif named_kind == "INPUT_OBJECT":
            item["selectable"] = False
            item["reason"] = "Input object: choose a nested scalar or enum field."
        elif named_kind not in {"SCALAR", "ENUM"}:
            item["selectable"] = False
            item["reason"] = "Unsupported input field type."
        result.append(item)
        nested = types.get(named_type)
        if isinstance(nested, Mapping) and named_kind == "INPUT_OBJECT" and not is_list:
            result.extend(_walk_input_fields(types, nested, path, visited | {name}, depth + 1))
    return result


def _named_type(reference: object) -> tuple[str, str]:
    current = reference
    while isinstance(current, Mapping):
        name = current.get("name")
        if isinstance(name, str) and name:
            return name, str(current.get("kind") or "")
        current = current.get("ofType")
    return "", ""


def _has_list(reference: object) -> bool:
    current = reference
    while isinstance(current, Mapping):
        if current.get("kind") == "LIST":
            return True
        current = current.get("ofType")
    return False


def type_reference(ref: object) -> str:
    if not isinstance(ref, Mapping):
        return "Unknown"
    if ref.get("kind") == "NON_NULL": return f"{type_reference(ref.get('ofType'))}!"
    if ref.get("kind") == "LIST": return f"[{type_reference(ref.get('ofType'))}]"
    return str(ref.get("name") or ref.get("kind") or "Unknown")


def is_required(ref: object) -> bool:
    return isinstance(ref, Mapping) and ref.get("kind") == "NON_NULL"
