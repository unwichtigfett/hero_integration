"""Canonical B8 runtime entrypoints with fail-closed preflight ownership.

This boundary resolves the persisted runtime plan before a provider exists.
It intentionally does not compile a second GraphQL request or treat an offset
reader as a change provider while B7 materialized runtime persistence is absent.
"""
from __future__ import annotations

from dataclasses import dataclass


BLOCKED_NO_B7_MATERIALIZED_RUNTIME = "BLOCKED_NO_B7_MATERIALIZED_RUNTIME"


class RuntimeBridgeError(ValueError):
    """The scheduler cannot safely enter the B8 runtime."""


@dataclass(frozen=True, slots=True)
class RuntimePreflight:
    status: str
    plan_key: str
    generation: int
    source_instance: str


def _frappe(frappe_module=None):
    if frappe_module is not None:
        return frappe_module
    import frappe
    return frappe


def _runtime_plan_service(frappe_module):
    from hero_integration.services.runtime_plan import (
        FrappeRuntimePlanAudit,
        FrappeRuntimePlanAuthority,
        FrappeRuntimePlanReferences,
        FrappeRuntimePlanStorage,
        RuntimePlanService,
    )
    return RuntimePlanService(
        FrappeRuntimePlanStorage(frappe_module),
        FrappeRuntimePlanReferences(frappe_module),
        FrappeRuntimePlanAudit(frappe_module),
        FrappeRuntimePlanAuthority(frappe_module),
    )


def _scheduler_actor(frappe_module) -> str:
    actor = getattr(getattr(frappe_module, "session", None), "user", "")
    if actor != "Administrator":
        raise PermissionError("B8 scheduler execution requires the Administrator service identity.")
    return actor


def run_scheduled_auto_sync(*, frappe_module=None) -> RuntimePreflight:
    """Revalidate the selected AUTO_SYNC plan before any provider construction.

    The blocked status is not an execution success. It says only that the
    canonical plan remains valid while its B7 materialized runtime is missing.
    """
    frappe = _frappe(frappe_module)
    settings = frappe.get_single("HERO Integration Settings")
    from hero_integration.services.polling import effective_mode
    from hero_integration.services.runtime_plan import RUNTIME_EXECUTION_MODE

    if effective_mode(settings) != "AUTO_SYNC":
        raise RuntimeBridgeError("Canonical scheduled sync requires AUTO_SYNC mode.")
    site_scope = getattr(getattr(frappe, "local", None), "site", "")
    source_instance = str(getattr(settings, "hero_source_instance", "default") or "default").strip()
    if not site_scope or not source_instance:
        raise RuntimeBridgeError("Canonical scheduled sync requires current site and source identities.")
    actor = _scheduler_actor(frappe)
    service = _runtime_plan_service(frappe)
    plan = service.current_approved(site_scope, source_instance)
    if plan is None:
        raise RuntimeBridgeError("No approved canonical runtime plan exists for this site/source instance.")
    executable = service.resolve_executable_plan(
        site_scope, source_instance, plan.generation, True, actor
    )
    if executable.selection.execution_mode != RUNTIME_EXECUTION_MODE:
        raise RuntimeBridgeError("Runtime plan execution mode is not AUTO_SYNC.")
    if executable.selection.operation_revision != executable.selection.service_revision:
        raise RuntimeBridgeError("Runtime plan operation revision is stale.")
    return RuntimePreflight(
        BLOCKED_NO_B7_MATERIALIZED_RUNTIME,
        executable.plan_key,
        executable.generation,
        executable.selection.source_instance,
    )
