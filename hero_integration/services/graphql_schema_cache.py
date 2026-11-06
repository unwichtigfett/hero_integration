"""Bounded process-local cache of pure, content-addressed schema compilation.

No source authority, mapping approval, request variables or remote data is cached.
Callers must treat the shared graphql-core schema as read-only.
"""
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
import hashlib
import json

from hero_integration.services.graphql_graph import normalize_schema


SCHEMA_CACHE_SIZE = 4
MAX_CACHEABLE_SCHEMA_CHARS = 2 * 1024 * 1024


def schema_text(value):
    """Detach mutable inputs and use their exact contents, never object identity."""
    if isinstance(value, str):
        return value
    if not isinstance(value, Mapping):
        raise TypeError('A GraphQL schema snapshot must be JSON text or a mapping.')
    return json.dumps(dict(value), ensure_ascii=False, allow_nan=False, separators=(',', ':'))


@dataclass(frozen=True)
class CompiledSchema:
    core: object
    fingerprint: str


@lru_cache(maxsize=SCHEMA_CACHE_SIZE)
def _compiled(text):
    from graphql import build_client_schema
    # Decode the cache key, not a caller-owned dictionary. No mutable caller
    # objects survive into the shared schema or its fingerprint.
    normal = normalize_schema(json.loads(text))
    canonical = json.dumps(normal, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()
    try:
        core = build_client_schema({'__schema':normal})
    except KeyError as error:
        raise ValueError('The GraphQL schema snapshot is incomplete.') from error
    return CompiledSchema(core, hashlib.sha256(canonical).hexdigest())


def compiled_schema(value):
    text = schema_text(value)
    # Large snapshots still work, but cannot grow the retained cache without
    # bound. The character bound also bounds Unicode key storage per entry.
    build = _compiled if len(text) <= MAX_CACHEABLE_SCHEMA_CHARS else _compiled.__wrapped__
    return build(text)


def clear_schema_cache():
    """Clear pure compilation state, e.g. for deterministic cold-start tests."""
    _compiled.cache_clear()
