"""Dynamic operation catalog and explainable name-only mapping suggestions."""
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
    """Build an unsaved GraphQL service draft for one saved-schema operation node.

    Only required arguments become variables; their values are never part of
    the document.  Composite results select ``__typename`` only, which every
    object, interface and union provides, so no schema field is invented.
    """
    match = _OPERATION_NODE.match(node_id or "")
    if not match:
        raise SchemaOperationError("Select a Query or Mutation operation.")
    kind, field_name = match.groups()
    operations = {item["operation_name"]: item for item in catalog(snapshot, kind)}
    if field_name not in operations:
        raise SchemaOperationError("The operation does not exist in the saved schema.")
    schema = normalize_schema(snapshot)
    types = {item.get("name"): item for item in schema.get("types") or [] if isinstance(item, Mapping)}
    root = types.get(operations[field_name]["root_type"]) or {}
    field = next(item for item in root.get("fields") or [] if isinstance(item, Mapping) and item.get("name") == field_name)
    required = [arg for arg in field.get("args") or []
                if isinstance(arg, Mapping) and is_required(arg.get("type")) and arg.get("defaultValue") is None]
    target, _ = _named_type(field.get("type"))
    composite = (types.get(target) or {}).get("kind") in {"OBJECT", "INTERFACE", "UNION"}
    operation_name = "".join(part[:1].upper() + part[1:] for part in re.split(r"_+", field_name) if part) or "Operation"
    definitions = ", ".join(f"${arg['name']}: {type_reference(arg.get('type'))}" for arg in required)
    arguments = ", ".join(f"{arg['name']}: ${arg['name']}" for arg in required)
    document = (f"{kind} {operation_name}{f'({definitions})' if definitions else ''} {{\n"
                f"  {field_name}{f'({arguments})' if arguments else ''}{' { __typename }' if composite else ''}\n}}")
    return {
        "operation_kind": kind,
        "operation_name": field_name,
        "return_type": type_reference(field.get("type")),
        "required_variables": [{"name": arg["name"], "type": type_reference(arg.get("type"))} for arg in required],
        "payload": {"name": field_name, "operation_name": operation_name, "query": document, "variables": {}},
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


def _argument(argument: Mapping) -> dict[str, object]:
    details = _argument_details(argument)
    return {key: details[key] for key in ("name", "type", "required")}


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


def suggest(source: str, candidates: list[str]) -> list[dict[str, object]]:
    """Explain name affinity only; callers must keep all results as drafts."""
    source_tokens = set(_tokens(source))
    result = []
    for candidate in candidates:
        overlap = source_tokens & set(_tokens(candidate))
        score = len(overlap) / max(len(source_tokens | set(_tokens(candidate))), 1)
        if score:
            result.append({"candidate": candidate, "confidence": round(score, 2), "reason": "shared normalized names: " + ", ".join(sorted(overlap))})
    return sorted(result, key=lambda item: (-item["confidence"], item["candidate"]))


def type_reference(ref: object) -> str:
    if not isinstance(ref, Mapping):
        return "Unknown"
    if ref.get("kind") == "NON_NULL": return f"{type_reference(ref.get('ofType'))}!"
    if ref.get("kind") == "LIST": return f"[{type_reference(ref.get('ofType'))}]"
    return str(ref.get("name") or ref.get("kind") or "Unknown")


def is_required(ref: object) -> bool:
    return isinstance(ref, Mapping) and ref.get("kind") == "NON_NULL"


def _tokens(value: str) -> tuple[str, ...]:
    return tuple(token.lower() for token in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?=[A-Z]|$)|\d+", value))
