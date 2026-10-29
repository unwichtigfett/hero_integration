"""Source-only durable Frappe audit adapter for controlled provisioning."""
from __future__ import annotations

import json
from hashlib import sha256

from hero_integration.services.controlled_schema_provisioning import ProvisioningState
from hero_integration.services.schema_provisioning_lifecycle import ProvisioningLifecycleError


class FrappeProvisioningAuditStore:
    """Persists B5 lifecycle evidence; its DocType must be installed before execution."""

    DOCTYPE = "HERO Schema Provisioning Audit"
    _FINAL_STATES = {ProvisioningState.APPLIED.value, ProvisioningState.PARTIAL.value, ProvisioningState.RECONCILIATION_REQUIRED.value}

    def __init__(self, frappe_module=None) -> None:
        self._frappe_module = frappe_module

    def current_actor(self, site_scope: str) -> str:
        frappe = self._frappe(site_scope)
        actor = getattr(getattr(frappe, "session", None), "user", None)
        if not isinstance(actor, str) or not actor:
            raise ProvisioningLifecycleError("An authenticated actor is required for schema provisioning.")
        return actor

    def reserve(self, plan, approval) -> str:
        frappe = self._frappe(plan.site_scope)
        self._require_authority(frappe, "create")
        if self.current_actor(plan.site_scope) != approval.actor:
            raise ProvisioningLifecycleError("Audit reservation actor does not match the approved actor.")
        payload = {
            "plan_id": plan.plan_id,
            "plan_fingerprint": plan.fingerprint,
            "mapping_revision": plan.mapping_revision,
            "metadata_fingerprint": plan.metadata_fingerprint,
            "actor": approval.actor,
            "approval_id": approval.approval_id,
            "approval_expires_at": approval.expires_at,
            "operations": [item.operation_id for item in plan.operations],
            "events": [],
        }
        name = sha256((plan.fingerprint + approval.approval_id + approval.correlation_id).encode()).hexdigest()[:32]
        document = frappe.get_doc({
            "doctype": self.DOCTYPE, "name": name, "site_scope": plan.site_scope,
            "correlation_id": approval.correlation_id, "lifecycle_state": ProvisioningState.PREPARED.value,
            "audit_payload_json": json.dumps(payload, sort_keys=True, separators=(",", ":")),
        })
        document.insert()
        self._commit_durable_boundary(frappe)
        return document.name

    def record(self, audit_id: str, operation_id: str, outcome: str) -> None:
        frappe = self._frappe()
        self._require_authority(frappe, "write")
        document = frappe.get_doc(self.DOCTYPE, audit_id)
        self._assert_document_scope(frappe, document)
        if document.lifecycle_state not in {ProvisioningState.PREPARED.value, ProvisioningState.APPLYING.value}:
            raise ProvisioningLifecycleError("A finalized provisioning audit cannot accept another operation outcome.")
        payload = json.loads(document.audit_payload_json)
        if any(item["operation_id"] == operation_id for item in payload["events"]):
            raise ProvisioningLifecycleError("A provisioning operation outcome is already recorded.")
        payload["events"].append({"operation_id": operation_id, "outcome": outcome})
        document.audit_payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        document.lifecycle_state = ProvisioningState.APPLYING.value
        document.save()
        self._commit_durable_boundary(frappe)

    def finalize(self, audit_id: str, state: ProvisioningState) -> None:
        if state.value not in self._FINAL_STATES:
            raise ProvisioningLifecycleError("Only terminal provisioning audit states may be finalized.")
        frappe = self._frappe()
        self._require_authority(frappe, "write")
        document = frappe.get_doc(self.DOCTYPE, audit_id)
        self._assert_document_scope(frappe, document)
        if document.lifecycle_state in self._FINAL_STATES and document.lifecycle_state != state.value:
            raise ProvisioningLifecycleError("A finalized provisioning audit state is immutable.")
        document.lifecycle_state = state.value
        document.save()
        self._commit_durable_boundary(frappe)

    def _frappe(self, site_scope: str | None = None):
        if self._frappe_module is None:
            import frappe
            api = frappe
        else:
            api = self._frappe_module
        if site_scope is not None and getattr(getattr(api, "local", None), "site", None) != site_scope:
            raise ProvisioningLifecycleError("Audit site scope does not match the current Frappe site.")
        return api

    def _assert_document_scope(self, frappe, document) -> None:
        if getattr(document, "site_scope", None) != getattr(getattr(frappe, "local", None), "site", None):
            raise ProvisioningLifecycleError("Audit document does not belong to the current site.")

    def _require_authority(self, frappe, permission: str) -> None:
        if getattr(getattr(frappe, "session", None), "user", None) != "Administrator" and "System Manager" not in frappe.get_roles():
            raise PermissionError("System Manager or Administrator authority is required.")
        frappe.has_permission(self.DOCTYPE, permission, throw=True)

    @staticmethod
    def _commit_durable_boundary(frappe) -> None:
        """Seal B5 audit evidence before or after an irreversible schema effect."""
        commit = getattr(getattr(frappe, "db", None), "commit", None)
        if not callable(commit):
            raise ProvisioningLifecycleError("The durable provisioning audit transaction boundary is unavailable.")
        try:
            commit()
        except Exception as error:
            raise ProvisioningLifecycleError("The durable provisioning audit transaction boundary failed.") from error
