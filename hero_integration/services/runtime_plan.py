"""Persistent, fail-closed B8 universal runtime-plan contract.

The model deliberately contains only the explicit read plan selected by a
System Manager.  It has no scheduler, transport, provider, or mutation path.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from hashlib import sha256
import json
import re
from threading import RLock
from typing import Protocol


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")
_GRAPHQL_NAME = re.compile(r"^[_A-Za-z][_0-9A-Za-z]*$")


class RuntimePlanError(ValueError):
    """A runtime plan is malformed, stale, or cannot be approved."""


class RuntimePlanConflict(RuntimePlanError):
    """A plan lifecycle or compare-and-swap precondition is stale."""


class RuntimePlanState(StrEnum):
    DRAFT = "DRAFT"
    APPROVED = "APPROVED"
    DISABLED = "DISABLED"


RUNTIME_EXECUTION_MODE = "AUTO_SYNC"


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise RuntimePlanError(f"{label} must be a stable non-secret identifier.")
    return value


def _positive(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise RuntimePlanError(f"{label} must be positive.")
    return value


def _actor(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 140 or any(ord(char) < 32 for char in value):
        raise RuntimePlanError(f"{label} must be a non-empty actor identity.")
    return value


@dataclass(frozen=True, slots=True)
class RuntimePlanSelection:
    """Immutable source identities which a runtime must revalidate before I/O."""

    site_scope: str
    source_instance: str
    graphql_service: str
    service_revision: int
    operation_revision: int
    graphql_mapping: str
    mapping_revision: int
    schema_snapshot: str
    schema_fingerprint: str
    operation_name: str
    execution_mode: str

    def __post_init__(self) -> None:
        _identifier(self.site_scope, "site scope")
        _identifier(self.source_instance, "source instance")
        _identifier(self.graphql_service, "GraphQL service")
        _identifier(self.graphql_mapping, "GraphQL mapping")
        _identifier(self.schema_snapshot, "schema snapshot")
        _positive(self.service_revision, "service revision")
        _positive(self.operation_revision, "operation revision")
        _positive(self.mapping_revision, "mapping revision")
        if not isinstance(self.schema_fingerprint, str) or not _FINGERPRINT.fullmatch(self.schema_fingerprint):
            raise RuntimePlanError("schema fingerprint must be a SHA-256 fingerprint.")
        if not isinstance(self.operation_name, str) or not _GRAPHQL_NAME.fullmatch(self.operation_name):
            raise RuntimePlanError("operation name must be a GraphQL name.")
        if self.execution_mode != RUNTIME_EXECUTION_MODE:
            raise RuntimePlanError("B8 runtime plans require execution mode AUTO_SYNC.")

    @property
    def plan_key(self) -> str:
        return sha256(f"hero-runtime-plan-v1|{self.site_scope}|{self.source_instance}".encode()).hexdigest()

    @property
    def semantic_hash(self) -> str:
        return sha256(json.dumps(self.payload(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def payload(self) -> dict[str, object]:
        return {
            "site_scope": self.site_scope,
            "source_instance": self.source_instance,
            "graphql_service": self.graphql_service,
            "service_revision": self.service_revision,
            "operation_revision": self.operation_revision,
            "graphql_mapping": self.graphql_mapping,
            "mapping_revision": self.mapping_revision,
            "schema_snapshot": self.schema_snapshot,
            "schema_fingerprint": self.schema_fingerprint,
            "operation_name": self.operation_name,
            "execution_mode": self.execution_mode,
        }


@dataclass(frozen=True, slots=True)
class RuntimePlan:
    plan_key: str
    selection: RuntimePlanSelection
    state: RuntimePlanState
    revision: int
    generation: int
    approved_by: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.plan_key, str) or not _FINGERPRINT.fullmatch(self.plan_key):
            raise RuntimePlanError("runtime plan key is invalid.")
        if self.plan_key != self.selection.plan_key:
            raise RuntimePlanError("runtime plan key does not match site/source scope.")
        if not isinstance(self.state, RuntimePlanState):
            raise RuntimePlanError("runtime plan state is invalid.")
        _positive(self.revision, "runtime plan revision")
        _positive(self.generation, "runtime plan generation")
        if self.approved_by:
            _actor(self.approved_by, "approving actor")


@dataclass(frozen=True, slots=True)
class ExecutableRuntimePlan:
    """Revalidated immutable input for a later runtime adapter.

    This deliberately preserves document names, revisions, and fingerprints;
    it does not construct a provider or perform a request.
    """

    plan_key: str
    plan_revision: int
    generation: int
    automatic: bool
    actor: str
    selection: RuntimePlanSelection

    def __post_init__(self) -> None:
        if not isinstance(self.plan_key, str) or not _FINGERPRINT.fullmatch(self.plan_key):
            raise RuntimePlanError("executable runtime plan key is invalid.")
        _positive(self.plan_revision, "executable plan revision")
        _positive(self.generation, "executable plan generation")
        if not isinstance(self.automatic, bool):
            raise RuntimePlanError("automatic execution flag must be boolean.")
        _actor(self.actor, "execution actor")
        if self.plan_key != self.selection.plan_key:
            raise RuntimePlanError("executable runtime plan scope is inconsistent.")


@dataclass(frozen=True, slots=True)
class RuntimePlanAuditEvent:
    plan_key: str
    site_scope: str
    source_instance: str
    action: str
    actor: str
    revision: int
    generation: int
    semantic_hash: str

    def __post_init__(self) -> None:
        if not isinstance(self.plan_key, str) or not _FINGERPRINT.fullmatch(self.plan_key):
            raise RuntimePlanError("audit plan key is invalid.")
        _identifier(self.site_scope, "audit site scope")
        _identifier(self.source_instance, "audit source instance")
        _actor(self.actor, "audit actor")
        _positive(self.revision, "audit revision")
        _positive(self.generation, "audit generation")
        if self.action not in {"DRAFT_CREATED", "DRAFT_UPDATED", "APPROVED", "DISABLED"}:
            raise RuntimePlanError("runtime plan audit action is invalid.")
        if not _FINGERPRINT.fullmatch(self.semantic_hash):
            raise RuntimePlanError("runtime plan audit semantic hash is invalid.")


class RuntimePlanStorage(Protocol):
    def read(self, plan_key: str) -> RuntimePlan | None: ...
    def read_scope(self, site_scope: str, source_instance: str) -> RuntimePlan | None: ...
    def create(self, plan: RuntimePlan) -> RuntimePlan: ...
    def replace(self, plan: RuntimePlan, expected_revision: int) -> RuntimePlan: ...


class RuntimePlanReferences(Protocol):
    def validate_current(self, selection: RuntimePlanSelection) -> None: ...


class RuntimePlanAudit(Protocol):
    def record(self, event: RuntimePlanAuditEvent) -> None: ...


class RuntimePlanAuthority(Protocol):
    def require_system_manager(self, actor: str, site_scope: str) -> None: ...


class RuntimePlanService:
    """Lifecycle owner; construction and runtime execution stay elsewhere."""

    def __init__(self, storage: RuntimePlanStorage, references: RuntimePlanReferences, audit: RuntimePlanAudit, authority: RuntimePlanAuthority) -> None:
        self._storage, self._references, self._audit, self._authority = storage, references, audit, authority

    def create_draft(self, selection: RuntimePlanSelection, *, actor: str) -> RuntimePlan:
        self._authorize(actor, selection.site_scope)
        if self._storage.read_scope(selection.site_scope, selection.source_instance) is not None:
            raise RuntimePlanConflict("A runtime plan already exists for this site/source instance.")
        plan = RuntimePlan(selection.plan_key, selection, RuntimePlanState.DRAFT, 1, 1)
        stored = self._storage.create(plan)
        self._record(stored, "DRAFT_CREATED", actor)
        return stored

    def update_draft(self, plan_key: str, selection: RuntimePlanSelection, *, expected_revision: int, actor: str) -> RuntimePlan:
        current = self._require(plan_key)
        self._authorize(actor, current.selection.site_scope)
        self._require_expected(current, expected_revision)
        if current.state is not RuntimePlanState.DRAFT:
            raise RuntimePlanConflict("Only a draft runtime plan may be edited.")
        if selection.plan_key != current.plan_key:
            raise RuntimePlanConflict("A runtime plan cannot move between site/source scopes.")
        if selection.semantic_hash == current.selection.semantic_hash:
            return current
        candidate = replace(current, selection=selection, revision=current.revision + 1, generation=current.generation + 1)
        stored = self._storage.replace(candidate, expected_revision)
        self._record(stored, "DRAFT_UPDATED", actor)
        return stored

    def approve(self, plan_key: str, *, expected_revision: int, actor: str) -> RuntimePlan:
        current = self._require(plan_key)
        self._authorize(actor, current.selection.site_scope)
        self._require_expected(current, expected_revision)
        if current.state is not RuntimePlanState.DRAFT:
            raise RuntimePlanConflict("Only a draft runtime plan may be approved.")
        self._references.validate_current(current.selection)
        candidate = replace(current, state=RuntimePlanState.APPROVED, revision=current.revision + 1, generation=current.generation + 1, approved_by=actor)
        stored = self._storage.replace(candidate, expected_revision)
        self._record(stored, "APPROVED", actor)
        return stored

    def disable(self, plan_key: str, *, expected_revision: int, actor: str) -> RuntimePlan:
        current = self._require(plan_key)
        self._authorize(actor, current.selection.site_scope)
        self._require_expected(current, expected_revision)
        if current.state is RuntimePlanState.DISABLED:
            raise RuntimePlanConflict("Runtime plan is already disabled.")
        candidate = replace(current, state=RuntimePlanState.DISABLED, revision=current.revision + 1, generation=current.generation + 1)
        stored = self._storage.replace(candidate, expected_revision)
        self._record(stored, "DISABLED", actor)
        return stored

    def current_approved(self, site_scope: str, source_instance: str) -> RuntimePlan | None:
        _identifier(site_scope, "site scope")
        _identifier(source_instance, "source instance")
        plan = self._storage.read_scope(site_scope, source_instance)
        if plan is None or plan.state is not RuntimePlanState.APPROVED:
            return None
        return plan

    def resolve_executable_plan(
        self, site_scope: str, source_instance: str, expected_generation: int, automatic: bool, actor: str
    ) -> ExecutableRuntimePlan:
        """Fail before provider construction unless the exact approved plan is current.

        The caller still owns scheduler-service authentication and all transport
        decisions.  Manual callers are additionally checked as System Managers
        here; automatic callers must arrive through a separately authenticated
        scheduler path.
        """
        _identifier(site_scope, "site scope")
        _identifier(source_instance, "source instance")
        _actor(actor, "execution actor")
        _positive(expected_generation, "expected runtime plan generation")
        if not isinstance(automatic, bool):
            raise RuntimePlanError("automatic execution flag must be boolean.")
        if not automatic:
            self._authorize(actor, site_scope)
        plan = self.current_approved(site_scope, source_instance)
        if plan is None:
            raise RuntimePlanConflict("No approved runtime plan exists for this site/source instance.")
        if plan.generation != expected_generation:
            raise RuntimePlanConflict("Runtime plan generation is stale.")
        if automatic and plan.selection.execution_mode != RUNTIME_EXECUTION_MODE:
            raise RuntimePlanConflict("Automatic execution requires an AUTO_SYNC runtime plan.")
        self._references.validate_current(plan.selection)
        return ExecutableRuntimePlan(plan.plan_key, plan.revision, plan.generation, automatic, actor, plan.selection)

    def _require(self, plan_key: str) -> RuntimePlan:
        if not isinstance(plan_key, str) or not _FINGERPRINT.fullmatch(plan_key):
            raise RuntimePlanError("runtime plan key is invalid.")
        plan = self._storage.read(plan_key)
        if plan is None:
            raise RuntimePlanConflict("Runtime plan does not exist.")
        return plan

    @staticmethod
    def _require_expected(plan: RuntimePlan, expected_revision: object) -> None:
        if not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or expected_revision != plan.revision:
            raise RuntimePlanConflict("Runtime plan revision is stale.")

    def _authorize(self, actor: str, site_scope: str) -> None:
        _actor(actor, "actor")
        self._authority.require_system_manager(actor, site_scope)

    def _record(self, plan: RuntimePlan, action: str, actor: str) -> None:
        self._audit.record(RuntimePlanAuditEvent(plan.plan_key, plan.selection.site_scope, plan.selection.source_instance, action, actor, plan.revision, plan.generation, plan.selection.semantic_hash))


class InMemoryRuntimePlanStorage:
    """Lock-protected test storage; it is not persistence or MariaDB evidence."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._plans: dict[str, RuntimePlan] = {}
        self._scopes: dict[tuple[str, str], str] = {}

    def read(self, plan_key: str) -> RuntimePlan | None:
        with self._lock:
            return self._plans.get(plan_key)

    def read_scope(self, site_scope: str, source_instance: str) -> RuntimePlan | None:
        with self._lock:
            key = self._scopes.get((site_scope, source_instance))
            return None if key is None else self._plans[key]

    def create(self, plan: RuntimePlan) -> RuntimePlan:
        with self._lock:
            scope = (plan.selection.site_scope, plan.selection.source_instance)
            if plan.plan_key in self._plans or scope in self._scopes:
                raise RuntimePlanConflict("A runtime plan already exists for this site/source instance.")
            self._plans[plan.plan_key] = plan
            self._scopes[scope] = plan.plan_key
            return plan

    def replace(self, plan: RuntimePlan, expected_revision: int) -> RuntimePlan:
        with self._lock:
            current = self._plans.get(plan.plan_key)
            if current is None or current.revision != expected_revision:
                raise RuntimePlanConflict("Runtime plan revision is stale.")
            if current.selection.plan_key != plan.selection.plan_key:
                raise RuntimePlanConflict("Runtime plan cannot cross site/source scope.")
            self._plans[plan.plan_key] = plan
            return plan


class InMemoryRuntimePlanAudit:
    def __init__(self) -> None:
        self.events: list[RuntimePlanAuditEvent] = []

    def record(self, event: RuntimePlanAuditEvent) -> None:
        self.events.append(event)


class FrappeRuntimePlanAuthority:
    """Checks the current Frappe session; an actor string cannot impersonate it."""

    PLAN = "HERO Runtime Plan"

    def __init__(self, frappe_module=None) -> None:
        self._frappe_module = frappe_module

    def require_system_manager(self, actor: str, site_scope: str) -> None:
        frappe = self._frappe()
        if getattr(getattr(frappe, "local", None), "site", None) != site_scope:
            raise RuntimePlanError("Runtime plan authority cannot cross site scope.")
        if getattr(getattr(frappe, "session", None), "user", None) != actor:
            raise PermissionError("Runtime plan actor must be the current session user.")
        if actor != "Administrator" and "System Manager" not in frappe.get_roles():
            raise PermissionError("System Manager or Administrator authority is required.")
        frappe.has_permission(self.PLAN, "write", throw=True)

    def _frappe(self):
        if self._frappe_module is not None:
            return self._frappe_module
        import frappe
        return frappe


class FrappeRuntimePlanStorage:
    """Frappe v16 source adapter; no DDL, commit, scheduler, or transport."""

    PLAN = "HERO Runtime Plan"

    def __init__(self, frappe_module=None) -> None:
        self._frappe_module = frappe_module

    def read(self, plan_key: str) -> RuntimePlan | None:
        if not isinstance(plan_key, str) or not _FINGERPRINT.fullmatch(plan_key):
            raise RuntimePlanError("runtime plan key is invalid.")
        frappe = self._frappe()
        rows = frappe.get_all(self.PLAN, filters={"plan_key": plan_key}, fields=["name"])
        if len(rows) > 1:
            raise RuntimePlanConflict("Runtime plan key is not unique.")
        if not rows:
            return None
        plan = self._decode(frappe.get_doc(self.PLAN, rows[0].name))
        self._assert_scope(frappe, plan.selection.site_scope)
        frappe.has_permission(self.PLAN, "read", throw=True)
        return plan

    def read_scope(self, site_scope: str, source_instance: str) -> RuntimePlan | None:
        _identifier(site_scope, "site scope")
        _identifier(source_instance, "source instance")
        frappe = self._frappe()
        self._assert_scope(frappe, site_scope)
        frappe.has_permission(self.PLAN, "read", throw=True)
        rows = frappe.get_all(self.PLAN, filters={"site_scope": site_scope, "source_instance": source_instance}, fields=["name"])
        if len(rows) > 1:
            raise RuntimePlanConflict("More than one runtime plan exists for the site/source scope.")
        return None if not rows else self._decode(frappe.get_doc(self.PLAN, rows[0].name))

    def create(self, plan: RuntimePlan) -> RuntimePlan:
        frappe = self._frappe()
        self._assert_scope(frappe, plan.selection.site_scope)
        self._assert_write(frappe)
        if self.read_scope(plan.selection.site_scope, plan.selection.source_instance) is not None:
            raise RuntimePlanConflict("A runtime plan already exists for this site/source instance.")
        try:
            frappe.get_doc(self._payload(plan)).insert()
        except Exception as error:
            if error.__class__.__name__ != "DuplicateEntryError":
                raise
            existing = self.read_scope(plan.selection.site_scope, plan.selection.source_instance)
            if existing is None or existing.plan_key != plan.plan_key:
                raise RuntimePlanConflict("Runtime plan creation conflicted with another scope.") from error
            raise RuntimePlanConflict("A runtime plan already exists for this site/source instance.") from error
        return plan

    def replace(self, plan: RuntimePlan, expected_revision: int) -> RuntimePlan:
        _positive(expected_revision, "expected runtime plan revision")
        frappe = self._frappe()
        self._assert_scope(frappe, plan.selection.site_scope)
        self._assert_write(frappe)
        rows = frappe.db.sql(
            "select name from `tabHERO Runtime Plan` where plan_key=%s for update", (plan.plan_key,), as_dict=True
        )
        if len(rows) != 1:
            raise RuntimePlanConflict("Runtime plan does not exist or is not unique.")
        document = frappe.get_doc(self.PLAN, rows[0].name)
        current = self._decode(document)
        if current.revision != expected_revision:
            raise RuntimePlanConflict("Runtime plan revision is stale.")
        if current.selection.plan_key != plan.selection.plan_key:
            raise RuntimePlanConflict("Runtime plan cannot cross site/source scope.")
        for field, value in self._payload(plan, doctype=False).items():
            setattr(document, field, value)
        document.save()
        return plan

    def _frappe(self):
        if self._frappe_module is not None:
            return self._frappe_module
        import frappe
        return frappe

    @staticmethod
    def _assert_scope(frappe, site_scope: str) -> None:
        if getattr(getattr(frappe, "local", None), "site", None) != site_scope:
            raise RuntimePlanError("Runtime plan storage cannot cross site scope.")

    def _assert_write(self, frappe) -> None:
        user = getattr(getattr(frappe, "session", None), "user", None)
        roles = getattr(frappe, "get_roles", lambda: ())()
        if user != "Administrator" and "System Manager" not in roles:
            raise PermissionError("System Manager or Administrator authority is required.")
        frappe.has_permission(self.PLAN, "write", throw=True)

    @classmethod
    def _payload(cls, plan: RuntimePlan, *, doctype: bool = True) -> dict[str, object]:
        selection = plan.selection
        values = {
            "plan_key": plan.plan_key,
            "site_scope": selection.site_scope,
            "source_instance": selection.source_instance,
            "graphql_service": selection.graphql_service,
            "service_revision": selection.service_revision,
            "operation_revision": selection.operation_revision,
            "graphql_mapping": selection.graphql_mapping,
            "mapping_revision": selection.mapping_revision,
            "schema_snapshot": selection.schema_snapshot,
            "schema_fingerprint": selection.schema_fingerprint,
            "operation_name": selection.operation_name,
            "execution_mode": selection.execution_mode,
            "semantic_hash": selection.semantic_hash,
            "state": plan.state.value,
            "plan_revision": plan.revision,
            "generation": plan.generation,
            "approved_by": plan.approved_by,
        }
        return ({"doctype": cls.PLAN, "name": plan.plan_key} | values) if doctype else values

    @staticmethod
    def _decode(document) -> RuntimePlan:
        try:
            selection = RuntimePlanSelection(
                site_scope=document.site_scope,
                source_instance=document.source_instance,
                graphql_service=document.graphql_service,
                service_revision=int(document.service_revision),
                operation_revision=int(document.operation_revision),
                graphql_mapping=document.graphql_mapping,
                mapping_revision=int(document.mapping_revision),
                schema_snapshot=document.schema_snapshot,
                schema_fingerprint=document.schema_fingerprint,
                operation_name=document.operation_name,
                execution_mode=document.execution_mode,
            )
            if getattr(document, "semantic_hash", "") != selection.semantic_hash:
                raise RuntimePlanError("Persisted runtime plan semantic hash is stale.")
            return RuntimePlan(document.plan_key, selection, RuntimePlanState(document.state), int(document.plan_revision), int(document.generation), document.approved_by or "")
        except (AttributeError, TypeError, ValueError) as error:
            raise RuntimePlanError("Persisted runtime plan is malformed.") from error


class FrappeRuntimePlanReferences:
    """Loads and compares all B7 identities immediately before approval."""

    def __init__(self, frappe_module=None) -> None:
        self._frappe_module = frappe_module

    def validate_current(self, selection: RuntimePlanSelection) -> None:
        frappe = self._frappe()
        if getattr(getattr(frappe, "local", None), "site", None) != selection.site_scope:
            raise RuntimePlanError("Runtime plan references cannot cross site scope.")
        snapshot = frappe.get_doc("HERO GraphQL Schema Snapshot", selection.schema_snapshot)
        service = frappe.get_doc("HERO GraphQL Service", selection.graphql_service)
        mapping = frappe.get_doc("HERO GraphQL Mapping", selection.graphql_mapping)
        if not (
            snapshot.source_instance == selection.source_instance
            and snapshot.schema_fingerprint == selection.schema_fingerprint
            and snapshot.analysis_status == "Validated"
            and snapshot.execution_contract_status != "Blocked"
        ):
            raise RuntimePlanError("Runtime plan schema snapshot is stale, blocked, or belongs to another source instance.")
        if not (
            int(service.service_revision or 0) == selection.service_revision
            and int(service.service_revision or 0) == selection.operation_revision
            and service.schema_snapshot == selection.schema_snapshot
            and service.schema_fingerprint == selection.schema_fingerprint
            and service.operation_name == selection.operation_name
            and service.status == "Validated"
            and service.execution_status == "Approved"
            and service.read_mapping == selection.graphql_mapping
            and int(service.approved_service_revision or 0) == selection.service_revision
            and int(service.approved_mapping_revision or 0) == selection.mapping_revision
        ):
            raise RuntimePlanError("Runtime plan GraphQL service approval is stale or mismatched.")
        if not (
            int(mapping.mapping_revision or 0) == selection.mapping_revision
            and mapping.schema_snapshot == selection.schema_snapshot
            and mapping.schema_fingerprint == selection.schema_fingerprint
            and mapping.source_instance == selection.source_instance
            and mapping.operation_name == selection.operation_name
            and mapping.operation_kind == "QUERY"
            and mapping.direction == "QUERY_TO_FRAPPE"
            and mapping.status == "Active"
        ):
            raise RuntimePlanError("Runtime plan GraphQL mapping is stale or mismatched.")

    def _frappe(self):
        if self._frappe_module is not None:
            return self._frappe_module
        import frappe
        return frappe


class FrappeRuntimePlanAudit:
    """Append-only, transaction-owned audit evidence for lifecycle transitions."""

    DOCTYPE = "HERO Runtime Plan Audit"

    def __init__(self, frappe_module=None) -> None:
        self._frappe_module = frappe_module

    def record(self, event: RuntimePlanAuditEvent) -> None:
        frappe = self._frappe()
        if getattr(getattr(frappe, "local", None), "site", None) != event.site_scope:
            raise RuntimePlanError("Runtime plan audit cannot cross site scope.")
        event_key = sha256(f"{event.plan_key}|{event.revision}|{event.action}".encode()).hexdigest()
        try:
            frappe.get_doc({
                "doctype": self.DOCTYPE,
                "event_key": event_key,
                "runtime_plan": event.plan_key,
                "site_scope": event.site_scope,
                "source_instance": event.source_instance,
                "action": event.action,
                "actor": event.actor,
                "plan_revision": event.revision,
                "generation": event.generation,
                "semantic_hash": event.semantic_hash,
            }).insert()
        except Exception as error:
            if error.__class__.__name__ != "DuplicateEntryError":
                raise
            existing = frappe.get_doc(self.DOCTYPE, event_key)
            if any(getattr(existing, field) != value for field, value in {
                "runtime_plan": event.plan_key, "action": event.action, "plan_revision": event.revision,
                "generation": event.generation, "semantic_hash": event.semantic_hash,
            }.items()):
                raise RuntimePlanConflict("Runtime plan audit identity conflicts with existing evidence.") from error

    def _frappe(self):
        if self._frappe_module is not None:
            return self._frappe_module
        import frappe
        return frappe
