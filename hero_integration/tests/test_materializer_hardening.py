"""Materializer and decoder hardening: strict GraphQL JSON, finite floats and 32-bit Int arguments."""
from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock

from hero_integration.client.graphql import HeroClientError, HeroEndpoint, HeroGraphQLClient
from hero_integration.graphql_protocol import graphql_result_decoder
from hero_integration.graphql_protocol.graphql_argument_values import GraphQLArgumentValueError, _parse, _validate
from hero_integration.graphql_protocol.graphql_response import GraphQLResponseEnvelopeError, parse_graphql_response, strict_json_loads
from hero_integration.graphql_protocol.graphql_operation_materializer import GraphQLOperationBinding, GraphQLOperationMaterializationError
from hero_integration.graphql_protocol.operation_kind import GraphQLOperationKind
from hero_integration.graphql_protocol.schema import GraphQLTypeRef


class _Body:
    def __init__(self, text: str):
        self.text = text.encode()

    def read(self, size=-1):
        return self.text

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class TestStrictGraphQLJson(unittest.TestCase):
    def test_non_finite_constants_and_duplicate_keys_are_rejected(self):
        for text in ('{"a": NaN}', '{"a": Infinity}', '{"a": -Infinity}', '{"a": 1, "a": 2}', '{"x": {"b": 1, "b": 1}}'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                strict_json_loads(text)
        self.assertEqual(strict_json_loads('{"a": [1, 2.5, null, "NaN"]}'), {"a": [1, 2.5, None, "NaN"]})

    def test_envelope_parser_rejects_invalid_graphql_json(self):
        for text in ('{"data": {"v": NaN}}', '{"data": {}, "data": {}}', "[" * 100000 + "]" * 100000):
            with self.subTest(text=text[:30]), self.assertRaises(GraphQLResponseEnvelopeError):
                parse_graphql_response(text)

    def test_read_client_treats_invalid_graphql_json_as_an_invalid_response(self):
        client = HeroGraphQLClient(HeroEndpoint("https://hero.example/graphql", "hero.example"), lambda: "token", max_retries=3)
        for text in ('{"data": {"v": NaN}}', '{"data": {"a": 1, "a": 2}}', "[" * 100000 + "]" * 100000):
            opener = MagicMock()
            opener.open.return_value = _Body(text)
            with self.subTest(text=text[:30]), self.assertRaises(HeroClientError) as caught:
                client._request_with_retry(opener, object())
            self.assertEqual(caught.exception.code, "INVALID_RESPONSE")
            self.assertEqual(opener.open.call_count, 1, "an invalid body is not retried")


class TestMaterializedNames(unittest.TestCase):
    def test_operation_and_alias_reject_graphql_syntax(self):
        binding = dict(binding_id="binding", schema_fingerprint="a" * 64, operation_node_id="node",
                       operation_kind=GraphQLOperationKind.QUERY, operation_name="Read", root_alias="result")
        GraphQLOperationBinding(**binding)
        for field in ("operation_name", "root_alias"):
            for value in ("9leading", "name { id }", "name: other", "name\nmutation"):
                with self.subTest(field=field, value=value), self.assertRaises(GraphQLOperationMaterializationError):
                    GraphQLOperationBinding(**{**binding, field: value})


class TestDecoderValues(unittest.TestCase):
    def test_float_results_must_be_finite(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value), self.assertRaises(graphql_result_decoder.GraphQLResultDecodeError):
                graphql_result_decoder._named_type(value, "Float")
        graphql_result_decoder._named_type(14.5, "Float")
        graphql_result_decoder._named_type(3, "Float")


class TestArgumentValues(unittest.TestCase):
    INT = GraphQLTypeRef("SCALAR", "Int")
    FLOAT = GraphQLTypeRef("SCALAR", "Float")

    def test_int_arguments_are_32_bit_for_queries_too(self):
        self.assertEqual(_validate(2**31 - 1, self.INT, "first"), 2**31 - 1)
        for value in (2**31, -(2**31) - 1, 10**20):
            with self.subTest(value=value), self.assertRaisesRegex(GraphQLArgumentValueError, "out of range"):
                _validate(value, self.INT, "first")

    def test_float_arguments_must_be_finite(self):
        for text in ("nan", "inf", "-Infinity"):
            with self.subTest(text=text), self.assertRaisesRegex(GraphQLArgumentValueError, "finite"):
                _parse(text, self.FLOAT, "ratio")
        self.assertEqual(_parse("1.5", self.FLOAT, "ratio"), 1.5)


if __name__ == "__main__":
    unittest.main()
