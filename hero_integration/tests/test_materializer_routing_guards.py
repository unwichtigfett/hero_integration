from __future__ import annotations

from dataclasses import replace
import inspect

import pytest

from hero_integration.services.graphql_graph import analyze_snapshot
from hero_integration.services.mapping_contract_v1 import StoredSnapshot
from hero_integration.services.materializer_routing import (
    MaterializerRouter,
    MaterializerRoutingError,
    MutationRoutingRequest,
    QueryRoutingRequest,
)
from hero_integration.services.schema_registry import build_schema_registry
from hero_integration.tests.materializer.test_mapping_contract_v1 import RAW_SCHEMA
from hero_integration.tests.test_schema_registry import _stored


def _router_context():
    snapshot = _stored()
    return snapshot, build_schema_registry(snapshot), MaterializerRouter()


def test_compiles_mutation_input_and_output_without_execution_authority():
    snapshot, registry, router = _router_context()

    plan = router.compile_mutation(
        snapshot,
        registry,
        MutationRoutingRequest(
            operation="updateCase",
            input_references=(
                "hero.mutation.updateCase.dryRun",
                "hero.mutation.updateCase.input.options.status",
                "hero.mutation.updateCase.input.status",
            ),
            arguments={
                "input": {"status": "OPEN", "options": {"status": "CLOSED"}},
                "dryRun": False,
            },
            output_references=("hero.mutation.updateCase.result.id",),
            revision=2,
        ),
    )

    assert plan.direction == "MUTATION_INPUT"
    assert plan.execution_authority == "NONE"
    assert plan.input_reference_ids == (
        "hero.mutation.updateCase.dryRun",
        "hero.mutation.updateCase.input.options.status",
        "hero.mutation.updateCase.input.status",
    )
    assert plan.output_reference_ids == ("hero.mutation.updateCase.result.id",)
    assert "mutation updateCase" in plan.request.document
    assert plan.request.mutation_safety_request is not None
    assert not hasattr(plan, "execute")
    assert not hasattr(plan.request, "execute")


@pytest.mark.parametrize(
    ("route", "message"),
    (
        (
            MutationRoutingRequest(
                operation="updateCase",
                input_references=("hero.mutation.updateCase.input.status",),
                arguments={"input": {"status": "INVALID"}},
                output_references=("hero.mutation.updateCase.result.id",),
            ),
            "enum",
        ),
        (
            MutationRoutingRequest(
                operation="updateCase",
                input_references=(
                    "hero.mutation.updateCase.input",
                    "hero.mutation.updateCase.input.status",
                ),
                arguments={"input": {}},
                output_references=("hero.mutation.updateCase.result.id",),
            ),
            "Required",
        ),
        (
            MutationRoutingRequest(
                operation="updateCase",
                input_references=("hero.query.case.id",),
                arguments={"input": {"status": "OPEN"}},
                output_references=("hero.mutation.updateCase.result.id",),
            ),
            "input reference",
        ),
        (
            MutationRoutingRequest(
                operation="updateCase",
                input_references=("hero.mutation.updateCase.input.status",),
                arguments={"input": {"status": "OPEN"}},
            ),
            "explicit selected_paths",
        ),
    ),
)
def test_rejects_invalid_mutation_types_directions_and_result_bindings(route, message):
    snapshot, registry, router = _router_context()

    with pytest.raises(MaterializerRoutingError, match=message):
        router.compile_mutation(snapshot, registry, route)


def test_rejects_query_mutation_direction_confusion_and_stale_fingerprint():
    snapshot, registry, router = _router_context()

    with pytest.raises(MaterializerRoutingError, match="output reference"):
        router.compile_query(
            snapshot,
            registry,
            QueryRoutingRequest(
                operation="case",
                output_references=("hero.mutation.updateCase.result.id",),
                arguments={"id": "case-1"},
            ),
        )

    with pytest.raises(MaterializerRoutingError, match="does not match"):
        router.compile_query(
            snapshot,
            replace(registry, fingerprint="0" * 64),
            QueryRoutingRequest(
                operation="case",
                output_references=("hero.query.case.id",),
                arguments={"id": "case-1"},
            ),
        )


def test_compiles_deterministic_collection_pagination_plan():
    fingerprint = analyze_snapshot(RAW_SCHEMA).fingerprint
    snapshot = StoredSnapshot("CASE-SNAPSHOT", RAW_SCHEMA, fingerprint, "hero-primary")
    registry = build_schema_registry(snapshot)
    route = QueryRoutingRequest(
        operation="searchCases",
        output_references=("hero.query.searchCases.id", "hero.query.searchCases.owner.name"),
        arguments={
            "filter": {
                "state": "ACTIVE",
                "ids": ["case-1"],
                "criteria": {"labels": [], "enabled": False, "rank": 0, "note": None},
            },
            "offset": 0,
            "orderBy": "ID_ASC",
            "includeDeleted": False,
        },
        identity_reference="hero.query.searchCases.id",
        pagination_argument="offset",
        revision=4,
    )

    first = MaterializerRouter().compile_query(snapshot, registry, route)
    second = MaterializerRouter().compile_query(snapshot, registry, route)

    assert first == second
    assert first.collection
    assert first.pagination_argument == "offset"
    assert first.pagination_variable is not None
    assert first.selected_response_paths == (
        ("data", "searchCases", "id"),
        ("data", "searchCases", "owner", "name"),
    )
    assert "query searchCases" in first.request.document
    assert first.request.variables[first.pagination_variable] == 0


def test_router_has_no_frappe_transport_or_legacy_read_contract_dependency():
    from hero_integration.services import materializer_routing

    source = inspect.getsource(materializer_routing)
    assert "frappe" not in source
    assert "requests" not in source
    assert "hero_integration.client" not in source
    assert "ReadContract" not in source
