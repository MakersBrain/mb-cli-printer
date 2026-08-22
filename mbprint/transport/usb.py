"""Raw USB bulk transport (PM-241, USB-capable M-series, and the Brother QL).

Printing and status queries are verified on a QL-1110NWB, which exposes a
64-byte bulk pair on interface 0 and answers a status request with zero-length
packets until the 32-byte block is ready.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterable
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


def decode_device_id(data: bytes) -> str | None:
    """Decode the big-endian length-prefixed IEEE 1284 device ID."""
    if len(data) < 2:
        return None
    length = int.from_bytes(data[:2], "big")
    if length < 2:
        return None
    return data[2 : min(length, len(data))].rstrip(b"\x00").decode("ascii", errors="replace")


def decode_port_status(value: int) -> dict[str, bool]:
    return {
        "selected": bool(value & 0x10),
        "paper_empty": bool(value & 0x20),
        "error": not bool(value & 0x08),
    }


def _usb_modules() -> tuple[Any, Any]:
    try:
        import usb.core
        import usb.util
    except ImportError:
        raise SystemExit("usb transport needs pyusb: pip install pyusb")
    return usb.core, usb.util


def _supported_device(dev: Any, vid: int | None, pid: int | None) -> bool:
    if vid is not None:
        return int(dev.idVendor) == vid and (pid is None or int(dev.idProduct) == pid)
    return any(
        int(dev.idVendor) == known_vid and (known_pid is None or int(dev.idProduct) == known_pid)
        for known_vid, known_pid in USB_IDS
    )


def find_usb_devices(vid: int | None = None, pid: int | None = None) -> list[Any]:
    """Enumerate supported USB printers without claiming or configuring them."""
    if pid is not None and vid is None:
        raise SystemExit("--usb-pid requires --usb-vid")
    usb_core, _ = _usb_modules()
    try:
        devices = list(usb_core.find(find_all=True) or [])
    except usb_core.NoBackendError:
        raise SystemExit("PyUSB has no libusb backend; enter 'nix develop' or install libusb-1.0")
    return [dev for dev in devices if _supported_device(dev, vid, pid)]


def usb_string(dev: Any, index: int) -> str | None:
    if not index:
        return None
    _, usb_util = _usb_modules()
    try:
        value = usb_util.get_string(dev, index)
        return str(value) if value else None
    except Exception:
        return None


def describe_usb_device(dev: Any) -> dict[str, Any]:
    return {
        "vid": int(dev.idVendor),
        "pid": int(dev.idProduct),
        "bus": int(dev.bus) if getattr(dev, "bus", None) is not None else None,
        "address": int(dev.address) if getattr(dev, "address", None) is not None else None,
        "manufacturer": usb_string(dev, int(dev.iManufacturer)),
        "product": usb_string(dev, int(dev.iProduct)),
        "serial": usb_string(dev, int(dev.iSerialNumber)),
    }


def select_usb_device(
    devices: Iterable[Any],
    *,
    serial: str | None = None,
    bus: int | None = None,
    address: int | None = None,
    serial_reader: Callable[[Any], str | None] = lambda dev: usb_string(
        dev, int(dev.iSerialNumber)
    ),
) -> Any:
    """Select exactly one device, refusing ambiguous or stale selectors."""
    matches = [
        dev
        for dev in devices
        if (bus is None or getattr(dev, "bus", None) == bus)
        and (address is None or getattr(dev, "address", None) == address)
    ]
    if serial is not None:
        matches = [dev for dev in matches if serial_reader(dev) == serial]
    if not matches:
        selectors = []
        if serial is not None:
            selectors.append(f"serial {serial!r}")
        if bus is not None:
            selectors.append(f"bus {bus}")
        if address is not None:
            selectors.append(f"address {address}")
        suffix = f" matching {', '.join(selectors)}" if selectors else ""
        raise SystemExit(f"no supported USB printer found{suffix} (check power and permissions)")
    if len(matches) > 1:
        choices = ", ".join(
            f"{int(dev.idVendor):04x}:{int(dev.idProduct):04x} "
            f"at bus {getattr(dev, 'bus', '?')} address {getattr(dev, 'address', '?')}"
            for dev in matches
        )
        raise SystemExit(
            f"multiple USB printers match: {choices}; select one with --usb-serial "
            "or --usb-bus and --usb-address"
        )
    return matches[0]


class USBTransport(Transport):
    name = "usb"

    def __init__(
        self,
        vid: int | None = None,
        pid: int | None = None,
        max_write: int = 512,
        interface: int = 0,
        alternate: int = 0,
        serial: str | None = None,
        bus: int | None = None,
        address: int | None = None,
    ):
        self.vid = vid
        self.pid = pid
        self.max_write = max_write
        self.interface = interface
        self.alternate = alternate
        self.serial = serial
        self.bus = bus
        self.address = address
        # pyusb objects, kept untyped: the library ships no stubs.
        self._dev: Any = None
        self._ep_out: Any = None
        self._ep_in: Any = None
        self.device_info: dict[str, Any] = {}

    async def open(self) -> None:
        _, usb_util = _usb_modules()
        dev = select_usb_device(
            find_usb_devices(self.vid, self.pid),
            serial=self.serial,
            bus=self.bus,
            address=self.address,
        )

        try:
            if dev.is_kernel_driver_active(self.interface):
                dev.detach_kernel_driver(self.interface)
        except Exception:
            pass  # not all platforms expose kernel driver state
        dev.set_configuration()
        cfg = dev.get_active_configuration()
        try:
            dev.set_interface_altsetting(interface=self.interface, alternate_setting=self.alternate)
            intf = cfg[(self.interface, self.alternate)]
        except Exception as exc:
            available = ", ".join(
                f"{item.bInterfaceNumber}:{item.bAlternateSetting}/protocol-{item.bInterfaceProtocol}"
                for item in cfg
            )
            raise SystemExit(
                f"USB interface {self.interface} alternate {self.alternate} is unavailable "
                f"({available or 'no interfaces found'}): {exc}"
            )
        ep = usb_util.find_descriptor(
            intf,
            custom_match=lambda e: (
                usb_util.endpoint_direction(e.bEndpointAddress) == usb_util.ENDPOINT_OUT
            ),
        )
        if ep is None:
            raise SystemExit("no bulk OUT endpoint on the USB printer")
        # The IN endpoint is optional: without it, status requests go unanswered
        # and the caller falls back to the layout's own dimensions.
        ep_in = usb_util.find_descriptor(
            intf,
            custom_match=lambda e: (
                usb_util.endpoint_direction(e.bEndpointAddress) == usb_util.ENDPOINT_IN
            ),
        )
        self._dev, self._ep_out, self._ep_in = dev, ep, ep_in
        self.device_info = describe_usb_device(dev) | {
            "interface": int(intf.bInterfaceNumber),
            "alternate": int(intf.bAlternateSetting),
            "protocol": int(intf.bInterfaceProtocol),
            "out_endpoint": int(ep.bEndpointAddress),
            "in_endpoint": int(ep_in.bEndpointAddress) if ep_in is not None else None,
            "packet_size": int(ep.wMaxPacketSize),
        }
        log.info(
            "USB %04x:%04x, interface %d alt %d protocol %d, "
            "bulk OUT 0x%02x, %d-byte packets, IN %s",
            dev.idVendor,
            dev.idProduct,
            intf.bInterfaceNumber,
            intf.bAlternateSetting,
            intf.bInterfaceProtocol,
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
                _, usb_util = _usb_modules()
                usb_util.dispose_resources(self._dev)
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

    async def get_device_id(self, timeout_ms: int = 1000) -> str | None:
        """Issue the standard USB Printer Class GET_DEVICE_ID request."""
        if self._dev is None:
            raise SystemExit("usb device not open")

        def request() -> str | None:
            try:
                raw = bytes(
                    self._dev.ctrl_transfer(
                        0xA1,
                        0,
                        0,
                        (self.interface << 8) | self.alternate,
                        1024,
                        timeout=timeout_ms,
                    )
                )
            except Exception as exc:
                log.debug("USB GET_DEVICE_ID failed: %s", exc)
                return None
            return decode_device_id(raw)

        return await asyncio.to_thread(request)

    async def get_port_status(self, timeout_ms: int = 1000) -> dict[str, bool] | None:
        """Issue the standard USB Printer Class GET_PORT_STATUS request."""
        if self._dev is None:
            raise SystemExit("usb device not open")

        def request() -> dict[str, bool] | None:
            try:
                raw = bytes(
                    self._dev.ctrl_transfer(0xA1, 1, 0, self.interface, 1, timeout=timeout_ms)
                )
            except Exception as exc:
                log.debug("USB GET_PORT_STATUS failed: %s", exc)
                return None
            return decode_port_status(raw[0]) if raw else None

        return await asyncio.to_thread(request)
