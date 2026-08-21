"""Printer definitions and model resolution.

Definitions are data-driven from printers.json (ported from the phomymo web app)
plus optional user definitions in ~/.config/mbprint/printers.json.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mbprint.log import get_logger

log = get_logger(__name__)

BUNDLED = Path(__file__).with_name("printers.json")
USER_DEFS = (
    Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "mbprint" / "printers.json"
)

# Fallback when nothing matches: the widest common M-series geometry.
DEFAULT_CONFIG: dict[str, Any] = {
    "id": "generic",
    "name": "Generic M-series",
    "protocol": "m-series",
    "widthBytes": 72,
    "dpi": 203,
    "alignment": "center",
    "rotated": False,
    "tape": False,
}

# Per-protocol write chunking. Kept identical to the reference implementation;
# the transport clamps these further to the negotiated link MTU.
PROTOCOL_CHUNK = {
    "brother": 1024,
    "m-series": 128,
    "m02": 128,
    "m110": 128,
    "d-series": 128,
    "p12": 128,
    "m04": 256,
    "tspl": 512,
}
# Inter-chunk pacing in ms, same as the reference driver.
PROTOCOL_CHUNK_DELAY = {
    "brother": 0,
    "tspl": 10,
    "m04": 20,
}
DEFAULT_CHUNK_DELAY_MS = 20


@dataclass
class PrinterDef:
    id: str
    name: str
    protocol: str
    width_bytes: int | None
    dpi: int = 203
    alignment: str = "center"
    rotated: bool = False
    tape: bool = False
    group: str = ""
    description: str = ""
    name_patterns: list[str] = field(default_factory=list)
    builtin: bool = True
    # Brother-specific geometry and capabilities, unused by the Phomemo families.
    additional_offset_r: int = 0
    invalidate_bytes: int = 200
    compression: bool = False
    min_rows: int = 0
    max_rows: int = 0

    @property
    def width_px(self) -> int:
        return int(self.width_bytes or DEFAULT_CONFIG["widthBytes"]) * 8

    @property
    def chunk_size(self) -> int:
        return PROTOCOL_CHUNK.get(self.protocol, 128)

    @property
    def chunk_delay_ms(self) -> int:
        return PROTOCOL_CHUNK_DELAY.get(self.protocol, DEFAULT_CHUNK_DELAY_MS)

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> PrinterDef:
        return cls(
            id=d["id"],
            name=d.get("name", d["id"]),
            protocol=d.get("protocol", "m-series"),
            width_bytes=d.get("widthBytes"),
            dpi=d.get("dpi") or 203,
            alignment=d.get("alignment") or "center",
            rotated=bool(d.get("rotated")),
            tape=bool(d.get("tape")),
            group=d.get("group", ""),
            description=d.get("description", ""),
            name_patterns=list(d.get("namePatterns") or []),
            builtin=bool(d.get("builtin", True)),
            additional_offset_r=int(d.get("additionalOffsetR") or 0),
            invalidate_bytes=int(d.get("invalidateBytes") or 200),
            compression=bool(d.get("compression")),
            min_rows=int(d.get("minRows") or 0),
            max_rows=int(d.get("maxRows") or 0),
        )


def _load(path: Path) -> list[dict[str, Any]]:
    try:
        with path.open(encoding="utf-8") as fh:
            printers: list[dict[str, Any]] = json.load(fh).get("printers", [])
            return printers
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        raise SystemExit(f"cannot read printer definitions {path}: {exc}")


def all_definitions() -> list[PrinterDef]:
    """Built-in definitions merged with user ones (user wins on id collision)."""
    customs = [PrinterDef.from_json({**d, "builtin": False}) for d in _load(USER_DEFS)]
    custom_ids = {d.id for d in customs}
    builtins = [PrinterDef.from_json(d) for d in _load(BUNDLED) if d["id"] not in custom_ids]
    return customs + builtins


def by_id(printer_id: str) -> PrinterDef | None:
    for d in all_definitions():
        if d.id == printer_id:
            return d
    return None


def detect(device_name: str | None) -> PrinterDef | None:
    """Match a BLE advertised name against known name patterns (longest first)."""
    if not device_name:
        return None
    name = device_name.upper()
    pairs = []
    for d in all_definitions():
        for pat in d.name_patterns:
            pairs.append((pat.upper(), d))
    pairs.sort(key=lambda p: -len(p[0]))
    for pat, d in pairs:
        if name.startswith(pat):
            return d
    return None


def resolve(model: str | None = None, device_name: str | None = None) -> PrinterDef:
    """Resolve a printer definition from an explicit model id, else the device name.

    Falling back to the generic definition is logged as a warning: the wrong head
    width produces a raster the printer cannot make sense of, so a silent
    fallback looks exactly like a broken printer.
    """
    if model and model != "auto":
        log.debug("model forced to %r", model)
        d = by_id(model)
        if not d:
            known = ", ".join(sorted(x.id for x in all_definitions()))
            raise SystemExit(f"unknown printer model {model!r}; known models: {known}")
        return d
    detected = detect(device_name)
    if detected:
        log.debug("detected %s [%s] from device name %r", detected.name, detected.id, device_name)
        return detected
    log.warning(
        "device name %r matches no known model; falling back to a generic %dpx %s "
        "head, which will misprint on most printers",
        device_name or "(unknown)",
        int(DEFAULT_CONFIG["widthBytes"]) * 8,
        DEFAULT_CONFIG["protocol"],
    )
    log.warning("pass --model MODEL, or run: mbprint config set model MODEL")
    log.info("known models: %s", ", ".join(sorted(d.id for d in all_definitions())))
    return PrinterDef.from_json(DEFAULT_CONFIG)
