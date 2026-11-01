"""HERO business numbers as ERPNext document names.

People know a customer by HERO ``Customer.nr`` and a project by its trade plus
``ProjectMatch.display_id`` (``PV-3333``).  New ERPNext records take these as
their document name.  The technical HERO ``id`` stays the identity key of the
HERO Integration Link; this module only names documents.

No Frappe dependency: the importer decides whether a name is free.
"""
from __future__ import annotations

import re
from collections.abc import Mapping

from hero_integration.services.project_naming import project_trade

# Letters and digits, optionally joined by single hyphens; no spaces, slashes or
# other characters that make a poor or unsafe ERPNext document name.
_NAME_PATTERN = re.compile(r"[0-9A-Za-z]+(?:-[0-9A-Za-z]+)*")
NAME_MAX_LENGTH = 140


class BusinessNumberError(ValueError):
    """A HERO number exists but cannot be used as an ERPNext document name."""


def business_number(value: object) -> str | None:
    """The HERO number as text, leading zeros kept; ``None`` when HERO sent none."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str):
        raise BusinessNumberError(f"HERO number {value!r} is not text.")
    number = value
    if len(number) > NAME_MAX_LENGTH or not _NAME_PATTERN.fullmatch(number):
        raise BusinessNumberError(
            f"HERO number {number[:40]!r} cannot be an ERPNext document name "
            "(only letters, digits and single inner hyphens, at most 140 characters)."
        )
    return number


def customer_document_name(record: Mapping[str, object]) -> str | None:
    """The ERPNext Customer name for a new HERO customer: ``Customer.nr`` unchanged."""
    return business_number(record.get("nr"))


# HERO projects end in "-H" so that projects created in ERPNext can never take
# a HERO ID (owner decision 2026-11-01).
HERO_PROJECT_SUFFIX = "-H"


def project_reference(record: Mapping[str, object]) -> str | None:
    """``<trade>-<display_id>-H``, e.g. ``PV-3333-H``; ``None`` without a display_id.

    The trade comes from the confirmed trade catalog; a missing or unknown trade
    raises instead of guessing a prefix.
    """
    number = business_number(record.get("display_id"))
    if number is None:
        return None
    trade_code, _trade_name = project_trade(record)
    return business_number(f"{trade_code}-{number}{HERO_PROJECT_SUFFIX}")
