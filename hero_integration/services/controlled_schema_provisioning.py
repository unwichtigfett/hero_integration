"""Controlled, execution-free schema provisioning planner for Mapping Registry v3."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
import json

from hero_integration.services.frappe_field_registry import FrappeFieldRegistry, ResolutionCode
from hero_integration.services.mapping_registry_model import MappingDefinition, TransformPolicy


class ProvisioningError(ValueError):
    pass


class ProvisioningOperationKind(StrEnum):
    CREATE_CUSTOM_DOCTYPE = "CREATE_CUSTOM_DOCTYPE"
    CREATE_CUSTOM_FIELD = "CREATE_CUSTOM_FIELD"
    REUSE_EXISTING_COMPATIBLE_TARGET = "REUSE_EXISTING_COMPATIBLE_TARGET"
    WAITING_APP = "WAITING_APP"
    REJECT_UNSUPPORTED_OR_UNSAFE = "REJECT_UNSUPPORTED_OR_UNSAFE"


class ProvisioningState(StrEnum):
    PREPARED = "PREPARED"
    AUTHORIZED = "AUTHORIZED"
    APPLYING = "APPLYING"
    APPLIED = "APPLIED"
    PARTIAL = "PARTIAL"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"


@dataclass(frozen=True)
class ModuleOwner:
    module: str
    app: str


@dataclass(frozen=True)
class ProvisioningRequest:
    mapping: MappingDefinition
    module: str
    metadata_fingerprint: str


@dataclass(frozen=True)
class ProvisioningField:
    """One validated initial field for a site-owned Custom DocType."""

    fieldname: str
    fieldtype: str


@dataclass(frozen=True)
class ProvisioningOperation:
    operation_id: str
    kind: ProvisioningOperationKind
    app: str
    module: str
    doctype: str
    fieldname: str | None
    fieldtype: str | None
    dependencies: tuple[str, ...] = ()
    reason: str = ""
    mapping_definition_ids: tuple[str, ...] = ()
    initial_fields: tuple[ProvisioningField, ...] = ()


@dataclass(frozen=True)
class ProvisioningPlan:
    plan_id: str
    fingerprint: str
    site_scope: str
    mapping_revision: int
    metadata_fingerprint: str
    operations: tuple[ProvisioningOperation, ...]
    state: ProvisioningState = ProvisioningState.PREPARED
    execution_authority: str = "NONE"


class ProvisioningPlanner:
    """Pure planner: it never imports Frappe, persists audit, or writes schema."""

    def prepare(
        self,
        site: str,
        requests: tuple[ProvisioningRequest, ...],
        fields: FrappeFieldRegistry,
        owners: tuple[ModuleOwner, ...],
    ) -> ProvisioningPlan:
        if site != fields.site or not site or not isinstance(requests, tuple) or not requests:
            raise ProvisioningError("A non-empty site-scoped immutable request set is required.")
        if any(not isinstance(item, ProvisioningRequest) for item in requests):
            raise ProvisioningError("Provisioning requests are malformed.")
        owner_index = {item.module: item.app for item in owners}
        if len(owner_index) != len(owners) or any(not item.module or not item.app for item in owners):
            raise ProvisioningError("Ambiguous module ownership.")
        revisions = {item.mapping.mapping_revision for item in requests}
        fingerprints = {item.metadata_fingerprint for item in requests}
        if len(revisions) != 1 or len(fingerprints) != 1 or not next(iter(fingerprints)):
            raise ProvisioningError("A plan requires one mapping revision and metadata fingerprint.")

        operations: list[ProvisioningOperation] = []
        pending_doctypes: dict[tuple[str, str, str], list[ProvisioningRequest]] = {}
        pending_fields: dict[tuple[str, str, str, str], list[ProvisioningRequest]] = {}
        for request in sorted(requests, key=lambda item: item.mapping.definition_id):
            mapping = request.mapping
            target = mapping.frappe
            reason = _provisionable_mapping_error(mapping)
            if reason:
                operations.append(_operation(mapping, request, ProvisioningOperationKind.REJECT_UNSUPPORTED_OR_UNSAFE, target.fieldname, reason))
                continue
            if target.app not in fields.installed_apps:
                operations.append(_operation(mapping, request, ProvisioningOperationKind.WAITING_APP, None, "Target application is not installed."))
                continue
            if owner_index.get(request.module) != target.app:
                operations.append(_operation(mapping, request, ProvisioningOperationKind.REJECT_UNSUPPORTED_OR_UNSAFE, None, "Module ownership is unresolved or incompatible."))
                continue
            resolution = fields.resolve(target.identity)
            if resolution.code == ResolutionCode.RESOLVED:
                operations.append(_operation(mapping, request, ProvisioningOperationKind.REUSE_EXISTING_COMPATIBLE_TARGET, target.fieldname, "Effective target metadata is compatible."))
            elif resolution.code == ResolutionCode.MISSING_DOCTYPE:
                pending_doctypes.setdefault((target.app, request.module, target.doctype), []).append(request)
            elif resolution.code == ResolutionCode.MISSING_DOCFIELD:
                pending_fields.setdefault((target.app, request.module, target.doctype, target.fieldname), []).append(request)
            elif resolution.code in {
                ResolutionCode.PROTECTED_FIELD,
                ResolutionCode.READ_ONLY_FIELD,
                ResolutionCode.UNSUPPORTED_FIELD,
                ResolutionCode.AMBIGUOUS_OWNERSHIP,
                ResolutionCode.UNRESOLVED_OWNERSHIP,
            }:
                operations.append(_operation(mapping, request, ProvisioningOperationKind.REJECT_UNSUPPORTED_OR_UNSAFE, target.fieldname, "Target is protected, unsupported, or ambiguously owned."))
            else:
                operations.append(_operation(mapping, request, ProvisioningOperationKind.REJECT_UNSUPPORTED_OR_UNSAFE, target.fieldname, "Target cannot be safely provisioned."))

        operations.extend(_doctype_operations(pending_doctypes))
        operations.extend(_field_operations(pending_fields))
        operations = sorted(operations, key=lambda item: item.operation_id)
        canonical = [_payload(item) for item in operations]
        digest = sha256(json.dumps(
            {"site": site, "revision": next(iter(revisions)), "metadata": next(iter(fingerprints)), "operations": canonical},
            sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()
        return ProvisioningPlan(
            f"plan:{digest[:24]}", digest, site, next(iter(revisions)), next(iter(fingerprints)), tuple(operations)
        )


def _doctype_operations(groups: dict[tuple[str, str, str], list[ProvisioningRequest]]) -> tuple[ProvisioningOperation, ...]:
    result: list[ProvisioningOperation] = []
    for (app, module, doctype), requests in sorted(groups.items()):
        requested = {(item.mapping.frappe.fieldname, item.mapping.frappe_field_type) for item in requests}
        by_name = {name: fieldtype for name, fieldtype in requested}
        if len(by_name) != len(requested):
            raise ProvisioningError("One missing DocType has conflicting requested field definitions.")
        initial_fields = tuple(ProvisioningField(name, fieldtype) for name, fieldtype in sorted(requested))
        mapping_ids = tuple(sorted(item.mapping.definition_id for item in requests))
        first = initial_fields[0]
        result.append(ProvisioningOperation(
            f"CREATE_CUSTOM_DOCTYPE:{app}:{module}:{doctype}",
            ProvisioningOperationKind.CREATE_CUSTOM_DOCTYPE,
            app, module, doctype, first.fieldname, first.fieldtype,
            reason="Create a site-owned Custom DocType with validated initial fields.",
            mapping_definition_ids=mapping_ids,
            initial_fields=initial_fields,
        ))
    return tuple(result)


def _field_operations(groups: dict[tuple[str, str, str, str], list[ProvisioningRequest]]) -> tuple[ProvisioningOperation, ...]:
    result: list[ProvisioningOperation] = []
    for (app, module, doctype, fieldname), requests in sorted(groups.items()):
        types = {item.mapping.frappe_field_type for item in requests}
        if len(types) != 1:
            raise ProvisioningError("One missing DocField has conflicting requested field types.")
        result.append(ProvisioningOperation(
            f"CREATE_CUSTOM_FIELD:{app}:{module}:{doctype}:{fieldname}",
            ProvisioningOperationKind.CREATE_CUSTOM_FIELD,
            app, module, doctype, fieldname, next(iter(types)),
            reason="Create a validated Custom Field only.",
            mapping_definition_ids=tuple(sorted(item.mapping.definition_id for item in requests)),
        ))
    return tuple(result)


def _operation(
    mapping: MappingDefinition,
    request: ProvisioningRequest,
    kind: ProvisioningOperationKind,
    fieldname: str | None,
    reason: str,
    dependencies: tuple[str, ...] = (),
) -> ProvisioningOperation:
    target = mapping.frappe
    suffix = fieldname or "doctype"
    return ProvisioningOperation(
        f"{kind.value}:{mapping.definition_id}:{suffix}",
        kind, target.app, request.module, target.doctype,
        fieldname, mapping.frappe_field_type if fieldname else None,
        dependencies, reason, (mapping.definition_id,),
    )


def _provisionable_mapping_error(mapping: MappingDefinition) -> str | None:
    """Allow standalone scalars only; link and row semantics require later explicit contracts."""
    source = mapping.graphql_type
    if source.list_depth:
        return "Collection targets require an explicit child-row provisioning contract."
    if mapping.frappe_field_type in {"Link", "Dynamic Link", "Table", "Table MultiSelect"}:
        return "Linked and child-table targets require explicit dependency metadata."
    family = {
        "Data": "TEXT", "Small Text": "TEXT", "Long Text": "TEXT", "Text": "TEXT",
        "Int": "NUMBER", "Long Int": "NUMBER", "Float": "NUMBER", "Currency": "NUMBER", "Percent": "NUMBER",
        "Check": "BOOLEAN", "Date": "DATE_TIME", "Datetime": "DATE_TIME",
    }.get(mapping.frappe_field_type)
    if family is None:
        return "Requested Frappe field type is not safely provisionable without additional metadata."
    scalar = source.named_type
    compatible = (
        (scalar in {"String", "ID"} and family == "TEXT" and mapping.transform == TransformPolicy.IDENTITY)
        or (scalar in {"Int", "Float"} and family == "NUMBER" and mapping.transform == TransformPolicy.IDENTITY)
        or (scalar == "Boolean" and family == "BOOLEAN" and mapping.transform == TransformPolicy.IDENTITY)
        or (scalar in {"Date", "DateTime", "Time"} and family == "DATE_TIME" and mapping.transform == TransformPolicy.IDENTITY)
        or (scalar == "String" and family == "NUMBER" and mapping.transform == TransformPolicy.STRING_TO_NUMBER)
        or (scalar in {"Int", "Float"} and family == "TEXT" and mapping.transform == TransformPolicy.NUMBER_TO_STRING)
    )
    return None if compatible else "GraphQL and requested Frappe field types require an unsupported implicit conversion."


def _payload(item: ProvisioningOperation) -> dict[str, object]:
    return {
        "id": item.operation_id, "kind": item.kind.value, "app": item.app, "module": item.module,
        "doctype": item.doctype, "fieldname": item.fieldname, "fieldtype": item.fieldtype,
        "dependencies": item.dependencies, "reason": item.reason,
        "mapping_definition_ids": item.mapping_definition_ids,
        "initial_fields": [(field.fieldname, field.fieldtype) for field in item.initial_fields],
    }
