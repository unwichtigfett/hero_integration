"""Offset page windows for approved readers. A window is positional evidence, never a HERO watermark."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json


MAX_OVERLAP = 20
MIGRATION_NO_OVERLAP_LIMIT = 20


def validate_overlap(value, page_size: int, *, migration=False) -> int:
    """Validate the configured overlap before transport, including small migration readers."""
    if type(page_size) is not int or not 1 <= page_size <= 1000:
        raise ValueError("Invalid effective page size.")
    if value is None or value == "":
        value = 0
    if isinstance(value, str) and value.isascii() and value.isdigit():
        value = int(value)
    small_migration_page = migration and page_size <= MIGRATION_NO_OVERLAP_LIMIT
    if type(value) is not int or not 0 <= value <= MAX_OVERLAP or (value >= page_size and not small_migration_page):
        raise ValueError("Page Overlap must be an integer from 0 to 20 and smaller than the effective page size.")
    return value


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def inventory_evidence(row):
    # Download locations are transport credentials and may rotate between reads.
    if "_hero_file_detail" in row:
        return {key: value for key, value in row.items() if key != "downloadUrl"}
    if "_hero_composition" in row:
        from hero_integration.services.service_migration import _stable_detail
        return {**row, "_hero_composition": _stable_detail(row["_hero_composition"])}
    return row


class InventoryWindowMoved(ValueError):
    """A previously acknowledged overlap no longer occupies the requested position."""


@dataclass(frozen=True)
class OffsetPage:
    rows: tuple
    next_offset: int | None
    checkpoint: dict
    changed: frozenset


def advance(records, *, offset, size, overlap, previous=None, identity=lambda row: row.get("id"), migration=False, evidence=lambda row: row) -> OffsetPage:
    """Suppress only identical rows from the last overlapping window; admit changes for normal conflict handling."""
    overlap = validate_overlap(overlap, size, migration=migration)
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
        stamp = fingerprint(evidence(record))
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
    if migration and len(records) <= MIGRATION_NO_OVERLAP_LIMIT:
        repeated = 0
    following = offset + len(records) - repeated if records else None
    checkpoint = {"next_offset": following, "size": size, "overlap": overlap, "count": len(records), "ids": identity_digest,
                  "tail": dict(list(stamps.items())[-repeated:]) if repeated else {}}
    if migration:
        # Keep the configured overlap for resume checks, including pre-policy checkpoints.
        checkpoint["applied_overlap"] = repeated
    return OffsetPage(tuple(accepted), following, checkpoint, frozenset(changed))
