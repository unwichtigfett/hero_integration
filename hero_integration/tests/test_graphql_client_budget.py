"""Bounded HERO transport: retries, 429, size cap, strict JSON, shared rate."""
from __future__ import annotations

import io
import json
import unittest
import urllib.error
from email.message import Message
from unittest import mock

from hero_integration.client import graphql
from hero_integration.client.graphql import HeroClientError, HeroGraphQLClient, validate_endpoint

QUERY = "query Q { viewer { id } }"


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(code, retry_after=None):
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError("https://hero.synthetic.invalid/graphql", code, "synthetic", headers, None)


class _Opener:
    def __init__(self, *outcomes):
        self.outcomes, self.calls = list(outcomes), 0

    def open(self, request, timeout):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return _Response(outcome if isinstance(outcome, bytes) else json.dumps(outcome).encode())


class TestGraphQLClientBudget(unittest.TestCase):
    def setUp(self):
        graphql._NEXT_REQUEST_AT.clear()
        self.endpoint = validate_endpoint("https://hero.synthetic.invalid/graphql", {"hero.synthetic.invalid"})

    def _query(self, opener, **client_args):
        client = HeroGraphQLClient(self.endpoint, lambda: "synthetic", **client_args)
        with mock.patch("hero_integration.client.graphql.urllib.request.build_opener", return_value=opener), \
             mock.patch("hero_integration.client.graphql.time.sleep") as sleep:
            try:
                return client.query(QUERY, {}), sleep
            except HeroClientError as error:
                return error, sleep

    def test_429_honours_bounded_retry_after(self):
        opener = _Opener(_http_error(429, "7"), {"data": {"viewer": {"id": 1}}})
        result, sleep = self._query(opener, max_retries=1)
        self.assertEqual(result, {"viewer": {"id": 1}})
        sleep.assert_called_once_with(7.0)

    def test_429_honours_an_http_date_retry_after(self):
        from email.utils import format_datetime
        from datetime import datetime, timedelta, timezone
        moment = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=30), usegmt=True)
        opener = _Opener(_http_error(429, moment), {"data": {"viewer": {"id": 1}}})
        result, sleep = self._query(opener, max_retries=1)
        self.assertEqual(result, {"viewer": {"id": 1}})
        self.assertTrue(25 <= sleep.call_args.args[0] <= 30, sleep.call_args)

    def test_server_errors_back_off_exponentially_with_jitter_then_stop(self):
        opener = _Opener(_http_error(503), _http_error(502), _http_error(500))
        error, sleep = self._query(opener, max_retries=2, retry_backoff_seconds=2)
        self.assertEqual((error.code, opener.calls), ("HTTP_500", 3))
        delays = [call.args[0] for call in sleep.call_args_list]
        self.assertTrue(2 <= delays[0] <= 4 and 4 <= delays[1] <= 8, delays)

    def test_client_errors_are_not_retried(self):
        opener = _Opener(_http_error(400), {"data": {}})
        error, sleep = self._query(opener, max_retries=3)
        self.assertEqual((error.code, opener.calls), ("HTTP_400", 1))
        sleep.assert_not_called()

    def test_timeouts_are_retried_within_budget(self):
        opener = _Opener(TimeoutError("synthetic"), {"data": {"viewer": None}})
        result, _ = self._query(opener, max_retries=1, retry_backoff_seconds=0)
        self.assertEqual(result, {"viewer": None})

    def test_oversized_non_json_and_partial_responses_are_rejected(self):
        error, _ = self._query(_Opener(b'{"data": {"v": "' + b"x" * 2048 + b'"}}'), max_retries=3, max_response_bytes=1024)
        self.assertEqual(error.code, "RESPONSE_TOO_LARGE")
        error, _ = self._query(_Opener(b'{"data": {"n": NaN}}'), max_retries=0)
        self.assertEqual(error.code, "INVALID_RESPONSE")
        error, _ = self._query(_Opener(b"<html>"), max_retries=0)
        self.assertEqual(error.code, "INVALID_RESPONSE")
        error, _ = self._query(_Opener({"data": {"viewer": None}, "errors": [{"message": "x"}]}), max_retries=0)
        # Partial data is never returned as a result; it travels only on the error.
        self.assertEqual((error.code, error.data), ("GRAPHQL_ERRORS", {"viewer": None}))
        error, _ = self._query(_Opener({"data": None}), max_retries=0)
        self.assertEqual(error.code, "MISSING_DATA")

    def test_rate_budget_is_shared_by_all_clients_of_one_host(self):
        first, second = (HeroGraphQLClient(self.endpoint, lambda: "synthetic", requests_per_minute=30) for _ in range(2))
        opener = _Opener({"data": {}}, {"data": {}})
        clock = iter([100.0, 100.0])
        with mock.patch("hero_integration.client.graphql.urllib.request.build_opener", return_value=opener), \
             mock.patch("hero_integration.client.graphql.time.monotonic", side_effect=lambda: next(clock)), \
             mock.patch("hero_integration.client.graphql.time.sleep") as sleep:
            first.query(QUERY, {})
            second.query(QUERY, {})
        sleep.assert_called_once_with(2.0)

    def test_mutation_is_rejected_before_any_transport_or_retry(self):
        opener = _Opener()
        client = HeroGraphQLClient(self.endpoint, lambda: "synthetic", max_retries=3)
        with mock.patch("hero_integration.client.graphql.urllib.request.build_opener", return_value=opener):
            with self.assertRaises(HeroClientError):
                client.query("mutation M { delete(id: 1) }", {})
        self.assertEqual(opener.calls, 0)


if __name__ == "__main__":
    unittest.main()
