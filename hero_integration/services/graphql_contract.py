"""Small strict GraphQL mutation contract parser and JSON variable validator.

This module is intentionally standalone: a saved snapshot is the only schema
authority, and no document or variable is sent to a remote endpoint here.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field

from hero_integration.services.graphql_graph import GraphSnapshotError, normalize_schema
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


def parse_mutation(document: str) -> MutationContract:
    tokens = _tokenize(document)
    stream = _Stream(tokens)
    stream.expect_name("mutation")
    operation_name = stream.take_name(optional=True) or ""
    variables = _variables(stream)
    stream.expect("{")
    root_field = stream.take_name()
    argument_variables = _root_arguments(stream)
    _skip_field_tail(stream)
    stream.expect("}")
    if not stream.finished:
        raise GraphQLContractError("A mutation contract must contain exactly one operation and root field.")
    canonical = json.dumps(
        {"document": document.strip(), "operation_name": operation_name, "root_field": root_field, "variables": variables},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return MutationContract(
        operation_name=operation_name,
        root_field=root_field,
        variable_types=variables,
        fingerprint=hashlib.sha256(canonical.encode()).hexdigest(),
        argument_variables=argument_variables,
    )


def validate_mutation(schema_snapshot: object, contract: MutationContract, variables: object) -> dict[str, object]:
    """Validate a single mutation operation and its JSON variables against a snapshot."""
    if not isinstance(variables, Mapping):
        raise GraphQLContractError("Mutation variables must be a JSON object.")
    try:
        schema = normalize_schema(schema_snapshot)
    except GraphSnapshotError as error:
        raise GraphQLContractError("The selected GraphQL schema is invalid.") from error
    types = _types(schema)
    root_name = _root_name(schema.get("mutationType"))
    root = types.get(root_name)
    if not root or root.get("kind") != "OBJECT":
        raise GraphQLContractError("The selected schema has no mutation root.")
    fields = {item.get("name"): item for item in root.get("fields") or [] if isinstance(item, Mapping)}
    field = fields.get(contract.root_field)
    if not isinstance(field, Mapping):
        raise GraphQLContractError("The selected mutation operation does not exist in the saved schema.")
    arguments = {item.get("name"): item for item in field.get("args") or [] if isinstance(item, Mapping)}
    for name, variable in contract.argument_variables.items():
        if name not in arguments or variable not in contract.variable_types:
            raise GraphQLContractError('Mutation argument or bound variable is unknown.')
        if not _compatible_variable_type(contract.variable_types[variable], arguments[name].get('type')):
            raise GraphQLContractError('Mutation variable type does not match its schema argument.')
    unknown = sorted(set(variables) - set(contract.variable_types))
    if unknown:
        raise GraphQLContractError("Mutation contains undeclared variables: {0}", ", ".join(unknown))
    validated: dict[str, object] = {}
    for name, ref in contract.variable_types.items():
        if name not in variables:
            if _non_null(ref):
                raise GraphQLContractError('Required GraphQL variable ${0} is missing.', name)
            continue
        validated[name] = _validate_value(variables[name], ref, types, f"${name}")
    for argument in arguments.values():
        argument_name = str(argument.get("name") or "")
        ref = argument.get("type")
        variable = contract.argument_variables.get(argument_name)
        if _non_null(ref) and (not variable or variable not in validated) and argument.get("defaultValue") is None:
            raise GraphQLContractError('Required mutation argument {0!r} is not supplied by a declared variable.', argument_name)
        if variable and variable in validated:
            _validate_value(validated[variable], ref, types, f'argument {argument_name}')
    return validated


def _root_arguments(stream: "_Stream") -> dict[str, str]:
    """Retain root variable bindings; variable defaults are not evaluated here."""
    result = {}
    if stream.accept('('):
        while not stream.accept(')'):
            name = stream.take_name()
            if name in result:
                raise GraphQLContractError('Duplicate mutation argument.')
            stream.expect(':')
            stream.expect('$')
            result[name] = stream.take_name()
    return result


def _compatible_variable_type(variable, argument):
    if not isinstance(variable, Mapping) or not isinstance(argument, Mapping):
        return False
    if argument.get('kind') == 'NON_NULL':
        return variable.get('kind') == 'NON_NULL' and _compatible_variable_type(variable.get('ofType'), argument.get('ofType'))
    if variable.get('kind') == 'NON_NULL':
        return _compatible_variable_type(variable.get('ofType'), argument)
    if argument.get('kind') == 'LIST':
        return variable.get('kind') == 'LIST' and _compatible_variable_type(variable.get('ofType'), argument.get('ofType'))
    return variable.get('kind') == 'NAMED' and variable.get('name') == argument.get('name')


def _variables(stream: "_Stream") -> dict[str, dict[str, object]]:
    if not stream.accept("("):
        return {}
    result: dict[str, dict[str, object]] = {}
    while not stream.accept(")"):
        stream.expect("$")
        name = stream.take_name()
        if name in result:
            raise GraphQLContractError('Duplicate GraphQL variable ${0}.', name)
        stream.expect(":")
        result[name] = _type_ref(stream)
        if stream.accept("="):
            _skip_value(stream)
        stream.accept(",")
    return result


def _type_ref(stream: "_Stream") -> dict[str, object]:
    if stream.accept("["):
        ref: dict[str, object] = {"kind": "LIST", "name": None, "ofType": _type_ref(stream)}
        stream.expect("]")
    else:
        ref = {"kind": "NAMED", "name": stream.take_name(), "ofType": None}
    if stream.accept("!"):
        ref = {"kind": "NON_NULL", "name": None, "ofType": ref}
    return ref


def _skip_field_tail(stream: "_Stream") -> None:
    if stream.accept("("):
        _skip_balanced(stream, "(", ")")
    if stream.accept("{"):
        _skip_balanced(stream, "{", "}")
    if stream.peek() != "}":
        raise GraphQLContractError("A mutation contract must select exactly one root field.")


def _skip_value(stream: "_Stream") -> None:
    token = stream.take()
    if token in {"[", "{"}:
        _skip_balanced(stream, token, "]" if token == "[" else "}")


def _skip_balanced(stream: "_Stream", opening: str, closing: str) -> None:
    depth = 1
    while depth:
        item = stream.take()
        if item == opening:
            depth += 1
        elif item == closing:
            depth -= 1


def _types(schema: dict[str, object]) -> dict[str, Mapping[str, object]]:
    values = schema.get("types")
    if not isinstance(values, list):
        raise GraphQLContractError("The selected GraphQL schema has no type list.")
    return {str(item["name"]): item for item in values if isinstance(item, Mapping) and isinstance(item.get("name"), str)}


def _validate_value(value: object, ref: object, types: Mapping[str, Mapping[str, object]], path: str) -> object:
    if not isinstance(ref, Mapping):
        raise GraphQLContractError('Invalid type for {0}.', path)
    kind = ref.get("kind")
    if kind == "NON_NULL":
        if value is None:
            raise GraphQLContractError('{0} must not be null.', path)
        return _validate_value(value, ref.get("ofType"), types, path)
    if value is None:
        return None
    if kind == "LIST":
        if not isinstance(value, list):
            raise GraphQLContractError('{0} must be a JSON array.', path)
        return [_validate_value(item, ref.get("ofType"), types, f"{path}[{index}]") for index, item in enumerate(value)]
    name = str(ref.get("name") or "")
    if name == "Int" and (not isinstance(value, int) or isinstance(value, bool)):
        raise GraphQLContractError('{0} must be an Int.', path)
    if name == "Float" and (not isinstance(value, (int, float)) or isinstance(value, bool)):
        raise GraphQLContractError('{0} must be a Float.', path)
    if name == "Boolean" and not isinstance(value, bool):
        raise GraphQLContractError('{0} must be a Boolean.', path)
    if name == "String" and not isinstance(value, str):
        raise GraphQLContractError('{0} must be a String.', path)
    if name == "ID" and (not isinstance(value, (str, int)) or isinstance(value, bool)):
        raise GraphQLContractError('{0} must be an ID.', path)
    item = types.get(name)
    if not item:
        return value  # custom scalar: JSON-safe transport value is the only local claim.
    if item.get("kind") == "ENUM":
        allowed = {entry.get("name") for entry in item.get("enumValues") or [] if isinstance(entry, Mapping)}
        if not isinstance(value, str) or value not in allowed:
            raise GraphQLContractError('{0} must be a valid {1} enum value.', path, name)
    if item.get("kind") == "INPUT_OBJECT":
        if not isinstance(value, Mapping):
            raise GraphQLContractError('{0} must be a JSON object.', path)
        fields = {entry.get("name"): entry for entry in item.get("inputFields") or [] if isinstance(entry, Mapping)}
        unknown = sorted(set(value) - set(fields))
        if unknown:
            raise GraphQLContractError("{0} contains unknown input field(s): {1}", path, ", ".join(unknown))
        return {
            str(field): _validate_value(value[field], fields[field].get("type"), types, f"{path}.{field}")
            for field in fields if field in value
        } | _required_input_fields(value, fields, path)
    return value


def _required_input_fields(value: Mapping[str, object], fields: Mapping[object, Mapping[str, object]], path: str) -> dict[str, object]:
    for field, item in fields.items():
        if field not in value and _non_null(item.get("type")) and item.get("defaultValue") is None:
            raise GraphQLContractError('Required input field {0}.{1} is missing.', path, field)
    return {}


def _non_null(ref: object) -> bool:
    return isinstance(ref, Mapping) and ref.get("kind") == "NON_NULL"


def _root_name(value: object) -> str:
    return str(value.get("name") or "") if isinstance(value, Mapping) else ""


def _tokenize(text: object) -> tuple[str, ...]:
    if not isinstance(text, str) or not text.strip():
        raise GraphQLContractError("GraphQL mutation document must be non-empty text.")
    tokens: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char.isspace() or char == ",":
            index += 1; continue
        if char == "#":
            index = text.find("\n", index)
            if index < 0: break
            continue
        if char == '"':
            end = index + 1
            while end < len(text):
                if text[end] == "\\": end += 2; continue
                if text[end] == '"': break
                end += 1
            if end >= len(text): raise GraphQLContractError("Unterminated GraphQL string.")
            tokens.append(text[index:end + 1]); index = end + 1; continue
        if char.isalpha() or char == "_":
            end = index + 1
            while end < len(text) and (text[end].isalnum() or text[end] == "_"): end += 1
            tokens.append(text[index:end]); index = end; continue
        if char in "!$():={}[]@":
            tokens.append(char); index += 1; continue
        if char.isdigit() or char == "-":
            end = index + 1
            while end < len(text) and (text[end].isdigit() or text[end] in ".eE+-"): end += 1
            tokens.append(text[index:end]); index = end; continue
        raise GraphQLContractError('Unsupported GraphQL character {0!r}.', char)
    return tuple(tokens)


class _Stream:
    def __init__(self, tokens: tuple[str, ...]): self.tokens, self.index = tokens, 0
    @property
    def finished(self) -> bool: return self.index >= len(self.tokens)
    def peek(self) -> str: return self.tokens[self.index] if not self.finished else ""
    def take(self) -> str:
        if self.finished: raise GraphQLContractError("Unexpected end of GraphQL document.")
        result = self.tokens[self.index]; self.index += 1; return result
    def accept(self, expected: str) -> bool:
        if self.peek() == expected: self.index += 1; return True
        return False
    def expect(self, expected: str) -> None:
        if not self.accept(expected): raise GraphQLContractError('Expected {0!r} in GraphQL document.', expected)
    def take_name(self, optional: bool = False) -> str:
        value = self.peek()
        if value and (value[0].isalpha() or value[0] == "_"):
            self.index += 1; return value
        if optional: return ""
        raise GraphQLContractError("Expected GraphQL name.")
    def expect_name(self, expected: str) -> None:
        if self.take_name() != expected: raise GraphQLContractError('Expected GraphQL {0} operation.', expected)
