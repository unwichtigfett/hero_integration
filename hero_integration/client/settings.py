from __future__ import annotations

from dataclasses import dataclass, field

import frappe

from .graphql import HeroClientError, HeroEndpoint, validate_endpoint


class HeroConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True)
class HeroConfiguration:
    endpoint: HeroEndpoint
    api_token: str = field(repr=False)
    timeout_seconds: int = 30
    source_instance: str = "default"


def get_configuration() -> HeroConfiguration:
    settings = frappe.get_single("HERO Integration Settings")
    hosts = {line.strip().lower() for line in (settings.allowed_hero_hosts or "").splitlines() if line.strip()}
    try:
        endpoint = validate_endpoint(settings.hero_api_url, hosts)
    except HeroClientError as exc:
        raise HeroConfigurationError(str(exc)) from exc
    token = settings.get_password("hero_api_token", raise_exception=False) or ""
    if not token:
        raise HeroConfigurationError("HERO API token is not configured.")
    source_instance = str(getattr(settings, "hero_source_instance", "default") or "default").strip()
    if not source_instance:
        raise HeroConfigurationError("HERO source instance is invalid.")
    return HeroConfiguration(
        endpoint=endpoint, api_token=token, timeout_seconds=int(settings.timeout_seconds or 30),
        source_instance=source_instance,
    )
