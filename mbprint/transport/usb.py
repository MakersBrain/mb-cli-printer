"""Raw USB bulk transport (PM-241, USB-capable M-series, and the Brother QL).

Printing and status queries are verified on a QL-1110NWB, which exposes a
64-byte bulk pair on interface 0 and answers a status request with zero-length
packets until the 32-byte block is ready.
"""

from __future__ import annotations

import asyncio
import time
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
        # The IN endpoint is optional: without it, status requests go unanswered
        # and the caller falls back to the layout's own dimensions.
        ep_in = usb.util.find_descriptor(
            intf,
            custom_match=lambda e: (
                usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN
            ),
        )
        self._dev, self._ep_out, self._ep_in = dev, ep, ep_in
        log.info(
            "USB %04x:%04x, bulk OUT 0x%02x, %d-byte packets, IN %s",
            dev.idVendor,
            dev.idProduct,
            ep.bEndpointAddress,
            ep.wMaxPacketSize,
            f"0x{ep_in.bEndpointAddress:02x}" if ep_in is not None else "none",
        )
        # Respect the endpoint's own packet size as the write ceiling.
        self.max_write = min(self.max_write, int(ep.wMaxPacketSize) or self.max_write)
        # A reply nobody read outlives the process that asked for it, so a stale
        # status block would otherwise answer the next run's question.
        if ep_in is not None:
            await asyncio.to_thread(self._drain)

    def _drain(self) -> None:
        """Discard anything the previous session left on the IN endpoint."""
        for _ in range(8):
            try:
                stale = bytes(self._ep_in.read(64, 50))
            except Exception:  # a timeout means there is nothing waiting
                return
            if not stale:
                return
            log.debug("dropped a stale %d-byte reply: %s", len(stale), hexdump(stale, 32))

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
            # A QL answers a status request with zero-length packets until the
            # block is ready, so poll until the deadline rather than believing
            # the first empty read.
            deadline = time.monotonic() + timeout_ms / 1000
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                try:
                    reply = bytes(self._ep_in.read(64, max(1, int(remaining * 1000))))
                except Exception:  # pyusb raises USBTimeoutError and friends
                    return None
                if reply:
                    return reply
                time.sleep(0.05)

        reply = await asyncio.to_thread(read)
        if reply and tracing(log):
            trace(log, "<- %d bytes: %s", len(reply), hexdump(reply))
        return reply or None
