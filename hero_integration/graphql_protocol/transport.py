from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Response:
    success: bool
    payload: str
    transport_succeeded: bool | None = None


class Transport(Protocol):
    @property
    def name(self) -> str: ...

    def send(
        self,
        payload: str,
        *,
        variables: dict[str, object] | None = None,
        operation_name: str | None = None,
    ) -> Response: ...
