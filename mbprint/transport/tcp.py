"""Raw TCP transport, for network printers that listen on port 9100.

This is how the Brother QL-1110NWB is normally driven over Ethernet or WiFi:
the job is the raw command stream, with no protocol on top.
"""

from __future__ import annotations

import asyncio

from mbprint.log import get_logger, hexdump, trace, tracing
from mbprint.transport import Transport

log = get_logger(__name__)

DEFAULT_PORT = 9100


class TCPTransport(Transport):
    name = "tcp"

    def __init__(self, host: str, port: int = DEFAULT_PORT, max_write: int = 4096,
                 timeout: float = 10.0):
        self.host = host
        self.port = port
        self.max_write = max_write
        self.timeout = timeout
        self._reader = None
        self._writer = None

    async def open(self) -> None:
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), self.timeout)
        except (OSError, asyncio.TimeoutError) as exc:
            raise SystemExit(f"cannot reach {self.host}:{self.port}: {exc}")
        log.info("connected to %s:%d, %d-byte writes", self.host, self.port, self.max_write)

    async def close(self) -> None:
        if self._writer is not None:
            try:
                await self._writer.drain()
                self._writer.close()
                await self._writer.wait_closed()
            except OSError:
                pass
            self._writer = None
            self._reader = None

    async def send(self, data: bytes) -> None:
        if self._writer is None:
            raise SystemExit("not connected")
        if tracing(log):
            trace(log, "-> write %d bytes: %s", len(data), hexdump(data))
        self._writer.write(bytes(data))
        await self._writer.drain()

    async def wait_for_response(self, timeout_ms: int = 500) -> bytes | None:
        if self._reader is None:
            return None
        try:
            return await asyncio.wait_for(self._reader.read(32), timeout_ms / 1000.0)
        except (asyncio.TimeoutError, OSError):
            return None

    async def delay(self, ms: int) -> None:
        # TCP is flow-controlled; the artificial pacing the BLE links need only
        # slows a network print down.
        return
