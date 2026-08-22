"""Mocked PyUSB integration tests for both supported printer families."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from mbprint.transport import usb as usbmod


class FakeEndpoint:
    def __init__(self, address: int):
        self.bEndpointAddress = address
        self.wMaxPacketSize = 64
        self.writes: list[tuple[bytes, int]] = []
        self.incoming: list[bytes] = []

    def write(self, data: bytes, timeout: int) -> int:
        self.writes.append((bytes(data), timeout))
        return len(data)

    def read(self, _size: int, _timeout: int) -> bytes:
        if self.incoming:
            return self.incoming.pop(0)
        raise TimeoutError


class FakeInterface(list[FakeEndpoint]):
    bInterfaceNumber = 0
    bAlternateSetting = 0
    bInterfaceProtocol = 2


class FakeConfiguration:
    def __init__(self, interface: FakeInterface):
        self.interface = interface

    def __getitem__(self, key: tuple[int, int]) -> FakeInterface:
        if key != (0, 0):
            raise KeyError(key)
        return self.interface

    def __iter__(self):
        return iter([self.interface])


class FakeDevice:
    iManufacturer = 1
    iProduct = 2
    iSerialNumber = 3

    def __init__(
        self,
        vid: int,
        pid: int,
        manufacturer: str,
        product: str,
        serial: str,
        address: int,
    ):
        self.idVendor = vid
        self.idProduct = pid
        self.bus = 1
        self.address = address
        self.strings = {1: manufacturer, 2: product, 3: serial}
        self.out_endpoint = FakeEndpoint(0x01)
        self.in_endpoint = FakeEndpoint(0x81)
        self.interface = FakeInterface([self.out_endpoint, self.in_endpoint])
        self.configuration = FakeConfiguration(self.interface)
        self.configured = False
        self.detached: list[int] = []
        self.altsettings: list[tuple[int, int]] = []
        self.control_requests: list[tuple[int, int, int, int, int, int]] = []
        self.disposed = False

    def is_kernel_driver_active(self, _interface: int) -> bool:
        return True

    def detach_kernel_driver(self, interface: int) -> None:
        self.detached.append(interface)

    def set_configuration(self) -> None:
        self.configured = True

    def get_active_configuration(self) -> FakeConfiguration:
        return self.configuration

    def set_interface_altsetting(self, interface: int, alternate_setting: int) -> None:
        self.altsettings.append((interface, alternate_setting))

    def ctrl_transfer(
        self,
        request_type: int,
        request: int,
        value: int,
        index: int,
        length: int,
        *,
        timeout: int,
    ) -> bytes:
        self.control_requests.append((request_type, request, value, index, length, timeout))
        if request == 0:
            identifier = f"MFG:{self.strings[1]};MDL:{self.strings[2]};".encode()
            return (len(identifier) + 2).to_bytes(2, "big") + identifier
        if request == 1:
            return b"\x18"  # selected, paper present, no error
        raise AssertionError(f"unexpected control request {request}")


class FakeCore:
    NoBackendError = RuntimeError

    def __init__(self, devices: list[FakeDevice]):
        self.devices = devices

    def find(self, *, find_all: bool):
        assert find_all is True
        return iter(self.devices)


class FakeUtil:
    ENDPOINT_OUT = 0x00
    ENDPOINT_IN = 0x80

    @staticmethod
    def endpoint_direction(address: int) -> int:
        return address & 0x80

    @staticmethod
    def find_descriptor(items: list[FakeEndpoint], *, custom_match: Any):
        return next((item for item in items if custom_match(item)), None)

    @staticmethod
    def get_string(device: FakeDevice, index: int) -> str | None:
        return device.strings.get(index)

    @staticmethod
    def dispose_resources(device: FakeDevice) -> None:
        device.disposed = True


@pytest.mark.parametrize(
    ("vid", "pid", "manufacturer", "product", "serial"),
    [
        (0x04F9, 0x209B, "Brother", "QL-1110NWB", "BROTHER-001"),
        (0x0483, 0x5740, "Phomemo", "M110", "PHOMEMO-001"),
    ],
)
def test_usb_transport_end_to_end_for_both_printer_families(
    monkeypatch, vid, pid, manufacturer, product, serial
):
    decoy = FakeDevice(vid, pid, manufacturer, product, "OTHER-PRINTER", 6)
    target = FakeDevice(vid, pid, manufacturer, product, serial, 7)
    core = FakeCore([decoy, target])
    util = FakeUtil()
    monkeypatch.setattr(usbmod, "_usb_modules", lambda: (core, util))
    transport = usbmod.USBTransport(vid=vid, pid=pid, serial=serial, max_write=512)

    async def exercise() -> tuple[bytes | None, str | None, dict[str, bool] | None]:
        await transport.open()
        target.in_endpoint.incoming.append(b"printer reply")
        await transport.send(b"print bytes")
        reply = await transport.wait_for_response(100)
        device_id = await transport.get_device_id()
        status = await transport.get_port_status()
        await transport.close()
        return reply, device_id, status

    reply, device_id, status = asyncio.run(exercise())

    assert decoy.configured is False
    assert target.configured is True
    assert target.detached == [0]
    assert target.altsettings == [(0, 0)]
    assert target.out_endpoint.writes == [(b"print bytes", 5000)]
    assert reply == b"printer reply"
    assert device_id == f"MFG:{manufacturer};MDL:{product};"
    assert status == {"selected": True, "paper_empty": False, "error": False}
    assert target.control_requests == [
        (0xA1, 0, 0, 0, 1024, 1000),
        (0xA1, 1, 0, 0, 1, 1000),
    ]
    assert transport.device_info["serial"] == serial
    assert transport.device_info["bus"] == 1
    assert transport.device_info["address"] == 7
    assert transport.max_write == 64
    assert target.disposed is True
