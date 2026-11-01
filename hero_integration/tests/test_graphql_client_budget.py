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
from hero_integration.client.graphql_mutation import HeroMutationClient

QUERY = "query Q { viewer { id } }"


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
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
        rate_budget = mock.patch.dict(graphql._NEXT_REQUEST_AT, {}, clear=True)
        rate_budget.start()
        self.addCleanup(rate_budget.stop)
        self.endpoint = validate_endpoint("https://hero.synthetic.invalid/graphql", {"hero.synthetic.invalid"})

    def _query(self, opener, **client_args):
        for outcome in opener.outcomes:
            if isinstance(outcome, urllib.error.HTTPError):
                self.addCleanup(outcome.close)
        client = HeroGraphQLClient(self.endpoint, lambda: "synthetic", **client_args)
        with mock.patch("hero_integration.client.graphql.urllib.request.build_opener", return_value=opener), \
             mock.patch("hero_integration.client.graphql.time.sleep") as sleep:
            try:
                return client.query(QUERY, {}), sleep
            except HeroClientError as error:
                return error, sleep

    def test_endpoint_rejects_untrusted_authority_and_url_components(self):
        allowed = {"hero.synthetic.invalid"}
        for url in (
            "http://hero.synthetic.invalid/graphql",
            "https://attacker.synthetic.invalid/graphql",
            "https://hero.synthetic.invalid@attacker.synthetic.invalid/graphql",
            "https://user:password@hero.synthetic.invalid/graphql",
            "https://hero.synthetic.invalid/graphql?token=synthetic",
            "https://hero.synthetic.invalid/graphql#fragment",
        ):
            with self.subTest(url=url), self.assertRaises(HeroClientError):
                validate_endpoint(url, allowed)

    def test_mutation_does_not_request_credentials_or_open_transport(self):
        token = mock.Mock(return_value="synthetic")
        client = HeroGraphQLClient(self.endpoint, token)
        with mock.patch("hero_integration.client.graphql.urllib.request.build_opener") as opener:
            with self.assertRaises(HeroClientError):
                client.query("mutation M { updateThing { id } }", {})
        token.assert_not_called()
        opener.assert_not_called()

    def test_429_honours_bounded_retry_after(self):
        opener = _Opener(_http_error(429, "7"), {"data": {"viewer": {"id": 1}}})
        result, sleep = self._query(opener, max_retries=1)
        self.assertEqual(result, {"viewer": {"id": 1}})
        sleep.assert_called_once_with(7.0)

    def test_429_retry_after_is_capped_and_invalid_values_use_backoff(self):
        for header, delay in (("9999", 300), ("not-a-date", 0), ("-2", 0)):
            with self.subTest(header=header):
                opener = _Opener(_http_error(429, header), {"data": {"viewer": {"id": 1}}})
                result, sleep = self._query(opener, max_retries=1, retry_backoff_seconds=0)
                self.assertEqual(result, {"viewer": {"id": 1}})
                self.assertEqual(opener.calls, 2)
                sleep.assert_called_once_with(delay)

    def test_429_honours_an_http_date_retry_after(self):
        from email.utils import format_datetime
        from datetime import datetime, timedelta, timezone
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        moment = format_datetime(now + timedelta(seconds=30), usegmt=True)
        opener = _Opener(_http_error(429, moment), {"data": {"viewer": {"id": 1}}})
        with mock.patch("hero_integration.client.graphql.time.time", return_value=now.timestamp()):
            result, sleep = self._query(opener, max_retries=1)
        self.assertEqual(result, {"viewer": {"id": 1}})
        sleep.assert_called_once_with(30.0)

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

    def test_redirect_response_is_not_retried_or_followed(self):
        opener = _Opener(_http_error(302), {"data": {}})
        error, sleep = self._query(opener, max_retries=3)
        self.assertEqual((error.code, opener.calls), ("HTTP_302", 1))
        sleep.assert_not_called()
        handler = graphql._NoRedirectHandler()
        self.assertIsNone(handler.redirect_request(None, None, 302, "Found", {},
                                                   "https://attacker.synthetic.invalid/"))

    def test_query_constructs_an_opener_with_proxy_disabled_and_verified_tls(self):
        opener = _Opener({"data": {}})
        client = HeroGraphQLClient(self.endpoint, lambda: "synthetic")
        with mock.patch("hero_integration.client.graphql.urllib.request.build_opener", return_value=opener) as build:
            self.assertEqual(client.query(QUERY, {}), {})
        proxy, https, redirect = build.call_args.args
        self.assertIsInstance(proxy, graphql.urllib.request.ProxyHandler)
        self.assertEqual(proxy.proxies, {})
        self.assertIsInstance(https, graphql.urllib.request.HTTPSHandler)
        self.assertTrue(https._context.check_hostname)
        self.assertEqual(https._context.verify_mode, graphql.ssl.CERT_REQUIRED)
        self.assertIsInstance(redirect, graphql._NoRedirectHandler)

    def test_timeouts_are_retried_within_budget(self):
        opener = _Opener(TimeoutError("synthetic"), {"data": {"viewer": None}})
        result, _ = self._query(opener, max_retries=1, retry_backoff_seconds=0)
        self.assertEqual(result, {"viewer": None})

    def test_mutation_timeout_is_never_retried(self):
        opener = _Opener(TimeoutError("synthetic"), {"data": {}})
        client = HeroMutationClient(self.endpoint, lambda: "synthetic")
        with mock.patch("hero_integration.client.graphql.urllib.request.build_opener", return_value=opener), \
             mock.patch("hero_integration.client.graphql.time.sleep") as sleep:
            with self.assertRaises(HeroClientError) as caught:
                client.execute_once("mutation M { updateThing { id } }", {})
        self.assertEqual((caught.exception.code, opener.calls), ("TRANSPORT", 1))
        sleep.assert_not_called()

    def test_mutation_retryable_http_errors_never_trigger_a_second_send(self):
        for status in (429, 500, 503):
            with self.subTest(status=status):
                failure = _http_error(status, "7")
                self.addCleanup(failure.close)
                opener = _Opener(failure, {"data": {"updateThing": {"id": "unexpected"}}})
                client = HeroMutationClient(self.endpoint, lambda: "synthetic")
                with mock.patch("hero_integration.client.graphql.urllib.request.build_opener", return_value=opener), \
                     mock.patch("hero_integration.client.graphql.time.sleep") as sleep:
                    with self.assertRaises(HeroClientError) as caught:
                        client.execute_once("mutation M { updateThing { id } }", {})
                self.assertEqual((caught.exception.code, opener.calls), (f"HTTP_{status}", 1))
                sleep.assert_not_called()

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

    def test_upstream_error_details_remain_sensitive_even_when_exception_text_is_generic(self):
        upstream = "synthetic confidential upstream detail"
        partial = {"viewer": {"id": "synthetic private value"}}
        error, _ = self._query(_Opener({"data": partial, "errors": [{"message": upstream}]}), max_retries=0)
        self.assertEqual(error.code, "GRAPHQL_ERRORS")
        self.assertNotIn(upstream, str(error))
        self.assertEqual(error.details, (upstream,))
        self.assertEqual(error.data, partial)

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
