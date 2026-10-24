"""B7 local-only migration engine with sealed reads and recoverable checkpoints.

Transport, commits and schema changes are deliberately outside this module.  It
only accepts already-approved B1-B6 evidence and injected adapters.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from hashlib import sha256
import json
from typing import Protocol

from hero_integration.client.graphql import HeroClientError, assert_read_only
from hero_integration.services.binding_registry import BindingRegistryService
from hero_integration.services.mapping_registry_model import MappingDirection
from hero_integration.services.read_contract import ReadContract, from_documents


class MigrationError(ValueError): pass
class MigrationConflict(MigrationError): pass
class RunState(StrEnum):
    PREPARED = "PREPARED"; AUTHORIZED = "AUTHORIZED"; RUNNING = "RUNNING"
    PARTIAL = "PARTIAL"; COMPLETED = "COMPLETED"; FAILED = "FAILED"
class ProgressState(StrEnum):
    COMPLETED = "COMPLETED"; SKIPPED = "SKIPPED"; FAILED = "FAILED"; BLOCKED = "BLOCKED"

_MISSING = object()


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip(): raise MigrationError(f"Migration {label} is required.")
    return value

def _positive(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1: raise MigrationError(f"Migration {label} must be positive.")
    return value

def _offset(value: object, label: str = "checkpoint") -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0: raise MigrationError(f"Migration {label} must be non-negative.")
    return value

def _fingerprint(value: object) -> str:
    value = _text(value, "snapshot fingerprint")
    if len(value) != 64 or any(c not in "0123456789abcdef" for c in value): raise MigrationError("Migration snapshot fingerprint is invalid.")
    return value


@dataclass(frozen=True, slots=True)
class MigrationRequest:
    run_id: str; site_scope: str; actor: str; source_instance: str; operation_identity: str
    snapshot_fingerprint: str; mapping_revision: int; target_app: str; target_doctype: str
    object_type: str; authorized: bool
    def __post_init__(self) -> None:
        for key in ("run_id", "site_scope", "actor", "source_instance", "operation_identity", "target_app", "target_doctype", "object_type"): _text(getattr(self, key), key)
        _fingerprint(self.snapshot_fingerprint); _positive(self.mapping_revision, "mapping revision")
        if self.authorized is not True: raise MigrationError("Migration request requires explicit authorization.")


@dataclass(frozen=True, slots=True)
class FieldPlan:
    definition_id: str; revision: int; source_path: tuple[str, ...]; target_field: str
    direction: MappingDirection = MappingDirection.HERO_TO_FRAPPE; availability: str = "READY"
    def __post_init__(self) -> None:
        _text(self.definition_id, "definition id"); _positive(self.revision, "field-plan revision"); _text(self.target_field, "target field")
        if not self.source_path or any(not isinstance(x, str) or not x for x in self.source_path): raise MigrationError("Field plan source path is invalid.")
        if not isinstance(self.direction, MappingDirection): raise MigrationError("Field plan direction is invalid.")
        if self.availability not in {"READY", "WAITING_APP", "PROVISIONING_REQUIRED", "INVALID"}: raise MigrationError("Field plan availability is invalid.")


@dataclass(frozen=True, slots=True)
class SealedRead:
    """Read-only, exact-query evidence derived from B1-B6 documents."""
    contract: ReadContract; query_document: str; snapshot_fingerprint: str; query_fingerprint: str
    def __post_init__(self) -> None:
        if not isinstance(self.contract, ReadContract): raise MigrationError("A validated ReadContract is required.")
        _fingerprint(self.snapshot_fingerprint)
        if not isinstance(self.query_document, str) or not self.query_document.strip(): raise MigrationError("Saved reviewed query is required.")
        try: assert_read_only(self.query_document)
        except HeroClientError as error: raise MigrationError("Sealed query must be read-only.") from error
        if self.query_fingerprint != _query_fingerprint(self.query_document): raise MigrationError("Sealed query fingerprint mismatch.")
    @classmethod
    def from_documents(cls, service, mapping, snapshot) -> "SealedRead":
        contract = from_documents(service, mapping, snapshot)
        query = service.query_document
        return cls(contract, query, snapshot.schema_fingerprint, _query_fingerprint(query))
    def validate(self, request: MigrationRequest) -> None:
        if request.snapshot_fingerprint != self.snapshot_fingerprint: raise MigrationConflict("Migration snapshot differs from sealed read.")
        if request.mapping_revision != self.contract.mapping_revision: raise MigrationConflict("Migration revision differs from sealed read.")
        expected_identity = "hero.query." + self.contract.records_path
        if request.operation_identity.lower() != expected_identity.lower(): raise MigrationConflict("Migration operation differs from sealed read root.")
        if self.query_fingerprint != _query_fingerprint(self.query_document): raise MigrationConflict("Sealed query bytes changed.")

def _query_fingerprint(value: object) -> str:
    if not isinstance(value, str): raise MigrationError("Saved reviewed query must be text.")
    return sha256(value.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class SourcePage:
    records: tuple[Mapping[str, object], ...]; next_offset: int | None
    def __post_init__(self) -> None:
        if not all(isinstance(row, Mapping) for row in self.records): raise MigrationError("Source page records must be objects.")
        if self.next_offset is not None: _offset(self.next_offset, "next offset")

@dataclass(frozen=True, slots=True)
class MigrationRun:
    request: MigrationRequest; state: RunState = RunState.PREPARED; checkpoint: int = 0; record_index: int = 0
    generation: int = 0; completed: int = 0; failed: int = 0; skipped: int = 0; last_page_fingerprint: str = ""
    def __post_init__(self) -> None:
        if not isinstance(self.state, RunState): raise MigrationError("Migration run state is invalid.")
        for key in ("checkpoint", "record_index", "generation", "completed", "failed", "skipped"): _offset(getattr(self, key), key)

@dataclass(frozen=True, slots=True)
class Progress:
    run_id: str; object_key: str; state: ProgressState; fields: tuple[str, ...] = (); detail: str = ""
    def __post_init__(self) -> None:
        _text(self.run_id, "progress run id"); _text(self.object_key, "progress object key")
        if not isinstance(self.state, ProgressState): raise MigrationError("Progress state is invalid.")
        if any(not isinstance(x, str) or not x for x in self.fields) or not isinstance(self.detail, str): raise MigrationError("Progress evidence is invalid.")

class SourceReader(Protocol):
    def read_page(self, request: MigrationRequest, sealed: SealedRead, offset: int) -> SourcePage: ...
class TargetWriter(Protocol):
    def resolve_or_write(self, site: str, app: str, doctype: str, identity: str, values: dict[str, object]) -> str: ...
class MigrationLedger(Protocol):
    def run(self, key: str) -> MigrationRun | None: ...
    def save_run(self, value: MigrationRun, expected: int | None) -> MigrationRun: ...
    def progress_for(self, run_id: str, object_key: str) -> Progress | None: ...
    def save_progress(self, value: Progress) -> Progress: ...


class InMemoryLedger:
    """Deterministic test provider; not proof of MariaDB locking."""
    def __init__(self) -> None: self.runs = {}; self.progress = {}
    def run(self, key): return self.runs.get(key)
    def save_run(self, value, expected):
        old = self.run(value.request.run_id)
        if (old is None and expected is not None) or (old is not None and old.generation != expected): raise MigrationConflict("Stale migration checkpoint.")
        self.runs[value.request.run_id] = value; return value
    def progress_for(self, run_id, object_key): return self.progress.get((run_id, object_key))
    def save_progress(self, value):
        old = self.progress_for(value.run_id, value.object_key)
        if old is not None and old != value and not _recovery_transition(old, value): raise MigrationConflict("Migration progress conflicts with prior evidence.")
        self.progress[(value.run_id, value.object_key)] = value; return value


class FrappeLedger:
    """Persistent B7 document adapter; no DDL, commit, or permission bypass."""
    RUN = "HERO Migration Run Ledger"; PROGRESS = "HERO Migration Progress Ledger"
    def __init__(self, frappe_module=None): self._frappe_module = frappe_module
    def _frappe(self):
        if self._frappe_module is not None: return self._frappe_module
        import frappe
        return frappe
    def _scope(self, site):
        frappe = self._frappe()
        if getattr(getattr(frappe, "local", None), "site", None) != site: raise MigrationError("Migration ledger cannot cross site scope.")
        user = getattr(getattr(frappe, "session", None), "user", None)
        if user != "Administrator" and not frappe.has_role("System Manager"): raise PermissionError("System Manager or Administrator authority is required.")
        return frappe
    def run(self, key):
        frappe = self._frappe(); rows = frappe.get_all(self.RUN, filters={"migration_run_id": key}, fields=["name"])
        if len(rows) > 1: raise MigrationConflict("Migration run identity is not unique.")
        return None if not rows else _run_from_doc(frappe.get_doc(self.RUN, rows[0].name))
    def save_run(self, value, expected):
        frappe = self._scope(value.request.site_scope); frappe.has_permission(self.RUN, "write", throw=True); existing = self.run(value.request.run_id)
        if existing is None:
            if expected is not None: raise MigrationConflict("Stale migration checkpoint.")
            frappe.has_permission(self.RUN, "create", throw=True); frappe.get_doc(_run_payload(value)).insert(); return value
        if existing.generation != expected: raise MigrationConflict("Stale migration checkpoint.")
        frappe.db.sql("select name from `tabHERO Migration Run Ledger` where name=%s for update", (value.request.run_id,))
        doc = frappe.get_doc(self.RUN, value.request.run_id)
        if _run_from_doc(doc).generation != expected: raise MigrationConflict("Stale migration checkpoint.")
        _assign(doc, _run_payload(value, doctype=False)); doc.save(); return value
    def progress_for(self, run_id, object_key):
        frappe = self._frappe(); rows = frappe.get_all(self.PROGRESS, filters={"migration_run_id": run_id, "object_key": object_key}, fields=["name"])
        if len(rows) > 1: raise MigrationConflict("Migration progress identity is not unique.")
        return None if not rows else _progress_from_doc(frappe.get_doc(self.PROGRESS, rows[0].name))
    def save_progress(self, value):
        run = self.run(value.run_id)
        if run is None: raise MigrationError("Migration progress requires a persistent run.")
        frappe = self._scope(run.request.site_scope); frappe.has_permission(self.PROGRESS, "write", throw=True); existing = self.progress_for(value.run_id, value.object_key)
        if existing is not None:
            if existing == value: return existing
            if not _recovery_transition(existing, value): raise MigrationConflict("Migration progress conflicts with prior evidence.")
            rows = frappe.get_all(self.PROGRESS, filters={"migration_run_id": value.run_id, "object_key": value.object_key}, fields=["name"])
            doc = frappe.get_doc(self.PROGRESS, rows[0].name); _assign(doc, _progress_payload(value)); doc.save(); return value
        frappe.has_permission(self.PROGRESS, "create", throw=True); frappe.get_doc(_progress_payload(value)).insert(); return value


class FrappeDocumentAdapter:
    """Document lifecycle writer with explicit stable target identity."""
    def __init__(self, identity_field: str, frappe_module=None): self.identity_field, self._frappe_module = _text(identity_field, "target identity field"), frappe_module
    def _frappe(self):
        if self._frappe_module is not None: return self._frappe_module
        import frappe
        return frappe
    def resolve_or_write(self, site, app, doctype, identity, values):
        frappe = self._frappe()
        if getattr(getattr(frappe, "local", None), "site", None) != site: raise MigrationError("Target document cannot cross site scope.")
        if getattr(getattr(frappe, "session", None), "user", None) != "Administrator" and not frappe.has_role("System Manager"): raise PermissionError("System Manager or Administrator authority is required.")
        _text(app, "target app"); _text(doctype, "target DocType"); _text(identity, "source identity")
        if not isinstance(values, dict) or self.identity_field in values: raise MigrationError("Target values are invalid or overwrite stable identity.")
        frappe.has_permission(doctype, "read", throw=True); rows = frappe.get_all(doctype, filters={self.identity_field: identity}, fields=["name"])
        if len(rows) > 1: raise MigrationConflict("Target identity is not unique.")
        if rows:
            frappe.has_permission(doctype, "write", throw=True); doc = frappe.get_doc(doctype, rows[0].name); _assign(doc, values); doc.save(); return doc.name
        frappe.has_permission(doctype, "create", throw=True); doc = frappe.get_doc({"doctype": doctype, self.identity_field: identity, **values}); doc.insert(); return doc.name


class MigrationEngine:
    def __init__(self, reader: SourceReader, writer: TargetWriter, bindings: BindingRegistryService, ledger: MigrationLedger, sealed: SealedRead):
        if not isinstance(sealed, SealedRead): raise MigrationError("Migration engine requires sealed read evidence.")
        self.reader, self.writer, self.bindings, self.ledger, self.sealed = reader, writer, bindings, ledger, sealed
    def prepare(self, request):
        self.sealed.validate(request); old = self.ledger.run(request.run_id)
        if old is not None and old.request != request: raise MigrationConflict("Incompatible run identity.")
        return old or self.ledger.save_run(MigrationRun(request), None)
    def authorize(self, run_id, expected):
        run = self._run(run_id)
        if run.state != RunState.PREPARED or run.generation != expected: raise MigrationConflict("Invalid lifecycle transition.")
        return self.ledger.save_run(replace(run, state=RunState.AUTHORIZED, generation=run.generation + 1), expected)
    def process(self, run_id, plans: Sequence[FieldPlan], expected):
        run = self._run(run_id)
        if run.state not in {RunState.AUTHORIZED, RunState.PARTIAL} or run.generation != expected: raise MigrationConflict("Run is not resumable.")
        self.sealed.validate(run.request); plans = _plans(plans, run.request.mapping_revision)
        if not plans: return self._partial(run, skipped=True)
        run = self.ledger.save_run(replace(run, state=RunState.RUNNING, generation=run.generation + 1), run.generation)
        seen_pages = {run.last_page_fingerprint} if run.last_page_fingerprint else set()
        while True:
            try: page = _page(self.reader.read_page(run.request, self.sealed, run.checkpoint), run.checkpoint)
            except Exception: return self._failed(run)
            fingerprint = _page_fingerprint(page.records)
            if fingerprint and fingerprint in seen_pages: return self._failed(run)
            if fingerprint: seen_pages.add(fingerprint)
            if not page.records and page.next_offset is not None: return self._failed(run)
            try: identities = [_identity(row) for row in page.records]
            except MigrationError: return self._failed(run)
            if len(identities) != len(set(identities)) or run.record_index > len(page.records): return self._failed(run)
            for index in range(run.record_index, len(page.records)):
                next_run = self._process_one(run, page.records[index], plans, index + 1, fingerprint)
                if next_run.state != RunState.RUNNING: return next_run
                run = next_run
            if page.next_offset is None: return self.ledger.save_run(replace(run, state=RunState.COMPLETED, record_index=0, generation=run.generation + 1), run.generation)
            run = self.ledger.save_run(replace(run, checkpoint=page.next_offset, record_index=0, last_page_fingerprint=fingerprint, generation=run.generation + 1), run.generation)
    def _process_one(self, run, row, plans, next_index, fingerprint):
        identity = _identity(row); values = {plan.target_field: _extract(row, plan.source_path) for plan in plans}
        if any(value is _MISSING for value in values.values()):
            self.ledger.save_progress(Progress(run.request.run_id, identity, ProgressState.BLOCKED, detail="missing-source-field")); return self._partial(run, skipped=True)
        try:
            target = self.writer.resolve_or_write(run.request.site_scope, run.request.target_app, run.request.target_doctype, identity, values)
            parent = self.bindings.create_or_resolve_object_binding(site_scope=run.request.site_scope, source_instance=run.request.source_instance, object_type=run.request.object_type, external_object_id=identity, target_app=run.request.target_app, target_doctype=run.request.target_doctype, target_name=target, mapping_definition_id=plans[0].definition_id, mapping_revision=run.request.mapping_revision)
            fields = tuple(self.bindings.create_or_resolve_field_binding(parent, source_field=".".join(plan.source_path), target_field=plan.target_field, mapping_definition_id=plan.definition_id, mapping_revision=plan.revision, direction=plan.direction).identity_key for plan in plans)
        except Exception:
            self.ledger.save_progress(Progress(run.request.run_id, identity, ProgressState.FAILED, detail="target-or-binding-write-failed")); return self._partial(run, failed=True)
        self.ledger.save_progress(Progress(run.request.run_id, identity, ProgressState.COMPLETED, fields))
        return self.ledger.save_run(replace(run, record_index=next_index, completed=run.completed + 1, last_page_fingerprint=fingerprint, generation=run.generation + 1), run.generation)
    def _partial(self, run, *, failed=False, skipped=False): return self.ledger.save_run(replace(run, state=RunState.PARTIAL, failed=run.failed + int(failed), skipped=run.skipped + int(skipped), generation=run.generation + 1), run.generation)
    def _failed(self, run): return self.ledger.save_run(replace(run, state=RunState.FAILED, failed=run.failed + 1, generation=run.generation + 1), run.generation)
    def _run(self, key):
        run = self.ledger.run(key)
        if run is None: raise MigrationError("Unknown migration run.")
        return run


def _plans(value, revision):
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)): raise MigrationError("Field plans must be a sequence.")
    ready = tuple(plan for plan in value if isinstance(plan, FieldPlan) and plan.availability == "READY")
    if len(ready) != len({plan.definition_id for plan in ready}) or len(ready) != len({plan.target_field for plan in ready}): raise MigrationConflict("Ready field plans contain duplicates.")
    if any(plan.revision != revision or plan.direction != MappingDirection.HERO_TO_FRAPPE for plan in ready): raise MigrationConflict("Ready plan no longer matches approved revision.")
    return ready

def _page(value, offset):
    records, next_offset = getattr(value, "records", None), getattr(value, "next_offset", None)
    try: page = SourcePage(tuple(records), next_offset)
    except TypeError as error: raise MigrationError("Source reader returned no page records.") from error
    if page.next_offset is not None and page.next_offset <= offset: raise MigrationError("Source page does not advance checkpoint.")
    return page

def _identity(row):
    value = row.get("id")
    if not isinstance(value, (str, int)) or isinstance(value, bool) or not str(value): raise MigrationError("Source row has no stable identity.")
    return str(value)
def _extract(value, path):
    for key in path:
        if not isinstance(value, Mapping) or key not in value: return _MISSING
        value = value[key]
    return value
def _page_fingerprint(rows):
    try: payload = json.dumps(rows, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError) as error: raise MigrationError("Source page is not deterministic JSON data.") from error
    return sha256(payload.encode()).hexdigest() if rows else ""
def _assign(doc, values):
    for key, value in values.items(): setattr(doc, key, value)
def _run_payload(value, *, doctype=True):
    request = {key: getattr(value.request, key) for key in value.request.__dataclass_fields__}
    fields = {"migration_run_id": value.request.run_id, "site_scope": value.request.site_scope, "request_json": json.dumps(request, sort_keys=True, separators=(",", ":")), "state": value.state.value, "checkpoint": value.checkpoint, "record_index": value.record_index, "generation": value.generation, "completed": value.completed, "failed": value.failed, "skipped": value.skipped, "last_page_fingerprint": value.last_page_fingerprint}
    return ({"doctype": FrappeLedger.RUN, "name": value.request.run_id} | fields) if doctype else fields
def _run_from_doc(doc):
    try: return MigrationRun(MigrationRequest(**json.loads(doc.request_json)), RunState(doc.state), int(doc.checkpoint or 0), int(doc.record_index or 0), int(doc.generation or 0), int(doc.completed or 0), int(doc.failed or 0), int(doc.skipped or 0), str(doc.last_page_fingerprint or ""))
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError) as error: raise MigrationError("Persistent migration run is malformed.") from error
def _recovery_transition(old, new):
    """A retry may replace only a recoverable failure with completed evidence."""
    return old.state in {ProgressState.FAILED, ProgressState.BLOCKED} and new.state is ProgressState.COMPLETED

def _progress_payload(value): return {"doctype": FrappeLedger.PROGRESS, "identity_key": _progress_key(value.run_id, value.object_key), "migration_run_id": value.run_id, "object_key": value.object_key, "state": value.state.value, "fields_json": json.dumps(value.fields), "detail": value.detail}
def _progress_key(run_id, object_key): return sha256((run_id + "\0" + object_key).encode()).hexdigest()
def _progress_from_doc(doc):
    try: return Progress(doc.migration_run_id, doc.object_key, ProgressState(doc.state), tuple(json.loads(doc.fields_json or "[]")), doc.detail or "")
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError) as error: raise MigrationError("Persistent migration progress is malformed.") from error
