"""Fail-closed authorization, durable-audit, and execution lifecycle for B5 plans."""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from time import time
from typing import Callable, Protocol

from hero_integration.services.controlled_schema_provisioning import (
    ProvisioningOperation,
    ProvisioningOperationKind,
    ProvisioningPlan,
    ProvisioningState,
)


class ProvisioningLifecycleError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProvisioningApproval:
    approval_id: str
    plan_id: str
    plan_fingerprint: str
    site_scope: str
    mapping_revision: int
    metadata_fingerprint: str
    actor: str
    roles: tuple[str, ...]
    operation_ids: tuple[str, ...]
    correlation_id: str
    expires_at: float


@dataclass(frozen=True)
class ProvisioningResult:
    state: ProvisioningState
    completed: tuple[str, ...]
    reconciliation_required: bool
    audit_id: str


class ProvisioningAudit(Protocol):
    def current_actor(self, site_scope: str) -> str: ...
    def reserve(self, plan: ProvisioningPlan, approval: ProvisioningApproval) -> str: ...
    def record(self, audit_id: str, operation_id: str, outcome: str) -> None: ...
    def finalize(self, audit_id: str, state: ProvisioningState) -> None: ...


class ProvisioningAdapter(Protocol):
    def metadata_fingerprint(self, site_scope: str) -> str: ...
    def apply(self, site_scope: str, operation: ProvisioningOperation) -> None: ...
    def verify(self, site_scope: str, operation: ProvisioningOperation) -> bool: ...


class InMemoryProvisioningAudit:
    """Test implementation of the durable-audit contract; not durability evidence."""

    def __init__(self, actor: str = "admin") -> None:
        self.actor = actor
        self.events: list[tuple[str, str, str]] = []
        self.fail: str | None = None

    def current_actor(self, site_scope: str) -> str:
        return self.actor

    def reserve(self, plan, approval) -> str:
        if self.fail == "reserve":
            raise ProvisioningLifecycleError("Audit reservation failed.")
        audit_id = sha256((plan.fingerprint + approval.approval_id).encode()).hexdigest()[:24]
        self.events.append((audit_id, "RESERVED", plan.plan_id))
        return audit_id

    def record(self, audit_id, operation_id, outcome) -> None:
        if self.fail == "record":
            raise ProvisioningLifecycleError("Audit outcome recording failed.")
        self.events.append((audit_id, outcome, operation_id))

    def finalize(self, audit_id, state) -> None:
        if self.fail == "finalize":
            raise ProvisioningLifecycleError("Audit finalization failed.")
        self.events.append((audit_id, state.value, ""))


class ProvisioningExecutor:
    """Applies only a fully approved immutable plan; never activates B4 mappings."""

    def __init__(
        self,
        audit: ProvisioningAudit,
        adapter: ProvisioningAdapter,
        clock: Callable[[], float] = time,
    ) -> None:
        self._audit, self._adapter, self._clock = audit, adapter, clock
        self._used_approvals: set[str] = set()

    def apply_authorized(self, plan: ProvisioningPlan, approval: ProvisioningApproval) -> ProvisioningResult:
        _authorize(plan, approval, self._used_approvals, self._audit.current_actor(plan.site_scope), self._clock())
        _require_executable_operations(plan)
        expected_metadata = plan.metadata_fingerprint
        if self._adapter.metadata_fingerprint(plan.site_scope) != expected_metadata:
            raise ProvisioningLifecycleError("Provisioning plan metadata is stale.")
        audit_id = self._audit.reserve(plan, approval)
        self._used_approvals.add(approval.approval_id)
        completed: list[str] = []
        schema_effect_started = False
        try:
            for operation in plan.operations:
                if self._adapter.metadata_fingerprint(plan.site_scope) != expected_metadata:
                    raise ProvisioningLifecycleError("Provisioning metadata changed before the approved operation.")
                self._adapter.apply(plan.site_scope, operation)
                if operation.kind != ProvisioningOperationKind.REUSE_EXISTING_COMPATIBLE_TARGET:
                    schema_effect_started = True
                if not self._adapter.verify(plan.site_scope, operation):
                    raise ProvisioningLifecycleError("Effective metadata verification failed.")
                self._audit.record(audit_id, operation.operation_id, "APPLIED")
                completed.append(operation.operation_id)
                expected_metadata = self._adapter.metadata_fingerprint(plan.site_scope)
        except Exception as error:
            state = ProvisioningState.PARTIAL if schema_effect_started else ProvisioningState.RECONCILIATION_REQUIRED
            try:
                self._audit.finalize(audit_id, state)
            except Exception:
                pass
            raise ProvisioningLifecycleError(
                "Provisioning did not activate mappings; reconciliation is required."
            ) from error
        try:
            self._audit.finalize(audit_id, ProvisioningState.APPLIED)
        except Exception as error:
            raise ProvisioningLifecycleError(
                "Schema effects may be complete, but the durable audit requires reconciliation."
            ) from error
        return ProvisioningResult(ProvisioningState.APPLIED, tuple(completed), False, audit_id)

    def reconciliation_status(self, audit_id: str) -> ProvisioningResult:
        """Return a non-mutating reconciliation marker; it never replays schema operations."""
        if not audit_id:
            raise ProvisioningLifecycleError("An audit identity is required for reconciliation.")
        return ProvisioningResult(ProvisioningState.RECONCILIATION_REQUIRED, (), True, audit_id)


def _require_executable_operations(plan: ProvisioningPlan) -> None:
    blocked = {
        ProvisioningOperationKind.WAITING_APP,
        ProvisioningOperationKind.REJECT_UNSUPPORTED_OR_UNSAFE,
    }
    if any(item.kind in blocked for item in plan.operations):
        raise ProvisioningLifecycleError("A plan with deferred or unsafe operations cannot be executed.")


def _authorize(
    plan: ProvisioningPlan,
    approval: ProvisioningApproval,
    used: set[str],
    current_actor: str,
    now: float,
) -> None:
    if plan.execution_authority != "NONE":
        raise ProvisioningLifecycleError("Plans must never carry execution authority.")
    if approval.approval_id in used:
        raise ProvisioningLifecycleError("Provisioning approval was already consumed.")
    if not approval.approval_id or not approval.actor or approval.actor != current_actor:
        raise ProvisioningLifecycleError("Provisioning approval actor does not match the authenticated actor.")
    if not isinstance(approval.expires_at, (float, int)) or approval.expires_at <= now:
        raise ProvisioningLifecycleError("Provisioning approval is expired.")
    if "System Manager" not in approval.roles and approval.actor != "Administrator":
        raise PermissionError("System Manager or Administrator authorization is required.")
    if (
        approval.plan_id != plan.plan_id
        or approval.plan_fingerprint != plan.fingerprint
        or approval.site_scope != plan.site_scope
        or approval.mapping_revision != plan.mapping_revision
        or approval.metadata_fingerprint != plan.metadata_fingerprint
        or tuple(item.operation_id for item in plan.operations) != approval.operation_ids
    ):
        raise ProvisioningLifecycleError("Provisioning approval does not bind the current complete plan.")
