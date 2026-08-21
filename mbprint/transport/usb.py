"""Raw USB bulk transport (PM-241, USB-capable M-series, and the Brother QL).

The Brother path is written from the device's descriptors rather than tried: no
QL has been connected over USB here, so the first print on one is worth
watching. The network and Bluetooth transports are verified on a QL-1110NWB.
"""

from __future__ import annotations

import asyncio
from typing import Any

from mbprint.log import get_logger, hexdump, trace, tracing
from mbprint.transport import Transport

log = get_logger(__name__)

# Known printer USB ids: Phomemo first, as the reference app filters them, then
# Brother, whose QL series is a standard USB printer-class device.
USB_IDS = [
    (0x0483, 0x5740),
    (0x0483, None),
    (0x2E3C, 0x5750),
    (0x2E3C, None),
    (0x04F9, None),  # Brother
]


class USBTransport(Transport):
    name = "usb"

    def __init__(self, vid: int | None = None, pid: int | None = None, max_write: int = 512):
        self.vid = vid
        self.pid = pid
        self.max_write = max_write
        # pyusb objects, kept untyped: the library ships no stubs.
        self._dev: Any = None
        self._ep_out: Any = None
        self._ep_in: Any = None

    async def open(self) -> None:
        try:
            import usb.core
            import usb.util
        except ImportError:
            raise SystemExit("usb transport needs pyusb: pip install pyusb")

        candidates = [(self.vid, self.pid)] if self.vid else USB_IDS
        dev = None
        for vid, pid in candidates:
            kw: dict[str, int] = {"idVendor": vid}
            if pid:
                kw["idProduct"] = pid
            dev = usb.core.find(**kw)
            if dev is not None:
                break
        if dev is None:
            raise SystemExit("no Phomemo USB printer found (check permissions / udev rules)")

        try:
            if dev.is_kernel_driver_active(0):
                dev.detach_kernel_driver(0)
        except Exception:
            pass  # not all platforms expose kernel driver state
        dev.set_configuration()
        cfg = dev.get_active_configuration()
        intf = cfg[(0, 0)]
        ep = usb.util.find_descriptor(
            intf,
            custom_match=lambda e: (
                usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT
            ),
        )
        if ep is None:
            raise SystemExit("no bulk OUT endpoint on the USB printer")
        self._dev, self._ep_out = dev, ep
        log.info(
            "USB %04x:%04x, bulk OUT 0x%02x, %d-byte packets",
            dev.idVendor,
            dev.idProduct,
            ep.bEndpointAddress,
            ep.wMaxPacketSize,
        )
        # Respect the endpoint's own packet size as the write ceiling.
        self.max_write = min(self.max_write, int(ep.wMaxPacketSize) or self.max_write)

    async def close(self) -> None:
        if self._dev is not None:
            try:
                import usb.util

                usb.util.dispose_resources(self._dev)
            except Exception:
                pass
            self._dev = None
            self._ep_out = None
            self._ep_in = None

    async def send(self, data: bytes) -> None:
        if self._ep_out is None:
            raise SystemExit("usb device not open")
        if tracing(log):
            trace(log, "-> write %d bytes: %s", len(data), hexdump(data))
        await asyncio.to_thread(self._ep_out.write, bytes(data), 5000)

    async def wait_for_response(self, timeout_ms: int = 500) -> bytes | None:
        """Read a status block, when the printer offers an IN endpoint."""
        if self._ep_in is None:
            await self.delay(timeout_ms)
            return None

        def read() -> bytes | None:
            try:
                return bytes(self._ep_in.read(64, timeout_ms))
            except Exception:  # pyusb raises USBTimeoutError and friends
                return None

        reply = await asyncio.to_thread(read)
        if reply and tracing(log):
            trace(log, "<- %d bytes: %s", len(reply), hexdump(reply))
        return reply or None
