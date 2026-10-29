"""Personal Draft persistence domain model for GraphQL mappings."""
from __future__ import annotations

from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from functools import wraps
from hashlib import sha256
import json
from uuid import UUID
from typing import ContextManager, Protocol


class MappingDraftError(ValueError):
    pass


class DraftConflict(MappingDraftError):
    pass


Canonicalizer = Callable[[dict[str, object]], dict[str, object]]


@dataclass(frozen=True, slots=True)
class ActiveMapping:
    site_scope: str
    definition_id: str
    name: str
    revision: int
    schema_fingerprint: str
    content: dict[str, object]

    def __post_init__(self) -> None:
        if not all(isinstance(v, str) and v for v in (self.site_scope, self.definition_id, self.name)):
            raise MappingDraftError("Active mapping identity is incomplete.")
        if not isinstance(self.revision, int) or isinstance(self.revision, bool) or self.revision < 1:
            raise MappingDraftError("Active mapping revision must be positive.")
        _object(self.content)

    @property
    def content_hash(self) -> str:
        return _hash(self.content)


@dataclass(frozen=True, slots=True)
class MappingDraft:
    key: str
    site_scope: str
    definition_id: str
    owner: str
    generation: int
    state: str
    content: dict[str, object]
    content_hash: str
    content_revision: int
    base_active_name: str
    base_active_revision: int
    base_active_hash: str
    base_schema_fingerprint: str
    # None is a deliberately incomplete legacy record. It must never be
    # reconstructed from a later active mapping for reconciliation.
    base_content: dict[str, object] | None = None
    reconciliation_revision: int = 0
    reconciliation: dict[str, object] | None = None
    new_definition: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class MappingDraftRevision:
    draft_key: str
    generation: int
    revision: int
    kind: str
    actor: str
    content: dict[str, object]
    content_hash: str
    base_active_name: str
    base_active_revision: int
    base_active_hash: str
    base_schema_fingerprint: str


@dataclass(frozen=True, slots=True)
class DraftSaveResult:
    draft: MappingDraft
    unchanged: bool
    reconciliation_updated: bool = False


@dataclass(frozen=True, slots=True)
class ReconciliationItem:
    """One conservative semantic unit of the personal mapping content."""

    identifier: str
    kind: str
    state: str
    base_json: str
    active_json: str
    draft_json: str
    identities: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ReconciliationPreview:
    definition_id: str
    generation: int
    draft_revision: int
    reconciliation_revision: int
    base_active_name: str
    base_active_revision: int
    base_active_hash: str
    active_name: str
    active_revision: int
    active_hash: str
    active_content_hash: str
    candidate: dict[str, object]
    items: tuple[ReconciliationItem, ...]

    @property
    def suggestions(self) -> tuple[ReconciliationItem, ...]:
        return tuple(item for item in self.items if item.state == "ACTIVE_ONLY")

    @property
    def conflicts(self) -> tuple[ReconciliationItem, ...]:
        return tuple(item for item in self.items if item.state == "CONFLICT")


@dataclass(frozen=True, slots=True)
class ReconciliationProof:
    version: int
    definition_id: str
    generation: int
    draft_revision: int
    reconciliation_revision: int
    base_active_name: str
    base_active_revision: int
    base_active_hash: str
    active_name: str
    active_revision: int
    active_hash: str
    active_content_hash: str
    candidate: dict[str, object]
    accepted_suggestions: tuple[str, ...]
    conflict_choices: tuple[tuple[str, str], ...]
    result_hash: str


@dataclass(frozen=True, slots=True)
class ReconciliationApplyResult:
    content: dict[str, object]
    proof: ReconciliationProof


def reconciliation_proof_json(value: ReconciliationProof) -> str:
    if not isinstance(value, ReconciliationProof):
        raise MappingDraftError("Invalid reconciliation proof.")
    return _json({
        "version": value.version, "definition_id": value.definition_id, "generation": value.generation,
        "draft_revision": value.draft_revision, "reconciliation_revision": value.reconciliation_revision,
        "base_active_name": value.base_active_name, "base_active_revision": value.base_active_revision,
        "base_active_hash": value.base_active_hash, "active_name": value.active_name,
        "active_revision": value.active_revision, "active_hash": value.active_hash,
        "active_content_hash": value.active_content_hash, "candidate": value.candidate,
        "accepted_suggestions": list(value.accepted_suggestions),
        "conflict_choices": dict(value.conflict_choices), "result_hash": value.result_hash,
    })


def reconciliation_proof_from_json(value: object) -> ReconciliationProof:
    if not isinstance(value, str) or len(value.encode("utf-8")) > 131072:
        raise MappingDraftError("Invalid reconciliation proof.")
    try:
        data = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise MappingDraftError("Invalid reconciliation proof.") from error
    if not isinstance(data, dict) or set(data) != {
        "version", "definition_id", "generation", "draft_revision", "reconciliation_revision",
        "base_active_name", "base_active_revision", "base_active_hash", "active_name", "active_revision",
        "active_hash", "active_content_hash", "candidate", "accepted_suggestions", "conflict_choices", "result_hash",
    }:
        raise MappingDraftError("Invalid reconciliation proof.")
    strings = ("definition_id", "base_active_name", "base_active_hash", "active_name", "active_hash",
               "active_content_hash", "result_hash")
    integers = ("version", "generation", "draft_revision", "reconciliation_revision", "base_active_revision", "active_revision")
    if any(not isinstance(data[key], str) or not data[key] for key in strings):
        raise MappingDraftError("Invalid reconciliation proof.")
    if any(type(data[key]) is not int or data[key] < 0 for key in integers):
        raise MappingDraftError("Invalid reconciliation proof.")
    candidate = _object(data["candidate"])
    if len(_json(candidate).encode("utf-8")) > 65536:
        raise MappingDraftError("Invalid reconciliation proof.")
    return ReconciliationProof(
        data["version"], data["definition_id"], data["generation"], data["draft_revision"],
        data["reconciliation_revision"], data["base_active_name"], data["base_active_revision"],
        data["base_active_hash"], data["active_name"], data["active_revision"], data["active_hash"],
        data["active_content_hash"], candidate,
        _identifiers(data["accepted_suggestions"], "accepted reconciliation suggestion"),
        tuple(sorted(_conflict_choices(data["conflict_choices"]).items())), data["result_hash"],
    )


class MappingDraftStorage(Protocol):
    def transaction(self, definition_id: str, actor: str) -> ContextManager: ...
    def active(self, definition_id: str) -> ActiveMapping: ...
    def active_or_none(self, definition_id: str) -> ActiveMapping | None: ...
    def parentless(self, definition_id: str, actor: str) -> MappingDraft | None: ...
    def assert_new_identity_available(self, definition_id: str, actor: str) -> None: ...
    def draft(self, key: str) -> MappingDraft | None: ...
    def save_draft(self, value: MappingDraft) -> None: ...
    def append_revision(self, value: MappingDraftRevision) -> None: ...


class InMemoryMappingDraftStorage:
    """Test double only: it does not establish database atomicity."""

    def __init__(self) -> None:
        self._active: dict[str, ActiveMapping] = {}
        self._drafts: dict[str, MappingDraft] = {}
        self._revisions: list[MappingDraftRevision] = []

    def put_active(self, value: ActiveMapping) -> None:
        self._active[value.definition_id] = _active_copy(value)

    def active(self, definition_id: str) -> ActiveMapping:
        try:
            return _active_copy(self._active[definition_id])
        except KeyError as error:
            raise MappingDraftError("Mapping definition does not exist.") from error

    def active_or_none(self, definition_id: str) -> ActiveMapping | None:
        return _active_copy(self._active[definition_id]) if definition_id in self._active else None

    def parentless(self, definition_id: str, actor: str) -> MappingDraft | None:
        rows = [d for d in self._drafts.values() if d.definition_id == definition_id
                and d.owner == actor and d.new_definition is not None]
        if len(rows) > 1:
            raise DraftConflict('Parentless definition identity is ambiguous.')
        return _draft_copy(rows[0]) if rows else None

    def assert_new_identity_available(self, definition_id: str, actor: str) -> None:
        if definition_id in self._active or any(d.definition_id == definition_id and d.owner != actor
                                                 for d in self._drafts.values()):
            raise DraftConflict('Definition identity is already owned or active.')

    def draft(self, key: str) -> MappingDraft | None:
        return _draft_copy(self._drafts[key]) if key in self._drafts else None

    def save_draft(self, value: MappingDraft) -> None:
        self._drafts[value.key] = _draft_copy(value)

    def append_revision(self, value: MappingDraftRevision) -> None:
        self._revisions.append(_revision_copy(value))

    def revisions(self) -> tuple[MappingDraftRevision, ...]:
        return tuple(_revision_copy(value) for value in self._revisions)

    def transaction(self, definition_id: str, actor: str):
        return nullcontext()


def _transactional(method):
    @wraps(method)
    def wrapped(self, definition_id, actor, *args, **kwargs):
        with self._storage.transaction(definition_id, actor):
            return method(self, definition_id, actor, *args, **kwargs)
    return wrapped


class MappingDraftService:
    """Owns personal identity, CAS and immutable saved draft snapshots only."""

    def __init__(self, storage: MappingDraftStorage, *, canonicalize: Canonicalizer | None = None) -> None:
        self._storage = storage
        self._canonicalize = canonicalize or _object

    def open_new_draft(self, definition: dict[str, object], actor: str, content: object) -> MappingDraft:
        """Create an owned new-definition draft without asserting an active parent."""
        definition = _new_definition(definition)
        actor = _actor(actor)
        identity = definition['definition_id']
        with self._storage.transaction(identity, actor):
            self._storage.assert_new_identity_available(identity, actor)
            if hasattr(self._storage, 'validate_new_definition'):
                self._storage.validate_new_definition(definition, content)
            prior = self._storage.parentless(identity, actor)
            proposed = _object(content)
            if prior:
                if prior.new_definition != definition:
                    raise DraftConflict('New-definition context changed or is unavailable.')
                if prior.state == 'OPEN':
                    if prior.content_hash != _hash(proposed):
                        raise DraftConflict('Existing personal draft differs; use revision-checked save.')
                    return prior
                if prior.state != 'DISCARDED':
                    raise DraftConflict('New-definition draft is unavailable.')
            key = draft_key(definition['site_scope'], identity, actor)
            generation = 1 if prior is None else prior.generation + 1
            value = MappingDraft(key, definition['site_scope'], identity, actor, generation, 'OPEN', proposed,
                _hash(proposed), 1, '', 0, '', definition['schema_fingerprint'], {}, 0, None, definition)
            self._storage.save_draft(value)
            self._storage.append_revision(MappingDraftRevision(key, generation, 1, 'DRAFT_SAVE', actor,
                _copy(proposed), value.content_hash, '', 0, '', definition['schema_fingerprint']))
            return value

    @_transactional
    def open_draft(self, definition_id: str, actor: str) -> MappingDraft:
        actor = _actor(actor)
        active = self._storage.active_or_none(definition_id) if hasattr(self._storage, 'active_or_none') else self._storage.active(definition_id)
        if active is None:
            draft = self._storage.parentless(definition_id, actor)
            if draft is None or draft.state != 'OPEN':
                raise DraftConflict('No open personal mapping draft exists for this administrator.')
            return draft
        key = draft_key(active.site_scope, active.definition_id, actor)
        prior = self._storage.draft(key)
        if prior and prior.state == "OPEN":
            return prior
        content = self._active_content(active)
        value = MappingDraft(
            key, active.site_scope, active.definition_id, actor, 1 if prior is None else prior.generation + 1,
            "OPEN", content, _hash(content), 0, active.name, active.revision,
            active.content_hash, active.schema_fingerprint, _copy(content), 0, None,
        )
        self._storage.save_draft(value)
        return value

    @_transactional
    def save(self, definition_id: str, actor: str, expected_draft_revision: int, expected_active_revision: int,
             content: object, *, expected_generation: int | None = None,
             expected_reconciliation_revision: int | None = None,
             reconciliation: ReconciliationProof | None = None) -> DraftSaveResult:
        active, draft = self._current(definition_id, actor)
        if expected_generation is None and getattr(self._storage, "requires_generation", False):
            raise DraftConflict("An expected personal draft generation is required.")
        if expected_generation is not None and (type(expected_generation) is not int or expected_generation != draft.generation):
            raise DraftConflict("Personal mapping draft generation is stale.")
        if (expected_reconciliation_revision is not None
                and (type(expected_reconciliation_revision) is not int
                     or expected_reconciliation_revision != draft.reconciliation_revision)):
            raise DraftConflict("Personal mapping reconciliation evidence is stale.")
        canonical = _object(self._canonicalize(_object(content)))
        content_hash = _hash(canonical)
        if reconciliation is None:
            self._assert_current(active, draft, expected_draft_revision, expected_active_revision)
            reconciliation_value = draft.reconciliation
            reconciliation_updated = False
        else:
            reconciliation_value = self._verify_reconciliation(
                active, draft, expected_draft_revision, expected_active_revision, canonical, reconciliation,
            )
            reconciliation_updated = reconciliation_value != draft.reconciliation
        if content_hash == draft.content_hash:
            if not reconciliation_updated:
                return DraftSaveResult(draft, True)
            updated = MappingDraft(
                draft.key, draft.site_scope, draft.definition_id, draft.owner, draft.generation, "OPEN",
                _copy(draft.content), draft.content_hash, draft.content_revision, draft.base_active_name,
                draft.base_active_revision, draft.base_active_hash, draft.base_schema_fingerprint,
                _copy_optional(draft.base_content), draft.reconciliation_revision + 1, reconciliation_value,
                _copy_optional(draft.new_definition),
            )
            self._storage.save_draft(updated)
            return DraftSaveResult(updated, True, True)
        updated = MappingDraft(
            draft.key, draft.site_scope, draft.definition_id, draft.owner, draft.generation, "OPEN",
            _copy(canonical), content_hash, draft.content_revision + 1, draft.base_active_name,
            draft.base_active_revision, draft.base_active_hash, draft.base_schema_fingerprint,
            _copy_optional(draft.base_content), draft.reconciliation_revision + (1 if reconciliation_updated else 0),
            reconciliation_value, _copy_optional(draft.new_definition),
        )
        self._storage.save_draft(updated)
        self._storage.append_revision(MappingDraftRevision(
            updated.key, updated.generation, updated.content_revision, "DRAFT_SAVE", updated.owner,
            _copy(updated.content), updated.content_hash, updated.base_active_name,
            updated.base_active_revision, updated.base_active_hash, updated.base_schema_fingerprint,
        ))
        return DraftSaveResult(updated, False, reconciliation_updated)

    @_transactional
    def reconcile_preview(self, definition_id: str, actor: str, expected_draft_revision: int,
                          expected_active_revision: int, candidate: object, *, expected_generation: int,
                          expected_reconciliation_revision: int) -> ReconciliationPreview:
        active, draft = self._current(definition_id, actor)
        if active is None:
            raise DraftConflict('A new definition has no active mapping to reconcile.')
        self._assert_reconciliation_current(
            active, draft, expected_draft_revision, expected_active_revision,
            expected_generation, expected_reconciliation_revision,
        )
        canonical = _object(self._canonicalize(_object(candidate)))
        return self._comparison(active, draft, canonical)

    def reconcile_apply(self, preview: ReconciliationPreview, *, accepted_suggestions: object,
                        conflict_choices: object) -> ReconciliationApplyResult:
        accepted = _identifiers(accepted_suggestions, "accepted reconciliation suggestion")
        choices = _conflict_choices(conflict_choices)
        expected_suggestions = {item.identifier for item in preview.suggestions}
        expected_conflicts = {item.identifier for item in preview.conflicts}
        if set(accepted) != expected_suggestions:
            raise MappingDraftError("Every conflict-free active change must be explicitly accepted together.")
        if set(choices) != expected_conflicts:
            raise MappingDraftError("Every reconciliation conflict requires an explicit choice.")
        if any(choice not in {"ACTIVE", "DRAFT"} for choice in choices.values()):
            raise MappingDraftError("Invalid reconciliation conflict choice.")
        result = _copy(preview.candidate)
        for item in preview.suggestions:
            _apply_unit(result, item, "active")
        for item in preview.conflicts:
            _apply_unit(result, item, "active" if choices[item.identifier] == "ACTIVE" else "draft")
        proof = ReconciliationProof(
            1, preview.definition_id, preview.generation, preview.draft_revision,
            preview.reconciliation_revision, preview.base_active_name, preview.base_active_revision,
            preview.base_active_hash, preview.active_name, preview.active_revision, preview.active_hash,
            preview.active_content_hash, _copy(preview.candidate), tuple(sorted(accepted)),
            tuple(sorted(choices.items())), _hash(result),
        )
        return ReconciliationApplyResult(result, proof)

    @_transactional
    def discard(self, definition_id: str, actor: str, expected_generation: int, expected_draft_revision: int, *, confirmed: bool = False) -> MappingDraft:
        if confirmed is not True:
            raise MappingDraftError("Explicit confirmation is required to discard a draft.")
        active, draft = self._current(definition_id, actor)
        if draft.generation != expected_generation or draft.content_revision != expected_draft_revision:
            raise DraftConflict("Personal mapping draft changed; reload before discarding.")
        discarded = MappingDraft(
            draft.key, draft.site_scope, draft.definition_id, draft.owner, draft.generation, "DISCARDED",
            _copy(draft.content), draft.content_hash, draft.content_revision, draft.base_active_name,
            draft.base_active_revision, draft.base_active_hash, draft.base_schema_fingerprint,
            _copy_optional(draft.base_content), draft.reconciliation_revision, _copy_optional(draft.reconciliation),
            _copy_optional(draft.new_definition),
        )
        self._storage.save_draft(discarded)
        return discarded

    def _current(self, definition_id: str, actor: str) -> tuple[ActiveMapping, MappingDraft]:
        actor = _actor(actor)
        active = self._storage.active_or_none(definition_id) if hasattr(self._storage, 'active_or_none') else self._storage.active(definition_id)
        draft = (self._storage.draft(draft_key(active.site_scope, active.definition_id, actor)) if active
                 else self._storage.parentless(definition_id, actor))
        if draft is None or draft.state != "OPEN":
            raise DraftConflict("No open personal mapping draft exists for this administrator.")
        if active is not None and draft.new_definition is not None:
            raise DraftConflict('New definition now has an active parent; reconcile before saving.')
        return active, draft

    def _active_content(self, active: ActiveMapping) -> dict[str, object]:
        value = self._storage.initial_content(active.content) if hasattr(self._storage, "initial_content") else active.content
        # The initial snapshot is derived before a personal draft exists.  The
        # storage canonicalizer intentionally validates against that draft, so
        # it must only run for an already-open candidate, never for B/A.
        return _object(value)

    def _assert_reconciliation_current(self, active: ActiveMapping, draft: MappingDraft,
                                       expected_draft_revision: int, expected_active_revision: int,
                                       expected_generation: int, expected_reconciliation_revision: int) -> None:
        if (type(expected_generation) is not int or expected_generation != draft.generation
                or type(expected_reconciliation_revision) is not int
                or expected_reconciliation_revision != draft.reconciliation_revision):
            raise DraftConflict("Personal mapping draft reconciliation is stale.")
        if type(expected_draft_revision) is not int or expected_draft_revision != draft.content_revision:
            raise DraftConflict("Personal mapping draft revision is stale.")
        if type(expected_active_revision) is not int or expected_active_revision != active.revision:
            raise DraftConflict("Active mapping revision is stale.")
        if draft.base_content is None:
            raise MappingDraftError("The immutable personal draft base is unavailable; create a new draft instead.")

    def _comparison(self, active: ActiveMapping, draft: MappingDraft,
                    candidate: dict[str, object]) -> ReconciliationPreview:
        if draft.base_content is None:
            raise MappingDraftError("The immutable personal draft base is unavailable; create a new draft instead.")
        base, current, proposed = _copy(draft.base_content), self._active_content(active), _copy(candidate)
        base_units, active_units, draft_units = _semantic_units(base), _semantic_units(current), _semantic_units(proposed)
        items: list[ReconciliationItem] = []
        for identifier in sorted(set(base_units) | set(active_units) | set(draft_units)):
            before = base_units.get(identifier, _absent())
            now = active_units.get(identifier, _absent())
            own = draft_units.get(identifier, _absent())
            state = _three_way_state(before, now, own)
            kind = "REFERENCES" if identifier == "references" else "DEFINITION"
            identities = _reference_identities(before, now, own) if kind == "REFERENCES" else ()
            items.append(ReconciliationItem(identifier, kind, state, _json(before), _json(now), _json(own), identities))
        return ReconciliationPreview(
            draft.definition_id, draft.generation, draft.content_revision, draft.reconciliation_revision,
            draft.base_active_name, draft.base_active_revision, draft.base_active_hash,
            active.name, active.revision, active.content_hash, _hash(current), _copy(proposed), tuple(items),
        )

    def _verify_reconciliation(self, active: ActiveMapping, draft: MappingDraft,
                               expected_draft_revision: int, expected_active_revision: int,
                               content: dict[str, object], proof: ReconciliationProof) -> dict[str, object]:
        if not isinstance(proof, ReconciliationProof) or proof.version != 1:
            raise MappingDraftError("Invalid reconciliation proof.")
        self._assert_reconciliation_current(
            active, draft, expected_draft_revision, expected_active_revision,
            proof.generation, proof.reconciliation_revision,
        )
        current_content_hash = _hash(self._active_content(active))
        expected = (
            proof.definition_id == draft.definition_id,
            proof.draft_revision == draft.content_revision,
            proof.base_active_name == draft.base_active_name,
            proof.base_active_revision == draft.base_active_revision,
            proof.base_active_hash == draft.base_active_hash,
            proof.active_name == active.name,
            proof.active_revision == active.revision,
            proof.active_hash == active.content_hash,
            proof.active_content_hash == current_content_hash,
        )
        if not all(expected):
            raise DraftConflict("Reconciliation proof no longer matches the active mapping.")
        preview = self._comparison(active, draft, _object(proof.candidate))
        applied = self.reconcile_apply(
            preview, accepted_suggestions=proof.accepted_suggestions,
            conflict_choices=dict(proof.conflict_choices),
        )
        if applied.proof.result_hash != proof.result_hash or _hash(content) != proof.result_hash:
            raise MappingDraftError("Reconciliation result differs from the server-computed content.")
        return {
            "version": 1,
            "base_active_name": draft.base_active_name,
            "base_active_revision": draft.base_active_revision,
            "base_active_hash": draft.base_active_hash,
            "active_name": active.name,
            "active_revision": active.revision,
            "active_hash": active.content_hash,
            "active_content_hash": current_content_hash,
            "content_hash": proof.result_hash,
        }

    @staticmethod
    def _assert_current(active: ActiveMapping, draft: MappingDraft, expected_draft_revision: int, expected_active_revision: int) -> None:
        if any(type(value) is not int or value < 0 for value in (expected_draft_revision, expected_active_revision)):
            raise DraftConflict("Expected revisions must be non-negative integers.")
        if draft.content_revision != expected_draft_revision:
            raise DraftConflict("Personal mapping draft revision is stale.")
        if active is None:
            if (draft.new_definition is None or expected_active_revision != 0 or draft.base_active_name
                    or draft.base_active_revision != 0 or draft.base_active_hash or draft.reconciliation is not None):
                raise DraftConflict('A new definition requires an empty active baseline.')
            return
        name, revision, content_hash = _save_baseline(draft)
        if active.revision != expected_active_revision or active.revision != revision:
            raise DraftConflict("Active mapping revision is stale.")
        if active.name != name or active.content_hash != content_hash:
            raise DraftConflict("Active mapping content changed; reload before saving the draft.")
        if active.schema_fingerprint != draft.base_schema_fingerprint:
            raise DraftConflict("Mapping schema changed; reload before saving the draft.")
        if draft.base_content is None:
            raise DraftConflict("The immutable personal draft base is unavailable; reconciliation is required.")


def draft_key(site_scope: str, definition_id: str, owner: str) -> str:
    if not all(isinstance(v, str) and v for v in (site_scope, definition_id, owner)):
        raise MappingDraftError("Draft identity is incomplete.")
    return sha256(chr(0).join(("hero-mapping-draft-v1", site_scope, definition_id, owner)).encode()).hexdigest()


def _actor(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise MappingDraftError("A server-derived administrator identity is required.")
    return value


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise MappingDraftError("Draft mapping content must be a JSON object.")
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False))
    except (TypeError, ValueError) as error:
        raise MappingDraftError("Draft mapping content is not JSON-safe.") from error


def _hash(value: dict[str, object]) -> str:
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _copy(value: dict[str, object]) -> dict[str, object]:
    return _object(value)


_REFERENCE_KEYS = frozenset({"mapping_definition_ids", "field_rules", "field_rule_bases"})


def _copy_optional(value):
    if value is None:
        return None
    return _copy(_object(value))


def _save_baseline(draft: MappingDraft) -> tuple[str, int, str]:
    """Use the most recently verified active state without replacing B."""
    evidence = draft.reconciliation
    if evidence is None:
        return draft.base_active_name, draft.base_active_revision, draft.base_active_hash
    if not isinstance(evidence, dict):
        raise DraftConflict("Personal mapping reconciliation evidence is invalid.")
    name, revision, content_hash = (evidence.get("active_name"), evidence.get("active_revision"),
                                   evidence.get("active_hash"))
    if not isinstance(name, str) or not name or type(revision) is not int or revision < 0 or not isinstance(content_hash, str) or not content_hash:
        raise DraftConflict("Personal mapping reconciliation evidence is invalid.")
    return name, revision, content_hash


def _new_definition(value: object) -> dict[str, object]:
    keys = {'site_scope', 'definition_id', 'source_instance', 'snapshot_name', 'schema_fingerprint',
            'direction', 'operation_kind', 'operation_name'}
    if not isinstance(value, dict) or set(value) != keys:
        raise MappingDraftError('New definition requires an exact source and operation context.')
    if any(not isinstance(value[key], str) or not value[key] or len(value[key]) > 140 for key in keys):
        raise MappingDraftError('New definition context contains an invalid identity.')
    try:
        if str(UUID(value['definition_id'])) != value['definition_id']:
            raise ValueError
    except (ValueError, AttributeError):
        raise MappingDraftError('New definition requires a canonical portable UUID.') from None
    if (len(value['schema_fingerprint']) != 64 or any(c not in '0123456789abcdef' for c in value['schema_fingerprint'])
            or (value['direction'], value['operation_kind']) not in {('QUERY_TO_FRAPPE', 'QUERY'), ('FRAPPE_TO_MUTATION', 'MUTATION')}):
        raise MappingDraftError('New definition direction or schema fingerprint is invalid.')
    return _copy(value)


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _absent() -> dict[str, object]:
    return {"present": False}


def _present(value: object) -> dict[str, object]:
    return {"present": True, "value": value}


def _semantic_units(content: dict[str, object]) -> dict[str, dict[str, object]]:
    values: dict[str, dict[str, object]] = {}
    for key in sorted(key for key in content if key not in _REFERENCE_KEYS):
        values["root:" + key] = _present(_copy_value(content[key]))
    references = {key: _copy_value(content[key]) for key in _REFERENCE_KEYS if key in content}
    if references:
        values["references"] = _present(references)
    return values


def _copy_value(value: object):
    try:
        return json.loads(_json(value))
    except (TypeError, ValueError) as error:
        raise MappingDraftError("Draft mapping content is not JSON-safe.") from error


def _same_value(left: object, right: object) -> bool:
    if type(left) in (int, float) and type(right) in (int, float):
        return left == right
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_same_value(left[key], right[key]) for key in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(_same_value(a, b) for a, b in zip(left, right))
    return left == right


def _three_way_state(base: object, active: object, draft: object) -> str:
    active_changed, draft_changed = not _same_value(active, base), not _same_value(draft, base)
    if not active_changed and not draft_changed:
        return "UNCHANGED"
    if active_changed and not draft_changed:
        return "ACTIVE_ONLY"
    if not active_changed and draft_changed:
        return "DRAFT_ONLY"
    if _same_value(active, draft):
        return "SAME_CHANGE"
    return "CONFLICT"


def _reference_identities(*values: object) -> tuple[str, ...]:
    identities: set[str] = set()
    for value in values:
        if not isinstance(value, dict) or not value.get("present"):
            continue
        source = value.get("value")
        if not isinstance(source, dict):
            continue
        for identity in source.get("mapping_definition_ids", []):
            if isinstance(identity, str):
                identities.add(identity)
        for key in ("field_rules", "field_rule_bases"):
            rules = source.get(key, {})
            if isinstance(rules, dict):
                identities.update(identity for identity in rules if isinstance(identity, str))
    return tuple(sorted(identities))


def _apply_unit(content: dict[str, object], item: ReconciliationItem, source: str) -> None:
    text = item.active_json if source == "active" else item.draft_json
    unit = json.loads(text)
    if not isinstance(unit, dict) or type(unit.get("present")) is not bool:
        raise MappingDraftError("Invalid reconciliation unit.")
    if item.identifier == "references":
        for key in _REFERENCE_KEYS:
            content.pop(key, None)
        if unit["present"]:
            value = unit.get("value")
            if not isinstance(value, dict):
                raise MappingDraftError("Invalid referenced-rule reconciliation unit.")
            content.update({key: _copy_value(value[key]) for key in value if key in _REFERENCE_KEYS})
        return
    if not item.identifier.startswith("root:"):
        raise MappingDraftError("Invalid reconciliation unit identity.")
    key = item.identifier.removeprefix("root:")
    if unit["present"]:
        content[key] = _copy_value(unit.get("value"))
    else:
        content.pop(key, None)


def _identifiers(values: object, label: str) -> tuple[str, ...]:
    if not isinstance(values, (tuple, list)) or len(values) > 512:
        raise MappingDraftError("Invalid reconciliation selections.")
    if any(not isinstance(value, str) or not value or len(value) > 512 for value in values):
        raise MappingDraftError("Invalid " + label + ".")
    if len(set(values)) != len(values):
        raise MappingDraftError("Duplicate reconciliation selection.")
    return tuple(values)


def _conflict_choices(values: object) -> dict[str, str]:
    if not isinstance(values, dict) or len(values) > 512:
        raise MappingDraftError("Invalid reconciliation conflict choices.")
    if any(not isinstance(key, str) or not key or len(key) > 512 or not isinstance(value, str)
           for key, value in values.items()):
        raise MappingDraftError("Invalid reconciliation conflict choice.")
    return dict(values)


def _active_copy(value: ActiveMapping) -> ActiveMapping:
    return ActiveMapping(value.site_scope, value.definition_id, value.name, value.revision, value.schema_fingerprint, _copy(value.content))


def _draft_copy(value: MappingDraft) -> MappingDraft:
    return MappingDraft(
        value.key, value.site_scope, value.definition_id, value.owner, value.generation, value.state,
        _copy(value.content), value.content_hash, value.content_revision, value.base_active_name,
        value.base_active_revision, value.base_active_hash, value.base_schema_fingerprint,
        _copy_optional(value.base_content), value.reconciliation_revision, _copy_optional(value.reconciliation),
        _copy_optional(value.new_definition),
    )


def _revision_copy(value: MappingDraftRevision) -> MappingDraftRevision:
    return MappingDraftRevision(value.draft_key, value.generation, value.revision, value.kind, value.actor, _copy(value.content), value.content_hash, value.base_active_name, value.base_active_revision, value.base_active_hash, value.base_schema_fingerprint)
