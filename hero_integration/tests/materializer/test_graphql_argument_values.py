import pytest
from hero_integration.graphql_protocol.graphql_argument_values import (
    GraphQLArgumentValueError,
    parse_argument_text,
    validate_argument_values,
)
from hero_integration.graphql_protocol.graphql_query_composer import build_query_composer_model
from hero_integration.graphql_protocol.schema import (
    GraphQLArgument,
    GraphQLField,
    GraphQLSchema,
    GraphQLType,
    GraphQLTypeRef,
)

def scalar(name):
    return GraphQLTypeRef(kind="SCALAR", name=name)


def argument(name, type_ref, default=None):
    return GraphQLArgument(name, None, type_ref, default)


def model():
    required_id = GraphQLTypeRef(kind="NON_NULL", of_type=scalar("ID"))
    required_id_list = GraphQLTypeRef(
        kind="NON_NULL",
        of_type=GraphQLTypeRef(kind="LIST", of_type=required_id),
    )
    arguments = (
        argument("sourceRequestId", required_id),
        argument("limit", scalar("Int"), "50"),
        argument("active", scalar("Boolean")),
        argument("ids", GraphQLTypeRef(kind="LIST", of_type=required_id)),
        argument("requiredIds", required_id_list),
    )
    field = GraphQLField(
        "GetProject", None, arguments, GraphQLTypeRef(kind="OBJECT", name="Project"), False, None
    )
    schema = GraphQLSchema(
        query_type_name="Query",
        mutation_type_name=None,
        subscription_type_name=None,
        types=(
            GraphQLType("OBJECT", "Query", None, fields=(field,)),
            GraphQLType("OBJECT", "Project", None),
            *(GraphQLType("SCALAR", name, None) for name in ("ID", "Int", "Boolean")),
        ),
    )
    return build_query_composer_model(schema, field)


def spec(name):
    return next(item for item in model().arguments if item.name == name)

def test_required_and_default_metadata():
    assert spec("sourceRequestId").required
    assert spec("limit").default_value=="50"
def test_scalar_typing():
    assert parse_argument_text(spec("sourceRequestId"),"ABC")=="ABC"
    assert parse_argument_text(spec("limit"),"25")==25
    assert parse_argument_text(spec("active"),"TRUE") is True
def test_invalid_scalar():
    with pytest.raises(GraphQLArgumentValueError): parse_argument_text(spec("limit"),"x")
    with pytest.raises(GraphQLArgumentValueError): parse_argument_text(spec("active"),"yes")
def test_list_and_nullability():
    assert parse_argument_text(spec("ids"),'["A",2]')==["A",2]
    with pytest.raises(GraphQLArgumentValueError): parse_argument_text(spec("ids"),'["A",null]')
def test_defaults_and_sources():
    r=validate_argument_values(model(),{"sourceRequestId":"R1","requiredIds":'["A"]'})
    assert r.values["limit"]==50 and r.sources["limit"]=="default"
def test_missing_required():
    with pytest.raises(GraphQLArgumentValueError,match="sourceRequestId"): validate_argument_values(model(),{"requiredIds":'["A"]'})
def test_unknown_argument():
    with pytest.raises(GraphQLArgumentValueError,match="unknown"): validate_argument_values(model(),{"sourceRequestId":"R1","requiredIds":'["A"]',"unknown":"x"})
def test_protocol_name_preserved():
    r=validate_argument_values(model(),{"sourceRequestId":"R1","requiredIds":'["A"]'})
    assert "sourceRequestId" in r.values and "source_request_id" not in r.values
