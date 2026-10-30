"""Capture the live HERO schema as a validated snapshot.

One code path serves the explicit refresh action, the check that runs after
the connection settings are saved, and the periodic refresh. A schema that
did not change keeps its snapshot: the fingerprint is the identity, so no new
snapshot is created and nothing is re-inherited.

The first successful capture for the current source binding verifies the API
token. Only a verified binding is refreshed periodically, so an invalid token
never causes repeated HERO requests.
"""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
import json

import frappe
from frappe.utils import add_to_date, get_datetime, now_datetime

SETTINGS = "HERO Integration Settings"
SNAPSHOT = "HERO GraphQL Schema Snapshot"
REFRESH_LOCK = "hero_integration_schema_refresh"
INTERVAL_MIN, INTERVAL_MAX, INTERVAL_DEFAULT = 10, 240, 60
# Introspection plus one compatibility fallback, each with one retry at the 120 s client maximum.
JOB_TIMEOUT = 600


class SchemaRefreshError(RuntimeError):
    """A fixed, secret-free reason why a refresh did not capture a schema."""


def refresh_interval(settings) -> int:
    value = getattr(settings, "schema_refresh_interval_minutes", None)
    return int(value) if value not in (None, "") else INTERVAL_DEFAULT


def connection_ready(settings, token_configured: bool | None = None) -> bool:
    """Enabled with an endpoint and a stored token; the DocType has already validated the endpoint."""
    if not getattr(settings, "hero_api_url", ""):
        return False
    if token_configured is None:
        token_configured = bool(settings.get_password("hero_api_token", raise_exception=False))
    return token_configured


def binding_verified(settings) -> bool:
    """A capture succeeded for the current endpoint, token and source instance."""
    return bool(getattr(settings, "schema_verified_on", None)) and int(
        getattr(settings, "schema_verified_revision", 0) or 0
    ) == int(getattr(settings, "source_binding_revision", 0) or 0)


def check_due_after_save(settings, token_configured: bool | None = None) -> bool:
    return connection_ready(settings, token_configured) and not binding_verified(settings)


def periodic_due(settings, now=None) -> bool:
    if not (connection_ready(settings) and binding_verified(settings)):
        return False
    last = getattr(settings, "schema_last_checked_on", None)
    if not last:
        return True
    return get_datetime(now or now_datetime()) >= add_to_date(get_datetime(last), minutes=refresh_interval(settings))


def state(settings, token_configured: bool) -> dict[str, object]:
    return {
        "refresh_interval_minutes": refresh_interval(settings),
        "token_verified": binding_verified(settings),
        "check_due": check_due_after_save(settings, token_configured),
        "last_checked_on": str(getattr(settings, "schema_last_checked_on", "") or ""),
        "last_status": getattr(settings, "schema_check_status", "") or "",
        "last_message": getattr(settings, "schema_check_message", "") or "",
    }


@contextmanager
def _refresh_lock():
    """One capture per site at a time; a concurrent capture fails fast instead of racing the unique fingerprint."""
    if not getattr(getattr(frappe, "local", None), "site", None):
        with nullcontext():
            yield
        return
    from frappe.utils.file_lock import LockTimeoutError
    from frappe.utils.synchronization import filelock

    try:
        with filelock(REFRESH_LOCK, timeout=1):
            yield
    except LockTimeoutError as error:
        raise SchemaRefreshError("A HERO schema refresh is already running.") from error


def refresh(actor: str) -> dict[str, object]:
    """Capture now and record the outcome on the settings; raise SchemaRefreshError on failure."""
    settings = frappe.get_single(SETTINGS)
    revision = int(getattr(settings, "source_binding_revision", 0) or 0)
    try:
        with _refresh_lock():
            result = _capture(actor)
    except SchemaRefreshError as error:
        _record(settings, "FAILED", str(error))
        raise
    _record(settings, "OK", _success_message(result), verified_revision=revision, actor=actor)
    return result


def _success_message(result: dict[str, object]) -> str:
    if result["status"] == "UNCHANGED":
        return "Schema unchanged; the current snapshot stays in use."
    return "New schema snapshot captured."


def _record(settings, status: str, message: str, *, verified_revision: int | None = None, actor: str = "") -> None:
    values = {"schema_last_checked_on": now_datetime(), "schema_check_status": status, "schema_check_message": message}
    if verified_revision is not None:
        values.update(schema_verified_revision=verified_revision, schema_verified_on=values["schema_last_checked_on"],
                      schema_verified_by=actor or None)
    for field, value in values.items():
        frappe.db.set_single_value(SETTINGS, field, value, update_modified=False)


def _capture(actor: str) -> dict[str, object]:
    from hero_integration.client.graphql import HeroClientError, HeroGraphQLClient
    from hero_integration.client.settings import HeroConfigurationError, get_configuration
    from hero_integration.services.introspection import (
        COMPATIBILITY_INTROSPECTION_QUERY,
        INTROSPECTION_QUERY,
        IntrospectionError,
        canonical_schema,
        captured_at_utc,
        endpoint_fingerprint,
        structural_diff,
    )

    try:
        configuration = get_configuration()
        endpoint_hash = endpoint_fingerprint(configuration.endpoint.url, configuration.source_instance)
        client = HeroGraphQLClient(
            configuration.endpoint,
            token_provider=lambda: configuration.api_token,
            timeout=configuration.timeout_seconds,
            max_retries=1,
        )
        try:
            response = client.introspect(INTROSPECTION_QUERY)
        except HeroClientError as error:
            if error.code != "GRAPHQL_ERRORS":
                raise
            response = client.introspect(COMPATIBILITY_INTROSPECTION_QUERY)
        schema = canonical_schema(response)
    except HeroClientError as error:
        raise SchemaRefreshError(_client_error_message(error)) from error
    except (HeroConfigurationError, IntrospectionError, ValueError) as error:
        raise SchemaRefreshError(f"HERO schema refresh failed safely: {error}") from error

    schema_json = {"__schema": schema}
    from hero_integration.services.graphql_graph import analyze_snapshot

    fingerprint = analyze_snapshot(schema_json).fingerprint
    type_count = len(schema["types"])
    existing = frappe.get_all(
        SNAPSHOT,
        filters={
            "schema_fingerprint": fingerprint,
            "source_instance": configuration.source_instance,
            "endpoint_fingerprint": endpoint_hash,
        },
        fields=["name", "schema_fingerprint", "execution_contract_status"],
        limit_page_length=1,
    )
    if existing:
        row = existing[0]
        return {
            "status": "UNCHANGED",
            "snapshot_name": row["name"],
            "schema_fingerprint": row["schema_fingerprint"],
            "execution_contract_status": row.get("execution_contract_status") or "Compatible",
            "type_count": type_count,
        }

    from hero_integration.services.snapshot_inheritance import parent_for

    # The parent is the most recently *created* snapshot of this source and endpoint.
    parent_name = parent_for(configuration.source_instance, endpoint_hash)
    previous = frappe.get_doc(SNAPSHOT, parent_name) if parent_name else None
    diff = structural_diff(json.loads(previous.snapshot_json) if previous else None, schema)
    captured_at = captured_at_utc()
    snapshot = frappe.get_doc({
        "doctype": SNAPSHOT,
        "snapshot_json": json.dumps(schema_json, ensure_ascii=False, sort_keys=True),
        "source_instance": configuration.source_instance,
        "endpoint_fingerprint": endpoint_hash,
        "schema_version": 1,
        "captured_at": captured_at,
        "parent_snapshot": parent_name or None,
        "structural_diff_json": json.dumps(diff, ensure_ascii=False, sort_keys=True),
        "execution_contract_status": "Blocked" if diff["breaking"] else "Compatible",
    })
    snapshot.insert()
    inheritance = inherit_after_capture(snapshot.name, actor) if parent_name else None
    return {
        "inheritance": inheritance,
        "status": "CAPTURED",
        "snapshot_name": snapshot.name,
        "schema_fingerprint": snapshot.schema_fingerprint,
        "execution_contract_status": snapshot.execution_contract_status,
        "captured_at": captured_at,
        "type_count": type_count,
        "structural_diff": diff,
    }


def _client_error_message(error) -> str:
    if getattr(error, "code", "") in {"HTTP_401", "HTTP_403"}:
        return "HERO rejected the API token. Check the token and save again."
    detail = next((item for item in getattr(error, "details", ()) if item), "")
    # GraphQL error messages are bounded server texts without request data or credentials.
    return f"HERO schema refresh failed safely: {error}" + (f" ({detail[:200]})" if detail else "")


def inherit_after_capture(snapshot_name: str, actor: str) -> dict[str, object]:
    """Inheritance failures never undo the captured snapshot; they are reported instead."""
    from hero_integration.services.snapshot_inheritance import inherit_parent_mappings

    savepoint = "hero_snapshot_inheritance"
    frappe.db.savepoint(savepoint)
    try:
        return inherit_parent_mappings(snapshot_name, actor)
    except Exception:
        frappe.db.rollback(save_point=savepoint)
        frappe.log_error(title="HERO snapshot mapping inheritance failed")
        return {"status": "FAILED", "child_snapshot": snapshot_name, "activation": False,
                "message": "Mapping inheritance failed; the snapshot was kept. Retry it from the Mapping Editor."}


def run_scheduled_schema_refresh() -> None:
    """Per-minute scheduler tick: enqueue one refresh when the interval has passed."""
    settings = frappe.get_single(SETTINGS)
    if not periodic_due(settings):
        return
    frappe.enqueue(
        "hero_integration.services.schema_refresh.scheduled_refresh_job",
        queue="long", timeout=JOB_TIMEOUT, job_id=REFRESH_LOCK, deduplicate=True,
    )


def scheduled_refresh_job() -> None:
    settings = frappe.get_single(SETTINGS)
    if not periodic_due(settings):
        return
    try:
        refresh(getattr(settings, "schema_verified_by", None) or "Administrator")
    except SchemaRefreshError:
        # The outcome is already recorded on the settings; the job itself succeeded.
        pass
