import pytest
from hero_integration.materializer.graphql_argument_values import GraphQLArgumentValueError,parse_argument_text,validate_argument_values
from hero_connector.tools.graphql_introspection import parse_introspection_payload
from hero_integration.materializer.graphql_query_composer import build_query_composer_model

def ref(kind,name=None,ofType=None): return {"kind":kind,"name":name,"ofType":ofType}
def arg(name,t,default=None): return {"name":name,"description":None,"defaultValue":default,"type":t}
def model():
    S=lambda n:ref("SCALAR",n)
    args=[arg("sourceRequestId",ref("NON_NULL",ofType=S("ID"))),arg("limit",S("Int"),"50"),arg("active",S("Boolean")),arg("ids",ref("LIST",ofType=ref("NON_NULL",ofType=S("ID")))),arg("requiredIds",ref("NON_NULL",ofType=ref("LIST",ofType=ref("NON_NULL",ofType=S("ID")))))]
    types=[{"kind":"OBJECT","name":"Query","description":None,"fields":[{"name":"GetProject","description":None,"args":args,"type":ref("OBJECT","Project"),"isDeprecated":False,"deprecationReason":None}]},{"kind":"OBJECT","name":"Project","description":None,"fields":[]}]
    types += [{"kind":"SCALAR","name":n,"description":None,"fields":None} for n in ("ID","Int","Boolean")]
    s=parse_introspection_payload({"data":{"__schema":{"queryType":{"name":"Query"},"mutationType":None,"subscriptionType":None,"types":types}}})
    return build_query_composer_model(s,s.query_fields[0])
def spec(n): return next(s for s in model().arguments if s.name==n)

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
