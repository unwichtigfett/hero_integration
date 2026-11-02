"""Derive B8 change-provider capabilities from explicit discovery evidence.

Offset scans and bounded comparisons cannot establish complete delta discovery
merely by returning records.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from hero_integration.services.sync_engine import ChangeCapabilities, ChangePage, SyncError


class ProviderCapability(StrEnum):
    """Discovery guarantee proven by the selected provider contract."""

    COMPLETE_DELTA_CURSOR = "COMPLETE_DELTA_CURSOR"
    VERIFIED_TUPLE_WATERMARK = "VERIFIED_TUPLE_WATERMARK"
    BOUND_OBJECT_RECONCILIATION = "BOUND_OBJECT_RECONCILIATION"
    UNSUPPORTED = "UNSUPPORTED"


class ProviderCapabilityError(SyncError):
    """The selected provider cannot safely serve the requested B8 operation."""


@dataclass(frozen=True, slots=True)
class ProviderContract:
    """Convert discovery evidence to closed-world B8 capabilities.

    BOUND_OBJECT_RECONCILIATION grants only bounded comparison, not a delta cursor;
    UNSUPPORTED grants no capability.
    """

    capability: ProviderCapability

    def __post_init__(self) -> None:
        if not isinstance(self.capability, ProviderCapability):
            raise ProviderCapabilityError("Provider capability is invalid.")

    def change_capabilities(self) -> ChangeCapabilities:
        if self.capability in {
            ProviderCapability.COMPLETE_DELTA_CURSOR,
            ProviderCapability.VERIFIED_TUPLE_WATERMARK,
        }:
            return ChangeCapabilities(cursor=True, complete_change_discovery=True, bounded_comparison=False)
        if self.capability is ProviderCapability.BOUND_OBJECT_RECONCILIATION:
            return ChangeCapabilities(cursor=False, complete_change_discovery=False, bounded_comparison=True)
        return ChangeCapabilities(cursor=False, complete_change_discovery=False, bounded_comparison=False)

    def permits_change_read(self) -> bool:
        return self.capability in {
            ProviderCapability.COMPLETE_DELTA_CURSOR,
            ProviderCapability.VERIFIED_TUPLE_WATERMARK,
        }


class GuardedChangeProvider:
    """Expose only proven provider reads to SyncEngine.

    Runtime eligibility checks precede construction. Direct calls still cannot
    promote offset scans or bounded comparisons to automatic synchronization.
    """

    def __init__(
        self,
        contract: ProviderContract,
        read_page: Callable[[str | None], ChangePage],
        *,
        tuple_watermark_verified: Callable[[ChangePage], bool] | None = None,
    ) -> None:
        if not isinstance(contract, ProviderContract):
            raise ProviderCapabilityError("Provider contract is required.")
        if not callable(read_page):
            raise ProviderCapabilityError("Provider reader is required.")
        if contract.capability is ProviderCapability.VERIFIED_TUPLE_WATERMARK and not callable(tuple_watermark_verified):
            raise ProviderCapabilityError("Verified tuple watermark requires a verifier.")
        self.contract = contract
        self._read_page = read_page
        self._tuple_watermark_verified = tuple_watermark_verified

    @property
    def capabilities(self) -> ChangeCapabilities:
        return self.contract.change_capabilities()

    def changes(self, cursor: str | None) -> ChangePage:
        if not self.contract.permits_change_read():
            raise ProviderCapabilityError("Provider cannot prove complete cursor-based change discovery.")
        page = self._read_page(cursor)
        if not isinstance(page, ChangePage):
            raise ProviderCapabilityError("Provider reader returned an invalid change page.")
        if self.contract.capability is ProviderCapability.VERIFIED_TUPLE_WATERMARK:
            if page.next_cursor is None and (not page.complete or not page.watermark or not self._tuple_watermark_verified(page)):
                raise ProviderCapabilityError("Tuple watermark is absent, incomplete, or unverified.")
        return page


def guarded_provider(
    contract: ProviderContract,
    read_page: Callable[[str | None], ChangePage],
    *,
    tuple_watermark_verified: Callable[[ChangePage], bool] | None = None,
) -> GuardedChangeProvider:
    """Build the sole generic B8 provider adapter from explicit evidence."""

    return GuardedChangeProvider(contract, read_page, tuple_watermark_verified=tuple_watermark_verified)


def offset_provider(offset_reader: Callable[[int], object]) -> GuardedChangeProvider:
    """Represent an offset-only source as unsupported without ever invoking it."""

    if not callable(offset_reader):
        raise ProviderCapabilityError("Offset provider reader is required.")
    return GuardedChangeProvider(
        ProviderContract(ProviderCapability.UNSUPPORTED),
        lambda _cursor: (_ for _ in ()).throw(ProviderCapabilityError("Offset reader must not run.")),
    )
