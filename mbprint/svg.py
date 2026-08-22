"""Exact-size SVG export for label layouts."""

from __future__ import annotations

import base64
import io
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from mbprint import layout

SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"
ET.register_namespace("", SVG_NS)
ET.register_namespace("xlink", XLINK_NS)


def _node(parent: ET.Element, tag: str, attributes: dict[str, Any]) -> ET.Element:
    return ET.SubElement(
        parent, f"{{{SVG_NS}}}{tag}", {key: str(value) for key, value in attributes.items()}
    )


def _paint(value: Any, default: str = "none") -> str:
    return default if value in (None, "", "transparent", "none") else str(value)


def _transform(el: layout.Element, x: float, y: float, w: float, h: float) -> str | None:
    rotation = float(el.get("rotation") or 0)
    return f"rotate({rotation:g} {x + w / 2:g} {y + h / 2:g})" if rotation else None


def _raster_data_uri(el: layout.Element, width: int, height: int) -> str:
    layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    if el.get("type") == "image":
        layout._render_image(layer, el, width, height, 0, 0)
    else:
        layout._render_barcode(layer, el, width, height, 0, 0)
    buffer = io.BytesIO()
    layer.save(buffer, "PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def _text(parent: ET.Element, el: layout.Element, x: float, y: float, w: float, h: float) -> None:
    background = _paint(el.get("background"))
    if background != "none":
        _node(parent, "rect", {"x": x, "y": y, "width": w, "height": h, "fill": background})
    value = str(el.get("text") or "")
    if not value.strip():
        return
    family, bold, italic, underline = layout._norm_text_style(el)
    size = float(el.get("fontSize") or 16)
    measure = ImageDraw.Draw(Image.new("RGB", (1, 1), "white"))
    if el.get("autoScale"):
        size = layout._auto_scale_size(measure, el, value, w, h, family, bold, italic, size)
    font = layout._load_font(family, bold, italic, round(size))
    lines = (
        value.split("\n") if el.get("noWrap") else layout._wrap(measure, value, font, max(1, w - 8))
    )
    line_height = size * 1.2
    total = len(lines) * line_height
    valign = el.get("verticalAlign") or "middle"
    if valign == "top":
        first_y = y + line_height / 2 + 2
    elif valign == "bottom":
        first_y = y + h - total + line_height / 2 - 2
    else:
        first_y = y + h / 2 - total / 2 + line_height / 2
    align = el.get("align") or el.get("textAlign") or "center"
    if align == "left":
        text_x, anchor = x + 4, "start"
    elif align == "right":
        text_x, anchor = x + w - 4, "end"
    else:
        text_x, anchor = x + w / 2, "middle"
    svg_family = {"sans": "sans-serif", "mono": "monospace"}.get(family, family)
    attributes = {
        "x": f"{text_x:g}",
        "y": f"{first_y:g}",
        "fill": el.get("color") or "black",
        "font-family": svg_family,
        "font-size": f"{size:g}",
        "font-weight": "bold" if bold else "normal",
        "font-style": "italic" if italic else "normal",
        "text-anchor": anchor,
        "dominant-baseline": "middle",
    }
    if underline:
        attributes["text-decoration"] = "underline"
    text = _node(parent, "text", attributes)
    for index, line in enumerate(lines):
        span = _node(
            text, "tspan", {"x": f"{text_x:g}", "dy": "0" if index == 0 else f"{line_height:g}"}
        )
        span.text = line


def _shape(parent: ET.Element, el: layout.Element, x: float, y: float, w: float, h: float) -> None:
    shape = el.get("shapeType") or el.get("shape") or "rectangle"
    fill = _paint(el.get("fill"))
    stroke = _paint(el.get("stroke"), "black" if fill == "none" else "none")
    common = {"fill": fill, "stroke": stroke, "stroke-width": float(el.get("strokeWidth") or 1)}
    if shape in ("ellipse", "circle"):
        _node(
            parent, "ellipse", common | {"cx": x + w / 2, "cy": y + h / 2, "rx": w / 2, "ry": h / 2}
        )
    elif shape in ("line", "hline"):
        _node(parent, "line", common | {"x1": x, "y1": y + h / 2, "x2": x + w, "y2": y + h / 2})
    elif shape == "vline":
        _node(parent, "line", common | {"x1": x + w / 2, "y1": y, "x2": x + w / 2, "y2": y + h})
    else:
        _node(parent, "rect", common | {"x": x, "y": y, "width": w, "height": h})


def _qr(parent: ET.Element, el: layout.Element, x: float, y: float, w: float, h: float) -> None:
    value = str(el.get("qrData") or "").strip()
    if not value:
        return
    import qrcode

    correction = {
        "L": qrcode.constants.ERROR_CORRECT_L,
        "M": qrcode.constants.ERROR_CORRECT_M,
        "Q": qrcode.constants.ERROR_CORRECT_Q,
        "H": qrcode.constants.ERROR_CORRECT_H,
    }.get(str(el.get("errorCorrection") or "M").upper(), qrcode.constants.ERROR_CORRECT_M)
    code = qrcode.QRCode(error_correction=correction, border=int(el.get("margin", 1)), box_size=1)
    code.add_data(value)
    code.make(fit=True)
    modules = code.get_matrix()
    count = len(modules)
    side = min(w, h)
    unit = side / count
    left, top = x + (w - side) / 2, y + (h - side) / 2
    commands = [
        f"M{left + column * unit:g},{top + row * unit:g}h{unit:g}v{unit:g}h-{unit:g}z"
        for row, values in enumerate(modules)
        for column, dark in enumerate(values)
        if dark
    ]
    _node(parent, "path", {"d": "".join(commands), "fill": "black"})


def render(label: layout.Label, record: layout.Record | None = None, decimal: str = ",") -> str:
    """Render one record as an exact-physical-size SVG document."""
    record = record or {}
    width, height = label.width_px, label.height_px
    root = ET.Element(
        f"{{{SVG_NS}}}svg",
        {
            "width": f"{label.width_mm:g}mm",
            "height": f"{label.height_mm:g}mm",
            "viewBox": f"0 0 {width} {height}",
            "version": "1.1",
        },
    )
    if label.name:
        title = _node(root, "title", {})
        title.text = label.name
    defs = _node(root, "defs", {})
    content = _node(root, "g", {})
    if label.round:
        clip = _node(defs, "clipPath", {"id": "label-clip", "clipPathUnits": "userSpaceOnUse"})
        _node(
            clip, "ellipse", {"cx": width / 2, "cy": height / 2, "rx": width / 2, "ry": height / 2}
        )
        content.set("clip-path", "url(#label-clip)")
    _node(content, "rect", {"x": 0, "y": 0, "width": width, "height": height, "fill": "white"})

    for index, original in enumerate(label.elements):
        el = dict(original)
        for key in ("text", "qrData", "barcodeData"):
            if el.get(key):
                el[key] = layout.substitute(str(el[key]), record, decimal)
        x, y = float(el.get("x") or 0), float(el.get("y") or 0)
        w, h = float(el.get("width") or 0), float(el.get("height") or 0)
        if w <= 0 or h <= 0:
            continue
        group = _node(content, "g", {})
        transform = _transform(el, x, y, w, h)
        if transform:
            group.set("transform", transform)
        if el.get("clipOverflow"):
            clip_id = f"element-clip-{index}"
            clip = _node(
                defs,
                "clipPath",
                {"id": clip_id, "clipPathUnits": "userSpaceOnUse"},
            )
            _node(clip, "rect", {"x": x, "y": y, "width": w, "height": h})
            group.set("clip-path", f"url(#{clip_id})")
        element_type = el.get("type")
        if element_type == "text":
            _text(group, el, x, y, w, h)
        elif element_type == "shape":
            _shape(group, el, x, y, w, h)
        elif element_type == "qr":
            _qr(group, el, x, y, w, h)
        elif element_type in ("image", "barcode"):
            uri = _raster_data_uri(el, max(1, round(w)), max(1, round(h)))
            _node(
                group,
                "image",
                {"x": x, "y": y, "width": w, "height": h, f"{{{XLINK_NS}}}href": uri},
            )
        else:
            content.remove(group)

    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="unicode", xml_declaration=True) + "\n"


def write(
    label: layout.Label,
    record: layout.Record,
    out_path: str | Path,
    decimal: str = ",",
) -> Path:
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(label, record, decimal), encoding="utf-8")
    return out
