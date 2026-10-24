from hero_integration.services.materializer_routing import (
    MaterializerRouter,
    QueryRoutingRequest,
)
from hero_integration.services.schema_registry import build_schema_registry
from hero_integration.tests.test_schema_registry import _stored


def test_compile_typed_query_plan_from_cataloged_references():
    snapshot = _stored()
    registry = build_schema_registry(snapshot)

    plan = MaterializerRouter().compile_query(
        snapshot,
        registry,
        QueryRoutingRequest(
            operation="case",
            output_references=(
                "hero.query.case.id",
                "hero.query.case.title",
            ),
            arguments={"id": "case-1"},
            identity_reference="hero.query.case.id",
            revision=7,
        ),
    )

    assert plan.direction == "QUERY_OUTPUT"
    assert plan.execution_authority == "NONE"
    assert plan.snapshot_revision == 7
    assert plan.selected_response_paths == (
        ("data", "case", "id"),
        ("data", "case", "title"),
    )
    assert "query case" in plan.request.document
    assert plan.request.mutation_safety_request is None