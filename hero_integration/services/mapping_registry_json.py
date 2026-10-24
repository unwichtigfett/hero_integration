"""Strict deterministic JSON interchange for the Mapping Registry v3.

The sole definition payload is ``MappingDefinition.to_payload()``.  This layer
does not discover schemas, validate mapping compatibility, persist revisions, or
grant execution authority.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
import json

from hero_integration.services.mapping_registry_model import (
    FORMAT_ID,
    MappingDefinition,
    MappingRegistryError,
)


FORMAT = FORMAT_ID
FORMAT_VERSION = 3
MAX_JSON_BYTES = 1_048_576
MAX_JSON_DEPTH = 32
MAX_DEFINITIONS = 2_048


class MappingRegistryJsonError(ValueError):
    """JSON input is not a complete, safe Mapping Registry v3 envelope."""


@dataclass(frozen=True)
class MappingRegistryJsonV3:
    """A complete verified JSON registry version, represented by canonical models."""

    site_scope: str
    registry_revision: int
    provenance: Mapping[str, str]
    definitions: tuple[MappingDefinition, ...]
    integrity_sha256: str

    def payload(self) -> dict[str, object]:
        return _with_integrity(_payload_without_integrity(
            self.site_scope, self.registry_revision, self.provenance, self.definitions
        ))


def serialize_mapping_registry_v3(
    *,
    site_scope: str,
    registry_revision: int,
    provenance: Mapping[str, str],
    definitions: Sequence[MappingDefinition | Mapping[str, object]],
) -> str:
    """Serialize the complete registry version using a canonical v3 envelope.

    Frozen top-level keys are: ``format``, ``format_version``, ``site_scope``,
    ``registry_revision``, ``provenance``, ``definitions``, and ``integrity``.
    The input is a complete replace candidate, never a merge instruction.
    """
    return _canonical_json(_with_integrity(_payload_without_integrity(
        site_scope, registry_revision, provenance, definitions
    )))


def parse_mapping_registry_v3(raw: str | bytes | bytearray) -> MappingRegistryJsonV3:
    """Fail closed on duplicate keys, unsupported fields, secrets or instructions."""
    encoded = _encoded(raw)
    try:
        parsed = json.loads(encoded.decode("utf-8"), object_pairs_hook=_no_duplicates, parse_constant=_reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, MappingRegistryJsonError) as error:
        raise MappingRegistryJsonError("Mapping registry JSON is malformed.") from error
    _check_tree(parsed)
    envelope = _validate_envelope(parsed)
    without_integrity = {key: value for key, value in envelope.items() if key != "integrity"}
    digest = _digest(without_integrity)
    integrity = envelope["integrity"]
    if integrity["algorithm"] != "sha256" or integrity["canonical_sha256"] != digest:
        raise MappingRegistryJsonError("Mapping registry JSON integrity metadata is invalid.")
    definitions = _definitions_from_payload(envelope["definitions"])
    return MappingRegistryJsonV3(
        site_scope=envelope["site_scope"], registry_revision=envelope["registry_revision"],
        provenance=dict(envelope["provenance"]), definitions=definitions, integrity_sha256=digest,
    )


def canonical_mapping_registry_json(value: MappingRegistryJsonV3 | Mapping[str, object]) -> str:
    """Return canonical JSON after full v3 validation; input is never repaired."""
    if isinstance(value, MappingRegistryJsonV3):
        return serialize_mapping_registry_v3(
            site_scope=value.site_scope, registry_revision=value.registry_revision,
            provenance=value.provenance, definitions=value.definitions,
        )
    if not isinstance(value, Mapping):
        raise MappingRegistryJsonError("Canonical serialization requires a v3 payload.")
    # A raw mapping must already be a complete v3 document.  Parsing before
    # projection prevents unknown or sensitive keys from being discarded.
    return canonical_mapping_registry_json(parse_mapping_registry_v3(_canonical_json(dict(value))))


def _payload_without_integrity(
    site_scope: object, registry_revision: object, provenance: object, definitions: object,
) -> dict[str, object]:
    typed = _definitions_from_input(definitions)
    payload = {
        "format": FORMAT, "format_version": FORMAT_VERSION, "site_scope": site_scope,
        "registry_revision": registry_revision, "provenance": provenance,
        "definitions": [item.to_payload() for item in sorted(typed, key=lambda item: item.definition_id)],
    }
    validated = _validate_envelope({**payload, "integrity": _placeholder_integrity()})
    _check_tree(validated)
    return {key: value for key, value in validated.items() if key != "integrity"}


def _definitions_from_input(value: object) -> tuple[MappingDefinition, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise MappingRegistryJsonError("definitions must be a sequence of mapping definitions.")
    result: list[MappingDefinition] = []
    for item in value:
        try:
            result.append(item if isinstance(item, MappingDefinition) else MappingDefinition.from_payload(item))
        except MappingRegistryError as error:
            raise MappingRegistryJsonError(str(error)) from error
    _unique_definitions(result)
    return tuple(result)


def _definitions_from_payload(value: object) -> tuple[MappingDefinition, ...]:
    if not isinstance(value, list):
        raise MappingRegistryJsonError("definitions must be an array.")
    return _definitions_from_input(value)


def _unique_definitions(definitions: Sequence[MappingDefinition]) -> None:
    if len(definitions) > MAX_DEFINITIONS:
        raise MappingRegistryJsonError("Too many mapping definitions.")
    if len({item.definition_id for item in definitions}) != len(definitions):
        raise MappingRegistryJsonError("Duplicate mapping definition_id in JSON import.")


def _validate_envelope(value: object) -> dict[str, object]:
    root = _object(value, "registry envelope")
    _exact_keys(root, {"format", "format_version", "site_scope", "registry_revision", "provenance", "definitions", "integrity"}, "registry envelope")
    if root["format"] != FORMAT or root["format_version"] != FORMAT_VERSION:
        raise MappingRegistryJsonError("Unsupported mapping registry interchange format or version.")
    _nonempty_string(root["site_scope"], "site_scope")
    if not isinstance(root["registry_revision"], int) or isinstance(root["registry_revision"], bool) or root["registry_revision"] < 0:
        raise MappingRegistryJsonError("registry_revision must be a non-negative integer.")
    provenance = _object(root["provenance"], "provenance")
    _exact_keys(provenance, {"source_instance", "snapshot_name", "schema_fingerprint"}, "provenance")
    for key in provenance:
        _nonempty_string(provenance[key], f"provenance.{key}")
    if not isinstance(root["definitions"], list):
        raise MappingRegistryJsonError("definitions must be an array.")
    _unique_definitions(_definitions_from_payload(root["definitions"]))
    integrity = _object(root["integrity"], "integrity")
    _exact_keys(integrity, {"algorithm", "canonical_sha256"}, "integrity")
    if integrity["algorithm"] != "sha256" or not isinstance(integrity["canonical_sha256"], str) or len(integrity["canonical_sha256"]) != 64:
        raise MappingRegistryJsonError("Unsupported integrity metadata.")
    try:
        int(integrity["canonical_sha256"], 16)
    except ValueError as error:
        raise MappingRegistryJsonError("Integrity digest must be hexadecimal.") from error
    return root


def _encoded(raw: str | bytes | bytearray) -> bytes:
    if isinstance(raw, str):
        value = raw.encode("utf-8")
    elif isinstance(raw, (bytes, bytearray)):
        value = bytes(raw)
    else:
        raise MappingRegistryJsonError("Mapping registry JSON must be text or UTF-8 bytes.")
    if not value or len(value) > MAX_JSON_BYTES:
        raise MappingRegistryJsonError("Mapping registry JSON exceeds the permitted input size.")
    return value


_FORBIDDEN_KEYS = frozenset({
    "api_key", "api_keys", "api_secret", "authorization", "credential", "credentials",
    "password", "secret", "token", "tokens", "binding", "bindings", "binding_instance",
    "binding_instances", "object_binding", "object_bindings", "field_binding", "field_bindings",
    "execution_approval", "execution_approvals", "mutation_approval", "mutation_approvals",
    "execute", "executable", "query_document", "mutation_document", "transport",
})


def _check_tree(value: object, depth: int = 0) -> None:
    if depth > MAX_JSON_DEPTH:
        raise MappingRegistryJsonError("Mapping registry JSON exceeds the permitted nesting depth.")
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str) or key.casefold() in _FORBIDDEN_KEYS:
                raise MappingRegistryJsonError("Mapping registry JSON contains prohibited private or executable data.")
            _check_tree(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _check_tree(item, depth + 1)


def _with_integrity(payload: Mapping[str, object]) -> dict[str, object]:
    result = dict(payload)
    result["integrity"] = {"algorithm": "sha256", "canonical_sha256": _digest(result)}
    return result


def _placeholder_integrity() -> dict[str, str]:
    return {"algorithm": "sha256", "canonical_sha256": "0" * 64}


def _digest(payload: Mapping[str, object]) -> str:
    return sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise MappingRegistryJsonError("Mapping registry payload must contain JSON primitive values.") from error


def _no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise MappingRegistryJsonError("Duplicate JSON key.")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise MappingRegistryJsonError(f"Unsupported JSON constant: {value}")


def _object(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise MappingRegistryJsonError(f"{context} must be an object.")
    return dict(value)


def _exact_keys(value: Mapping[str, object], expected: set[str], context: str) -> None:
    if set(value) != expected:
        raise MappingRegistryJsonError(f"{context} has missing or unsupported keys.")


def _nonempty_string(value: object, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MappingRegistryJsonError(f"{context} must be a non-empty string.")
    return value
