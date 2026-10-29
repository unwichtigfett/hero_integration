from __future__ import annotations

import pytest

from hero_integration.graphql_protocol.graphql_write_safety import (
    GraphQLMutationApproval,
    GraphQLOperationKind,
    GraphQLWriteRequest,
    GraphQLWriteSafetyError,
    GraphQLWriteSafetyPolicy,
    GraphQLWriteSafetyState,
    detect_operation_kind,
)


def test_explicit_query_is_read_only() -> None:
    request = GraphQLWriteRequest.create(
        "query Lookup { __typename }",
        operation_name="Lookup",
    )

    result = GraphQLWriteSafetyPolicy().assess(
        request
    )

    assert (
        request.operation_kind
        is GraphQLOperationKind.QUERY
    )
    assert result.state is GraphQLWriteSafetyState.READ_ONLY
    assert result.can_execute is True


def test_shorthand_query_is_read_only() -> None:
    request = GraphQLWriteRequest.create(
        "{ __typename }"
    )

    assert (
        request.operation_kind
        is GraphQLOperationKind.QUERY
    )
    assert (
        GraphQLWriteSafetyPolicy()
        .assess(request)
        .can_execute
        is True
    )


def test_mutation_is_blocked_by_default() -> None:
    request = GraphQLWriteRequest.create(
        "mutation UpdateProject { updateProject }",
        operation_name="UpdateProject",
    )

    result = GraphQLWriteSafetyPolicy().assess(
        request
    )

    assert (
        request.operation_kind
        is GraphQLOperationKind.MUTATION
    )
    assert result.state is GraphQLWriteSafetyState.BLOCKED
    assert result.can_execute is False
    assert "disabled" in result.reason


def test_enabled_mutation_still_requires_explicit_approval() -> None:
    request = GraphQLWriteRequest.create(
        "mutation UpdateProject { updateProject }",
        operation_name="UpdateProject",
    )

    result = GraphQLWriteSafetyPolicy(
        mutations_enabled=True
    ).assess(request)

    assert (
        result.state
        is GraphQLWriteSafetyState.APPROVAL_REQUIRED
    )
    assert result.can_execute is False


def test_exact_mutation_approval_can_be_assessed_as_approved() -> None:
    request = GraphQLWriteRequest.create(
        (
            "mutation UpdateProject($id: ID!) "
            "{ updateProject(id: $id) }"
        ),
        variables={"id": "42"},
        operation_name="UpdateProject",
    )
    approval = GraphQLMutationApproval.create(
        request
    )

    result = GraphQLWriteSafetyPolicy(
        mutations_enabled=True
    ).assess(
        request,
        approval=approval,
    )

    assert (
        result.state
        is GraphQLWriteSafetyState.APPROVED
    )
    assert result.can_execute is True


def test_approval_is_invalid_after_variable_change() -> None:
    original = GraphQLWriteRequest.create(
        (
            "mutation UpdateProject($id: ID!) "
            "{ updateProject(id: $id) }"
        ),
        variables={"id": "42"},
        operation_name="UpdateProject",
    )
    approval = GraphQLMutationApproval.create(
        original
    )

    changed = GraphQLWriteRequest.create(
        original.document,
        variables={"id": "43"},
        operation_name="UpdateProject",
    )

    result = GraphQLWriteSafetyPolicy(
        mutations_enabled=True
    ).assess(
        changed,
        approval=approval,
    )

    assert original.fingerprint != changed.fingerprint
    assert result.state is GraphQLWriteSafetyState.BLOCKED
    assert result.can_execute is False
    assert "does not match" in result.reason


def test_approval_is_invalid_after_document_change() -> None:
    original = GraphQLWriteRequest.create(
        "mutation UpdateProject { updateProject }",
        operation_name="UpdateProject",
    )
    approval = GraphQLMutationApproval.create(
        original
    )

    changed = GraphQLWriteRequest.create(
        (
            "mutation UpdateProject "
            "{ updateProject otherField }"
        ),
        operation_name="UpdateProject",
    )

    result = GraphQLWriteSafetyPolicy(
        mutations_enabled=True
    ).assess(
        changed,
        approval=approval,
    )

    assert result.state is GraphQLWriteSafetyState.BLOCKED


def test_subscription_is_always_blocked() -> None:
    request = GraphQLWriteRequest.create(
        "subscription Changes { changed }",
        operation_name="Changes",
    )

    result = GraphQLWriteSafetyPolicy(
        mutations_enabled=True
    ).assess(request)

    assert (
        request.operation_kind
        is GraphQLOperationKind.SUBSCRIPTION
    )
    assert result.state is GraphQLWriteSafetyState.BLOCKED
    assert result.can_execute is False


def test_approval_cannot_target_query() -> None:
    request = GraphQLWriteRequest.create(
        "query Lookup { __typename }",
        operation_name="Lookup",
    )

    with pytest.raises(
        GraphQLWriteSafetyError,
        match="only target a mutation",
    ):
        GraphQLMutationApproval.create(request)


def test_named_operation_selects_correct_kind() -> None:
    document = """
    query ReadProject { project }
    mutation UpdateProject { updateProject }
    """

    assert (
        detect_operation_kind(
            document,
            operation_name="ReadProject",
        )
        is GraphQLOperationKind.QUERY
    )

    assert (
        detect_operation_kind(
            document,
            operation_name="UpdateProject",
        )
        is GraphQLOperationKind.MUTATION
    )


def test_multiple_operations_without_name_are_unknown() -> None:
    request = GraphQLWriteRequest.create(
        """
        query ReadProject { project }
        mutation UpdateProject { updateProject }
        """
    )

    assert (
        request.operation_kind
        is GraphQLOperationKind.UNKNOWN
    )
    assert (
        GraphQLWriteSafetyPolicy()
        .assess(request)
        .state
        is GraphQLWriteSafetyState.BLOCKED
    )


def test_fragment_before_query_does_not_hide_operation_kind() -> None:
    document = """
    fragment ProjectFields on Project {
      id
    }

    query ReadProject {
      project {
        ...ProjectFields
      }
    }
    """

    assert (
        detect_operation_kind(
            document,
            operation_name="ReadProject",
        )
        is GraphQLOperationKind.QUERY
    )


def test_non_json_variables_are_rejected() -> None:
    with pytest.raises(
        GraphQLWriteSafetyError,
        match="JSON serializable",
    ):
        GraphQLWriteRequest.create(
            "mutation UpdateProject { updateProject }",
            variables={"ids": {1, 2}},
            operation_name="UpdateProject",
        )


def test_request_retains_detached_variables_and_fingerprints_scope() -> None:
    variables = {"input": {"name": "Ada"}}
    first = GraphQLWriteRequest.create(
        "mutation Update($input: Input!) { update(input: $input) }",
        variables=variables,
        operation_name="Update",
        request_scope="endpoint:hero-a",
    )
    variables["input"]["name"] = "changed"
    other_scope = GraphQLWriteRequest.create(
        first.document,
        variables={"input": {"name": "Ada"}},
        operation_name="Update",
        request_scope="endpoint:hero-b",
    )

    assert first.variables == {"input": {"name": "Ada"}}
    assert first.request_scope == "endpoint:hero-a"
    assert first.fingerprint != other_scope.fingerprint
    with pytest.raises(TypeError):
        first.variables["input"] = {}  # type: ignore[index]


def test_variable_key_order_does_not_change_fingerprint() -> None:
    first = GraphQLWriteRequest.create(
        "mutation UpdateProject { updateProject }",
        variables={"a": 1, "b": 2},
        operation_name="UpdateProject",
    )
    second = GraphQLWriteRequest.create(
        "mutation UpdateProject { updateProject }",
        variables={"b": 2, "a": 1},
        operation_name="UpdateProject",
    )

    assert first.fingerprint == second.fingerprint
