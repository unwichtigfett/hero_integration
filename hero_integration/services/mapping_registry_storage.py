"""Isolated transactional storage contract for Mapping Registry v3.

This module defines the site-scoped compare-and-swap behaviour for both the
source-only Frappe v16 adapter and the deterministic isolated test provider.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from threading import RLock
from types import MappingProxyType
from typing import Protocol, runtime_checkable
import json


class MappingRegistryStorageError(RuntimeError):
    """The storage contract cannot safely complete an operation."""


class MappingRegistryRevisionConflict(MappingRegistryStorageError):
    """The caller's expected active revision is no longer current."""


class MappingRegistryInjectedFailure(MappingRegistryStorageError):
    """A deterministic test-only failure used to verify rollback behaviour."""


@dataclass(frozen=True, slots=True)
class StoredMappingVersion:
    """An immutable complete mapping configuration for one site revision."""

    site_scope: str
    revision: int
    payload: object

    def __post_init__(self) -> None:
        _validate_site_scope(self.site_scope)
        if not isinstance(self.revision, int) or isinstance(self.revision, bool) or self.revision < 1:
            raise MappingRegistryStorageError("Mapping registry revision must be a positive integer.")


@runtime_checkable
class MappingRegistryStorage(Protocol):
    """Narrow site-scoped storage contract required by Registry v3 services."""

    def read_active(self, site_scope: str) -> StoredMappingVersion | None: ...

    def history(self, site_scope: str) -> tuple[StoredMappingVersion, ...]: ...

    def replace(
        self, site_scope: str, expected_revision: int | None, payload: object
    ) -> StoredMappingVersion: ...


class InMemoryMappingRegistryStorage:
    """Thread-safe isolated transaction provider; it is not a MariaDB proof.

    Candidate data and the active pointer are prepared off-state and published
    by one lock-protected replacement. A reader can therefore observe either
    the old complete active version or the new complete active version, never
    an intermediate candidate. Prior revisions are append-only.
    """

    def __init__(self) -> None:
        self._lock = RLock()
        self._history: dict[str, tuple[StoredMappingVersion, ...]] = {}
        self._active: dict[str, int] = {}
        self._fail_next: str | None = None

    def fail_next(self, phase: str) -> None:
        """Inject one failure before publication (``persist`` or ``activate``)."""
        if phase not in {"persist", "activate"}:
            raise MappingRegistryStorageError("Failure phase must be persist or activate.")
        with self._lock:
            self._fail_next = phase

    def read_active(self, site_scope: str) -> StoredMappingVersion | None:
        _validate_site_scope(site_scope)
        with self._lock:
            revision = self._active.get(site_scope)
            if revision is None:
                return None
            return _snapshot(self._history[site_scope][revision - 1])

    def history(self, site_scope: str) -> tuple[StoredMappingVersion, ...]:
        _validate_site_scope(site_scope)
        with self._lock:
            return tuple(_snapshot(item) for item in self._history.get(site_scope, ()))

    def replace(
        self, site_scope: str, expected_revision: int | None, payload: object
    ) -> StoredMappingVersion:
        """Atomically persist and activate a complete replacement revision.

        ``payload`` is opaque to storage: import/edit services must validate it
        first. It is deep-frozen before publication so later caller mutation
        cannot alter historical records. ``None`` is the sole expected value
        for a fresh empty registry; all updates require exact CAS revision.
        """
        _validate_site_scope(site_scope)
        _validate_expected_revision(expected_revision)
        frozen_payload = _freeze(payload)
        with self._lock:
            current = self._active.get(site_scope)
            if current != expected_revision:
                raise MappingRegistryRevisionConflict(
                    f"Expected active revision {expected_revision!r}, found {current!r}."
                )
            if self._consume_failure("persist"):
                raise MappingRegistryInjectedFailure("Candidate persistence failed before activation.")

            candidate = StoredMappingVersion(site_scope, (current or 0) + 1, frozen_payload)
            proposed_history = self._history.get(site_scope, ()) + (candidate,)
            proposed_active = dict(self._active)
            proposed_history_by_site = dict(self._history)
            proposed_history_by_site[site_scope] = proposed_history
            proposed_active[site_scope] = candidate.revision
            if self._consume_failure("activate"):
                raise MappingRegistryInjectedFailure(
                    "Activation failed; previous active revision was retained."
                )
            self._history = proposed_history_by_site
            self._active = proposed_active
            return _snapshot(candidate)

    def _consume_failure(self, phase: str) -> bool:
        if self._fail_next == phase:
            self._fail_next = None
            return True
        return False


def _validate_site_scope(site_scope: object) -> None:
    if not isinstance(site_scope, str) or not site_scope:
        raise MappingRegistryStorageError("Mapping registry site scope must be non-empty.")


def _validate_expected_revision(expected_revision: object) -> None:
    if expected_revision is None:
        return
    if not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or expected_revision < 1:
        raise MappingRegistryStorageError("Expected revision must be None or a positive integer.")


def _freeze(value: object) -> object:
    """Copy recursively into immutable containers without imposing a schema."""
    if isinstance(value, Mapping):
        return MappingProxyType({_freeze(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (str, bytes, int, float, bool, type(None))):
        return value
    if isinstance(value, (tuple, list)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (frozenset, set)):
        return frozenset(_freeze(item) for item in value)
    return deepcopy(value)


def _snapshot(version: StoredMappingVersion) -> StoredMappingVersion:
    """Return a value object without exposing mutable storage handles."""
    return StoredMappingVersion(version.site_scope, version.revision, _copy_frozen(version.payload))


def _copy_frozen(value: object) -> object:
    """Create an independent immutable snapshot without exposing storage state."""
    if isinstance(value, Mapping):
        return MappingProxyType({_copy_frozen(key): _copy_frozen(item) for key, item in value.items()})
    if isinstance(value, (str, bytes, int, float, bool, type(None))):
        return value
    if isinstance(value, tuple):
        return tuple(_copy_frozen(item) for item in value)
    if isinstance(value, frozenset):
        return frozenset(_copy_frozen(item) for item in value)
    return deepcopy(value)


class FrappeMappingRegistryStorage:
    """Frappe-v16 document adapter for the same site-scoped CAS contract.

    The source DocTypes are installed only by a separately authorized migration.
    This adapter deliberately performs no DDL, migration, commit, or transport
    action.  Its transaction behaviour is not asserted by the isolated tests.
    """

    _REGISTRY = "HERO Mapping Registry"
    _VERSION = "HERO Mapping Registry Version"

    def __init__(self, frappe_module=None) -> None:
        self._frappe_module = frappe_module

    def read_active(self, site_scope: str) -> StoredMappingVersion | None:
        frappe = self._frappe()
        self._assert_scope(frappe, site_scope)
        registry = self._registry_document(frappe, site_scope)
        if registry is None or int(registry.active_revision or 0) == 0:
            return None
        version = frappe.get_doc(self._VERSION, registry.active_version)
        return StoredMappingVersion(site_scope, int(version.revision), _decode_payload(version.mapping_payload_json))

    def history(self, site_scope: str) -> tuple[StoredMappingVersion, ...]:
        frappe = self._frappe()
        self._assert_scope(frappe, site_scope)
        registry = self._registry_document(frappe, site_scope)
        if registry is None:
            return ()
        rows = frappe.get_all(
            self._VERSION,
            filters={"mapping_registry": registry.name},
            fields=["revision", "mapping_payload_json"],
            order_by="revision asc",
        )
        return tuple(
            StoredMappingVersion(site_scope, int(row.revision), _decode_payload(row.mapping_payload_json))
            for row in rows
        )

    def replace(self, site_scope: str, expected_revision: int | None, payload: object) -> StoredMappingVersion:
        """Persist then point to one complete version under a row lock.

        A failure rolls back this adapter's savepoint.  It does not claim that
        external effects or a caller-owned transaction are rollback-capable.
        """
        _validate_site_scope(site_scope)
        _validate_expected_revision(expected_revision)
        frappe = self._frappe()
        self._assert_scope(frappe, site_scope)
        self._assert_write_authority(frappe)
        serialized = json.dumps(_json_value(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        savepoint = "hero_mapping_registry_v3_replace"
        frappe.db.savepoint(savepoint)
        try:
            registry = self._locked_registry(frappe, site_scope)
            current = None if registry is None or int(registry.active_revision or 0) == 0 else int(registry.active_revision)
            if current != expected_revision:
                raise MappingRegistryRevisionConflict(
                    f"Expected active revision {expected_revision!r}, found {current!r}."
                )
            if registry is None:
                registry = frappe.get_doc({"doctype": self._REGISTRY, "site_scope": site_scope, "active_revision": 0})
                registry.insert()
            revision = (current or 0) + 1
            version = frappe.get_doc({
                "doctype": self._VERSION,
                "mapping_registry": registry.name,
                "revision": revision,
                "format_version": "hero-frappe-mapping-v3",
                "schema_fingerprint": str(_payload_value(payload, "schema_fingerprint") or ""),
                "integrity_hash": _integrity(serialized),
                "mapping_count": len(_payload_value(payload, "definitions") or ()),
                "mapping_payload_json": serialized,
            })
            version.insert()
            registry.active_revision = revision
            registry.active_version = version.name
            registry.format_version = "hero-frappe-mapping-v3"
            registry.updated_by_registry = "mapping-registry-v3"
            registry.save()
            return StoredMappingVersion(site_scope, revision, _freeze(payload))
        except Exception:
            frappe.db.rollback(save_point=savepoint)
            raise

    def _frappe(self):
        if self._frappe_module is not None:
            return self._frappe_module
        import frappe
        return frappe

    def _assert_scope(self, frappe, site_scope: str) -> None:
        if getattr(getattr(frappe, "local", None), "site", None) != site_scope:
            raise MappingRegistryStorageError("A mapping registry operation cannot cross site scope.")

    def _assert_write_authority(self, frappe) -> None:
        user = getattr(getattr(frappe, "session", None), "user", None)
        if user != "Administrator" and not frappe.has_role("System Manager"):
            raise PermissionError("System Manager or Administrator authority is required.")
        frappe.has_permission(self._REGISTRY, "write", throw=True)

    def _registry_document(self, frappe, site_scope: str):
        rows = frappe.get_all(self._REGISTRY, filters={"site_scope": site_scope}, fields=["name", "active_revision", "active_version"])
        if len(rows) > 1:
            raise MappingRegistryStorageError("Site scope has more than one mapping registry pointer.")
        return frappe.get_doc(self._REGISTRY, rows[0].name) if rows else None

    def _locked_registry(self, frappe, site_scope: str):
        rows = frappe.db.sql(
            "select name from `tabHERO Mapping Registry` where site_scope=%s for update",
            (site_scope,), as_dict=True,
        )
        if len(rows) > 1:
            raise MappingRegistryStorageError("Site scope has more than one mapping registry pointer.")
        return frappe.get_doc(self._REGISTRY, rows[0].name) if rows else None


def _json_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, frozenset):
        return sorted(_json_value(item) for item in value)
    return value


def _decode_payload(value: object) -> object:
    try:
        return _freeze(json.loads(str(value)))
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise MappingRegistryStorageError("Stored mapping registry payload is invalid.") from error


def _payload_value(payload: object, key: str) -> object:
    return payload.get(key) if isinstance(payload, Mapping) else None


def _integrity(serialized: str) -> str:
    from hashlib import sha256
    return sha256(serialized.encode("utf-8")).hexdigest()
