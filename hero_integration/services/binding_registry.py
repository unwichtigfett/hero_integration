"""Execution-free B6 object and field binding registry."""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from hashlib import sha256
import json
import re
from threading import RLock
from typing import Protocol

from hero_integration.services.mapping_registry_model import MappingDirection

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_USER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@+-]{0,139}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")

class BindingRegistryError(ValueError): pass
class BindingConflict(BindingRegistryError): pass

class BindingState(StrEnum):
    UNACKNOWLEDGED = "UNACKNOWLEDGED"
    SYNCHRONIZED = "SYNCHRONIZED"
    LOCAL_CHANGED = "LOCAL_CHANGED"
    REMOTE_CHANGED = "REMOTE_CHANGED"
    MAPPING_CHANGED = "MAPPING_CHANGED"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"

def _id(value: object, label: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value): raise BindingRegistryError(f"{label} must be a stable non-secret identifier.")
    return value
def _target_id(value: object, doctype: str) -> str:
    if doctype == 'User' and isinstance(value, str) and _USER_ID.fullmatch(value): return value
    return _id(value, 'target document')
def _hash(value: object, label: str) -> str:
    if not isinstance(value, str) or not _HASH.fullmatch(value): raise BindingRegistryError(f"{label} must be a SHA-256 fingerprint.")
    return value
def _key(kind: str, **parts: object) -> str:
    return sha256(json.dumps({"kind": kind, **parts}, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
def state_fingerprint(value: object, *, present: bool) -> str:
    """Typed integrity token; it is not a confidentiality mechanism."""
    try: encoded = json.dumps({"present": present, "value": value if present else None}, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as error: raise BindingRegistryError("State must be JSON-serializable.") from error
    return sha256(encoded.encode()).hexdigest()

@dataclass(frozen=True, slots=True)
class ObjectBinding:
    identity_key: str; site_scope: str; source_instance: str; object_type: str; external_object_id: str; target_app: str; target_doctype: str; target_name: str; mapping_definition_id: str; mapping_revision: int; revision: int = 1; lifecycle: str = "VALID"; reconciliation: str = ""
    @classmethod
    def create(cls, *, site_scope: str, source_instance: str, object_type: str, external_object_id: str, target_app: str, target_doctype: str, target_name: str, mapping_definition_id: str, mapping_revision: int) -> "ObjectBinding":
        return cls(_key("hero-object-binding-v1", site=site_scope, source=source_instance, object_type=object_type, external=external_object_id, app=target_app, doctype=target_doctype, name=target_name, definition=mapping_definition_id, mapping_revision=mapping_revision), site_scope, source_instance, object_type, external_object_id, target_app, target_doctype, target_name, mapping_definition_id, mapping_revision)
    def __post_init__(self) -> None:
        for label, value in zip(("identity", "site", "source", "object type", "external object", "target app", "target DocType", "mapping definition"), (self.identity_key, self.site_scope, self.source_instance, self.object_type, self.external_object_id, self.target_app, self.target_doctype, self.mapping_definition_id)): _id(value, label)
        _target_id(self.target_name, self.target_doctype)
        if not isinstance(self.mapping_revision, int) or self.mapping_revision < 1 or not isinstance(self.revision, int) or self.revision < 1: raise BindingRegistryError("Binding revisions must be positive.")

@dataclass(frozen=True, slots=True)
class FieldBinding:
    identity_key: str; object_binding_key: str; site_scope: str; source_instance: str; source_field: str; target_app: str; target_doctype: str; target_name: str; target_field: str; mapping_definition_id: str; mapping_revision: int; direction: MappingDirection; revision: int = 1; state: BindingState = BindingState.UNACKNOWLEDGED; acknowledged_source_fingerprint: str | None = None; acknowledged_target_fingerprint: str | None = None; acknowledged_version: int = 0; reconciliation: str = ""
    @classmethod
    def create(cls, parent: ObjectBinding, *, source_field: str, target_field: str, mapping_definition_id: str, mapping_revision: int, direction: MappingDirection) -> "FieldBinding":
        return cls(_key("hero-field-binding-v1", parent=parent.identity_key, source_field=source_field, target_field=target_field, definition=mapping_definition_id, mapping_revision=mapping_revision, direction=direction.value), parent.identity_key, parent.site_scope, parent.source_instance, source_field, parent.target_app, parent.target_doctype, parent.target_name, target_field, mapping_definition_id, mapping_revision, direction)
    def __post_init__(self) -> None:
        for label, value in zip(("identity", "object binding", "site", "source", "source field", "target app", "target DocType", "target field", "mapping definition"), (self.identity_key, self.object_binding_key, self.site_scope, self.source_instance, self.source_field, self.target_app, self.target_doctype, self.target_field, self.mapping_definition_id)): _id(value, label)
        _target_id(self.target_name, self.target_doctype)
        if not isinstance(self.mapping_revision, int) or self.mapping_revision < 1 or not isinstance(self.revision, int) or self.revision < 1 or self.acknowledged_version < 0: raise BindingRegistryError("Field binding revisions are invalid.")
        for value in (self.acknowledged_source_fingerprint, self.acknowledged_target_fingerprint):
            if value is not None: _hash(value, "Acknowledged state")

class BindingStorage(Protocol):
    def object(self, site: str, key: str) -> ObjectBinding | None: ...
    def field(self, site: str, key: str) -> FieldBinding | None: ...
    def fields_for_object(self, site: str, parent: str) -> tuple[FieldBinding, ...]: ...
    def fields_for_source(self, site: str, source_instance: str) -> tuple[FieldBinding, ...]: ...
    def put_object(self, value: ObjectBinding) -> ObjectBinding: ...
    def put_field(self, value: FieldBinding) -> FieldBinding: ...
    def update_field(self, value: FieldBinding, expected_revision: int) -> FieldBinding: ...

class InMemoryBindingStorage:
    """Deterministic provider; not evidence of MariaDB locking or uniqueness."""
    def __init__(self) -> None: self._lock = RLock(); self._objects = {}; self._fields = {}
    def object(self, site, key): return self._objects.get((site, key))
    def field(self, site, key): return self._fields.get((site, key))
    def fields_for_object(self, site, parent): return tuple(sorted((v for (s, _), v in self._fields.items() if s == site and v.object_binding_key == parent), key=lambda v: v.identity_key))
    def fields_for_source(self, site, source_instance): return tuple(sorted((v for (s, _), v in self._fields.items() if s == site and v.source_instance == source_instance), key=lambda v: v.identity_key))
    def put_object(self, value):
        with self._lock:
            prior = self.object(value.site_scope, value.identity_key)
            if prior is None: self._objects[(value.site_scope, value.identity_key)] = value; return value
            return prior
    def put_field(self, value):
        with self._lock:
            prior = self.field(value.site_scope, value.identity_key)
            if prior is None:
                if self.object(value.site_scope, value.object_binding_key) is None: raise BindingRegistryError("Field binding parent does not exist.")
                self._fields[(value.site_scope, value.identity_key)] = value; return value
            return prior
    def update_field(self, value, expected_revision):
        with self._lock:
            old = self.field(value.site_scope, value.identity_key)
            if old is None or old.revision != expected_revision: raise BindingConflict("Field binding revision is stale.")
            self._fields[(value.site_scope, value.identity_key)] = value; return value

class BindingRegistryService:
    def __init__(self, storage: BindingStorage) -> None: self._storage = storage
    def create_or_resolve_object_binding(self, **kwargs) -> ObjectBinding:
        candidate = ObjectBinding.create(**kwargs); old = self._storage.object(candidate.site_scope, candidate.identity_key)
        return self._compatible(self._storage.put_object(candidate) if old is None else old, candidate)
    def create_or_resolve_field_binding(self, parent: ObjectBinding, **kwargs) -> FieldBinding:
        if self._storage.object(parent.site_scope, parent.identity_key) != parent: raise BindingRegistryError("Field binding requires its canonical persisted parent.")
        candidate = FieldBinding.create(parent, **kwargs); old = self._storage.field(candidate.site_scope, candidate.identity_key)
        return self._compatible(self._storage.put_field(candidate) if old is None else old, candidate)
    @staticmethod
    def _compatible(old, candidate):
        return compatible_binding(old, candidate)
    def get_object_binding(self, site_scope: str, identity_key: str): return self._storage.object(site_scope, identity_key)
    def get_field_bindings(self, site_scope: str, object_binding_key: str): return self._storage.fields_for_object(site_scope, object_binding_key)
    def get_field_bindings_for_source(self, site_scope: str, source_instance: str): return self._storage.fields_for_source(site_scope, source_instance)
    def get_inbound_field_bindings_for_source(self, site_scope: str, source_instance: str): return tuple(binding for binding in self._storage.fields_for_source(site_scope, source_instance) if binding.direction is MappingDirection.HERO_TO_FRAPPE)
    def record_verified_acknowledgment(self, binding: FieldBinding, *, expected_revision: int, mapping_definition_id: str, mapping_revision: int, source_fingerprint: str, target_fingerprint: str, evidence_id: str, version: int) -> FieldBinding:
        _id(evidence_id, "acknowledgment evidence")
        if mapping_definition_id != binding.mapping_definition_id or mapping_revision != binding.mapping_revision: raise BindingRegistryError("Acknowledgment does not bind the current mapping.")
        _hash(source_fingerprint, "source acknowledgment"); _hash(target_fingerprint, "target acknowledgment")
        if expected_revision != binding.revision or version < binding.acknowledged_version: raise BindingConflict("Acknowledgment is stale.")
        if version == binding.acknowledged_version and (binding.acknowledged_source_fingerprint, binding.acknowledged_target_fingerprint) not in {(None, None), (source_fingerprint, target_fingerprint)}: raise BindingConflict("Acknowledgment conflicts with recorded evidence.")
        if version == binding.acknowledged_version and binding.state == BindingState.SYNCHRONIZED: return binding
        return self._storage.update_field(replace(binding, revision=binding.revision + 1, state=BindingState.SYNCHRONIZED, acknowledged_source_fingerprint=source_fingerprint, acknowledged_target_fingerprint=target_fingerprint, acknowledged_version=version, reconciliation=""), expected_revision)
    @staticmethod
    def change_state(binding: FieldBinding, *, active_mapping_revision: int | None, mapping_available: bool, source_fingerprint: str | None, target_fingerprint: str | None) -> BindingState:
        if not mapping_available: return BindingState.RECONCILIATION_REQUIRED
        if active_mapping_revision != binding.mapping_revision: return BindingState.MAPPING_CHANGED
        if binding.acknowledged_source_fingerprint is None or binding.acknowledged_target_fingerprint is None: return BindingState.UNACKNOWLEDGED
        if source_fingerprint != binding.acknowledged_source_fingerprint and target_fingerprint != binding.acknowledged_target_fingerprint: return BindingState.RECONCILIATION_REQUIRED
        if source_fingerprint != binding.acknowledged_source_fingerprint: return BindingState.REMOTE_CHANGED
        if target_fingerprint != binding.acknowledged_target_fingerprint: return BindingState.LOCAL_CHANGED
        return BindingState.SYNCHRONIZED

    @staticmethod
    def inbound_reconciliation(binding: FieldBinding, *, active_mapping_revision: int | None, source_fingerprint: str | None, target_fingerprint: str) -> str:
        """Classify an inbound comparison without changing acknowledged or observed state.

        Both fingerprints must represent values normalized under the same
        active mapping. None means the source field was omitted, not NULL;
        callers must separately hold values whose null semantics are unknown.
        Applying a value and recording its acknowledgment remain transactional
        responsibilities of the calling writer.
        """
        if binding.direction is not MappingDirection.HERO_TO_FRAPPE:
            raise BindingRegistryError("Inbound comparison requires an inbound field binding.")
        if active_mapping_revision != binding.mapping_revision:
            return "MAPPING_CHANGED"
        if source_fingerprint is None:
            return "OMITTED"
        _hash(source_fingerprint, "source state")
        _hash(target_fingerprint, "target state")
        source_base = binding.acknowledged_source_fingerprint
        target_base = binding.acknowledged_target_fingerprint
        if source_base is None or target_base is None:
            return "UNACKNOWLEDGED"
        if source_fingerprint == target_fingerprint:
            return "CONFIRM_EQUAL"
        if source_fingerprint == source_base:
            return "KEEP_LOCAL"
        if target_fingerprint == target_base:
            return "APPLY_SOURCE"
        return "CONFLICT"


class FrappeBindingStorage:
    """Narrow source-only adapter; it uses no DDL, commits, or public API."""
    OBJECT = "HERO Object Binding"
    FIELD = "HERO Field Binding"
    def __init__(self, frappe_module=None) -> None: self._frappe_module = frappe_module
    def _frappe(self):
        if self._frappe_module is not None: return self._frappe_module
        import frappe
        return frappe
    def _scope(self, site):
        frappe = self._frappe()
        if getattr(getattr(frappe, "local", None), "site", None) != site: raise BindingRegistryError("Binding access cannot cross site scope.")
        return frappe
    def _write(self, frappe, doctype, permission="write"):
        user = getattr(getattr(frappe, "session", None), "user", None)
        if user != "Administrator" and "System Manager" not in frappe.get_roles(): raise PermissionError("System Manager or Administrator authority is required.")
        frappe.has_permission(doctype, permission, throw=True)
    def object(self, site, key):
        frappe = self._scope(site); rows = frappe.get_all(self.OBJECT, filters={"site_scope": site, "identity_key": key}, fields=["name"])
        if len(rows) > 1: raise BindingConflict("Object binding identity is not unique.")
        return None if not rows else self._object(frappe.get_doc(self.OBJECT, rows[0].name))
    def field(self, site, key):
        frappe = self._scope(site); rows = frappe.get_all(self.FIELD, filters={"site_scope": site, "identity_key": key}, fields=["name"])
        if len(rows) > 1: raise BindingConflict("Field binding identity is not unique.")
        return None if not rows else self._field(frappe.get_doc(self.FIELD, rows[0].name))
    def fields_for_object(self, site, parent):
        frappe = self._scope(site); rows = frappe.get_all(self.FIELD, filters={"site_scope": site, "object_binding": parent}, fields=["name"], order_by="identity_key asc")
        return tuple(self._field(frappe.get_doc(self.FIELD, row.name)) for row in rows)
    def fields_for_source(self, site, source_instance):
        frappe = self._scope(site); rows = frappe.get_all(self.FIELD, filters={"site_scope": site, "source_instance": source_instance}, fields=["name"], order_by="identity_key asc")
        return tuple(self._field(frappe.get_doc(self.FIELD, row.name)) for row in rows)
    def put_object(self, value):
        frappe = self._scope(value.site_scope); self._write(frappe, self.OBJECT, "create"); old = self.object(value.site_scope, value.identity_key)
        if old is not None: return old
        try:
            frappe.get_doc({"doctype": self.OBJECT, "identity_key": value.identity_key, "site_scope": value.site_scope, "source_instance": value.source_instance, "object_type": value.object_type, "external_object_id": value.external_object_id, "target_app": value.target_app, "target_doctype": value.target_doctype, "target_name": value.target_name, "mapping_definition_id": value.mapping_definition_id, "mapping_revision": value.mapping_revision, "binding_revision": value.revision, "lifecycle": value.lifecycle, "reconciliation": value.reconciliation}).insert()
            return value
        except Exception as error:
            return self._after_duplicate(error, lambda: self.object(value.site_scope, value.identity_key), value)
    def put_field(self, value):
        frappe = self._scope(value.site_scope); self._write(frappe, self.FIELD, "create")
        parent = self.object(value.site_scope, value.object_binding_key)
        if parent is None: raise BindingRegistryError("Field binding parent does not exist.")
        if (value.source_instance, value.target_app, value.target_doctype, value.target_name) != (parent.source_instance, parent.target_app, parent.target_doctype, parent.target_name): raise BindingRegistryError("Field binding does not match its object binding identity.")
        old = self.field(value.site_scope, value.identity_key)
        if old is not None: return old
        try:
            frappe.get_doc({"doctype": self.FIELD, "identity_key": value.identity_key, "object_binding": value.object_binding_key, "site_scope": value.site_scope, "source_instance": value.source_instance, "source_field": value.source_field, "target_app": value.target_app, "target_doctype": value.target_doctype, "target_name": value.target_name, "target_field": value.target_field, "mapping_definition_id": value.mapping_definition_id, "mapping_revision": value.mapping_revision, "direction": value.direction.value, "binding_revision": value.revision, "sync_state": value.state.value, "acknowledged_source_fingerprint": value.acknowledged_source_fingerprint, "acknowledged_target_fingerprint": value.acknowledged_target_fingerprint, "acknowledged_version": value.acknowledged_version, "reconciliation": value.reconciliation}).insert()
            return value
        except Exception as error:
            return self._after_duplicate(error, lambda: self.field(value.site_scope, value.identity_key), value)
    def update_field(self, value, expected_revision):
        frappe = self._scope(value.site_scope); self._write(frappe, self.FIELD)
        frappe.db.sql("select name from `tabHERO Field Binding` where identity_key=%s and site_scope=%s for update", (value.identity_key, value.site_scope))
        old = self.field(value.site_scope, value.identity_key)
        if old is None or old.revision != expected_revision: raise BindingConflict("Field binding revision is stale.")
        doc = frappe.get_doc(self.FIELD, value.identity_key)
        for key, item in {"binding_revision": value.revision, "sync_state": value.state.value, "acknowledged_source_fingerprint": value.acknowledged_source_fingerprint, "acknowledged_target_fingerprint": value.acknowledged_target_fingerprint, "acknowledged_version": value.acknowledged_version, "reconciliation": value.reconciliation}.items(): setattr(doc, key, item)
        doc.save(); return value
    @staticmethod
    def _object(doc): return ObjectBinding(doc.identity_key, doc.site_scope, doc.source_instance, doc.object_type, doc.external_object_id, doc.target_app, doc.target_doctype, doc.target_name, doc.mapping_definition_id, int(doc.mapping_revision), int(doc.binding_revision), doc.lifecycle or "VALID", doc.reconciliation or "")
    @staticmethod
    def _field(doc): return FieldBinding(doc.identity_key, doc.object_binding, doc.site_scope, doc.source_instance, doc.source_field, doc.target_app, doc.target_doctype, doc.target_name, doc.target_field, doc.mapping_definition_id, int(doc.mapping_revision), MappingDirection(doc.direction), int(doc.binding_revision), BindingState(doc.sync_state or BindingState.UNACKNOWLEDGED), doc.acknowledged_source_fingerprint or None, doc.acknowledged_target_fingerprint or None, int(doc.acknowledged_version or 0), doc.reconciliation or "")
    @staticmethod
    def _after_duplicate(error, reread, candidate):
        """Only Frappe's explicit duplicate exception is safe to retry by reread."""
        if error.__class__.__name__ != "DuplicateEntryError": raise error
        existing = reread()
        if existing is None: raise BindingConflict("Duplicate insertion did not expose a canonical binding.") from error
        return compatible_binding(existing, candidate)


def compatible_binding(existing, candidate):
    """Resolve immutable identity without resetting acknowledgment or lifecycle state."""
    if type(existing) is not type(candidate):
        raise BindingConflict("Canonical binding identity collides with incompatible state.")
    if isinstance(existing, ObjectBinding) and (existing.lifecycle != "VALID" or existing.reconciliation):
        raise BindingConflict("Object binding requires explicit lifecycle reconciliation.")
    mutable = ("revision",) if isinstance(candidate, ObjectBinding) else (
        "revision", "state", "acknowledged_source_fingerprint", "acknowledged_target_fingerprint",
        "acknowledged_version", "reconciliation",
    )
    identity_only = replace(existing, **{name: getattr(candidate, name) for name in mutable})
    if identity_only != candidate:
        raise BindingConflict("Canonical binding identity collides with incompatible state.")
    return existing
