"""Reconstruct the approved B2/B7 read boundary for B8 without persistence.

The versioned ``b8_compilation`` object in an approved read mapping is the
canonical compilation input. It contains no credentials, source payload, or
transport state. This module calls the existing B2 ``QueryAdapter`` instead
of parsing or compiling GraphQL itself.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from hashlib import sha256
import json

from hero_integration.services.mapping_contract_v1 import (
    MappingContractError,
    QueryAdapter,
    QueryDraft,
    SnapshotAdapter,
    StoredSnapshot,
)
from hero_integration.services.migration_engine import SealedRead
from hero_integration.services.read_contract import universal_from_documents
from hero_integration.services.runtime_plan import FrappeRuntimePlanReferences, RuntimePlanSelection
from hero_integration.services.sync_engine import MaterializedReadPlan, SyncError


class RuntimeReconstructionError(SyncError):
    """An approved plan cannot be reconstructed exactly from current inputs."""


def reconstruct_materialized_read_plan(selection: RuntimePlanSelection, *, frappe_module=None) -> MaterializedReadPlan:
    """Revalidate and rebuild one immutable B2 request before provider creation."""
    frappe = frappe_module or _frappe()
    try:
        FrappeRuntimePlanReferences(frappe).validate_current(selection)
        snapshot = frappe.get_doc("HERO GraphQL Schema Snapshot", selection.schema_snapshot)
        service = frappe.get_doc("HERO GraphQL Service", selection.graphql_service)
        mapping = frappe.get_doc("HERO GraphQL Mapping", selection.graphql_mapping)
        payload = _mapping_payload(mapping.mapping_json)
        draft = _draft(_compilation_input(payload), selection)
        validated = SnapshotAdapter().adapt(StoredSnapshot(
            snapshot.name, snapshot.snapshot_json, snapshot.schema_fingerprint,
            snapshot.source_instance, snapshot.analysis_status,
        ))
        request = QueryAdapter().materialize(validated, draft)
        sealed = _universal_sealed_read(service, mapping, snapshot, selection, request, payload)
        _validate_against_sealed_read(sealed, draft)
    except (MappingContractError, ValueError, TypeError, json.JSONDecodeError) as error:
        raise RuntimeReconstructionError("Approved B8 materialization inputs cannot be reconstructed.") from error
    if (
        request.schema_fingerprint != validated.ir_fingerprint
        or request.operation_name != selection.operation_name
        or request.protocol_field_name != draft.field
        or not request.result_bindings
    ):
        raise RuntimeReconstructionError("Reconstructed B2 request differs from the approved B7 read.")
    return MaterializedReadPlan(sealed, request, selection.source_instance)


def canonical_operation_contract(selection: RuntimePlanSelection, request) -> dict[str, object]:
    """Return the immutable B8 approval payload for one B2 materialization.

    The payload is persisted inside the approved mapping revision.  The
    runtime plan separately approves that revision, so changing any seal
    invalidates the plan through the existing revision/generation gate.
    """
    response_paths = [list(binding.response_path) for binding in request.result_bindings]
    payload = {
        "version": 1,
        "source_instance": selection.source_instance,
        "service_revision": selection.service_revision,
        "operation_revision": selection.operation_revision,
        "mapping_revision": selection.mapping_revision,
        "schema_fingerprint": selection.schema_fingerprint,
        "operation_name": selection.operation_name,
        "document_sha256": _hash_text(request.document),
        "variables_sha256": _hash_json(request.variables),
        "response_paths_sha256": _hash_json(response_paths),
        "request_fingerprint": request.request_fingerprint,
    }
    payload["operation_identity"] = _hash_json(payload)
    return payload


def _universal_sealed_read(service, mapping, snapshot, selection, request, payload) -> SealedRead:
    """Validate B8-only approval without relaxing the legacy read contract."""
    expected = canonical_operation_contract(selection, request)
    provided = payload.get("b8_canonical_operation")
    if not isinstance(provided, Mapping) or dict(provided) != expected:
        raise RuntimeReconstructionError("Approved B8 canonical operation seal is absent or stale.")
    if service.query_document != request.document:
        raise RuntimeReconstructionError("Approved B8 service query differs from its canonical materialization.")
    contract = universal_from_documents(service, mapping, snapshot)
    return SealedRead(contract, request.document, snapshot.schema_fingerprint, _hash_text(request.document))


def _mapping_payload(raw: object) -> Mapping[str, object]:
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeReconstructionError("Approved GraphQL mapping JSON is malformed.") from error
    if not isinstance(parsed, Mapping):
        raise RuntimeReconstructionError("Approved GraphQL mapping must be an object.")
    return parsed


def _compilation_input(parsed: Mapping[str, object]) -> Mapping[str, object]:
    if not isinstance(parsed, Mapping):
        raise RuntimeReconstructionError("Approved GraphQL mapping must be an object.")
    value = parsed.get("b8_compilation")
    if not isinstance(value, Mapping) or set(value) - {
        "version", "operation_name", "alias", "field", "selected_paths", "variables", "pagination", "ordering",
    }:
        raise RuntimeReconstructionError("Approved mapping lacks a canonical B8 compilation input.")
    if value.get("version") != 1 or _contains_secret_key(value):
        raise RuntimeReconstructionError("B8 compilation input is unsupported or contains secret-like data.")
    return value


def _hash_text(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeReconstructionError("Canonical operation document is invalid.")
    return sha256(value.encode()).hexdigest()


def _hash_json(value: object) -> str:
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError, RecursionError) as error:
        raise RuntimeReconstructionError("Canonical operation payload is invalid.") from error
    return sha256(encoded.encode()).hexdigest()


def _draft(value: Mapping[str, object], selection: RuntimePlanSelection) -> QueryDraft:
    operation_name, alias, field = (value.get(key) for key in ("operation_name", "alias", "field"))
    selected, variables = value.get("selected_paths"), value.get("variables")
    if (
        operation_name != selection.operation_name
        or not isinstance(alias, str) or not isinstance(field, str)
        or not isinstance(selected, Sequence) or isinstance(selected, (str, bytes))
        or not all(isinstance(path, str) and path for path in selected)
        or not isinstance(variables, Mapping)
        or _contains_dynamic_value(variables)
    ):
        raise RuntimeReconstructionError("B8 compilation input is incomplete or has dynamic arguments.")
    return QueryDraft(
        operation_name=operation_name, alias=alias, field=field,
        selected_paths=tuple(selected), variables=dict(variables),
        pagination=value.get("pagination") if isinstance(value.get("pagination"), str) else None,
        ordering=value.get("ordering") if isinstance(value.get("ordering"), str) else None,
    )


def _validate_against_sealed_read(sealed: SealedRead, draft: QueryDraft) -> None:
    """Bind persisted B2 arguments and result bindings to the B7 read contract.

    The materializer's rendered query alone cannot expose a changed scalar
    variable value.  Resolve the two reviewed symbolic values locally, then
    require the persisted B2 input to be exactly that concrete mapping.
    """
    contract = sealed.contract
    if draft.alias != contract.records_path or contract.identity_path not in draft.selected_paths:
        raise RuntimeReconstructionError("B8 compilation bindings differ from the sealed B7 read contract.")
    expected: dict[str, object] = {}
    for name, value in contract.variables.items():
        if value == "$offset":
            expected[name] = 0
        elif value == "$page_size":
            expected[name] = contract.page_size
        elif isinstance(value, str) and value.startswith("$"):
            raise RuntimeReconstructionError("B8 compilation cannot resolve an unreviewed symbolic argument.")
        else:
            expected[name] = value
    if dict(draft.variables) != expected:
        raise RuntimeReconstructionError("B8 compilation arguments differ from the sealed B7 read contract.")


def _contains_secret_key(value: object) -> bool:
    if isinstance(value, Mapping):
        return any("token" in str(key).lower() or "secret" in str(key).lower() or _contains_secret_key(item) for key, item in value.items())
    if isinstance(value, (tuple, list)):
        return any(_contains_secret_key(item) for item in value)
    return False


def _contains_dynamic_value(value: object) -> bool:
    if isinstance(value, str):
        return value.startswith("$")
    if isinstance(value, Mapping):
        return any(_contains_dynamic_value(item) for item in value.values())
    if isinstance(value, (tuple, list)):
        return any(_contains_dynamic_value(item) for item in value)
    return False


def _frappe():
    import frappe
    return frappe
