"""Convert SVG documents into editable label.json layouts."""

from __future__ import annotations

import json
import math
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from mbprint.svg import XLINK_NS

METADATA_ID = "mbprint-label"

Matrix = tuple[float, float, float, float, float, float]
IDENTITY: Matrix = (1, 0, 0, 1, 0, 0)
_TRANSFORM = re.compile(r"([a-zA-Z]+)\s*\(([^)]*)\)")
_LENGTH = re.compile(r"^\s*([-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?)\s*([a-z%]*)\s*$")
_MM_PER_UNIT = {
    "mm": 1.0,
    "cm": 10.0,
    "in": 25.4,
    "pt": 25.4 / 72,
    "pc": 25.4 / 6,
    "px": 25.4 / 96,
    "": 25.4 / 96,
}


def _tag(node: ET.Element) -> str:
    return node.tag.rsplit("}", 1)[-1]


def _number(value: str | None, default: float = 0.0) -> float:
    match = re.match(r"\s*([-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?)", value or "")
    return default if match is None else float(match.group(1))


def _clean(value: float) -> int | float:
    rounded = round(value)
    return rounded if abs(value - rounded) < 1e-9 else round(value, 6)


def _viewbox(root: ET.Element) -> tuple[float, float, float, float] | None:
    parts = re.split(r"[\s,]+", (root.get("viewBox") or "").strip())
    if len(parts) != 4:
        return None
    try:
        x, y, width, height = (float(part) for part in parts)
    except ValueError:
        return None
    return (x, y, width, height) if width > 0 and height > 0 else None


def _millimetres(value: str | None) -> float | None:
    match = _LENGTH.match(value or "")
    if match is None or match.group(2) == "%" or match.group(2) not in _MM_PER_UNIT:
        return None
    return float(match.group(1)) * _MM_PER_UNIT[match.group(2)]


def _geometry(root: ET.Element, stem: str) -> tuple[float, float, float, bool, bool, str]:
    box = _viewbox(root)
    width_mm, height_mm = _millimetres(root.get("width")), _millimetres(root.get("height"))
    if box is not None:
        width_mm = width_mm if width_mm is not None else box[2] * _MM_PER_UNIT["px"]
        height_mm = height_mm if height_mm is not None else box[3] * _MM_PER_UNIT["px"]
    if not width_mm or not height_mm:
        raise SystemExit(
            "SVG label needs a physical size: set width and height on <svg> "
            '(for example width="30mm" height="20mm"), or a viewBox'
        )
    dots_per_mm = box[2] / width_mm if box else 8.0
    title = next((node for node in root if _tag(node) == "title"), None)
    name = (title.text or "").strip() if title is not None and title.text else stem
    truthy = {"1", "true", "yes"}
    return (
        width_mm,
        height_mm,
        dots_per_mm,
        (root.get("data-mb-round") or "").lower() in truthy,
        (root.get("data-mb-continuous") or "").lower() in truthy,
        name,
    )


def _multiply(left: Matrix, right: Matrix) -> Matrix:
    a, b, c, d, e, f = left
    g, h, i, j, k, m = right
    return (
        a * g + c * h,
        b * g + d * h,
        a * i + c * j,
        b * i + d * j,
        a * k + c * m + e,
        b * k + d * m + f,
    )


def _matrix(transform: str | None) -> Matrix:
    result = IDENTITY
    for name, body in _TRANSFORM.findall(transform or ""):
        values = [_number(part) for part in re.split(r"[\s,]+", body.strip()) if part]
        if name == "matrix" and len(values) == 6:
            current: Matrix = tuple(values)  # type: ignore[assignment]
        elif name == "translate" and values:
            current = (1, 0, 0, 1, values[0], values[1] if len(values) > 1 else 0)
        elif name == "scale" and values:
            current = (values[0], 0, 0, values[1] if len(values) > 1 else values[0], 0, 0)
        elif name == "rotate" and values:
            angle = math.radians(values[0])
            cosine, sine = math.cos(angle), math.sin(angle)
            current = (cosine, sine, -sine, cosine, 0, 0)
            if len(values) >= 3:
                cx, cy = values[1:3]
                current = _multiply(
                    _multiply((1, 0, 0, 1, cx, cy), current),
                    (1, 0, 0, 1, -cx, -cy),
                )
        else:
            raise ValueError(f"unsupported transform {name}({body})")
        result = _multiply(result, current)
    return result


def _point(matrix: Matrix, x: float, y: float) -> tuple[float, float]:
    a, b, c, d, e, f = matrix
    return a * x + c * y + e, b * x + d * y + f


def _box(
    matrix: Matrix, x: float, y: float, width: float, height: float
) -> tuple[float, float, float, float, float]:
    """Transform a box into label geometry plus clockwise rotation."""
    a, b, c, d, _, _ = matrix
    scale_x, scale_y = math.hypot(a, b), math.hypot(c, d)
    if scale_x == 0 or scale_y == 0 or abs(a * c + b * d) > 1e-6:
        raise ValueError("skewed or degenerate geometry")
    center_x, center_y = _point(matrix, x + width / 2, y + height / 2)
    out_width, out_height = width * scale_x, height * scale_y
    rotation = math.degrees(math.atan2(b, a))
    return (
        center_x - out_width / 2,
        center_y - out_height / 2,
        out_width,
        out_height,
        rotation,
    )


def _style(node: ET.Element, inherited: dict[str, str]) -> dict[str, str]:
    result = dict(inherited)
    for part in (node.get("style") or "").split(";"):
        key, separator, value = part.partition(":")
        if separator:
            result[key.strip()] = value.strip()
    for key in (
        "fill",
        "stroke",
        "stroke-width",
        "font-family",
        "font-size",
        "font-weight",
        "font-style",
        "text-decoration",
        "text-anchor",
    ):
        if node.get(key) is not None:
            result[key] = node.get(key, "")
    return result


def _base_element(
    node: ET.Element, matrix: Matrix, x: float, y: float, width: float, height: float
) -> dict[str, Any]:
    bx, by, bw, bh, rotation = _box(matrix, x, y, width, height)
    element: dict[str, Any] = {
        "x": _clean(bx),
        "y": _clean(by),
        "width": _clean(bw),
        "height": _clean(bh),
    }
    if node.get("id"):
        element["id"] = node.get("id")
    if abs(rotation) > 1e-9:
        element["rotation"] = _clean(rotation)
    return element


def _semantic(node: ET.Element, matrix: Matrix) -> dict[str, Any] | None:
    kind = node.get("data-mb")
    if kind not in ("qr", "barcode"):
        return None
    x = _number(node.get("x"))
    y = _number(node.get("y"))
    width = _number(node.get("width"))
    height = _number(node.get("height"))
    if width <= 0 or height <= 0:
        radius_x = _number(node.get("rx")) or _number(node.get("r"))
        radius_y = _number(node.get("ry")) or _number(node.get("r"))
        x = _number(node.get("cx")) - radius_x
        y = _number(node.get("cy")) - radius_y
        width, height = radius_x * 2, radius_y * 2
    if width <= 0 or height <= 0:
        raise ValueError(f'data-mb="{kind}" needs a positive width and height')
    element = _base_element(node, matrix, x, y, width, height)
    element["type"] = kind
    element["qrData" if kind == "qr" else "barcodeData"] = (
        node.get("data-mb-data") or "".join(node.itertext())
    ).strip()
    for attribute, key in (
        ("data-mb-margin", "margin"),
        ("data-mb-error-correction", "errorCorrection"),
        ("data-mb-barcode-format", "barcodeFormat"),
    ):
        if node.get(attribute) is not None:
            element[key] = node.get(attribute)
    return element


def _convert_node(
    node: ET.Element,
    parent_matrix: Matrix,
    inherited_style: dict[str, str],
    elements: list[dict[str, Any]],
    warnings: list[str],
    root_size: tuple[float, float],
) -> None:
    tag = _tag(node)
    if tag in ("defs", "metadata", "title", "desc", "style"):
        return
    try:
        matrix = _multiply(parent_matrix, _matrix(node.get("transform")))
    except ValueError as exc:
        warnings.append(f"skipped {tag} {node.get('id', '')}: {exc}")
        return
    style = _style(node, inherited_style)
    try:
        semantic = _semantic(node, matrix)
        if semantic is not None:
            elements.append(semantic)
            return
        if tag in ("svg", "g", "a", "switch"):
            for child in node:
                _convert_node(child, matrix, style, elements, warnings, root_size)
            return
        if tag == "rect":
            x, y = _number(node.get("x")), _number(node.get("y"))
            width, height = _number(node.get("width")), _number(node.get("height"))
            # Ignore the white page background emitted by mbprint and many editors.
            if (
                x == 0
                and y == 0
                and width == root_size[0]
                and height == root_size[1]
                and style.get("fill", "").lower() in ("white", "#fff", "#ffffff")
            ):
                return
            element = _base_element(node, matrix, x, y, width, height)
            element |= {"type": "shape", "shapeType": "rectangle"}
        elif tag in ("ellipse", "circle"):
            radius_x = _number(node.get("rx")) or _number(node.get("r"))
            radius_y = _number(node.get("ry")) or _number(node.get("r"))
            x, y = _number(node.get("cx")) - radius_x, _number(node.get("cy")) - radius_y
            element = _base_element(node, matrix, x, y, radius_x * 2, radius_y * 2)
            element |= {"type": "shape", "shapeType": "ellipse"}
        elif tag == "line":
            x1, y1 = _number(node.get("x1")), _number(node.get("y1"))
            x2, y2 = _number(node.get("x2")), _number(node.get("y2"))
            if x1 != x2 and y1 != y2:
                raise ValueError("diagonal lines cannot be represented by label.json")
            element = _base_element(
                node, matrix, min(x1, x2), min(y1, y2), abs(x2 - x1) or 1, abs(y2 - y1) or 1
            )
            element |= {"type": "shape", "shapeType": "vline" if x1 == x2 else "line"}
        elif tag == "text":
            text = "".join(node.itertext()).strip()
            if not text:
                return
            size = _number(style.get("font-size"), 16)
            x, baseline = _number(node.get("x")), _number(node.get("y"))
            width = _number(node.get("width"), max(size, root_size[0] - x))
            height = _number(node.get("height"), size * 1.2)
            anchor = style.get("text-anchor", "start")
            left = x - width if anchor == "end" else x - width / 2 if anchor == "middle" else x
            element = _base_element(node, matrix, left, baseline - height * 0.8, width, height)
            element |= {
                "type": "text",
                "text": text,
                "fontSize": _clean(size),
                "align": {"start": "left", "middle": "center", "end": "right"}.get(anchor, "left"),
            }
            if style.get("fill") not in (None, "", "black", "#000", "#000000"):
                element["color"] = style["fill"]
            if style.get("font-family"):
                element["fontFamily"] = style["font-family"]
            if style.get("font-weight") in ("bold", "600", "700", "800", "900"):
                element["bold"] = True
            if style.get("font-style") in ("italic", "oblique"):
                element["italic"] = True
            if "underline" in style.get("text-decoration", ""):
                element["underline"] = True
        elif tag == "image":
            element = _base_element(
                node,
                matrix,
                _number(node.get("x")),
                _number(node.get("y")),
                _number(node.get("width")),
                _number(node.get("height")),
            )
            href = node.get("href") or node.get(f"{{{XLINK_NS}}}href")
            element |= {"type": "image", "imageData": href or ""}
        elif tag in ("path", "polygon", "polyline", "use", "foreignObject"):
            raise ValueError(f"{tag} cannot be represented by label.json")
        else:
            warnings.append(f"skipped unsupported SVG element <{tag}>")
            return
    except ValueError as exc:
        warnings.append(f"skipped {tag} {node.get('id', '')}: {exc}")
        return

    if tag not in ("text", "image"):
        fill = style.get("fill")
        stroke = style.get("stroke")
        if fill not in (None, "", "none", "transparent"):
            element["fill"] = fill
        if stroke not in (None, "", "none", "transparent"):
            element["stroke"] = stroke
        if style.get("stroke-width"):
            element["strokeWidth"] = _clean(_number(style["stroke-width"], 1))
    elements.append(element)


def convert(source: str, stem: str = "") -> tuple[dict[str, Any], list[str]]:
    """Convert SVG source to label.json data, preferring exact embedded metadata."""
    try:
        root = ET.fromstring(source)
    except ET.ParseError as exc:
        raise SystemExit(f"not a readable SVG document: {exc}")
    metadata = next(
        (
            node
            for node in root.iter()
            if _tag(node) == "metadata" and node.get("id") == METADATA_ID
        ),
        None,
    )
    if metadata is not None and metadata.text:
        try:
            value = json.loads(metadata.text)
        except ValueError as exc:
            raise SystemExit(f"mbprint SVG metadata is not valid JSON: {exc}")
        if not isinstance(value, dict):
            raise SystemExit("mbprint SVG metadata must contain a JSON object")
        return value, []

    width_mm, height_mm, dots_per_mm, round_label, continuous, name = _geometry(root, stem)
    viewbox = _viewbox(root)
    root_width = viewbox[2] if viewbox else width_mm * dots_per_mm
    root_height = viewbox[3] if viewbox else height_mm * dots_per_mm
    origin = (1, 0, 0, 1, -viewbox[0], -viewbox[1]) if viewbox else IDENTITY
    elements: list[dict[str, Any]] = []
    warnings: list[str] = []
    _convert_node(root, origin, {}, elements, warnings, (root_width, root_height))
    return (
        {
            "name": name or stem,
            "widthMm": _clean(width_mm),
            "heightMm": _clean(height_mm),
            "dotsPerMm": _clean(dots_per_mm),
            "round": round_label,
            "continuous": continuous,
            "elements": elements,
            "fields": [],
        },
        warnings,
    )


def write(source_path: str | Path, out_path: str | Path) -> tuple[Path, list[str]]:
    source = Path(source_path)
    try:
        text = source.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise SystemExit(f"SVG file not found: {source}")
    data, warnings = convert(text, source.stem)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return out, warnings
