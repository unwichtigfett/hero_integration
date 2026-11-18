"""Read path hardening: bounded responses, rate limits and source input checks. HERO is never contacted."""
from __future__ import annotations

import io
import json
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

from hero_integration.client import graphql
from hero_integration.client.graphql import HeroClientError, HeroEndpoint, HeroGraphQLClient
from hero_integration.tests.saved_reader_fixture import versioned_contract_source


class _Body:
    def __init__(self, payload: bytes):
        self.payload, self.requested = payload, []

    def read(self, size=-1):
        self.requested.append(size)
        return self.payload if size is None or size < 0 else self.payload[:size]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _client(**kwargs):
    return HeroGraphQLClient(HeroEndpoint("https://hero.example/graphql", "hero.example"), lambda: "token", **kwargs)


def _http_error(code, retry_after=None):
    headers = {"Retry-After": retry_after} if retry_after is not None else {}
    return urllib.error.HTTPError("https://hero.example/graphql", code, "error", headers, io.BytesIO(b""))


class TestBoundedResponses(unittest.TestCase):
    def test_oversized_body_is_rejected_without_reading_it_completely_or_retrying(self):
        client = _client(max_retries=2, max_response_bytes=1024)
        body = _Body(b"{" + b" " * 5000 + b"}")
        opener = MagicMock()
        opener.open.return_value = body
        with patch.object(graphql.time, "sleep") as sleep, self.assertRaises(HeroClientError) as caught:
            client._request_with_retry(opener, object())
        self.assertEqual(caught.exception.code, "RESPONSE_TOO_LARGE")
        self.assertEqual(body.requested, [1025], "at most one byte past the bound is read")
        self.assertEqual(opener.open.call_count, 1)
        sleep.assert_not_called()

    def test_regular_body_within_the_bound_is_parsed(self):
        opener = MagicMock()
        opener.open.return_value = _Body(json.dumps({"data": {"ok": True}}).encode())
        self.assertEqual(_client()._request_with_retry(opener, object()), {"data": {"ok": True}})

    def test_size_limit_must_stay_within_the_hard_bound(self):
        for value in (0, 1023, graphql.MAX_RESPONSE_BYTES + 1, True, "1024"):
            with self.subTest(value=value), self.assertRaises(HeroClientError):
                _client(max_response_bytes=value)


class TestRateLimits(unittest.TestCase):
    def test_429_is_retried_with_a_bounded_retry_after(self):
        opener = MagicMock()
        opener.open.side_effect = [_http_error(429, "2"), _Body(b'{"data": {}}')]
        with patch.object(graphql.time, "sleep") as sleep:
            self.assertEqual(_client(max_retries=1, retry_backoff_seconds=30)._request_with_retry(opener, object()), {"data": {}})
        sleep.assert_called_once_with(2)

        opener.open.side_effect = [_http_error(429, "99999"), _Body(b'{"data": {}}')]
        with patch.object(graphql.time, "sleep") as sleep:
            _client(max_retries=1)._request_with_retry(opener, object())
        sleep.assert_called_once_with(graphql.MAX_RETRY_AFTER_SECONDS)

        opener.open.side_effect = [_http_error(429, "Wed, 21 Oct 2026 07:28:00 GMT"), _Body(b'{"data": {}}')]
        with patch.object(graphql.time, "sleep") as sleep:
            _client(max_retries=1, retry_backoff_seconds=3)._request_with_retry(opener, object())
        # A past HTTP date falls back to the jittered exponential backoff.
        sleep.assert_called_once()
        self.assertTrue(3 <= sleep.call_args.args[0] <= 6, sleep.call_args)

    def test_without_retries_429_and_4xx_fail_once(self):
        for code in (429, 400, 401):
            opener = MagicMock()
            opener.open.side_effect = [_http_error(code, "1")]
            with self.subTest(code=code), patch.object(graphql.time, "sleep") as sleep, self.assertRaises(HeroClientError) as caught:
                _client(max_retries=0 if code == 429 else 3)._request_with_retry(opener, object())
            self.assertEqual(caught.exception.code, f"HTTP_{code}")
            self.assertEqual(opener.open.call_count, 1)
            sleep.assert_not_called()


class TestHeroSourceInput(unittest.TestCase):
    def test_offsets_are_validated_before_any_request(self):
        client = MagicMock()
        source = versioned_contract_source(client)
        for offset in (-1, True, 1.5, "10", 10_000_001):
            with self.subTest(offset=offset), self.assertRaises(ValueError):
                source.customers(offset)
        client.query.assert_not_called()

    def test_non_object_data_is_rejected(self):
        client = MagicMock()
        client.query.return_value = ["not", "an", "object"]
        with self.assertRaises(ValueError):
            versioned_contract_source(client).projects(0)


if __name__ == "__main__":
    unittest.main()
