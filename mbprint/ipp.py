"""Minimal IPP client, used to ask a network printer what media is loaded.

Brother's QL series accepts print jobs on port 9100 but answers no status
there. It does implement IPP on port 631, where `media-ready` reports the roll
that is physically loaded and `printer-state-reasons` reports things like an
open cover. That is worth one small request: printing on the wrong roll wastes
labels, and the printer knows the answer.

Only Get-Printer-Attributes is implemented, over http.client, so this costs no
dependency.
"""

from __future__ import annotations

import http.client
import re
import struct
from typing import Any

from mbprint.log import get_logger, hexdump, trace, tracing

log = get_logger(__name__)

# IPP attribute values are repeatable, so every attribute maps to a list.
Attributes = dict[str, list[Any]]

DEFAULT_PORT = 631
DEFAULT_PATH = "/ipp/print"

TAG_END = 0x03
DELIMITERS = {0x01, 0x02, 0x03, 0x04, 0x05}
INTEGER_TAGS = {0x21, 0x23}  # integer, enum
STRING_TAGS = {0x41, 0x42, 0x44, 0x45, 0x46, 0x47, 0x48, 0x49}


def _attr(tag: int, name: str, value: str) -> bytes:
    return (
        struct.pack(">BH", tag, len(name))
        + name.encode()
        + struct.pack(">H", len(value))
        + value.encode()
    )


def _additional(tag: int, value: str) -> bytes:
    """A further value for the previous attribute: same tag, empty name."""
    return struct.pack(">BHH", tag, 0, len(value)) + value.encode()


def build_request(uri: str, requested: tuple[str, ...]) -> bytes:
    body = struct.pack(">BBHI", 2, 0, 0x000B, 1)  # IPP 2.0, Get-Printer-Attributes
    body += bytes([0x01])  # operation attributes
    body += _attr(0x47, "attributes-charset", "utf-8")
    body += _attr(0x48, "attributes-natural-language", "en")
    body += _attr(0x45, "printer-uri", uri)
    if requested:
        body += _attr(0x44, "requested-attributes", requested[0])
        for name in requested[1:]:
            body += _additional(0x44, name)
    return body + bytes([TAG_END])


def parse_response(data: bytes) -> Attributes:
    """Decode an IPP response into {attribute: [values]}, skipping collections."""
    attributes: Attributes = {}
    i = 8  # version, status code, request id
    current = None
    while i < len(data):
        tag = data[i]
        i += 1
        if tag in DELIMITERS:
            if tag == TAG_END:
                break
            continue
        if i + 2 > len(data):
            break
        name_len = struct.unpack(">H", data[i : i + 2])[0]
        i += 2
        name = data[i : i + name_len].decode("utf-8", "replace")
        i += name_len
        if i + 2 > len(data):
            break
        value_len = struct.unpack(">H", data[i : i + 2])[0]
        i += 2
        raw = data[i : i + value_len]
        i += value_len
        if name:
            current = name
            attributes.setdefault(current, [])
        if current is None:
            continue
        if tag in INTEGER_TAGS and value_len == 4:
            attributes[current].append(struct.unpack(">i", raw)[0])
        elif tag in STRING_TAGS:
            attributes[current].append(raw.decode("utf-8", "replace"))
        # Collections (0x34) are nested; the keyword forms carry what we need.
    return attributes


DEFAULT_ATTRIBUTES = (
    "media-ready",
    "media-default",
    "printer-state",
    "printer-state-reasons",
    "printer-make-and-model",
)


def get_printer_attributes(
    host: str,
    port: int = DEFAULT_PORT,
    path: str = DEFAULT_PATH,
    timeout: float = 5.0,
    requested: tuple[str, ...] = DEFAULT_ATTRIBUTES,
) -> Attributes | None:
    """Ask a printer for its attributes. Returns None if it does not answer."""
    uri = f"ipp://{host}:{port}{path}"
    body = build_request(uri, requested)
    try:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
        conn.request(
            "POST",
            path,
            body,
            {"Content-Type": "application/ipp", "Content-Length": str(len(body))},
        )
        raw = conn.getresponse().read()
        conn.close()
    except (OSError, http.client.HTTPException) as exc:
        log.debug("IPP query to %s failed: %s", uri, exc)
        return None
    if tracing(log):
        trace(log, "<- ipp %d bytes: %s", len(raw), hexdump(raw))
    attributes = parse_response(raw)
    log.debug("IPP %s: %s", uri, {k: v for k, v in attributes.items() if v})
    return attributes


# media-ready keywords look like `roll_current_12x0mm` or
# `om_brother-label-29x90mm_29x90mm`; both end in the size in millimetres.
_SIZE = re.compile(r"(\d+)x(\d+)mm")


def media_size(keyword: str) -> tuple[float, float] | None:
    """Width and length in mm from an IPP media keyword. Length 0 = continuous."""
    matches = _SIZE.findall(keyword or "")
    if not matches:
        return None
    width, length = matches[-1]
    return float(width), float(length)


def loaded_media(
    host: str, port: int = DEFAULT_PORT, timeout: float = 5.0
) -> dict[str, Any] | None:
    """What roll is in the printer, and is it ready to print?"""
    attributes = get_printer_attributes(host, port, timeout=timeout)
    if not attributes:
        return None
    ready = (attributes.get("media-ready") or attributes.get("media-default") or [None])[0]
    states = {3: "idle", 4: "printing", 5: "stopped"}
    state = (attributes.get("printer-state") or [None])[0]
    return {
        "keyword": ready,
        "size_mm": media_size(ready) if ready else None,
        "state": states.get(state, state),
        "reasons": [r for r in attributes.get("printer-state-reasons", []) if r != "none"],
        "model": (attributes.get("printer-make-and-model") or [None])[0],
    }
