from __future__ import annotations
from collections.abc import Mapping
import json
from dataclasses import dataclass
from .graphql_query_composer import GraphQLArgumentSpec, GraphQLQueryComposerModel
from .schema import GraphQLSchema, GraphQLTypeRef

class GraphQLArgumentValueError(ValueError):
    pass

@dataclass(frozen=True)
class GraphQLArgumentValidationResult:
    values: dict[str, object]
    sources: dict[str, str]

def _unwrap(ref: GraphQLTypeRef) -> GraphQLTypeRef:
    if ref.kind != "NON_NULL":
        return ref
    if ref.of_type is None:
        raise GraphQLArgumentValueError("Invalid NON_NULL GraphQL type.")
    return ref.of_type

def _validate(
    value: object,
    ref: GraphQLTypeRef,
    name: str,
    *,
    schema: GraphQLSchema | None = None,
) -> object:
    if ref.kind == "NON_NULL":
        if value is None:
            raise GraphQLArgumentValueError(f"GraphQL argument {name!r} must not be null.")
        return _validate(value, _unwrap(ref), name, schema=schema)
    if value is None:
        return None
    if ref.kind == "LIST":
        if ref.of_type is None or not isinstance(value, list):
            raise GraphQLArgumentValueError(f"GraphQL list argument {name!r} requires a JSON array.")
        return [
            _validate(v, ref.of_type, f"{name}[{index}]", schema=schema)
            for index, v in enumerate(value)
        ]
    kind=ref.named_type()
    valid={
        "Int": lambda v: isinstance(v,int) and not isinstance(v,bool),
        "Float": lambda v: isinstance(v,(int,float)) and not isinstance(v,bool),
        "Boolean": lambda v: isinstance(v,bool),
        "String": lambda v: isinstance(v,str),
        "ID": lambda v: isinstance(v,(str,int)) and not isinstance(v,bool),
    }
    if kind in valid and not valid[kind](value):
        raise GraphQLArgumentValueError(f"Invalid value for GraphQL {kind} argument {name!r}.")
    graphql_type = schema.type_named(kind) if schema is not None and kind else None
    if graphql_type is not None and graphql_type.kind == "ENUM":
        allowed = {item.name for item in graphql_type.enum_values}
        if not isinstance(value, str) or value not in allowed:
            raise GraphQLArgumentValueError(
                f"Invalid value for GraphQL enum {kind} argument {name!r}."
            )
    if graphql_type is not None and graphql_type.kind == "INPUT_OBJECT":
        if not isinstance(value, Mapping):
            raise GraphQLArgumentValueError(
                f"GraphQL input object argument {name!r} requires a JSON object."
            )
        fields = {item.name: item for item in graphql_type.input_fields}
        unknown = sorted(set(value) - set(fields))
        if unknown:
            raise GraphQLArgumentValueError(
                f"Unknown GraphQL input field(s) for {name!r}: "
                + ", ".join(unknown)
                + "."
            )
        result: dict[str, object] = {}
        for field in graphql_type.input_fields:
            child_name = f"{name}.{field.name}"
            if field.name not in value:
                if field.type_ref.kind == "NON_NULL" and field.default_value is None:
                    raise GraphQLArgumentValueError(
                        f"Required GraphQL input field {child_name!r} has no value."
                    )
                continue
            result[field.name] = _validate(
                value[field.name],
                field.type_ref,
                child_name,
                schema=schema,
            )
        return result
    return value

def _parse(text: str, ref: GraphQLTypeRef, name: str, *, default: bool=False) -> object:
    target=_unwrap(ref)
    kind=target.named_type()
    if target.kind=="LIST":
        try: value=json.loads(text)
        except json.JSONDecodeError as exc:
            raise GraphQLArgumentValueError(f"GraphQL list argument {name!r} requires valid JSON.") from exc
        return _validate(value,target,name)
    if kind in {"String","ID"} and not default:
        return text
    if kind=="Int":
        try: return int(text,10)
        except ValueError as exc: raise GraphQLArgumentValueError(f"GraphQL Int argument {name!r} requires an integer.") from exc
    if kind=="Float":
        try: return float(text)
        except ValueError as exc: raise GraphQLArgumentValueError(f"GraphQL Float argument {name!r} requires a number.") from exc
    if kind=="Boolean":
        if text.casefold()=="true": return True
        if text.casefold()=="false": return False
        raise GraphQLArgumentValueError(f"GraphQL Boolean argument {name!r} requires true or false.")
    try: value=json.loads(text)
    except json.JSONDecodeError: value=text
    return _validate(value,target,name)

def validate_typed_argument_values(
    model: GraphQLQueryComposerModel,
    values: dict[str, object],
    *,
    schema: GraphQLSchema | None = None,
) -> dict[str, object]:
    specs = {
        spec.name: spec
        for spec in model.arguments
    }

    unknown = sorted(set(values) - set(specs))
    if unknown:
        raise GraphQLArgumentValueError(
            "Unknown GraphQL argument(s): "
            + ", ".join(unknown)
        )

    validated: dict[str, object] = {}

    for spec in model.arguments:
        if spec.name not in values:
            if spec.required:
                raise GraphQLArgumentValueError(
                    f"Required GraphQL argument "
                    f"{spec.name!r} has no value."
                )
            continue

        validated[spec.name] = _validate(
            values[spec.name],
            spec.argument.type_ref,
            spec.name,
            schema=schema,
        )

    return validated


def parse_argument_text(spec: GraphQLArgumentSpec, text: str) -> object:
    text=text.strip()
    if not text:
        if spec.required:
            raise GraphQLArgumentValueError(f"Required GraphQL argument {spec.name!r} has no value.")
        if spec.default_value is None:
            return None
        return _parse(spec.default_value,spec.argument.type_ref,spec.name,default=True)
    return _parse(text,spec.argument.type_ref,spec.name)

def validate_argument_values(model: GraphQLQueryComposerModel, raw_values: dict[str,str]) -> GraphQLArgumentValidationResult:
    specs={s.name:s for s in model.arguments}
    unknown=sorted(set(raw_values)-set(specs))
    if unknown:
        raise GraphQLArgumentValueError("Unknown GraphQL argument(s): "+", ".join(unknown))
    values={}
    sources={}
    for spec in model.arguments:
        raw=raw_values.get(spec.name,"")
        if raw.strip():
            values[spec.name]=parse_argument_text(spec,raw); sources[spec.name]="user"
        elif spec.default_value is not None:
            values[spec.name]=parse_argument_text(spec,""); sources[spec.name]="default"
        elif spec.required:
            parse_argument_text(spec,"")
    return GraphQLArgumentValidationResult(values,sources)
