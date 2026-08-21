"""Transports. Each one exposes the same async interface the protocol layer uses."""

from __future__ import annotations

import asyncio
from types import TracebackType
from typing import Any, TypeVar

TransportT = TypeVar("TransportT", bound="Transport")


class Transport:
    """Base transport.

    `max_write` is the largest payload the link accepts in one write. The
    protocol layer clamps its own chunk size to this so we never exceed the MTU.
    """

    name = "transport"
    max_write = 512

    async def open(self) -> None:  # pragma: no cover - trivial
        pass

    async def close(self) -> None:  # pragma: no cover - trivial
        pass

    async def send(self, data: bytes) -> None:
        raise NotImplementedError

    async def delay(self, ms: int) -> None:
        if ms > 0:
            await asyncio.sleep(ms / 1000.0)

    async def wait_for_response(self, timeout_ms: int = 500) -> bytes | None:
        """Default: no notification channel, just pace with a delay."""
        await self.delay(timeout_ms)
        return None

    async def __aenter__(self: TransportT) -> TransportT:
        await self.open()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()


def build(kind: str, **kwargs: Any) -> Transport:
    if kind == "ble":
        from mbprint.transport.ble import BLETransport

        return BLETransport(**kwargs)
    if kind == "serial":
        from mbprint.transport.serial_port import SerialTransport

        return SerialTransport(**kwargs)
    if kind == "usb":
        from mbprint.transport.usb import USBTransport

        return USBTransport(**kwargs)
    if kind == "tcp":
        from mbprint.transport.tcp import TCPTransport

        return TCPTransport(**kwargs)
    if kind == "file":
        from mbprint.transport.file import FileTransport

        return FileTransport(**kwargs)
    raise SystemExit(f"unknown transport {kind!r}; use ble, tcp, serial, usb or file")
