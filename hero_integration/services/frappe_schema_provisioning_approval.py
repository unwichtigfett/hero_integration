"""Durable, server-owned approval source for controlled schema provisioning."""
from __future__ import annotations

import json
from time import time

from hero_integration.services.controlled_schema_provisioning import ProvisioningPlan
from hero_integration.services.schema_provisioning_lifecycle import (
    ProvisioningApproval,
    ProvisioningLifecycleError,
)


class FrappeProvisioningApprovalStore:
    """Issues and consumes one site-scoped approval through Frappe documents."""

    DOCTYPE = "HERO Schema Provisioning Approval"

    def __init__(self, frappe_module=None, clock=time) -> None:
        self._frappe_module = frappe_module
        self._clock = clock

    def issue(self, plan: ProvisioningPlan, correlation_id: str, ttl_seconds: int = 300) -> ProvisioningApproval:
        if not isinstance(ttl_seconds, int) or not 1 <= ttl_seconds <= 900 or not correlation_id:
            raise ProvisioningLifecycleError("Provisioning approval expiry and correlation identity are invalid.")
        frappe = self._frappe(plan.site_scope)
        actor = self._assert_system_manager(frappe, "create")
        now = float(self._clock())
        roles = tuple(sorted(frappe.get_roles(actor)))
        payload = _payload(plan, actor, roles, correlation_id, now + ttl_seconds)
        document = frappe.get_doc({
            "doctype": self.DOCTYPE,
            "site_scope": plan.site_scope,
            "approval_state": "APPROVED",
            "actor": actor,
            "correlation_id": correlation_id,
            "expires_at_epoch": now + ttl_seconds,
            "approval_payload_json": json.dumps(payload, sort_keys=True, separators=(",", ":")),
        })
        document.insert()
        return _approval(document.name, payload)

    def consume(self, plan: ProvisioningPlan, approval: ProvisioningApproval, current_actor: str, now: float) -> None:
        frappe = self._frappe(plan.site_scope)
        actor = self._assert_system_manager(frappe, "write")
        if actor != current_actor:
            raise ProvisioningLifecycleError("Authenticated actor changed before approval consumption.")
        rows = frappe.db.sql(
            "select name from `tabHERO Schema Provisioning Approval` where name=%s for update",
            (approval.approval_id,),
            as_dict=True,
        )
        if len(rows) != 1:
            raise ProvisioningLifecycleError("Provisioning approval does not exist.")
        document = frappe.get_doc(self.DOCTYPE, rows[0].name)
        if document.site_scope != plan.site_scope or document.approval_state != "APPROVED":
            raise ProvisioningLifecycleError("Provisioning approval is not available for this site.")
        payload = json.loads(document.approval_payload_json)
        stored = _approval(document.name, payload)
        if stored != approval or stored.actor != current_actor or stored.expires_at <= now:
            raise ProvisioningLifecycleError("Provisioning approval is stale, forged, expired, or actor-mismatched.")
        _validate_binding(plan, stored)
        document.approval_state = "CONSUMED"
        document.consumed_at_epoch = float(now)
        document.save()

    def _frappe(self, site_scope: str):
        if self._frappe_module is None:
            import frappe
            api = frappe
        else:
            api = self._frappe_module
        if getattr(getattr(api, "local", None), "site", None) != site_scope:
            raise ProvisioningLifecycleError("Provisioning approval cannot cross site scope.")
        return api

    def _assert_system_manager(self, frappe, permission: str) -> str:
        actor = getattr(getattr(frappe, "session", None), "user", None)
        if not isinstance(actor, str) or not actor:
            raise ProvisioningLifecycleError("An authenticated actor is required.")
        if actor != "Administrator" and "System Manager" not in frappe.get_roles():
            raise PermissionError("System Manager or Administrator authorization is required.")
        frappe.has_permission(self.DOCTYPE, permission, throw=True)
        return actor


def _payload(plan: ProvisioningPlan, actor: str, roles: tuple[str, ...], correlation_id: str, expires_at: float) -> dict[str, object]:
    return {
        "plan_id": plan.plan_id, "plan_fingerprint": plan.fingerprint, "site_scope": plan.site_scope,
        "mapping_revision": plan.mapping_revision, "metadata_fingerprint": plan.metadata_fingerprint,
        "actor": actor, "roles": list(roles), "operation_ids": [item.operation_id for item in plan.operations],
        "correlation_id": correlation_id, "expires_at": expires_at,
    }


def _approval(approval_id: str, payload: dict[str, object]) -> ProvisioningApproval:
    try:
        return ProvisioningApproval(
            approval_id, payload["plan_id"], payload["plan_fingerprint"], payload["site_scope"],
            payload["mapping_revision"], payload["metadata_fingerprint"], payload["actor"],
            tuple(payload["roles"]), tuple(payload["operation_ids"]), payload["correlation_id"], payload["expires_at"],
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ProvisioningLifecycleError("Stored provisioning approval payload is invalid.") from error


def _validate_binding(plan: ProvisioningPlan, approval: ProvisioningApproval) -> None:
    if (
        approval.plan_id != plan.plan_id or approval.plan_fingerprint != plan.fingerprint
        or approval.site_scope != plan.site_scope or approval.mapping_revision != plan.mapping_revision
        or approval.metadata_fingerprint != plan.metadata_fingerprint
        or approval.operation_ids != tuple(item.operation_id for item in plan.operations)
    ):
        raise ProvisioningLifecycleError("Stored provisioning approval does not bind the current plan.")
