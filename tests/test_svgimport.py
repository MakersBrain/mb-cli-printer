"""SVG to label.json conversion and round-trip metadata."""

from __future__ import annotations

import json

from mbprint import cli, layout, svg, svgimport


def test_svg_json_round_trip_uses_exact_metadata():
    label = layout.Label(
        width_mm=30,
        height_mm=20,
        dots_per_mm=8,
        round=True,
        continuous=False,
        name="Round trip",
        elements=[
            {
                "id": "name",
                "type": "text",
                "x": 5,
                "y": 7,
                "width": 100,
                "height": 24,
                "text": "{{name}}",
                "fontSize": 14,
            },
            {"type": "qr", "x": 130, "y": 20, "width": 70, "height": 70, "qrData": "{{qr}}"},
        ],
        fields=[{"name": "name", "label": "Name"}],
    )
    source = svg.render(label, {"name": "Rendered", "qr": "https://example.test"})
    converted, warnings = svgimport.convert(source)
    assert warnings == []
    assert converted["name"] == "Round trip"
    assert converted["widthMm"] == 30
    assert converted["round"] is True
    assert converted["elements"] == label.elements
    assert converted["fields"] == label.fields


def test_import_plain_svg_supported_subset():
    source = """<svg xmlns="http://www.w3.org/2000/svg" width="30mm" height="20mm"
      viewBox="0 0 240 160"><title>Imported</title>
      <rect x="10" y="20" width="30" height="40" fill="none" stroke="black"/>
      <g transform="translate(5 6)"><ellipse cx="80" cy="50" rx="10" ry="15" fill="black"/></g>
      <text x="100" y="40" font-size="12" font-weight="bold">Hello</text>
      <image x="120" y="60" width="20" height="30" href="data:image/png;base64,AA=="/>
      <rect data-mb="qr" data-mb-data="{{qr}}" x="150" y="60" width="40" height="40"/>
      <path d="M0 0L1 1"/>
    </svg>"""
    converted, warnings = svgimport.convert(source)
    assert converted["widthMm"] == 30
    assert converted["heightMm"] == 20
    assert converted["dotsPerMm"] == 8
    assert [element["type"] for element in converted["elements"]] == [
        "shape",
        "shape",
        "text",
        "image",
        "qr",
    ]
    assert converted["elements"][1]["x"] == 75
    assert converted["elements"][1]["y"] == 41
    assert converted["elements"][2]["bold"] is True
    assert converted["elements"][3]["imageData"].startswith("data:image/png")
    assert converted["elements"][4]["qrData"] == "{{qr}}"
    assert warnings == ["skipped path : path cannot be represented by label.json"]


def test_import_svg_bakes_translation_and_preserves_rotation():
    source = """<svg xmlns="http://www.w3.org/2000/svg" width="20mm" height="10mm"
      viewBox="10 20 160 80"><g transform="translate(5 6)">
      <rect x="15" y="24" width="20" height="10" transform="rotate(30 25 29)"/>
      </g></svg>"""
    converted, warnings = svgimport.convert(source)
    element = converted["elements"][0]
    assert warnings == []
    assert element["width"] == 20
    assert element["height"] == 10
    assert element["rotation"] == 30
    assert element["x"] == 10
    assert element["y"] == 10


def test_import_svg_cli_writes_label_json(tmp_path):
    source = tmp_path / "input.svg"
    source.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="10mm" height="5mm" '
        'viewBox="0 0 80 40"><rect x="1" y="2" width="3" height="4"/></svg>',
        encoding="utf-8",
    )
    destination = tmp_path / "converted.json"
    assert cli.main(["import-svg", str(source), "-o", str(destination)]) == 0
    converted = json.loads(destination.read_text(encoding="utf-8"))
    assert converted["widthMm"] == 10
    assert converted["elements"][0]["shapeType"] == "rectangle"
