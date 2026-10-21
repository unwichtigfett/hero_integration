"""Small read-only HERO GraphQL client with no Frappe dependency."""
from __future__ import annotations

import json
import ssl
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit


class HeroClientError(RuntimeError):
    pass


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
    tokens = document.replace("{", " ").replace("}", " ").split()
    if any(token.lower() in {"mutation", "subscription"} for token in tokens):
        raise HeroClientError("Only HERO GraphQL queries are allowed.")


class HeroGraphQLClient:
    def __init__(
        self,
        endpoint: HeroEndpoint,
        token_provider: Callable[[], str],
        timeout: int = 30,
        max_retries: int = 2,
        retry_backoff_seconds: int = 1,
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

    def query(self, document: str, variables: Mapping[str, object]) -> dict[str, object]:
        assert_read_only(document)
        return self._post(document, variables)

    def introspect(self, document: str) -> dict[str, object]:
        """Execute the fixed read-only introspection document.

        Introspection uses the same HTTPS-only, allowlisted, no-redirect
        transport as queries, but its caller must still validate the complete
        response before persistence.
        """
        assert_read_only(document)
        return {"data": self._post(document, {})}

    def _post(self, document: str, variables: Mapping[str, object]) -> dict[str, object]:
        token = self.token_provider()
        if not isinstance(token, str) or not token.strip() or any(ord(char) < 32 for char in token):
            raise HeroClientError("HERO API token is missing or invalid.")
        body = json.dumps({"query": document, "variables": dict(variables)}, allow_nan=False).encode()
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
        if not isinstance(payload, dict) or payload.get("errors") or not isinstance(payload.get("data"), dict):
            raise HeroClientError("HERO response is invalid.")
        return payload["data"]

    def _request_with_retry(self, opener, request) -> dict[str, object]:
        for attempt in range(self.max_retries + 1):
            try:
                with opener.open(request, timeout=self.timeout) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                if not isinstance(payload, dict):
                    raise HeroClientError("HERO response is invalid.")
                return payload
            except urllib.error.HTTPError as exc:
                retryable = exc.code >= 500
            except (urllib.error.URLError, OSError):
                retryable = True
            except (UnicodeError, ValueError) as exc:
                raise HeroClientError("HERO response is invalid.") from exc
            if not retryable or attempt == self.max_retries:
                raise HeroClientError("HERO request failed.") from None
            time.sleep(self.retry_backoff_seconds * (2**attempt))
        raise HeroClientError("HERO request failed.")
