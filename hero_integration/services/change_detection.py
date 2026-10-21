"""Field-level, redacted comparison state for a linked HERO target."""

from __future__ import annotations

import hashlib
import json
from typing import Mapping


def field_hashes(values: Mapping[str, object]) -> dict[str, str]:
    """Hash normalised field values without retaining unrestricted payloads."""

    return {
        str(field): hashlib.sha256(
            json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":")).encode()
        ).hexdigest()
        for field, value in sorted(values.items())
    }


def encode(hashes: Mapping[str, str]) -> str:
    return json.dumps(dict(sorted(hashes.items())), sort_keys=True, separators=(",", ":"))


def decode(value: object) -> dict[str, str] | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return None
    if not isinstance(decoded, dict) or not all(isinstance(key, str) and isinstance(item, str) for key, item in decoded.items()):
        return None
    return decoded


def changed_fields(previous: Mapping[str, str], current: Mapping[str, str]) -> set[str]:
    return {
        field for field in set(previous) | set(current)
        if previous.get(field) != current.get(field)
    }
