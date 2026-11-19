"""Offset page windows for approved readers. A window is positional evidence, never a HERO watermark."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json


def validate_overlap(value, page_size: int) -> int:
    if type(page_size) is not int or not 1 <= page_size <= 1000:
        raise ValueError("Invalid effective page size.")
    if value is None or value == "":
        value = 0
    if isinstance(value, str) and value.isascii() and value.isdigit():
        value = int(value)
    if type(value) is not int or not 0 <= value <= 20 or value >= page_size:
        raise ValueError("Page Overlap must be an integer from 0 to 20 and smaller than the effective page size.")
    return value


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True)
class OffsetPage:
    rows: tuple
    next_offset: int | None
    checkpoint: dict
    changed: frozenset


def advance(records, *, offset, size, overlap, previous=None, identity=lambda row: row.get("id")) -> OffsetPage:
    """Suppress only identical rows from the last overlapping window; admit changes for normal conflict handling."""
    overlap = validate_overlap(overlap, size)
    if type(offset) is not int or offset < 0 or len(records) > size:
        raise ValueError("Offset or response length is outside the approved page contract.")
    previous = previous or {}
    if previous and (previous.get("next_offset") != offset or previous.get("size") != size
                     or previous.get("overlap") != overlap):
        raise ValueError("Offset checkpoint does not match the requested page.")
    tail = previous.get("tail", {})
    stamps, accepted, changed = {}, [], set()
    for record in records:
        key = identity(record)
        if type(key) not in (int, str) or not str(key).strip() or str(key) in stamps:
            raise ValueError("Missing or repeated source identity in an offset page.")
        key = str(key)
        stamp = fingerprint(record)
        stamps[key] = stamp
        if key in tail:
            if tail[key] == stamp:
                continue
            changed.add(key)
        accepted.append(record)
    # An ignored offset must never loop merely because each request used a larger offset.
    identity_digest = fingerprint(list(stamps))
    if records and identity_digest == previous.get("ids"):
        raise ValueError("HERO repeated an offset page.")
    repeated = overlap if len(records) == size else 0
    following = offset + len(records) - repeated if records else None
    checkpoint = {"next_offset": following, "size": size, "overlap": overlap, "count": len(records), "ids": identity_digest,
                  "tail": dict(list(stamps.items())[-repeated:]) if repeated else {}}
    return OffsetPage(tuple(accepted), following, checkpoint, frozenset(changed))
