"""Personal Draft persistence domain model for GraphQL mappings."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
import json


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

    def draft(self, key: str) -> MappingDraft | None:
        return _draft_copy(self._drafts[key]) if key in self._drafts else None

    def save_draft(self, value: MappingDraft) -> None:
        self._drafts[value.key] = _draft_copy(value)

    def append_revision(self, value: MappingDraftRevision) -> None:
        self._revisions.append(_revision_copy(value))

    def revisions(self) -> tuple[MappingDraftRevision, ...]:
        return tuple(_revision_copy(value) for value in self._revisions)


class MappingDraftService:
    """Owns personal identity, CAS and immutable saved draft snapshots only."""

    def __init__(self, storage: InMemoryMappingDraftStorage, *, canonicalize: Canonicalizer | None = None) -> None:
        self._storage = storage
        self._canonicalize = canonicalize or _object

    def open_draft(self, definition_id: str, actor: str) -> MappingDraft:
        active = self._storage.active(definition_id)
        actor = _actor(actor)
        key = draft_key(active.site_scope, active.definition_id, actor)
        prior = self._storage.draft(key)
        if prior and prior.state == "OPEN":
            return prior
        value = MappingDraft(
            key, active.site_scope, active.definition_id, actor, 1 if prior is None else prior.generation + 1,
            "OPEN", _copy(active.content), active.content_hash, 0, active.name, active.revision,
            active.content_hash, active.schema_fingerprint,
        )
        self._storage.save_draft(value)
        return value

    def save(self, definition_id: str, actor: str, expected_draft_revision: int, expected_active_revision: int, content: object) -> DraftSaveResult:
        active, draft = self._current(definition_id, actor)
        self._assert_current(active, draft, expected_draft_revision, expected_active_revision)
        canonical = _object(self._canonicalize(_object(content)))
        content_hash = _hash(canonical)
        if content_hash == draft.content_hash:
            return DraftSaveResult(draft, True)
        updated = MappingDraft(
            draft.key, draft.site_scope, draft.definition_id, draft.owner, draft.generation, "OPEN",
            _copy(canonical), content_hash, draft.content_revision + 1, draft.base_active_name,
            draft.base_active_revision, draft.base_active_hash, draft.base_schema_fingerprint,
        )
        self._storage.save_draft(updated)
        self._storage.append_revision(MappingDraftRevision(
            updated.key, updated.generation, updated.content_revision, "DRAFT_SAVE", updated.owner,
            _copy(updated.content), updated.content_hash, updated.base_active_name,
            updated.base_active_revision, updated.base_active_hash, updated.base_schema_fingerprint,
        ))
        return DraftSaveResult(updated, False)

    def discard(self, definition_id: str, actor: str, expected_generation: int, expected_draft_revision: int) -> MappingDraft:
        active, draft = self._current(definition_id, actor)
        if draft.generation != expected_generation or draft.content_revision != expected_draft_revision:
            raise DraftConflict("Personal mapping draft changed; reload before discarding.")
        if active.revision != draft.base_active_revision or active.content_hash != draft.base_active_hash:
            raise DraftConflict("Active mapping changed; reload before discarding the draft.")
        discarded = MappingDraft(
            draft.key, draft.site_scope, draft.definition_id, draft.owner, draft.generation, "DISCARDED",
            _copy(draft.content), draft.content_hash, draft.content_revision, draft.base_active_name,
            draft.base_active_revision, draft.base_active_hash, draft.base_schema_fingerprint,
        )
        self._storage.save_draft(discarded)
        return discarded

    def _current(self, definition_id: str, actor: str) -> tuple[ActiveMapping, MappingDraft]:
        active = self._storage.active(definition_id)
        draft = self._storage.draft(draft_key(active.site_scope, active.definition_id, _actor(actor)))
        if draft is None or draft.state != "OPEN":
            raise DraftConflict("No open personal mapping draft exists for this administrator.")
        return active, draft

    @staticmethod
    def _assert_current(active: ActiveMapping, draft: MappingDraft, expected_draft_revision: int, expected_active_revision: int) -> None:
        if draft.content_revision != expected_draft_revision:
            raise DraftConflict("Personal mapping draft revision is stale.")
        if active.revision != expected_active_revision or active.revision != draft.base_active_revision:
            raise DraftConflict("Active mapping revision is stale.")
        if active.content_hash != draft.base_active_hash:
            raise DraftConflict("Active mapping content changed; reload before saving the draft.")


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
        return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    except (TypeError, ValueError) as error:
        raise MappingDraftError("Draft mapping content is not JSON-safe.") from error


def _hash(value: dict[str, object]) -> str:
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _copy(value: dict[str, object]) -> dict[str, object]:
    return _object(value)


def _active_copy(value: ActiveMapping) -> ActiveMapping:
    return ActiveMapping(value.site_scope, value.definition_id, value.name, value.revision, value.schema_fingerprint, _copy(value.content))


def _draft_copy(value: MappingDraft) -> MappingDraft:
    return MappingDraft(value.key, value.site_scope, value.definition_id, value.owner, value.generation, value.state, _copy(value.content), value.content_hash, value.content_revision, value.base_active_name, value.base_active_revision, value.base_active_hash, value.base_schema_fingerprint)


def _revision_copy(value: MappingDraftRevision) -> MappingDraftRevision:
    return MappingDraftRevision(value.draft_key, value.generation, value.revision, value.kind, value.actor, _copy(value.content), value.content_hash, value.base_active_name, value.base_active_revision, value.base_active_hash, value.base_schema_fingerprint)

