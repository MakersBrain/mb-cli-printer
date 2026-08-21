"""Bluetooth LE (GATT) transport.

Phomemo printers expose a write characteristic (0xFF02) and, on most models, a
notify characteristic (0xFF03) used for status replies and P12 handshaking.
The write size is clamped to the negotiated ATT MTU minus the 3-byte header,
so every model gets the largest safe payload its link supports.
"""

from __future__ import annotations

import asyncio

from mbprint.log import get_logger, hexdump, trace, tracing
from mbprint.transport import Transport

log = get_logger(__name__)

SERVICE_UUIDS = [
    "0000ff00-0000-1000-8000-00805f9b34fb",  # standard Phomemo
    "0000ffe0-0000-1000-8000-00805f9b34fb",  # common thermal printer service
    "0000ae30-0000-1000-8000-00805f9b34fb",  # some label printers
    "49535343-fe7d-4ae5-8fa9-9fafd205e455",  # ISSC transparent UART
]
WRITE_UUIDS = [
    "0000ff02-0000-1000-8000-00805f9b34fb",
    "0000ffe1-0000-1000-8000-00805f9b34fb",
    "0000ae01-0000-1000-8000-00805f9b34fb",
    "49535343-8841-43f4-a8d4-ecbe34729bb3",
]
NOTIFY_UUIDS = [
    "0000ff03-0000-1000-8000-00805f9b34fb",
    "0000ffe1-0000-1000-8000-00805f9b34fb",
    "0000ae02-0000-1000-8000-00805f9b34fb",
    "49535343-1e4d-4bd9-ba61-23c647249616",
]

# Printer status queries, format 1F 11 <code>.
QUERY_COMMANDS = {
    "battery": bytes([0x1F, 0x11, 0x08]),
    "firmware": bytes([0x1F, 0x11, 0x07]),
    "serial": bytes([0x1F, 0x11, 0x09]),
    "paper": bytes([0x1F, 0x11, 0x11]),
    "cover": bytes([0x1F, 0x11, 0x12]),
    "version": bytes([0x1F, 0x11, 0x33]),
    "mac": bytes([0x1F, 0x11, 0x20]),
    "power": bytes([0x1F, 0x11, 0x0E]),
    "label": bytes([0x1F, 0x11, 0x19]),
}

ATT_HEADER = 3
FALLBACK_MTU = 23


async def scan(timeout: float = 6.0):
    """Return [(address, name)] for every advertising BLE device."""
    from bleak import BleakScanner

    devices = await BleakScanner.discover(timeout=timeout)
    return [(d.address, d.name or "") for d in devices]


class BLETransport(Transport):
    name = "ble"

    def __init__(self, address: str | None = None, device_name: str | None = None,
                 timeout: float = 20.0, max_write: int | None = None):
        self.address = address
        self.device_name = device_name
        self.timeout = timeout
        self._forced_max_write = max_write
        self.max_write = FALLBACK_MTU - ATT_HEADER
        self._client = None
        self._write_char = None
        self._notify_char = None
        self._write_with_response = False
        self._notifications: asyncio.Queue | None = None
        self.resolved_name = device_name

    async def _find_device(self):
        from bleak import BleakScanner

        if self.address:
            dev = await BleakScanner.find_device_by_address(self.address, timeout=self.timeout)
            if dev is None:
                raise SystemExit(f"BLE device {self.address} not found")
            return dev
        log.info("scanning for BLE devices (%.0fs)", min(self.timeout, 8.0))
        devices = await BleakScanner.discover(timeout=min(self.timeout, 8.0))
        log.debug("found %d device(s): %s", len(devices),
                  ", ".join(f"{d.address} {d.name or '(unnamed)'}" for d in devices))
        wanted = (self.device_name or "").upper()
        for d in devices:
            nm = (d.name or "").upper()
            if wanted and wanted in nm:
                return d
        if wanted:
            raise SystemExit(f"no BLE device whose name contains {self.device_name!r}")
        # No hint given: fall back to anything that looks like a Phomemo.
        from mbprint.printers import detect

        for d in devices:
            if detect(d.name):
                return d
        raise SystemExit("no Phomemo printer found; pass --device NAME or --address MAC")

    async def open(self) -> None:
        from bleak import BleakClient

        dev = await self._find_device()
        self.resolved_name = getattr(dev, "name", None) or self.device_name
        self.address = getattr(dev, "address", self.address)
        log.info("connecting to %s [%s]", self.resolved_name or "(unnamed)", self.address)
        client = BleakClient(dev, timeout=self.timeout)
        await client.connect()
        log.debug("GATT connected")
        self._client = client

        service = None
        for uuid in SERVICE_UUIDS:
            service = client.services.get_service(uuid)
            if service is not None:
                log.debug("using service %s", uuid)
                break
        if service is None:
            log.debug("no known service UUID; searching every characteristic")
        chars = list(service.characteristics) if service else [
            c for s in client.services for c in s.characteristics
        ]

        for c in chars:
            if c.uuid.lower() in WRITE_UUIDS and (
                "write" in c.properties or "write-without-response" in c.properties
            ):
                self._write_char = c
                break
        if self._write_char is None:
            for c in chars:
                if "write-without-response" in c.properties or "write" in c.properties:
                    self._write_char = c
                    break
        if self._write_char is None:
            raise SystemExit("no writable characteristic on this device")
        self._write_with_response = "write-without-response" not in self._write_char.properties
        log.debug("write characteristic %s %s (response=%s)", self._write_char.uuid,
                  sorted(self._write_char.properties), self._write_with_response)

        for c in chars:
            if c.uuid.lower() in NOTIFY_UUIDS and "notify" in c.properties:
                self._notify_char = c
                break
        if self._notify_char is not None:
            self._notifications = asyncio.Queue()
            try:
                await client.start_notify(self._notify_char, self._on_notify)
                log.debug("notifications on %s", self._notify_char.uuid)
            except Exception as exc:
                log.debug("could not subscribe to notifications: %s", exc)
                self._notify_char = None
                self._notifications = None

        mtu = getattr(client, "mtu_size", None) or FALLBACK_MTU
        if mtu <= FALLBACK_MTU and hasattr(client, "_acquire_mtu"):
            # BlueZ does not expose the negotiated MTU until a write is
            # acquired; without this every write is capped at 20 bytes.
            try:
                await client._acquire_mtu()
                mtu = getattr(client, "mtu_size", None) or mtu
                log.debug("acquired MTU from BlueZ: %d", mtu)
            except Exception as exc:
                log.debug("could not acquire MTU (%s); keeping the default", exc)
        negotiated = max(20, int(mtu) - ATT_HEADER)
        self.max_write = min(self._forced_max_write or negotiated, negotiated)
        log.info("connected to %s: ATT MTU %d, %d-byte writes",
                 self.resolved_name or self.address, mtu, self.max_write)
        if self.max_write <= 20:
            log.warning("link MTU was not negotiated: 20-byte writes make printing "
                        "roughly six times slower than normal")

    def _on_notify(self, _char, data: bytearray) -> None:
        trace(log, "<- notify: %s", hexdump(bytes(data)))
        if self._notifications is not None:
            self._notifications.put_nowait(bytes(data))

    async def close(self) -> None:
        if self._client is not None:
            try:
                if self._notify_char is not None:
                    await self._client.stop_notify(self._notify_char)
            except Exception:
                pass
            try:
                log.debug("disconnecting")
                await self._client.disconnect()
            except Exception:
                pass
            self._client = None

    async def send(self, data: bytes) -> None:
        if self._client is None or self._write_char is None:
            raise SystemExit("BLE not connected")
        if len(data) > self.max_write:
            raise ValueError(
                f"write of {len(data)} bytes exceeds link MTU payload {self.max_write}"
            )
        if tracing(log):
            trace(log, "-> write %d bytes: %s", len(data), hexdump(data))
        try:
            await self._client.write_gatt_char(
                self._write_char, bytes(data), response=self._write_with_response
            )
        except Exception as exc:
            log.debug("write failed (%s)", exc)
            if self._write_with_response:
                raise
            # Some firmwares reject write-without-response mid-stream.
            log.debug("retrying with write-with-response")
            self._write_with_response = True
            await self._client.write_gatt_char(self._write_char, bytes(data), response=True)

    async def wait_for_response(self, timeout_ms: int = 500) -> bytes | None:
        if self._notifications is None:
            await self.delay(timeout_ms)
            return None
        try:
            return await asyncio.wait_for(self._notifications.get(), timeout_ms / 1000.0)
        except asyncio.TimeoutError:
            return None

    async def query(self, what: str, timeout_ms: int = 800) -> bytes | None:
        cmd = QUERY_COMMANDS.get(what)
        if cmd is None:
            raise SystemExit(f"unknown query {what!r}; use one of {sorted(QUERY_COMMANDS)}")
        log.debug("query %s: %s", what, hexdump(cmd))
        await self.send(cmd)
        reply = await self.wait_for_response(timeout_ms)
        log.debug("query %s reply: %s", what, hexdump(reply) if reply else "(none)")
        return reply
