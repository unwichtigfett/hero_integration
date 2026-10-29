"""Readable display names for saved GraphQL schema snapshots.

Snapshots keep their internal name (UID) everywhere; only the UI shows
``S-ddmmyy-NN``: the creation day and the 1-based order of the snapshot on
that day, e.g. the third snapshot of 31.10.2026 is ``S-311026-03``.  The
number is derived from the creation order and is not stored.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, datetime


def _moment(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value.strip().replace(" ", "T", 1))
        except ValueError:
            return None
    return None


def snapshot_moment(row: Mapping[str, object]) -> datetime | None:
    """Creation time of a snapshot row; capture or modification time only as a fallback."""
    for key in ("creation", "captured_at", "modified"):
        moment = _moment(row.get(key))
        if moment is not None:
            return moment.replace(tzinfo=None)
    return None


def display_names(rows: Iterable[Mapping[str, object]]) -> dict[str, str]:
    """Map every snapshot name to ``S-ddmmyy-NN``; rows without a usable time keep their name."""
    stamped, result = [], {}
    for row in rows:
        name = str(row.get("name") or "")
        if not name:
            continue
        moment = snapshot_moment(row)
        if moment is None:
            result[name] = name
        else:
            stamped.append((moment, name))
    counts: dict[date, int] = {}
    for moment, name in sorted(stamped):
        day = moment.date()
        counts[day] = counts.get(day, 0) + 1
        result[name] = f"S-{moment:%d%m%y}-{counts[day]:02d}"
    return result


def newest(rows: Iterable[Mapping[str, object]]) -> Mapping[str, object] | None:
    """The most recently created row, ties broken by name for a stable choice."""
    stamped = [(snapshot_moment(row), str(row.get("name") or ""), row) for row in rows]
    stamped = [item for item in stamped if item[0] is not None and item[1]]
    return max(stamped, key=lambda item: (item[0], item[1]))[2] if stamped else None
