"""Read the explicitly bound HERO project address, never a customer fallback."""
from __future__ import annotations

from hashlib import sha256
import json

import frappe

from hero_integration.client.graphql import HeroClientError, HeroGraphQLClient
from hero_integration.client.settings import HeroConfigurationError, get_configuration
from hero_integration.services.project_workspace import _read, _text


QUERY = (
    "query HeroProjectWorkspaceAddress($id: Int) { project_match(project_match_id: $id) "
    "{ id address { full_address } } }"
)
CACHE_SECONDS = 300


def _binding(project):
    # This owner validates unique binding, synchronization state and the currently
    # configured HERO account. It exposes no Integration Link fields to callers.
    from hero_integration.services.project_send import _bound_link
    return _bound_link(project)


def _client(link):
    config = get_configuration(fresh=True)
    if not config.source_binding_confirmed or (link.source_instance or "default") != config.source_instance:
        raise HeroConfigurationError("The HERO source binding requires review.")
    return HeroGraphQLClient(config.endpoint, lambda: config.api_token,
        min(config.timeout_seconds, 15), max_retries=0,
        max_response_bytes=256 * 1024, requests_per_minute=60), config.source_binding_revision


@frappe.whitelist(methods=["GET"])
def get_site_address(project: str):
    """Project read access authorizes only its own confirmed HERO address value."""
    if not getattr(frappe.session, "user", None) or frappe.session.user == "Guest":
        frappe.throw(frappe._("Log in to view project information."), frappe.PermissionError)
    project = _text(project, 140)
    _read("Project", project)  # Always before binding, cache or upstream access.
    from hero_integration.services.project_send import ProjectSendError

    try:
        link = _binding(project)
    except ProjectSendError:
        return {"project": project, "state": "not_linked", "address": None}
    except HeroConfigurationError:
        return {"project": project, "state": "unavailable", "address": None}
    identity = str(link.source_id)
    if not identity.isascii() or not identity.isdigit() or not 0 < int(identity) <= 2147483647:
        return {"project": project, "state": "unavailable", "address": None}
    try:
        client, revision = _client(link)
    except (HeroClientError, HeroConfigurationError):
        return {"project": project, "state": "unavailable", "address": None}
    key = "project-site-address:" + sha256(json.dumps(
        [frappe.local.site, link.source_instance, identity, link.name, client.endpoint.url, revision],
        default=str,
    ).encode()).hexdigest()
    cached = frappe.cache.get_value(key)
    if cached is not None:
        return {"project": project, **cached}
    try:
        data = client.query(QUERY, {"id": int(identity)})
    except HeroClientError:
        return {"project": project, "state": "unavailable", "address": None}
    value = data.get("project_match") if isinstance(data, dict) else None
    if not isinstance(value, dict) or str(value.get("id")) != identity:
        return {"project": project, "state": "unavailable", "address": None}
    address = value.get("address")
    text = address.get("full_address") if isinstance(address, dict) else None
    text = text.strip() if isinstance(text, str) else ""
    if len(text) > 4096:
        return {"project": project, "state": "unavailable", "address": None}
    # Only this explicitly selected business field crosses the source boundary.
    result = {"state": "ready" if text else "missing", "address": text or None,
        "source": "ProjectMatch.address.full_address"}
    frappe.cache.set_value(key, result, expires_in_sec=CACHE_SECONDS)
    return {"project": project, **result}
