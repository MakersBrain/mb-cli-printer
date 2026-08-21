"""Raw USB bulk transport (PM-241 and USB-capable M-series)."""

from __future__ import annotations

import asyncio

from mbprint.log import get_logger, hexdump, trace, tracing
from mbprint.transport import Transport

log = get_logger(__name__)

# Known Phomemo USB ids, same list the reference app filters on.
USB_IDS = [
    (0x0483, 0x5740),
    (0x0483, None),
    (0x2E3C, 0x5750),
    (0x2E3C, None),
]


class USBTransport(Transport):
    name = "usb"

    def __init__(self, vid: int | None = None, pid: int | None = None, max_write: int = 512):
        self.vid = vid
        self.pid = pid
        self.max_write = max_write
        self._dev = None
        self._ep_out = None

    async def open(self) -> None:
        try:
            import usb.core
            import usb.util
        except ImportError:
            raise SystemExit("usb transport needs pyusb: pip install pyusb")

        candidates = [(self.vid, self.pid)] if self.vid else USB_IDS
        dev = None
        for vid, pid in candidates:
            kw = {"idVendor": vid}
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
            custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress)
            == usb.util.ENDPOINT_OUT,
        )
        if ep is None:
            raise SystemExit("no bulk OUT endpoint on the USB printer")
        self._dev, self._ep_out = dev, ep
        log.info("USB %04x:%04x, bulk OUT 0x%02x, %d-byte packets",
                 dev.idVendor, dev.idProduct, ep.bEndpointAddress, ep.wMaxPacketSize)
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

    async def send(self, data: bytes) -> None:
        if self._ep_out is None:
            raise SystemExit("usb device not open")
        if tracing(log):
            trace(log, "-> write %d bytes: %s", len(data), hexdump(data))
        await asyncio.to_thread(self._ep_out.write, bytes(data), 5000)
