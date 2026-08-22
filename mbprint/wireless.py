"""Brother QL wireless-setting PJL commands.

The format is used by Brother iPrint&Label's native
``InfrastructureWiFiSetter``.  It is a settings protocol, not encryption:
password bytes are XOR-obfuscated with a fixed key and must still be treated as
credentials in captures and generated files.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass

PJL_HEADER = b"\x1b%-12345X@PJL\r\n"
PJL_FOOTER = b"\x1b%-12345X"
REBOOT_COMMAND = bytes.fromhex("1b 69 58 2a 31 03 00 01 2e 00 00 00 2c 00")
PASSWORD_KEY = bytes.fromhex("0d ae e4 a1 8b 7f 26 5e 72 5b 17 7a 71 cd ec 4d")

ENCRYPTIONS = {
    "none": 1,
    "wep": 2,
    "tkip": 3,
    "aes": 4,
    "ckip": 5,
    "cmic": 6,
    "ckip-cmic": 7,
    "tkip-aes": 8,
}

AUTHENTICATIONS = {
    "open": 1,
    "shared-key": 2,
    "wpa-psk": 3,
    "leap": 7,
    "eap-fast": 13,
    "peap": 15,
    "eap-ttls": 16,
    "eap-tls": 17,
    "wpa-only": 18,
    "wpa2-only": 19,
}


@dataclass(frozen=True)
class AccessPoint:
    ssid: str
    channel: int
    power: int
    enterprise: bool
    encrypted: bool


def _pjl(command: bytes) -> bytes:
    return PJL_HEADER + b"@PJL " + command + b"\r\n" + PJL_FOOTER


def wifi_scan_start_command() -> bytes:
    """Start the access-point search used by Brother's setup application."""
    return _pjl(_parameter("458845", "31-3a"))


def wifi_scan_result_command() -> bytes:
    """Request the results of a previously started access-point search."""
    return _pjl(b"INFO AVAILABLEWLAN")


def wifi_info_command() -> bytes:
    """Request OBJBRNET values containing Wi-Fi state and addresses."""
    return _pjl(b"INQUIRE OBJBRNET")


def parse_wifi_status(data: bytes) -> bool | None:
    match = re.search(rb'"?458867\s*:\s*([01])', data)
    return match.group(1) == b"1" if match else None


def parse_ip_address(data: bytes) -> str | None:
    match = re.search(rb'"?458967\.2\s*:\s*"?(-?[0-9a-fA-F-]+)', data)
    if not match:
        return None
    try:
        octets = [int(part, 16) for part in match.group(1).strip(b"-").split(b"-")]
    except ValueError:
        return None
    if len(octets) != 4 or any(value > 255 for value in octets):
        return None
    return ".".join(str(value) for value in octets)


def _decode_ssid(value: str) -> str:
    if not re.fullmatch(r"(?:-[0-9a-fA-F]{1,2})+", value):
        return value
    try:
        return bytes(int(part, 16) for part in value.lstrip("-").split("-")).decode("utf-8")
    except (UnicodeDecodeError, ValueError):
        return value


def parse_access_points(data: bytes) -> list[AccessPoint]:
    """Decode Brother ``VAP`` rows; ignore headers and unfamiliar row shapes."""
    points: list[AccessPoint] = []
    text = data.decode("utf-8", errors="replace").replace("\x00", "")
    for row in csv.reader(text.splitlines()):
        if len(row) < 8 or row[0].strip().strip('"') != "VAP":
            continue
        try:
            channel = int(row[4].strip())
            power = int(row[5].strip())
        except ValueError:
            continue
        points.append(
            AccessPoint(
                ssid=_decode_ssid(row[1].strip().strip('"')),
                channel=channel,
                power=power,
                enterprise=row[6].strip().strip('"') == "3",
                encrypted=row[7].strip().strip('"') == "2",
            )
        )
    return points


def xor_password(value: bytes) -> bytes:
    """Apply Brother's reversible fixed-key password obfuscation."""
    return bytes(byte ^ PASSWORD_KEY[i % len(PASSWORD_KEY)] for i, byte in enumerate(value))


def encode_ssid(value: str) -> bytes:
    """Encode the SSID exactly as ``convToCharCodeStr`` does."""
    return "".join(f"-{byte:x}" for byte in value.encode("utf-8")).encode("ascii")


def _parameter(oid: str, value: bytes | str | int) -> bytes:
    if isinstance(value, int):
        raw = str(value).encode("ascii")
    elif isinstance(value, str):
        raw = value.encode("ascii")
    else:
        raw = value
    return b'DEFAULT OBJBRNET="' + oid.encode("ascii") + b":" + raw + b'"'


@dataclass(frozen=True)
class WirelessSettings:
    ssid: str
    password: str = ""
    encryption: str = "tkip-aes"
    authentication: str = "wpa-psk"
    infrastructure: bool = True
    wireless_direct: bool = False
    reboot: bool = True

    def command(self) -> bytes:
        if not self.ssid:
            raise ValueError("SSID must not be empty")
        if self.encryption not in ENCRYPTIONS:
            raise ValueError(f"unknown encryption {self.encryption!r}")
        if self.authentication not in AUTHENTICATIONS:
            raise ValueError(f"unknown authentication {self.authentication!r}")
        if self.authentication != "open" and not self.password:
            raise ValueError("the selected authentication needs a password")
        if self.authentication == "open" and self.encryption != "none":
            raise ValueError("open authentication must use --encryption none")

        encryption = ENCRYPTIONS[self.encryption]
        authentication = AUTHENTICATIONS[self.authentication]
        params: list[tuple[str, bytes | str | int]] = [
            ("458867", "0"),
            ("458878", "1"),
            ("458877", encode_ssid(self.ssid)),
        ]
        if authentication in (3, 18, 19):
            params.append(("99458890", xor_password(self.password.encode("utf-8"))))
        elif encryption == ENCRYPTIONS["wep"]:
            params.append(("99458889.1", xor_password(self.password.encode("utf-8"))))
        params.extend(
            [
                ("458880", encryption),
                ("458881", authentication),
                ("459138.2", int(self.infrastructure)),
                ("459138.3", int(self.wireless_direct)),
                ("458865", "1"),
            ]
        )
        body = b"".join(b"@PJL " + _parameter(oid, value) + b"\r\n" for oid, value in params)
        return PJL_HEADER + body + PJL_FOOTER + (REBOOT_COMMAND if self.reboot else b"")
