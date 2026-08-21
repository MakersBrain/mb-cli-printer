"""Classic Bluetooth (RFCOMM / SPP) transport.

The Brother QL series, and several Phomemo models, expose a serial port profile
rather than BLE GATT. Linux speaks RFCOMM natively, so this connects straight to
the device's channel without binding an /dev/rfcomm node first.

Unlike BLE the link is a stream: writes are not capped by an ATT MTU, and the
printer's status blocks come back on the same channel.

Some CPython builds ship without `AF_BLUETOOTH` — notably the standalone builds
uv installs — so the socket is opened through libc when the socket module cannot
do it itself. Same syscalls either way.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import ctypes.util
import os
import socket

from mbprint.log import get_logger, hexdump, trace, tracing
from mbprint.transport import Transport

log = get_logger(__name__)

# Serial Port Profile almost always lands on channel 1; Brother's app used it.
DEFAULT_CHANNEL = 1

AF_BLUETOOTH = 31
BTPROTO_RFCOMM = 3
NATIVE = hasattr(socket, "AF_BLUETOOTH") and hasattr(socket, "BTPROTO_RFCOMM")


def _bdaddr(address: str) -> bytes:
    """MAC text to the little-endian six bytes sockaddr_rc wants."""
    parts = address.split(":")
    if len(parts) != 6:
        raise SystemExit(f"not a Bluetooth address: {address!r}")
    try:
        return bytes(int(p, 16) for p in reversed(parts))
    except ValueError:
        raise SystemExit(f"not a Bluetooth address: {address!r}") from None


class _LibcRfcomm:
    """RFCOMM over libc, for interpreters built without AF_BLUETOOTH."""

    def __init__(self) -> None:
        self._libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        self.fd = -1

    def connect(self, address: str, channel: int, timeout: float) -> None:
        self.fd = self._libc.socket(AF_BLUETOOTH, socket.SOCK_STREAM, BTPROTO_RFCOMM)
        if self.fd < 0:
            raise OSError(ctypes.get_errno(), "socket(AF_BLUETOOTH) failed")
        # struct sockaddr_rc { sa_family_t; bdaddr_t[6]; uint8_t channel; }
        addr = ctypes.create_string_buffer(
            AF_BLUETOOTH.to_bytes(2, "little") + _bdaddr(address) + bytes([channel]),
            10,
        )
        if self._libc.connect(self.fd, addr, 10) < 0:
            err = ctypes.get_errno()
            os.close(self.fd)
            self.fd = -1
            raise OSError(err, os.strerror(err))

    def sendall(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            sent = os.write(self.fd, view)
            view = view[sent:]

    def recv(self, size: int) -> bytes:
        return os.read(self.fd, size)

    def settimeout(self, seconds: float) -> None:
        # SO_RCVTIMEO: struct timeval { time_t sec; suseconds_t usec; }
        whole = int(seconds)
        timeval = whole.to_bytes(8, "little") + int((seconds - whole) * 1e6).to_bytes(8, "little")
        self._libc.setsockopt(self.fd, socket.SOL_SOCKET, socket.SO_RCVTIMEO, timeval, len(timeval))

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1


class BluetoothTransport(Transport):
    name = "bluetooth"

    def __init__(
        self,
        address: str,
        channel: int = DEFAULT_CHANNEL,
        max_write: int = 1024,
        timeout: float = 15.0,
    ):
        self.address = address
        self.channel = channel
        self.max_write = max_write
        self.timeout = timeout
        self._sock: socket.socket | _LibcRfcomm | None = None

    def _connect(self) -> socket.socket | _LibcRfcomm:
        """Open the channel, whichever way this interpreter can."""
        if NATIVE:
            native = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
            native.settimeout(self.timeout)
            try:
                native.connect((self.address, self.channel))
            except OSError:
                native.close()
                raise
            return native
        log.debug("this interpreter has no AF_BLUETOOTH; going through libc")
        fallback = _LibcRfcomm()
        fallback.connect(self.address, self.channel, self.timeout)
        return fallback

    async def open(self) -> None:
        try:
            sock = await asyncio.to_thread(self._connect)
        except OSError as exc:
            raise SystemExit(
                f"cannot open RFCOMM channel {self.channel} on {self.address}: {exc}\n"
                f"  pair it first: bluetoothctl pair {self.address} "
                f"&& bluetoothctl trust {self.address}"
            ) from exc
        self._sock = sock
        log.info(
            "connected to %s on RFCOMM channel %d, %d-byte writes",
            self.address,
            self.channel,
            self.max_write,
        )

    async def close(self) -> None:
        if self._sock is not None:
            with contextlib.suppress(OSError):
                self._sock.close()
            self._sock = None

    async def send(self, data: bytes) -> None:
        if self._sock is None:
            raise SystemExit("not connected")
        if tracing(log):
            trace(log, "-> write %d bytes: %s", len(data), hexdump(data))
        await asyncio.to_thread(self._sock.sendall, bytes(data))

    async def wait_for_response(self, timeout_ms: int = 500) -> bytes | None:
        if self._sock is None:
            return None
        self._sock.settimeout(max(0.05, timeout_ms / 1000.0))
        try:
            reply = await asyncio.to_thread(self._sock.recv, 64)
        except (TimeoutError, OSError):
            return None
        finally:
            with contextlib.suppress(OSError):
                self._sock.settimeout(self.timeout)
        if reply and tracing(log):
            trace(log, "<- %d bytes: %s", len(reply), hexdump(reply))
        return reply or None
