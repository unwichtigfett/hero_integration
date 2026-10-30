"""Small read-only HERO GraphQL client with no Frappe dependency."""
from __future__ import annotations

import json
import random
import ssl
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

from hero_integration.graphql_protocol.graphql_response import strict_json_loads


# Upper bound for one HERO response body. The largest regular response, a full schema
# introspection, is well below 5 MB; the bound only prevents unbounded memory use.
MAX_RESPONSE_BYTES = 20 * 1024 * 1024
# A server-requested Retry-After is honoured only up to this many seconds.
MAX_RETRY_AFTER_SECONDS = 300
# One request-rate budget per HERO host, shared by every client in this process.
_RATE_LOCK = threading.Lock()
_NEXT_REQUEST_AT: dict[str, float] = {}


class HeroClientError(RuntimeError):
    """A redacted, machine-readable HERO transport or GraphQL failure."""

    def __init__(self, message: str, *, code: str = "UNKNOWN", details: tuple[str, ...] = (), data: dict | None = None):
        super().__init__(message)
        self.code = code
        # Bounded GraphQL error messages only; never request data or credentials.
        self.details = tuple(details)
        # Partial GraphQL data delivered together with errors; kept in memory only.
        self.data = data


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True)
class HeroEndpoint:
    url: str
    host: str


def validate_endpoint(url: str, allowed_hosts: set[str]) -> HeroEndpoint:
    parsed = urlsplit((url or "").strip())
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise HeroClientError("HERO endpoint must use HTTPS and include a hostname.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise HeroClientError("HERO endpoint must not contain credentials, query, or fragment.")
    host = parsed.hostname.rstrip(".").lower()
    if not allowed_hosts or host not in allowed_hosts:
        raise HeroClientError("HERO endpoint host is not allowed.")
    port = parsed.port or 443
    authority = f"{host}:{port}" if port != 443 else host
    return HeroEndpoint(url=f"https://{authority}{parsed.path or '/'}", host=host)


def assert_read_only(document: str) -> None:
    """Accept exactly one GraphQL query operation (fragments allowed).

    The complete executable document is parsed: lexical token splitting let
    e.g. ``mutation($id: ID!)`` through and cannot handle literals or
    fragments.  Without graphql-core, reads fail closed.
    """
    try:
        from graphql import GraphQLError, parse
        from graphql.language import OperationDefinitionNode, OperationType
    except ImportError as error:
        raise HeroClientError("GraphQL query validation is unavailable.") from error
    try:
        parsed = parse(document)
    except (GraphQLError, TypeError) as error:
        raise HeroClientError("HERO GraphQL document is invalid.") from error
    operations = [item for item in parsed.definitions if isinstance(item, OperationDefinitionNode)]
    if len(operations) != 1 or operations[0].operation != OperationType.QUERY:
        raise HeroClientError("Only one HERO GraphQL query operation is allowed.")


class HeroGraphQLClient:
    def __init__(
        self,
        endpoint: HeroEndpoint,
        token_provider: Callable[[], str],
        timeout: int = 30,
        max_retries: int = 2,
        retry_backoff_seconds: int = 1,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
        requests_per_minute: int = 0,
    ):
        if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 120:
            raise HeroClientError("HERO timeout must be between 1 and 120 seconds.")
        self.endpoint = endpoint
        self.token_provider = token_provider
        self.timeout = timeout
        if not isinstance(max_retries, int) or isinstance(max_retries, bool) or not 0 <= max_retries <= 3:
            raise HeroClientError("HERO retry count must be between 0 and 3.")
        self.max_retries = max_retries
        if not isinstance(retry_backoff_seconds, int) or isinstance(retry_backoff_seconds, bool) or not 0 <= retry_backoff_seconds <= 3600:
            raise HeroClientError("HERO retry backoff must be between 0 and 3600 seconds.")
        self.retry_backoff_seconds = retry_backoff_seconds
        if not isinstance(max_response_bytes, int) or isinstance(max_response_bytes, bool) or not 1024 <= max_response_bytes <= MAX_RESPONSE_BYTES:
            raise HeroClientError("HERO response size limit is invalid.")
        self.max_response_bytes = max_response_bytes
        if not isinstance(requests_per_minute, int) or isinstance(requests_per_minute, bool) or not 0 <= requests_per_minute <= 10000:
            raise HeroClientError("HERO request rate must be between 0 (unbounded) and 10000 per minute.")
        self.min_interval_seconds = 60 / requests_per_minute if requests_per_minute else 0.0

    def query(self, document: str, variables: Mapping[str, object]) -> dict[str, object]:
        assert_read_only(document)
        return self._post(document, variables)

    def introspect(self, document: str, *, operation_name: str = "") -> dict[str, object]:
        """Execute the fixed read-only introspection document.

        Introspection uses the same HTTPS-only, allowlisted, no-redirect
        transport as queries, but its caller must still validate the complete
        response before persistence.
        """
        assert_read_only(document)
        return {"data": self._post(document, None, operation_name=operation_name)}

    def _post(
        self,
        document: str,
        variables: Mapping[str, object] | None,
        *,
        operation_name: str = "",
    ) -> dict[str, object]:
        token = self.token_provider()
        if not isinstance(token, str) or not token.strip() or any(ord(char) < 32 for char in token):
            raise HeroClientError("HERO API token is missing or invalid.")
        body_payload: dict[str, object] = {"query": document}
        if variables is not None:
            body_payload["variables"] = dict(variables)
        if operation_name:
            body_payload["operationName"] = operation_name
        body = json.dumps(body_payload, allow_nan=False).encode()
        request = urllib.request.Request(
            self.endpoint.url,
            data=body,
            method="POST",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "application/json"},
        )
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()),
            _NoRedirectHandler(),
        )
        payload = self._request_with_retry(opener, request)
        if not isinstance(payload, dict):
            raise HeroClientError("HERO response is not a JSON object.", code="INVALID_RESPONSE")
        if payload.get("errors"):
            errors = payload["errors"] if isinstance(payload["errors"], list) else []
            details = tuple(str(item.get("message") or "")[:300] for item in errors[:10] if isinstance(item, dict))
            partial = payload.get("data") if isinstance(payload.get("data"), dict) else None
            raise HeroClientError("HERO GraphQL rejected the request.", code="GRAPHQL_ERRORS", details=details, data=partial)
        if not isinstance(payload.get("data"), dict):
            raise HeroClientError("HERO GraphQL response contains no data.", code="MISSING_DATA")
        return payload["data"]

    def _await_rate_budget(self) -> None:
        if not self.min_interval_seconds:
            return
        with _RATE_LOCK:
            now = time.monotonic()
            start = max(now, _NEXT_REQUEST_AT.get(self.endpoint.host, 0.0))
            _NEXT_REQUEST_AT[self.endpoint.host] = start + self.min_interval_seconds
        if start > now:
            time.sleep(start - now)

    def _request_with_retry(self, opener, request) -> dict[str, object]:
        """Only HTTP 429, 5xx and transport failures are retried, with
        exponential backoff plus jitter or a bounded server Retry-After."""
        for attempt in range(self.max_retries + 1):
            base = self.retry_backoff_seconds * (2**attempt)
            delay = base + random.uniform(0, base) if base else 0
            self._await_rate_budget()
            try:
                with opener.open(request, timeout=self.timeout) as response:
                    # Read at most one byte past the bound, so an oversized body never fills memory.
                    body = response.read(self.max_response_bytes + 1)
                if len(body) > self.max_response_bytes:
                    raise HeroClientError("HERO response exceeds the size limit.", code="RESPONSE_TOO_LARGE")
                payload = strict_json_loads(body.decode("utf-8"))
                if not isinstance(payload, dict):
                    raise HeroClientError("HERO response is invalid.", code="INVALID_RESPONSE")
                return payload
            except urllib.error.HTTPError as exc:
                # 429 (rate limited) is retried like 5xx; a bounded Retry-After from HERO replaces the backoff.
                retryable, code = exc.code >= 500 or exc.code == 429, f"HTTP_{exc.code}"
                if exc.code == 429:
                    delay = _retry_after(exc, delay)
            except (urllib.error.URLError, OSError):
                retryable, code = True, "TRANSPORT"
            except (UnicodeError, ValueError, RecursionError) as exc:
                raise HeroClientError("HERO response is invalid.", code="INVALID_RESPONSE") from exc
            if not retryable or attempt == self.max_retries:
                raise HeroClientError("HERO request failed.", code=code) from None
            time.sleep(delay)
        raise HeroClientError("HERO request failed.")


def _retry_after(error, fallback: float) -> float:
    value = str((getattr(error, "headers", None) or {}).get("Retry-After", "") or "").strip()
    if value.isdigit():
        return min(int(value), MAX_RETRY_AFTER_SECONDS)
    if value:
        try:
            moment = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            moment = None
        remaining = moment.timestamp() - time.time() if moment is not None else 0.0
        if remaining > 0:
            return min(remaining, MAX_RETRY_AFTER_SECONDS)
    return min(fallback, MAX_RETRY_AFTER_SECONDS)
