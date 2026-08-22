"""Read-only Brother printer diagnostic commands and response decoders."""

from __future__ import annotations

from typing import TypeAlias

# ``BasePrinter::requestSystemReport`` in Brother's native SDK emits this
# four-byte raster command.  It does not change printer state.
SYSTEM_REPORT_COMMAND = b"\x1biXG"
SYSTEM_REPORT_MARKER = "<<PRINTER CONFIGURATION>>"

SystemReport: TypeAlias = dict[str, dict[str, str]]


def decode_system_report(data: bytes) -> str:
    """Remove the binary response prefix and decode a Brother system report."""
    text = data.decode("utf-8", errors="replace").replace("\x00", "")
    marker = text.find(SYSTEM_REPORT_MARKER)
    if marker < 0:
        raise ValueError("response is not a Brother printer configuration report")
    return text[marker:].strip()


def parse_system_report(data: bytes) -> SystemReport:
    """Parse the report's INI-like sections while preserving its printed values."""
    report: SystemReport = {}
    section: dict[str, str] | None = None
    for raw_line in decode_system_report(data).splitlines():
        line = raw_line.strip()
        if line.startswith("[") and line.endswith("]"):
            section = report.setdefault(line[1:-1].strip(), {})
        elif section is not None and "=" in line:
            key, value = line.split("=", 1)
            section[key.strip()] = value.strip()
    return report
