"""Serial / Bluetooth RFCOMM transport (e.g. /dev/rfcomm0, /dev/ttyUSB0)."""

from __future__ import annotations

import asyncio

from mbprint.log import get_logger, hexdump, trace, tracing
from mbprint.transport import Transport

log = get_logger(__name__)


class SerialTransport(Transport):
    name = "serial"

    def __init__(self, port: str, baudrate: int = 115200, max_write: int = 512):
        self.port = port
        self.baudrate = baudrate
        self.max_write = max_write
        self._ser = None

    async def open(self) -> None:
        try:
            import serial
        except ImportError:
            raise SystemExit("serial transport needs pyserial: pip install pyserial")
        try:
            self._ser = serial.Serial(self.port, self.baudrate, timeout=1, write_timeout=10)
            log.info("opened %s at %d baud, %d-byte writes",
                     self.port, self.baudrate, self.max_write)
        except Exception as exc:
            raise SystemExit(f"cannot open {self.port}: {exc}")

    async def close(self) -> None:
        if self._ser:
            self._ser.flush()
            self._ser.close()
            self._ser = None

    async def send(self, data: bytes) -> None:
        if not self._ser:
            raise SystemExit("serial port not open")
        if tracing(log):
            trace(log, "-> write %d bytes: %s", len(data), hexdump(data))
        await asyncio.to_thread(self._ser.write, bytes(data))

    async def wait_for_response(self, timeout_ms: int = 500) -> bytes | None:
        if not self._ser:
            return None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_ms / 1000.0
        while loop.time() < deadline:
            waiting = self._ser.in_waiting
            if waiting:
                return await asyncio.to_thread(self._ser.read, waiting)
            await asyncio.sleep(0.02)
        return None
