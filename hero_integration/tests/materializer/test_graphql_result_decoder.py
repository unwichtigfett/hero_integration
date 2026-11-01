import json
import unittest

from hero_integration.graphql_protocol.transport import Response
from hero_integration.graphql_protocol.graphql_operation_materializer import (
    GraphQLOperationRequest,
    GraphQLResultBinding,
)
from hero_integration.graphql_protocol.graphql_result_decoder import (
    GraphQLResultDecodeError,
    decode_graphql_result,
)
from hero_integration.graphql_protocol.operation_kind import (
    GraphQLOperationKind,
)


def make_binding(
    selection_path,
    response_path,
    graphql_type="String",
):
    return GraphQLResultBinding(
        selection_path=selection_path,
        protocol_field_node_ids=tuple(
            f"field:test.{part}"
            for part in selection_path
        ),
        response_path=response_path,
        graphql_type=graphql_type,
    )


def make_request(*bindings):
    return GraphQLOperationRequest(
        binding_id="test.binding",
        schema_fingerprint="test-schema",
        operation_node_id="operation:query:test",
        operation_kind=GraphQLOperationKind.QUERY,
        protocol_field_name="test",
        operation_name="TestOperation",
        root_alias="result",
        document="query TestOperation { result: test }",
        argument_bindings=(),
        result_bindings=tuple(bindings),
        request_fingerprint="test-request",
        mutation_safety_request=None,
        _canonical_variables="{}",
    )


def make_response(
    payload,
    success=True,
    transport=True,
):
    return Response(
        success,
        json.dumps(payload),
        transport,
    )


class GraphQLResultDecoderTests(unittest.TestCase):

    def assert_code(self, expected, fn):
        with self.assertRaises(
            GraphQLResultDecodeError
        ) as caught:
            fn()

        self.assertEqual(
            caught.exception.code,
            expected,
        )

        return caught.exception

    def test_nested_scalar_path(self):
        decoded = decode_graphql_result(
            make_request(
                make_binding(
                    ("project", "name"),
                    (
                        "data",
                        "result",
                        "project",
                        "name",
                    ),
                )
            ),
            make_response(
                {
                    "data": {
                        "result": {
                            "project": {
                                "name": "Alpha"
                            }
                        }
                    }
                }
            ),
        )

        self.assertEqual(
            decoded.value_for(
                ("project", "name")
            ),
            "Alpha",
        )

    def test_nullable_parent_preserves_null(self):
        decoded = decode_graphql_result(
            make_request(
                make_binding(
                    ("project", "name"),
                    (
                        "data",
                        "result",
                        "project",
                        "name",
                    ),
                    "String!",
                )
            ),
            make_response(
                {
                    "data": {
                        "result": {
                            "project": None
                        }
                    }
                }
            ),
        )

        self.assertIsNone(
            decoded.value_for(
                ("project", "name")
            )
        )

    def test_missing_path_fails_closed(self):
        self.assert_code(
            "RESULT_RESPONSE_PATH_MISSING",
            lambda: decode_graphql_result(
                make_request(
                    make_binding(
                        ("name",),
                        (
                            "data",
                            "result",
                            "name",
                        ),
                    )
                ),
                make_response(
                    {
                        "data": {
                            "result": {}
                        }
                    }
                ),
            ),
        )

    def test_collection_projection_preserves_order(self):
        decoded = decode_graphql_result(
            make_request(
                make_binding(
                    ("email",),
                    (
                        "data",
                        "result",
                        "email",
                    ),
                )
            ),
            make_response(
                {
                    "data": {
                        "result": [
                            {
                                "email":
                                    "a@example.test"
                            },
                            {
                                "email":
                                    None
                            },
                            {
                                "email":
                                    "b@example.test"
                            },
                        ]
                    }
                }
            ),
        )

        self.assertEqual(
            decoded.value_for(
                ("email",)
            ),
            (
                "a@example.test",
                None,
                "b@example.test",
            ),
        )

    def test_collection_missing_member_fails_closed(self):
        self.assert_code(
            "RESULT_RESPONSE_PATH_MISSING",
            lambda: decode_graphql_result(
                make_request(
                    make_binding(
                        ("email",),
                        (
                            "data",
                            "result",
                            "email",
                        ),
                    )
                ),
                make_response(
                    {
                        "data": {
                            "result": [
                                {
                                    "email":
                                        "a@example.test"
                                },
                                {},
                            ]
                        }
                    }
                ),
            ),
        )

    def test_scalar_list_leaf_is_not_projection(self):
        decoded = decode_graphql_result(
            make_request(
                make_binding(
                    (),
                    (
                        "data",
                        "result",
                    ),
                    "[Int!]!",
                )
            ),
            make_response(
                {
                    "data": {
                        "result": [
                            1,
                            2,
                            3,
                        ]
                    }
                }
            ),
        )

        self.assertEqual(
            decoded.value_for(()),
            (1, 2, 3),
        )

    def test_enum_or_custom_scalar_leaf(self):
        decoded = decode_graphql_result(
            make_request(
                make_binding(
                    ("status",),
                    (
                        "data",
                        "result",
                        "status",
                    ),
                    "ProjectStatus!",
                )
            ),
            make_response(
                {
                    "data": {
                        "result": {
                            "status":
                                "ACTIVE"
                        }
                    }
                }
            ),
        )

        self.assertEqual(
            decoded.value_for(
                ("status",)
            ),
            "ACTIVE",
        )

    def test_structured_named_leaf_is_rejected(self):
        self.assert_code(
            "RESULT_TYPE_INVALID",
            lambda: decode_graphql_result(
                make_request(
                    make_binding(
                        ("status",),
                        (
                            "data",
                            "result",
                            "status",
                        ),
                        "ProjectStatus",
                    )
                ),
                make_response(
                    {
                        "data": {
                            "result": {
                                "status": {
                                    "value":
                                        "ACTIVE"
                                }
                            }
                        }
                    }
                ),
            ),
        )

    def test_standard_scalar_type_is_checked(self):
        self.assert_code(
            "RESULT_TYPE_INVALID",
            lambda: decode_graphql_result(
                make_request(
                    make_binding(
                        ("id",),
                        (
                            "data",
                            "result",
                            "id",
                        ),
                        "Int!",
                    )
                ),
                make_response(
                    {
                        "data": {
                            "result": {
                                "id":
                                    "not-an-int"
                            }
                        }
                    }
                ),
            ),
        )

    def test_graphql_error_is_fail_closed(self):
        error = self.assert_code(
            "RESULT_GRAPHQL_ERROR",
            lambda: decode_graphql_result(
                make_request(
                    make_binding(
                        ("name",),
                        (
                            "data",
                            "result",
                            "name",
                        ),
                    )
                ),
                make_response(
                    {
                        "data": {
                            "result":
                                None
                        },
                        "errors": [
                            {
                                "message":
                                    "private upstream text",
                                "path": [
                                    "result"
                                ],
                            }
                        ],
                    },
                    success=False,
                ),
            ),
        )

        self.assertNotIn(
            "private upstream text",
            str(error),
        )

    def test_transport_failure_is_distinct(self):
        self.assert_code(
            "RESULT_TRANSPORT_FAILED",
            lambda: decode_graphql_result(
                make_request(
                    make_binding(
                        ("name",),
                        (
                            "data",
                            "result",
                            "name",
                        ),
                    )
                ),
                Response(
                    False,
                    "{}",
                    False,
                ),
            ),
        )

    def test_successful_transport_with_partial_graphql_errors_still_fails_closed(self):
        request = make_request(make_binding(("name",), ("data", "result", "name")))
        response = make_response({
            "data": {"result": {"name": "visible"}},
            "errors": [{"message": "synthetic upstream detail", "path": ["result", "other"]}],
        }, success=True)
        error = self.assert_code("RESULT_GRAPHQL_ERROR", lambda: decode_graphql_result(request, response))
        self.assertNotIn("synthetic upstream detail", str(error))

    def test_malformed_json_is_rejected(self):
        self.assert_code(
            "RESULT_RESPONSE_INVALID",
            lambda: decode_graphql_result(
                make_request(
                    make_binding(
                        ("name",),
                        (
                            "data",
                            "result",
                            "name",
                        ),
                    )
                ),
                Response(
                    True,
                    "not-json",
                    True,
                ),
            ),
        )

    def test_selection_map_is_immutable(self):
        decoded = decode_graphql_result(
            make_request(
                make_binding(
                    ("name",),
                    (
                        "data",
                        "result",
                        "name",
                    ),
                )
            ),
            make_response(
                {
                    "data": {
                        "result": {
                            "name":
                                "Alpha"
                        }
                    }
                }
            ),
        )

        values = decoded.selection_map()

        self.assertEqual(
            values[("name",)],
            "Alpha",
        )

        with self.assertRaises(TypeError):
            values[("name",)] = "Changed"


if __name__ == "__main__":
    unittest.main()
