"""Reconstruct the approved B2/B7 read boundary for B8 without persistence.

The versioned ``b8_compilation`` object in an approved read mapping is the
canonical compilation input. It contains no credentials, source payload, or
transport state. This module calls the existing B2 ``QueryAdapter`` instead
of parsing or compiling GraphQL itself.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
import json

from hero_integration.services.mapping_contract_v1 import (
    MappingContractError,
    QueryAdapter,
    QueryDraft,
    SnapshotAdapter,
    StoredSnapshot,
)
from hero_integration.services.migration_engine import SealedRead
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
        sealed = SealedRead.from_documents(service, mapping, snapshot)
        draft = _draft(_compilation_input(mapping.mapping_json), selection)
        _validate_against_sealed_read(sealed, draft)
        validated = SnapshotAdapter().adapt(StoredSnapshot(
            snapshot.name, snapshot.snapshot_json, snapshot.schema_fingerprint,
            snapshot.source_instance, snapshot.analysis_status,
        ))
        request = QueryAdapter().materialize(validated, draft)
    except (MappingContractError, ValueError, TypeError, json.JSONDecodeError) as error:
        raise RuntimeReconstructionError("Approved B8 materialization inputs cannot be reconstructed.") from error
    if (
        request.document != sealed.query_document
        or request.schema_fingerprint != validated.ir_fingerprint
        or request.operation_name != selection.operation_name
        or request.protocol_field_name != draft.field
        or not request.result_bindings
    ):
        raise RuntimeReconstructionError("Reconstructed B2 request differs from the approved B7 read.")
    return MaterializedReadPlan(sealed, request, selection.source_instance)


def _compilation_input(raw: object) -> Mapping[str, object]:
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeReconstructionError("Approved GraphQL mapping JSON is malformed.") from error
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
