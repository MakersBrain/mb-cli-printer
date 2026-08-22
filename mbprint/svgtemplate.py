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

import io
import os
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from functools import lru_cache
from pathlib import Path
from typing import Any

from PIL import Image

from mbprint import layout, svgimport
from mbprint.log import get_logger
from mbprint.svg import SVG_NS

log = get_logger(__name__)

ET.register_namespace("", SVG_NS)


def _user_units(value: str | None, default: float = 0.0) -> float:
    """A length in the coordinate system of the document, ignoring its unit."""
    match = svgimport._LENGTH.match(value or "")
    if match is None or match.group(2) not in svgimport._MM_PER_UNIT:
        return default
    return float(match.group(1))


def load(path: Path) -> layout.Label:
    """Read an SVG file as a label whose document is rendered whole."""
    try:
        source = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise SystemExit(f"label file not found: {path}")
    width_mm, height_mm, dots_per_mm, round_label, continuous, name = svgimport.geometry(
        svgimport.parse_root(source), path.stem
    )
    log.debug(
        "loaded %s: SVG template, %gmm x %gmm at %g dots/mm",
        path,
        width_mm,
        height_mm,
        dots_per_mm,
    )
    return layout.Label(
        width_mm=width_mm,
        height_mm=height_mm,
        dots_per_mm=dots_per_mm,
        round=round_label,
        continuous=continuous,
        name=name,
        source=path,
        svg_source=source,
    )


@lru_cache(maxsize=4)
def templates(source: str) -> list[str]:
    """Every string in the document that carries template syntax."""
    return list(
        dict.fromkeys(
            value
            for node in svgimport.parse_root(source).iter()
            for value in (node.text, node.tail, *node.attrib.values())
            if value and "{{" in value
        )
    )


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


def _camel(name: str) -> str:
    head, *rest = name.split("-")
    return head + "".join(part.title() for part in rest)


def _element_options(node: ET.Element, data: str) -> layout.Element:
    """Build a layout element from `data-mb-*` attributes on a marked node."""
    el: dict[str, Any] = {
        _camel(key[len("data-mb-") :]): value
        for key, value in node.attrib.items()
        if key.startswith("data-mb-") and key != "data-mb-data"
    }
    kind = node.get("data-mb")
    el["type"] = kind
    el["qrData" if kind == "qr" else "barcodeData"] = data
    if "margin" in el:
        el["margin"] = int(_user_units(el["margin"]))
    return el


def _generated(node: ET.Element, kind: str) -> ET.Element | None:
    """The group replacing one marked node, or None when it carries no data."""
    from mbprint import svg as svgout

    data = (node.get("data-mb-data") or node.text or "").strip()
    if not data:
        log.debug("dropping empty %s marker %r", kind, node.get("id", "?"))
        return None
    x, y, width, height = _box(node)
    if width <= 0 or height <= 0:
        raise SystemExit(
            f'the data-mb="{kind}" element needs x, y, width and height '
            "(or cx/cy plus r) in user units"
        )
    group = ET.Element(f"{{{SVG_NS}}}g")
    if transform := node.get("transform"):
        group.set("transform", transform)
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
    return group


def _fill_markers(root: ET.Element) -> None:
    """Replace `data-mb` marked nodes with generated QR or barcode content."""
    for parent in list(root.iter()):
        if not any(child.get("data-mb") in ("qr", "barcode") for child in parent):
            continue
        children: list[ET.Element] = []
        for child in parent:
            kind = child.get("data-mb")
            if kind not in ("qr", "barcode"):
                children.append(child)
                continue
            group = _generated(child, kind)
            if group is not None:
                children.append(group)
        parent[:] = children


def substitute(source: str, record: layout.Record | None = None, decimal: str = ",") -> str:
    """Fill placeholders and generated content for one record."""
    record = record or {}
    root = svgimport.parse_root(source)
    for node in root.iter():
        if node.text and "{{" in node.text:
            node.text = layout.substitute(node.text, record, decimal)
        if node.tail and "{{" in node.tail:
            node.tail = layout.substitute(node.tail, record, decimal)
        for key, value in list(node.attrib.items()):
            if "{{" in value:
                node.set(key, layout.substitute(value, record, decimal))
    _fill_markers(root)
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
    name: str, binary: str, src: Path, out: Path, width: int, height: int, base: Path | None
) -> list[str]:
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
    binary = shutil.which(name)
    if binary is None:
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
        log.debug("rasterizing with %s", name)
        result = subprocess.run(
            _command(name, binary, src, out, width, height, base), capture_output=True, text=True
        )
        if result.returncode != 0 or not out.exists():
            raise SystemExit(
                f"{name} could not render the SVG label: "
                f"{(result.stderr or result.stdout or '').strip()}"
            )
        return out.read_bytes()


def rasterize(source: str, width: int, height: int, base: Path | None = None) -> Image.Image:
    """Render an SVG document to an RGB image of exactly `width` x `height`."""
    forced = os.environ.get("MBPRINT_SVG_RENDERER")
    if forced and forced not in _BACKENDS:
        raise SystemExit(f"MBPRINT_SVG_RENDERER must be one of: {', '.join(_BACKENDS)}")
    for name in (forced,) if forced else _BACKENDS:
        png = _run_backend(name, source, width, height, base)
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
    width = max(1, round(label.width_px * scale))
    height = max(1, round(label.height_px * scale))
    base = label.source.parent if label.source else None
    return rasterize(substitute(label.svg_source or "", record, decimal), width, height, base)
