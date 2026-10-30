"""Numeric input boundaries shared by composer and materializer requests."""

import pytest

from hero_integration.graphql_protocol.graphql_argument_values import (
    GraphQLArgumentValueError,
    parse_argument_text,
    validate_argument_values,
    validate_typed_argument_values,
)
from hero_integration.graphql_protocol.graphql_query_composer import build_query_composer_model
from hero_integration.graphql_protocol.schema import (
    GraphQLArgument,
    GraphQLField,
    GraphQLSchema,
    GraphQLType,
    GraphQLTypeRef,
)


def numeric_model(name, *, default=None, nested=False):
    scalar = GraphQLTypeRef("SCALAR", name)
    inputs = ()
    if nested:
        field_type = GraphQLTypeRef("LIST", of_type=scalar)
        inputs = (GraphQLType("INPUT_OBJECT", "Filter", None, input_fields=(
            GraphQLArgument("values", None, field_type, None),
        )),)
        ref = GraphQLTypeRef("INPUT_OBJECT", "Filter")
    else:
        ref = scalar
    field = GraphQLField("records", None, (
        GraphQLArgument("filter" if nested else "value", None, ref, default),
    ), GraphQLTypeRef("SCALAR", "String"), False, None)
    schema = GraphQLSchema("Query", None, None, (
        GraphQLType("OBJECT", "Query", None, fields=(field,)),
        GraphQLType("SCALAR", name, None),
        GraphQLType("SCALAR", "String", None),
        *inputs,
    ))
    return schema, build_query_composer_model(schema, field)


@pytest.mark.parametrize("value", [-(2**31), 0, 2**31 - 1])
def test_int_boundaries_preserve_values(value):
    schema, model = numeric_model("Int")
    assert validate_typed_argument_values(model, {"value": value}, schema=schema) == {"value": value}
    assert parse_argument_text(model.arguments[0], str(value)) == value


@pytest.mark.parametrize("value", [-(2**31) - 1, 2**31])
def test_int_overflow_rejected_in_typed_text_and_default_inputs(value):
    schema, model = numeric_model("Int", default=str(value))
    with pytest.raises(GraphQLArgumentValueError, match="out of range"):
        validate_typed_argument_values(model, {"value": value}, schema=schema)
    with pytest.raises(GraphQLArgumentValueError, match="out of range"):
        parse_argument_text(model.arguments[0], str(value))
    with pytest.raises(GraphQLArgumentValueError, match="out of range"):
        validate_argument_values(model, {})


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), 10**400])
def test_non_finite_or_unrepresentable_float_rejected(value):
    schema, model = numeric_model("Float")
    with pytest.raises(GraphQLArgumentValueError, match="must be finite"):
        validate_typed_argument_values(model, {"value": value}, schema=schema)


@pytest.mark.parametrize("text", ["NaN", "Infinity", "-Infinity", "1e999"])
def test_non_finite_float_text_and_default_rejected(text):
    _, model = numeric_model("Float", default=text)
    with pytest.raises(GraphQLArgumentValueError, match="must be finite"):
        parse_argument_text(model.arguments[0], text)
    with pytest.raises(GraphQLArgumentValueError):
        validate_argument_values(model, {})


@pytest.mark.parametrize("value", [0, -0.0, 0.5, 1e308])
def test_finite_float_inputs_are_not_silently_rewritten(value):
    schema, model = numeric_model("Float")
    result = validate_typed_argument_values(model, {"value": value}, schema=schema)
    assert result == {"value": value}
    assert type(result["value"]) is type(value)


@pytest.mark.parametrize("kind,value", [("Int", 2**31), ("Float", float("inf"))])
def test_nested_input_list_reports_exact_offending_path(kind, value):
    schema, model = numeric_model(kind, nested=True)
    with pytest.raises(GraphQLArgumentValueError, match=r"filter\.values\[1\]"):
        validate_typed_argument_values(model, {"filter": {"values": [0, value]}}, schema=schema)


@pytest.mark.parametrize("kind,value", [("Int", True), ("Float", False), ("Int", 1.5)])
def test_invalid_numeric_types_remain_rejected(kind, value):
    schema, model = numeric_model(kind)
    with pytest.raises(GraphQLArgumentValueError, match="Invalid value"):
        validate_typed_argument_values(model, {"value": value}, schema=schema)


def test_omission_explicit_null_and_server_default_remain_distinct():
    schema, model = numeric_model("Int", default="5")
    assert validate_typed_argument_values(model, {}, schema=schema) == {}
    assert validate_typed_argument_values(model, {"value": None}, schema=schema) == {"value": None}
    result = validate_argument_values(model, {})
    assert result.values == {"value": 5}
    assert result.sources == {"value": "default"}


def test_integer_id_is_not_restricted_to_graphql_int_range():
    schema, model = numeric_model("ID")
    value = 2**63
    assert validate_typed_argument_values(model, {"value": value}, schema=schema) == {"value": value}


@pytest.mark.parametrize("kind,text", [("Int", '{"values":[1,2147483648]}'), ("Float", '{"values":[1,1e999]}')])
def test_nested_numeric_text_and_default_report_the_offending_path(kind, text):
    _, model = numeric_model(kind, nested=True, default=text.replace('"values"', 'values'))
    with pytest.raises(GraphQLArgumentValueError, match=r"filter\.values\[1\]"):
        parse_argument_text(model.arguments[0], text)
    with pytest.raises(GraphQLArgumentValueError, match=r"filter\.values\[1\]"):
        validate_argument_values(model, {})


def test_nested_text_preserves_unknown_and_custom_scalar_values():
    schema, model = numeric_model("Int", nested=True)
    text = '{"values":[-2147483648,2147483647],"unknown":{"custom":"raw"}}'
    assert parse_argument_text(model.arguments[0], text) == {"values": [-(2**31), 2**31 - 1], "unknown": {"custom": "raw"}}


def test_numeric_list_text_preserves_invalid_shape_rejection():
    _, model = numeric_model("Int", nested=True)
    # The existing object text contract does not validate unrelated unknown fields.
    assert parse_argument_text(model.arguments[0], '{"values":"unchanged"}') == {"values": "unchanged"}


def test_nested_text_rejects_boolean_without_converting_it():
    _, model = numeric_model("Float", nested=True)
    with pytest.raises(GraphQLArgumentValueError, match=r"filter\.values\[1\]"):
        parse_argument_text(model.arguments[0], '{"values":[0,true]}')


@pytest.mark.parametrize("kind", ["Float", "ID"])
def test_graphql_default_large_number_obeys_declared_scalar(kind):
    _, model = numeric_model(kind, nested=True, default="{values: [2147483648]}")
    assert validate_argument_values(model, {}).values == {"filter": {"values": [2147483648]}}


def test_graphql_default_nested_int_boundary_and_omission():
    _, model = numeric_model("Int", nested=True, default="{values: [-2147483648, 2147483647]}")
    assert validate_argument_values(model, {}).values == {"filter": {"values": [-2147483648, 2147483647]}}
    assert validate_typed_argument_values(model, {}) == {}


@pytest.mark.parametrize("literal", ["{values: [$x]}", "{values: [1}", "{values: [1], values: [2]}", "{values: [1]} extra"])
def test_default_must_be_unambiguous_constant_graphql_value(literal):
    _, model = numeric_model("Int", nested=True, default=literal)
    with pytest.raises(GraphQLArgumentValueError):
        validate_argument_values(model, {})


def test_graphql_default_preserves_enum_boolean_null_and_escaped_string():
    from hero_integration.graphql_protocol.schema import GraphQLEnumValue
    enum = GraphQLType("ENUM", "Mode", None, enum_values=(GraphQLEnumValue("READY", None, False, None),))
    fields = (
        GraphQLArgument("mode", None, GraphQLTypeRef("ENUM", "Mode"), None),
        GraphQLArgument("active", None, GraphQLTypeRef("SCALAR", "Boolean"), None),
        GraphQLArgument("note", None, GraphQLTypeRef("SCALAR", "String"), None),
        GraphQLArgument("optional", None, GraphQLTypeRef("SCALAR", "String"), None),
    )
    ref = GraphQLTypeRef("INPUT_OBJECT", "Filter")
    # Build the GraphQL string using JSON escaping to exercise commas and escapes.
    import json
    expected = 'a, b\n"quoted"'
    literal = '{mode: READY, active: false, note: ' + json.dumps(expected) + ', optional: null}'
    field = GraphQLField("records", None, (GraphQLArgument("filter", None, ref, literal),), GraphQLTypeRef("SCALAR", "String"), False, None)
    schema = GraphQLSchema("Query", None, None, (GraphQLType("OBJECT", "Query", None, fields=(field,)),
        GraphQLType("INPUT_OBJECT", "Filter", None, input_fields=fields), enum, GraphQLType("SCALAR", "String", None)))
    model = build_query_composer_model(schema, field)
    assert validate_argument_values(model, {}).values == {"filter": {"mode": "READY", "active": False, "note": expected, "optional": None}}


def test_nested_objects_and_custom_scalar_defaults_preserve_contents():
    inner = GraphQLType("INPUT_OBJECT", "Inner", None, input_fields=(
        GraphQLArgument("values", None, GraphQLTypeRef("LIST", of_type=GraphQLTypeRef("SCALAR", "Int")), None),
        GraphQLArgument("opaque", None, GraphQLTypeRef("SCALAR", "Custom"), None),
    ))
    outer = GraphQLType("INPUT_OBJECT", "Outer", None, input_fields=(
        GraphQLArgument("inner", None, GraphQLTypeRef("INPUT_OBJECT", "Inner"), None),
    ))
    literal = '{inner: {values: [1, 2147483647], opaque: "a,b"}}'
    field = GraphQLField("records", None, (GraphQLArgument("filter", None, GraphQLTypeRef("INPUT_OBJECT", "Outer"), literal),), GraphQLTypeRef("SCALAR", "String"), False, None)
    schema = GraphQLSchema("Query", None, None, (GraphQLType("OBJECT", "Query", None, fields=(field,)),
        GraphQLType("SCALAR", "String", None), GraphQLType("SCALAR", "Custom", None), inner, outer))
    model = build_query_composer_model(schema, field)
    assert validate_argument_values(model, {}).values == {"filter": {"inner": {"values": [1, 2147483647], "opaque": "a,b"}}}
    assert validate_typed_argument_values(model, {"filter": None}, schema=schema) == {"filter": None}
    assert validate_typed_argument_values(model, {}, schema=schema) == {}
