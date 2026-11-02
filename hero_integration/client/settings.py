from __future__ import annotations

from dataclasses import dataclass, field

import frappe

from .graphql import HeroClientError, HeroEndpoint, validate_endpoint


class HeroConfigurationError(RuntimeError):
    pass


BINDING_FIELDS = ("hero_api_url", "hero_api_token", "hero_source_instance")


@dataclass(frozen=True)
class HeroConfiguration:
    endpoint: HeroEndpoint
    api_token: str = field(repr=False)
    timeout_seconds: int = 30
    source_instance: str = "default"
    source_binding_revision: int = 0
    source_binding_confirmed: bool = True


def next_source_binding(revision: int, confirmed_revision: int, *, binding_changed: bool, confirm: bool) -> tuple[int, int]:
    """Return binding and confirmed revisions after Settings save.
    Endpoint, token or source changes require explicit confirmation because HERO
    provides no independent account identity. Legacy bindings start at revision 0."""
    revision = int(revision or 0) + (1 if binding_changed else 0)
    confirmed_revision = revision if confirm else int(confirmed_revision or 0)
    return revision, confirmed_revision


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
    revision = int(getattr(settings, "source_binding_revision", 0) or 0)
    return HeroConfiguration(
        endpoint=endpoint, api_token=token, timeout_seconds=int(settings.timeout_seconds or 30),
        source_instance=source_instance, source_binding_revision=revision,
        source_binding_confirmed=int(getattr(settings, "source_binding_confirmed_revision", 0) or 0) == revision,
    )
