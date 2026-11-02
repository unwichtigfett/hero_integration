"""Validate one read-only query Draft against stored introspection, without I/O.

Supports optional operation names, variables, aliases, arguments, inline
fragments and @include/@skip. Named fragments/subscriptions are rejected.
This editor subset does not validate inline-fragment type overlap.
"""
from __future__ import annotations

import json
from collections.abc import Mapping

from hero_integration.services.graphql_contract import (
    GraphQLContractError,
    _Stream,
    _compatible_variable_type,
    _non_null,
    _type_ref,
    _validate_value,
)
from hero_integration.services.graphql_graph import GraphSnapshotError, normalize_schema
from hero_integration.services.localized_error import LocalizedError

MAX_DEPTH = 12
MAX_FIELDS = 500
_LEAF_KINDS = {"SCALAR", "ENUM"}
_COMPOSITE_KINDS = {"OBJECT", "INTERFACE", "UNION"}
_INPUT_KINDS = {"SCALAR", "ENUM", "INPUT_OBJECT"}
_BUILTIN_INPUT = {"Int", "Float", "String", "Boolean", "ID"}


class QueryDraftError(LocalizedError):
    pass


def _tokens(text: object) -> tuple[str, ...]:
    if not isinstance(text, str) or not text.strip():
        raise QueryDraftError("The GraphQL query must be non-empty text.")
    tokens: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char.isspace() or char == ",":
            index += 1
            continue
        if char == "#":
            end = text.find("\n", index)
            index = len(text) if end < 0 else end + 1
            continue
        if text.startswith("...", index):
            tokens.append("...")
            index += 3
            continue
        if char == '"':
            if text.startswith('"""', index):
                end = text.find('"""', index + 3)
                if end < 0:
                    raise QueryDraftError("Unterminated GraphQL block string.")
                tokens.append(json.dumps(text[index + 3:end]))
                index = end + 3
                continue
            end = index + 1
            while end < len(text) and text[end] != '"':
                end += 2 if text[end] == "\\" else 1
            if end >= len(text):
                raise QueryDraftError("Unterminated GraphQL string.")
            tokens.append(text[index:end + 1])
            index = end + 1
            continue
        if char.isalpha() or char == "_":
            end = index + 1
            while end < len(text) and (text[end].isalnum() or text[end] == "_"):
                end += 1
            tokens.append(text[index:end])
            index = end
            continue
        if char in "!$():={}[]@":
            tokens.append(char)
            index += 1
            continue
        if char.isdigit() or char == "-":
            end = index + 1
            while end < len(text) and (text[end].isdigit() or text[end] in ".eE+-"):
                end += 1
            tokens.append(text[index:end])
            index = end
            continue
        raise QueryDraftError('Unsupported GraphQL character {0!r}.', char)
    return tuple(tokens)


def _named(ref: object) -> str:
    while isinstance(ref, Mapping):
        if ref.get("kind") not in {"NON_NULL", "LIST"}:
            return str(ref.get("name") or "")
        ref = ref.get("ofType")
    return ""


def _label(ref: object) -> str:
    if not isinstance(ref, Mapping):
        return "Unknown"
    if ref.get("kind") == "NON_NULL":
        return _label(ref.get("ofType")) + "!"
    if ref.get("kind") == "LIST":
        return "[" + _label(ref.get("ofType")) + "]"
    return str(ref.get("name") or "Unknown")


class _Validator:
    def __init__(self, schema: dict[str, object], document: str):
        try:
            body = normalize_schema(schema)
        except GraphSnapshotError as error:
            raise QueryDraftError("The saved schema is invalid.") from error
        self.types = {str(item["name"]): item for item in body.get("types", [])
                      if isinstance(item, Mapping) and isinstance(item.get("name"), str)}
        root = body.get("queryType")
        self.root = str(root.get("name") or "") if isinstance(root, Mapping) else ""
        if self.root not in self.types:
            raise QueryDraftError("The saved schema has no query root type.")
        try:
            self.stream = _Stream(_tokens(document))
        except GraphQLContractError as error:
            raise QueryDraftError.from_error(error) from error
        self.variables: dict[str, dict[str, object]] = {}
        self.defaults: set[str] = set()
        self.used: set[str] = set()
        self.fields = 0
        self.root_fields: list[str] = []

    def run(self) -> dict[str, object]:
        stream, name = self.stream, ""
        try:
            if stream.peek() != "{":
                keyword = stream.take_name()
                if keyword in {"mutation", "subscription"}:
                    raise QueryDraftError("GraphQL Services queries must not contain mutations or subscriptions.")
                if keyword != "query":
                    raise QueryDraftError("Only one GraphQL query operation is supported; fragments are not.")
                name = stream.take_name(optional=True)
                self._variable_definitions()
            self._selection_set(self.root, 1, root=True)
            if not stream.finished:
                raise QueryDraftError("A query service must contain exactly one GraphQL operation.")
        except GraphQLContractError as error:
            raise QueryDraftError.from_error(error) from error
        unused = sorted(set(self.variables) - self.used)
        if unused:
            raise QueryDraftError("Unused GraphQL variable(s): {0}", ", ".join("$" + item for item in unused))
        return {
            "operation_name": name,
            "root_fields": self.root_fields,
            "variables": {key: _label(value) for key, value in self.variables.items()},
            "required_variables": sorted(key for key, value in self.variables.items()
                                         if _non_null(value) and key not in self.defaults),
            "field_count": self.fields,
        }

    def _variable_definitions(self) -> None:
        stream = self.stream
        if not stream.accept("("):
            return
        while not stream.accept(")"):
            stream.expect("$")
            name = stream.take_name()
            if name in self.variables:
                raise QueryDraftError('Duplicate GraphQL variable ${0}.', name)
            stream.expect(":")
            ref = _type_ref(stream)
            base = _named(ref)
            kind = self.types.get(base, {}).get("kind") or ("SCALAR" if base in _BUILTIN_INPUT else "")
            if kind not in _INPUT_KINDS:
                raise QueryDraftError('Variable ${0} must use an input type, not {1}.', name, base or 'an unknown type')
            self.variables[name] = self._schema_ref(ref)
            if stream.accept("="):
                self.defaults.add(name)
                self._value(self.variables[name], f"${name} default", variables_allowed=False)

    def _schema_ref(self, ref: Mapping[str, object]) -> dict[str, object]:
        """Translate a parsed variable type into introspection shape."""
        if ref.get("kind") in {"NON_NULL", "LIST"}:
            return {"kind": ref["kind"], "name": None, "ofType": self._schema_ref(ref["ofType"])}
        name = str(ref.get("name") or "")
        return {"kind": self.types.get(name, {}).get("kind") or "SCALAR", "name": name, "ofType": None}

    def _selection_set(self, type_name: str, depth: int, *, root: bool = False) -> None:
        if depth > MAX_DEPTH:
            raise QueryDraftError('The query is nested deeper than {0} levels.', MAX_DEPTH)
        stream = self.stream
        stream.expect("{")
        selected = False
        while not stream.accept("}"):
            selected = True
            if stream.accept("..."):
                condition = type_name
                if stream.peek() == "on":
                    stream.take()
                    condition = stream.take_name()
                    if self.types.get(condition, {}).get("kind") not in _COMPOSITE_KINDS:
                        raise QueryDraftError('Inline fragment type {0} is not an object, interface or union.', condition)
                elif stream.peek() not in {"{", "@"}:
                    raise QueryDraftError("Named fragment spreads are not supported in GraphQL Services.")
                self._directives()
                self._selection_set(condition, depth + 1)
                continue
            self._field(type_name, depth, root=root)
        if not selected:
            raise QueryDraftError("A selection set must select at least one field.")

    def _field(self, type_name: str, depth: int, *, root: bool) -> None:
        stream = self.stream
        name = stream.take_name()
        if stream.accept(":"):
            name = stream.take_name()
        self.fields += 1
        if self.fields > MAX_FIELDS:
            raise QueryDraftError('The query selects more than {0} fields.', MAX_FIELDS)
        if name == "__typename":
            if stream.peek() == "(":
                raise QueryDraftError("__typename takes no arguments.")
            self._directives()
            if stream.peek() == "{":
                raise QueryDraftError("__typename cannot have a selection set.")
            return
        if name.startswith("__"):
            raise QueryDraftError("Introspection fields are not allowed in GraphQL Services.")
        owner = self.types.get(type_name, {})
        if owner.get("kind") == "UNION":
            raise QueryDraftError('Select fields of union {0} through inline fragments.', type_name)
        fields = {item.get("name"): item for item in owner.get("fields") or [] if isinstance(item, Mapping)}
        definition = fields.get(name)
        if not definition:
            raise QueryDraftError('Field {0}.{1} does not exist in the saved schema.', type_name, name)
        if root:
            self.root_fields.append(name)
        self._arguments(definition, f"{type_name}.{name}")
        self._directives()
        target = _named(definition.get("type"))
        kind = self.types.get(target, {}).get("kind") or ("SCALAR" if target in _BUILTIN_INPUT else "")
        if kind in _COMPOSITE_KINDS:
            if stream.peek() != "{":
                raise QueryDraftError('Field {0}.{1} returns {2} and needs a selection set.', type_name, name, target)
            self._selection_set(target, depth + 1)
        elif stream.peek() == "{":
            raise QueryDraftError('Field {0}.{1} returns the leaf type {2} and cannot have a selection set.', type_name, name, target)

    def _arguments(self, definition: Mapping[str, object], path: str) -> None:
        stream = self.stream
        arguments = {item.get("name"): item for item in definition.get("args") or [] if isinstance(item, Mapping)}
        given: set[str] = set()
        if stream.accept("("):
            while not stream.accept(")"):
                name = stream.take_name()
                if name in given:
                    raise QueryDraftError('Duplicate argument {0}({1}).', path, name)
                if name not in arguments:
                    raise QueryDraftError('Argument {0} does not exist on {1}.', name, path)
                given.add(name)
                stream.expect(":")
                self._value(arguments[name].get("type"), f"{path}({name})", variables_allowed=True)
        for name, item in arguments.items():
            if name not in given and _non_null(item.get("type")) and item.get("defaultValue") is None:
                raise QueryDraftError('Required argument {0}({1}) is missing.', path, name)

    def _directives(self) -> None:
        stream = self.stream
        while stream.accept("@"):
            name = stream.take_name()
            if name not in {"include", "skip"}:
                raise QueryDraftError('Directive @{0} is not supported in GraphQL Services.', name)
            stream.expect("(")
            if stream.take_name() != "if":
                raise QueryDraftError('@{0} requires exactly the argument if.', name)
            stream.expect(":")
            self._value({"kind": "NON_NULL", "ofType": {"kind": "SCALAR", "name": "Boolean"}}, f"@{name}(if)", variables_allowed=True)
            stream.expect(")")

    def _value(self, ref: object, path: str, *, variables_allowed: bool) -> None:
        stream = self.stream
        if stream.accept("$"):
            if not variables_allowed:
                raise QueryDraftError('{0} cannot reference a variable.', path)
            name = stream.take_name()
            variable = self.variables.get(name)
            if variable is None:
                raise QueryDraftError('Variable ${0} is used but not defined.', name)
            self.used.add(name)
            compatible = _compatible_variable_type(self._parsed(variable), ref)
            if not compatible and name in self.defaults and isinstance(ref, Mapping) and ref.get("kind") == "NON_NULL":
                compatible = _compatible_variable_type(self._parsed(variable), ref.get("ofType"))
            if not compatible:
                raise QueryDraftError('Variable ${0} of type {1} cannot be used for {2} ({3}).', name, _label(variable), path, _label(ref))
            return
        token = stream.take()
        literal = self._literal(token, ref, path, variables_allowed)
        if literal is not _UNCHECKED:
            try:
                _validate_value(literal, ref, self.types, path)
            except GraphQLContractError as error:
                raise QueryDraftError.from_error(error) from error

    def _parsed(self, ref: Mapping[str, object]) -> dict[str, object]:
        if ref.get("kind") in {"NON_NULL", "LIST"}:
            return {"kind": ref["kind"], "ofType": self._parsed(ref["ofType"])}
        return {"kind": "NAMED", "name": ref.get("name")}

    def _literal(self, token: str, ref: object, path: str, variables_allowed: bool) -> object:
        stream = self.stream
        inner = ref.get("ofType") if isinstance(ref, Mapping) and ref.get("kind") == "NON_NULL" else ref
        if token == "[":
            item = inner.get("ofType") if isinstance(inner, Mapping) and inner.get("kind") == "LIST" else inner
            if not isinstance(inner, Mapping) or inner.get("kind") != "LIST":
                raise QueryDraftError('{0} is not a list.', path)
            while not stream.accept("]"):
                self._value(item, f"{path}[]", variables_allowed=variables_allowed)
            return _UNCHECKED
        if token == "{":
            name = _named(inner)
            definition = self.types.get(name, {})
            if definition.get("kind") != "INPUT_OBJECT":
                raise QueryDraftError('{0} is not an input object.', path)
            fields = {item.get("name"): item for item in definition.get("inputFields") or [] if isinstance(item, Mapping)}
            given: set[str] = set()
            while not stream.accept("}"):
                field = stream.take_name()
                if field not in fields or field in given:
                    raise QueryDraftError('{0}.{1} is unknown or duplicated.', path, field)
                given.add(field)
                stream.expect(":")
                self._value(fields[field].get("type"), f"{path}.{field}", variables_allowed=variables_allowed)
            for field, item in fields.items():
                if field not in given and _non_null(item.get("type")) and item.get("defaultValue") is None:
                    raise QueryDraftError('Required input field {0}.{1} is missing.', path, field)
            return _UNCHECKED
        if token == "null":
            return None
        if token in {"true", "false"}:
            return token == "true"
        if token.startswith('"'):
            try:
                return json.loads(token)
            except json.JSONDecodeError as error:
                raise QueryDraftError('{0} contains an invalid string literal.', path) from error
        if token[0].isdigit() or token[0] == "-":
            try:
                return int(token)
            except ValueError:
                try:
                    return float(token)
                except ValueError as error:
                    raise QueryDraftError('{0} contains an invalid number.', path) from error
        if token[0].isalpha() or token[0] == "_":
            if self.types.get(_named(inner), {}).get("kind") != "ENUM":
                raise QueryDraftError('{0} expects {1}, not the enum value {2}.', path, _label(ref), token)
            return token
        raise QueryDraftError('{0} contains an unsupported value.', path)


_UNCHECKED = object()


def validate(schema: dict[str, object], document: str) -> dict[str, object]:
    """Validate one read-only query document against stored introspection."""
    return _Validator(schema, document).run()


def validate_variables(schema: dict[str, object], document: str, values: object) -> dict[str, object]:
    """Validate runtime variable values against the document's declared variables."""
    if values is None:
        values = {}
    if not isinstance(values, Mapping):
        raise QueryDraftError("Query variables must be a JSON object.")
    validator = _Validator(schema, document)
    validator.run()
    unknown = sorted(set(values) - set(validator.variables))
    if unknown:
        raise QueryDraftError("Unknown query variable(s): {0}", ", ".join(unknown))
    result: dict[str, object] = {}
    for name, ref in validator.variables.items():
        if name not in values:
            if _non_null(ref) and name not in validator.defaults:
                raise QueryDraftError('Required query variable ${0} is missing.', name)
            continue
        try:
            result[name] = _validate_value(values[name], ref, validator.types, f"${name}")
        except GraphQLContractError as error:
            raise QueryDraftError.from_error(error) from error
    return result
