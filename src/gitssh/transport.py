"""Shared transport backend interfaces for gitssh."""

from __future__ import annotations

from typing import Protocol

from .protocol import Message


class TransportError(RuntimeError):
    """Raised when a transport backend operation fails."""


class TransportBackend(Protocol):
    """Minimal interface consumed by gitssh client/server."""

    def name(self) -> str:
        ...

    def snapshot_inbound_cursor(self) -> str | None:
        ...

    def read_inbound_messages(self, cursor: str | None) -> tuple[list[Message], str | None]:
        ...

    def fetch_inbound(self) -> None:
        ...

    def write_outbound_message(self, message: Message) -> str:
        ...

    def push_outbound(self) -> None:
        ...

    def close(self) -> None:
        ...
