"""Local validation of read-only HERO query service drafts against a saved schema.

Mirrors ``mutation_draft``: it validates one GraphQL query operation against the
stored introspection only and never contacts HERO.  Supported syntax is the
subset the GraphQL Services editor needs: one (optionally named) query with
variable definitions, fields with aliases and arguments, inline fragments and
``@include``/``@skip``.  Named fragments and subscriptions are rejected.
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

MAX_DEPTH = 12
MAX_FIELDS = 500
_LEAF_KINDS = {"SCALAR", "ENUM"}
_COMPOSITE_KINDS = {"OBJECT", "INTERFACE", "UNION"}
_INPUT_KINDS = {"SCALAR", "ENUM", "INPUT_OBJECT"}
_BUILTIN_INPUT = {"Int", "Float", "String", "Boolean", "ID"}


class QueryDraftError(ValueError):
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
        raise QueryDraftError(f"Unsupported GraphQL character {char!r}.")
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
            raise QueryDraftError(str(error)) from error
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
            raise QueryDraftError(str(error)) from error
        unused = sorted(set(self.variables) - self.used)
        if unused:
            raise QueryDraftError("Unused GraphQL variable(s): " + ", ".join("$" + item for item in unused))
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
                raise QueryDraftError(f"Duplicate GraphQL variable ${name}.")
            stream.expect(":")
            ref = _type_ref(stream)
            base = _named(ref)
            kind = self.types.get(base, {}).get("kind") or ("SCALAR" if base in _BUILTIN_INPUT else "")
            if kind not in _INPUT_KINDS:
                raise QueryDraftError(f"Variable ${name} must use an input type, not {base or 'an unknown type'}.")
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
            raise QueryDraftError(f"The query is nested deeper than {MAX_DEPTH} levels.")
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
                        raise QueryDraftError(f"Inline fragment type {condition} is not an object, interface or union.")
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
            raise QueryDraftError(f"The query selects more than {MAX_FIELDS} fields.")
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
            raise QueryDraftError(f"Select fields of union {type_name} through inline fragments.")
        fields = {item.get("name"): item for item in owner.get("fields") or [] if isinstance(item, Mapping)}
        definition = fields.get(name)
        if not definition:
            raise QueryDraftError(f"Field {type_name}.{name} does not exist in the saved schema.")
        if root:
            self.root_fields.append(name)
        self._arguments(definition, f"{type_name}.{name}")
        self._directives()
        target = _named(definition.get("type"))
        kind = self.types.get(target, {}).get("kind") or ("SCALAR" if target in _BUILTIN_INPUT else "")
        if kind in _COMPOSITE_KINDS:
            if stream.peek() != "{":
                raise QueryDraftError(f"Field {type_name}.{name} returns {target} and needs a selection set.")
            self._selection_set(target, depth + 1)
        elif stream.peek() == "{":
            raise QueryDraftError(f"Field {type_name}.{name} returns the leaf type {target} and cannot have a selection set.")

    def _arguments(self, definition: Mapping[str, object], path: str) -> None:
        stream = self.stream
        arguments = {item.get("name"): item for item in definition.get("args") or [] if isinstance(item, Mapping)}
        given: set[str] = set()
        if stream.accept("("):
            while not stream.accept(")"):
                name = stream.take_name()
                if name in given:
                    raise QueryDraftError(f"Duplicate argument {path}({name}).")
                if name not in arguments:
                    raise QueryDraftError(f"Argument {name} does not exist on {path}.")
                given.add(name)
                stream.expect(":")
                self._value(arguments[name].get("type"), f"{path}({name})", variables_allowed=True)
        for name, item in arguments.items():
            if name not in given and _non_null(item.get("type")) and item.get("defaultValue") is None:
                raise QueryDraftError(f"Required argument {path}({name}) is missing.")

    def _directives(self) -> None:
        stream = self.stream
        while stream.accept("@"):
            name = stream.take_name()
            if name not in {"include", "skip"}:
                raise QueryDraftError(f"Directive @{name} is not supported in GraphQL Services.")
            stream.expect("(")
            if stream.take_name() != "if":
                raise QueryDraftError(f"@{name} requires exactly the argument if.")
            stream.expect(":")
            self._value({"kind": "NON_NULL", "ofType": {"kind": "SCALAR", "name": "Boolean"}}, f"@{name}(if)", variables_allowed=True)
            stream.expect(")")

    def _value(self, ref: object, path: str, *, variables_allowed: bool) -> None:
        stream = self.stream
        if stream.accept("$"):
            if not variables_allowed:
                raise QueryDraftError(f"{path} cannot reference a variable.")
            name = stream.take_name()
            variable = self.variables.get(name)
            if variable is None:
                raise QueryDraftError(f"Variable ${name} is used but not defined.")
            self.used.add(name)
            compatible = _compatible_variable_type(self._parsed(variable), ref)
            if not compatible and name in self.defaults and isinstance(ref, Mapping) and ref.get("kind") == "NON_NULL":
                compatible = _compatible_variable_type(self._parsed(variable), ref.get("ofType"))
            if not compatible:
                raise QueryDraftError(f"Variable ${name} of type {_label(variable)} cannot be used for {path} ({_label(ref)}).")
            return
        token = stream.take()
        literal = self._literal(token, ref, path, variables_allowed)
        if literal is not _UNCHECKED:
            try:
                _validate_value(literal, ref, self.types, path)
            except GraphQLContractError as error:
                raise QueryDraftError(str(error)) from error

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
                raise QueryDraftError(f"{path} is not a list.")
            while not stream.accept("]"):
                self._value(item, f"{path}[]", variables_allowed=variables_allowed)
            return _UNCHECKED
        if token == "{":
            name = _named(inner)
            definition = self.types.get(name, {})
            if definition.get("kind") != "INPUT_OBJECT":
                raise QueryDraftError(f"{path} is not an input object.")
            fields = {item.get("name"): item for item in definition.get("inputFields") or [] if isinstance(item, Mapping)}
            given: set[str] = set()
            while not stream.accept("}"):
                field = stream.take_name()
                if field not in fields or field in given:
                    raise QueryDraftError(f"{path}.{field} is unknown or duplicated.")
                given.add(field)
                stream.expect(":")
                self._value(fields[field].get("type"), f"{path}.{field}", variables_allowed=variables_allowed)
            for field, item in fields.items():
                if field not in given and _non_null(item.get("type")) and item.get("defaultValue") is None:
                    raise QueryDraftError(f"Required input field {path}.{field} is missing.")
            return _UNCHECKED
        if token == "null":
            return None
        if token in {"true", "false"}:
            return token == "true"
        if token.startswith('"'):
            try:
                return json.loads(token)
            except json.JSONDecodeError as error:
                raise QueryDraftError(f"{path} contains an invalid string literal.") from error
        if token[0].isdigit() or token[0] == "-":
            try:
                return int(token)
            except ValueError:
                try:
                    return float(token)
                except ValueError as error:
                    raise QueryDraftError(f"{path} contains an invalid number.") from error
        if token[0].isalpha() or token[0] == "_":
            if self.types.get(_named(inner), {}).get("kind") != "ENUM":
                raise QueryDraftError(f"{path} expects {_label(ref)}, not the enum value {token}.")
            return token
        raise QueryDraftError(f"{path} contains an unsupported value.")


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
        raise QueryDraftError("Unknown query variable(s): " + ", ".join(unknown))
    result: dict[str, object] = {}
    for name, ref in validator.variables.items():
        if name not in values:
            if _non_null(ref) and name not in validator.defaults:
                raise QueryDraftError(f"Required query variable ${name} is missing.")
            continue
        try:
            result[name] = _validate_value(values[name], ref, validator.types, f"${name}")
        except GraphQLContractError as error:
            raise QueryDraftError(str(error)) from error
    return result
