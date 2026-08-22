"""SVG layouts used as templates: substitute `{{fields}}`, then rasterize.

A label.json layout is rebuilt element by element by `layout.render`. An SVG
layout is the opposite: the file is kept intact so whatever a design tool drew
survives, and only the parts that carry template syntax change. Placeholders in
text nodes and attribute values are filled per record, elements marked
`data-mb="qr"` or `data-mb="barcode"` are replaced by generated content, and the
finished document is handed to an external SVG renderer at the printer's dot
resolution.
"""

from __future__ import annotations

import copy
import io
import os
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from PIL import Image

from mbprint import layout
from mbprint.log import get_logger
from mbprint.svg import SVG_NS

log = get_logger(__name__)

# User units per millimeter for the length units an SVG root may use. A
# unitless length is CSS pixels, which are 1/96 inch.
_MM_PER_UNIT = {
    "mm": 1.0,
    "cm": 10.0,
    "in": 25.4,
    "pt": 25.4 / 72,
    "pc": 25.4 / 6,
    "px": 25.4 / 96,
    "": 25.4 / 96,
}
_LENGTH = re.compile(r"^\s*([-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?)\s*([a-z%]*)\s*$")


def is_svg(path: str | Path) -> bool:
    return Path(path).suffix.lower() == ".svg"


def _length(value: str | None) -> tuple[float, str] | None:
    """Split an SVG length into its number and unit, or None if unusable."""
    match = _LENGTH.match(value or "")
    if not match:
        return None
    unit = match.group(2)
    if unit == "%" or unit not in _MM_PER_UNIT:
        return None
    return float(match.group(1)), unit


def _mm(value: str | None) -> float | None:
    parsed = _length(value)
    return None if parsed is None else parsed[0] * _MM_PER_UNIT[parsed[1]]


def _user_units(value: str | None, default: float = 0.0) -> float:
    """A length in the coordinate system of the document, ignoring its unit."""
    parsed = _length(value)
    return default if parsed is None else parsed[0]


def _viewbox(root: ET.Element) -> tuple[float, float] | None:
    parts = re.split(r"[\s,]+", (root.get("viewBox") or "").strip())
    if len(parts) != 4:
        return None
    try:
        _, _, width, height = (float(p) for p in parts)
    except ValueError:
        return None
    return (width, height) if width > 0 and height > 0 else None


def _flag(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes")


@dataclass
class Geometry:
    width_mm: float
    height_mm: float
    dots_per_mm: float
    round: bool = False
    continuous: bool = False
    name: str = ""


def parse(source: str, stem: str = "") -> Geometry:
    """Read the physical size and dot resolution an SVG label asks for."""
    root = _parse_root(source)
    box = _viewbox(root)
    width_mm = _mm(root.get("width"))
    height_mm = _mm(root.get("height"))
    if (width_mm is None or height_mm is None) and box is not None:
        # No usable width/height: treat the viewBox as CSS pixels.
        width_mm = width_mm if width_mm is not None else box[0] * _MM_PER_UNIT["px"]
        height_mm = height_mm if height_mm is not None else box[1] * _MM_PER_UNIT["px"]
    if not width_mm or not height_mm:
        raise SystemExit(
            "SVG label needs a physical size: set width and height on <svg> "
            '(for example width="30mm" height="20mm"), or a viewBox'
        )
    # The viewBox fixes how many user units span the label, so rendering at that
    # many dots per millimeter keeps one user unit equal to one printer dot.
    dots_per_mm = box[0] / width_mm if box else 8.0
    title = root.find(f"{{{SVG_NS}}}title")
    name = (title.text or "").strip() if title is not None and title.text else stem
    return Geometry(
        width_mm=width_mm,
        height_mm=height_mm,
        dots_per_mm=dots_per_mm,
        round=_flag(root.get("data-mb-round")),
        continuous=_flag(root.get("data-mb-continuous")),
        name=name,
    )


def _parse_root(source: str) -> ET.Element:
    try:
        return ET.fromstring(source)
    except ET.ParseError as exc:
        raise SystemExit(f"not a readable SVG document: {exc}")


def templates(source: str) -> list[str]:
    """Every string in the document that carries template syntax."""
    found: list[str] = []
    for node in _parse_root(source).iter():
        candidates = [node.text, node.tail, *node.attrib.values()]
        for value in candidates:
            if value and "{{" in value and value not in found:
                found.append(value)
    return found


# --- generated content -----------------------------------------------------


def _box(node: ET.Element) -> tuple[float, float, float, float]:
    """The x/y/width/height a marked element reserves for generated content."""
    width = _user_units(node.get("width"))
    height = _user_units(node.get("height"))
    x = _user_units(node.get("x"))
    y = _user_units(node.get("y"))
    if width <= 0 or height <= 0:
        # Circles and ellipses describe the same box a different way.
        rx = _user_units(node.get("rx")) or _user_units(node.get("r"))
        ry = _user_units(node.get("ry")) or _user_units(node.get("r"))
        if rx > 0 and ry > 0:
            cx, cy = _user_units(node.get("cx")), _user_units(node.get("cy"))
            x, y, width, height = cx - rx, cy - ry, rx * 2, ry * 2
    return x, y, width, height


def _element_options(node: ET.Element, data: str) -> layout.Element:
    """Build a layout element from `data-mb-*` attributes on a marked node."""
    el: dict[str, Any] = {}
    for key, value in node.attrib.items():
        if key.startswith("data-mb-") and key != "data-mb-data":
            name = key[len("data-mb-") :]
            el["".join(part.title() if i else part for i, part in enumerate(name.split("-")))] = (
                value
            )
    kind = node.get("data-mb")
    el["type"] = kind
    el["qrData" if kind == "qr" else "barcodeData"] = data
    if "margin" in el:
        el["margin"] = int(_user_units(str(el["margin"])))
    return el


def _fill_markers(root: ET.Element) -> None:
    """Replace `data-mb` marked nodes with generated QR or barcode content."""
    from mbprint import svg as svgout

    parents = {child: parent for parent in root.iter() for child in parent}
    for node in list(root.iter()):
        kind = node.get("data-mb")
        if kind not in ("qr", "barcode"):
            continue
        parent = parents.get(node)
        if parent is None:
            continue
        data = (node.get("data-mb-data") or node.text or "").strip()
        index = list(parent).index(node)
        parent.remove(node)
        if not data:
            log.debug("dropping empty %s marker %r", kind, node.get("id", "?"))
            continue
        x, y, width, height = _box(node)
        if width <= 0 or height <= 0:
            raise SystemExit(
                f'the data-mb="{kind}" element needs x, y, width and height '
                "(or cx/cy plus r) in user units"
            )
        group = ET.Element(f"{{{SVG_NS}}}g")
        if node.get("transform"):
            group.set("transform", node.get("transform", ""))
        el = _element_options(node, data)
        if kind == "qr":
            svgout._qr(group, el, x, y, width, height)
        else:
            uri = svgout._raster_data_uri(el, max(1, round(width)), max(1, round(height)))
            ET.SubElement(
                group,
                f"{{{SVG_NS}}}image",
                {
                    "x": f"{x:g}",
                    "y": f"{y:g}",
                    "width": f"{width:g}",
                    "height": f"{height:g}",
                    "href": uri,
                },
            )
        parent.insert(index, group)


def substitute(source: str, record: layout.Record | None = None, decimal: str = ",") -> str:
    """Fill placeholders and generated content for one record."""
    record = record or {}
    root = copy.deepcopy(_parse_root(source))
    for node in root.iter():
        if node.text and "{{" in node.text:
            node.text = layout.substitute(node.text, record, decimal)
        if node.tail and "{{" in node.tail:
            node.tail = layout.substitute(node.tail, record, decimal)
        for key, value in list(node.attrib.items()):
            if "{{" in value:
                node.set(key, layout.substitute(value, record, decimal))
    _fill_markers(root)
    ET.register_namespace("", SVG_NS)
    return ET.tostring(root, encoding="unicode", xml_declaration=True) + "\n"


# --- rasterizing -----------------------------------------------------------

_BACKENDS = ("cairosvg", "resvg", "rsvg-convert", "inkscape")


def _cairosvg(source: str, width: int, height: int, base: Path | None) -> bytes | None:
    try:
        import cairosvg
    except ImportError:
        return None
    png: bytes = cairosvg.svg2png(
        bytestring=source.encode("utf-8"),
        output_width=width,
        output_height=height,
        url=str(base) + os.sep if base else None,
    )
    return png


@lru_cache(maxsize=8)
def _system_family(generic: str) -> str | None:
    """The concrete font family fontconfig picks for a generic family name."""
    fc = shutil.which("fc-match")
    if not fc:
        return None
    try:
        out = subprocess.run(
            [fc, "-f", "%{family[0]}", generic], capture_output=True, text=True, timeout=5
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return out or None


def _resvg_font_options() -> list[str]:
    """resvg has no fontconfig, so name the families `sans-serif` and friends map to."""
    options: list[str] = []
    for generic, flag in (
        ("sans-serif", "--sans-serif-family"),
        ("serif", "--serif-family"),
        ("monospace", "--monospace-family"),
    ):
        family = _system_family(generic)
        if family:
            options += [flag, family]
            if generic == "sans-serif":
                options += ["--font-family", family]
    return options


def _command(
    name: str, src: Path, out: Path, width: int, height: int, base: Path | None
) -> list[str] | None:
    binary = shutil.which(name)
    if not binary:
        return None
    if name == "resvg":
        command = [binary, "--width", str(width), "--height", str(height)]
        command += _resvg_font_options()
        if base:
            command += ["--resources-dir", str(base)]
        return [*command, str(src), str(out)]
    if name == "rsvg-convert":
        return [binary, "-w", str(width), "-h", str(height), "-o", str(out), str(src)]
    return [
        binary,
        "--export-type=png",
        f"--export-width={width}",
        f"--export-height={height}",
        f"--export-filename={out}",
        str(src),
    ]


def _run_backend(
    name: str, source: str, width: int, height: int, base: Path | None
) -> bytes | None:
    """Rasterize with one backend, or return None when it is not installed."""
    if name == "cairosvg":
        return _cairosvg(source, width, height, base)
    if shutil.which(name) is None:
        return None
    # Write the document beside the original so relative hrefs still resolve.
    directory = base if base and os.access(base, os.W_OK) else None
    with (
        tempfile.TemporaryDirectory(prefix=".mbprint-svg-") as work,
        tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", suffix=".svg", prefix=".mbprint-", dir=directory
        ) as handle,
    ):
        out = Path(work) / "label.png"
        src = Path(handle.name)
        handle.write(source)
        handle.flush()
        command = _command(name, src, out, width, height, base)
        if command is None:
            return None
        log.debug("rasterizing with %s", name)
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0 or not out.exists():
            raise SystemExit(
                f"{name} could not render the SVG label: "
                f"{(result.stderr or result.stdout or '').strip()}"
            )
        return out.read_bytes()


def rasterize(source: str, width: int, height: int, base: Path | None = None) -> Image.Image:
    """Render an SVG document to an RGB image of exactly `width` x `height`."""
    forced = os.environ.get("MBPRINT_SVG_RENDERER")
    candidates = (forced,) if forced else _BACKENDS
    if forced and forced not in _BACKENDS:
        raise SystemExit(f"MBPRINT_SVG_RENDERER must be one of: {', '.join(_BACKENDS)}")
    for name in candidates:
        png = _run_backend(str(name), source, width, height, base)
        if png is not None:
            image = Image.open(io.BytesIO(png)).convert("RGBA")
            if image.size != (width, height):
                image = image.resize((width, height), Image.Resampling.LANCZOS)
            canvas = Image.new("RGBA", (width, height), (255, 255, 255, 255))
            canvas.alpha_composite(image)
            return canvas.convert("RGB")
    raise SystemExit(
        "printing an SVG label needs an SVG renderer. Install one of: "
        "resvg, rsvg-convert, inkscape, or the cairosvg extra "
        "(uv sync --extra svg). Set MBPRINT_SVG_RENDERER to pick one."
    )


def render(
    label: layout.Label, record: layout.Record | None = None, scale: float = 1.0, decimal: str = ","
) -> Image.Image:
    """Render one record of an SVG label at `scale` times its dot resolution."""
    source = label.svg_source or ""
    width = max(1, round(label.width_px * scale))
    height = max(1, round(label.height_px * scale))
    base = label.source.parent if label.source else None
    return rasterize(substitute(source, record, decimal), width, height, base)
