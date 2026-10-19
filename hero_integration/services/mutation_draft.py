"""Local-only validation for HERO mutation drafts against a saved schema."""
from __future__ import annotations

import re


_MUTATION = re.compile(r"^\s*mutation(?:\s+[_A-Za-z][_0-9A-Za-z]*)?(?:\s*\([^)]*\))?\s*\{\s*([_A-Za-z][_0-9A-Za-z]*)")
_OPERATION = re.compile(r"\b(query|mutation|subscription)\b", re.IGNORECASE)
_STRING_LITERAL = re.compile(r'"')


def operation_kind(document: str) -> str:
    """Return the sole top-level GraphQL operation kind without parsing variables as secrets."""
    if not isinstance(document, str):
        raise ValueError("The GraphQL document must be text.")
    anonymous_query = document.lstrip().startswith("{")
    operations = (["query"] if anonymous_query else []) + _top_level_operations(document)
    if len(operations) != 1:
        raise ValueError("A draft must contain exactly one GraphQL operation.")
    return operations[0]


def validate(schema: dict[str, object], document: str) -> dict[str, object]:
    if operation_kind(document) != "mutation":
        raise ValueError("A local mutation draft must contain exactly one mutation operation.")
    # Draft values would be persisted in the GraphQL document.  Require
    # parameter variables instead; their values stay in the browser and are
    # deliberately absent from the DocType.
    if _STRING_LITERAL.search(document):
        raise ValueError("Mutation drafts must not contain literal values.")
    match = _MUTATION.match(document)
    if not match:
        raise ValueError("A local mutation draft must start with a GraphQL mutation operation.")
    body = schema.get("__schema") or schema.get("schema") or schema
    if not isinstance(body, dict):
        raise ValueError("The saved schema is invalid.")
    mutation_type = body.get("mutationType") or body.get("mutation_type") or {}
    root_name = mutation_type.get("name") if isinstance(mutation_type, dict) else ""
    types = body.get("types") if isinstance(body.get("types"), list) else []
    root = next((item for item in types if isinstance(item, dict) and item.get("name") == root_name), None)
    if not isinstance(root, dict):
        raise ValueError("The saved schema has no mutation root.")
    operation = match.group(1)
    field = next((item for item in root.get("fields", []) if isinstance(item, dict) and item.get("name") == operation), None)
    if not isinstance(field, dict):
        raise ValueError("The mutation operation does not exist in the saved schema.")
    return {"operation": operation, "root": root_name, "return_type": _type_name(field.get("type"))}


def _top_level_operations(document: str) -> list[str]:
    """Find operation keywords at GraphQL document depth zero.

    GraphQL field names can legitimately be called ``query`` or ``mutation``;
    only top-level keywords identify operations.  Strings and comments are
    skipped so a user-visible description cannot change the draft kind.
    """
    operations: list[str] = []
    depth = 0
    previous_nonspace = ""
    index = 0
    length = len(document)
    while index < length:
        char = document[index]
        if char == "#":
            newline = document.find("\n", index + 1)
            index = length if newline < 0 else newline + 1
            continue
        if char == '"':
            index = _skip_string(document, index)
            continue
        if char == "{":
            depth += 1
            previous_nonspace = char
            index += 1
            continue
        if char == "}":
            depth -= 1
            previous_nonspace = char
            index += 1
            continue
        if depth == 0 and not previous_nonspace:
            match = _OPERATION.match(document, index)
            if match:
                operations.append(match.group(1).lower())
                previous_nonspace = match.group(0)[-1]
                index = match.end()
                continue
        elif depth == 0 and previous_nonspace == "}":
            match = _OPERATION.match(document, index)
            if match:
                operations.append(match.group(1).lower())
                previous_nonspace = match.group(0)[-1]
                index = match.end()
                continue
        if not char.isspace():
            previous_nonspace = char
        index += 1
    return operations


def _skip_string(document: str, index: int) -> int:
    triple_quoted = document.startswith('"""', index)
    delimiter = '"""' if triple_quoted else '"'
    index += len(delimiter)
    while index < len(document):
        if document.startswith(delimiter, index):
            return index + len(delimiter)
        if document[index] == "\\" and not triple_quoted:
            index += 2
        else:
            index += 1
    raise ValueError("The GraphQL document contains an unterminated string.")


def _type_name(type_ref: object) -> str:
    current = type_ref
    while isinstance(current, dict) and current.get("ofType"):
        current = current["ofType"]
    return str(current.get("name") or current.get("kind") or "unknown") if isinstance(current, dict) else "unknown"
