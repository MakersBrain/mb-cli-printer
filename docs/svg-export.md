# SVG export and import

`mbprint` converts layouts in both directions:

```text
label.json --mbprint svg--------> exact-size SVG
SVG        --mbprint import-svg-> editable label.json
```

The forward conversion is exact for label geometry and retains supported
elements as vectors. The reverse conversion is exact for SVGs exported by
`mbprint` while their metadata remains intact; SVGs created elsewhere use a
documented, best-effort mapping.

## Export label.json to SVG

`mbprint svg` writes one SVG for each expanded CSV record or copy.

```sh
# One record to one named file
mbprint svg -l label.json --data name="Sample" -o sample.svg

# A CSV batch to a directory of numbered files
mbprint svg -l label.json -c inventory.csv -o vectors/
```

The root SVG carries physical `width` and `height` values in millimetres plus a
`viewBox` in the layout's dot coordinate system. It therefore preserves the
same label geometry without requiring a printer model or DPI selection. The
visible artwork contains the values from the selected record; embedded
round-trip metadata retains the original templates and editable element
definitions.

## Element fidelity

- Text remains SVG `<text>` with native spans, alignment, font style,
  underlining, wrapping, backgrounds, and template substitution.
- Rectangles, ellipses, and lines remain native vector shapes.
- QR modules are emitted as a vector path.
- Rotation and overflow clipping remain SVG transforms and clip paths.
- Round labels use a label-level vector clip path.
- Image and barcode elements use embedded PNG data inside the self-contained
  SVG. Their brightness, contrast, sizing, and human-readable barcode text
  follow the existing Pillow renderer.

SVG font rendering ultimately uses the fonts available in the viewer. The
export chooses compatible generic families (`sans-serif`, `serif`, or
`monospace`), but line metrics can vary slightly between applications. Convert
text to paths in a design tool when exact cross-machine font outlines are more
important than editable text.

## Import SVG to label.json

```sh
mbprint import-svg label.svg -o label.json
```

The output path defaults to `label.json`. Existing output is overwritten.

### Exact mbprint round trips

SVGs exported by `mbprint` contain the original editable layout as JSON inside
an SVG `<metadata id="mbprint-label">` element. Importing one restores:

- label dimensions, dot resolution, name, and media flags;
- original text, QR, and barcode templates rather than one rendered record;
- element coordinates, styles, rotation, and clipping settings;
- the layout's field definitions.

The metadata does not change how browsers, printers, or design tools display
the SVG. It can contain the original layout's text and embedded image data, so
inspect or remove it before publishing an SVG when those source values should
not be shared.

The metadata is authoritative. If an exported SVG is visually edited while its
original `mbprint-label` metadata remains, `import-svg` restores the original
JSON and does not infer those visual edits. Remove that metadata element first
when the visible SVG artwork should be imported with the third-party mapping
below; that mapping is necessarily less precise.

### Importing SVGs from other tools

The SVG must have either physical `width` and `height` attributes or a valid
`viewBox`. Unitless dimensions and a bare `viewBox` follow the SVG standard's
96-pixel-per-inch size. When both physical dimensions and a `viewBox` exist,
their ratio becomes `dotsPerMm` in the JSON layout.

| SVG content | label.json result |
|---|---|
| `<text>` | editable text with inferred bounds and basic family, size, weight, style, underline, color, and anchor |
| `<rect>` | rectangle shape |
| `<ellipse>`, `<circle>` | ellipse shape |
| horizontal or vertical `<line>` | line shape |
| `<image>` | image element; its data URI or `href` is retained |
| `data-mb="qr"` | QR element using `data-mb-data` or node text |
| `data-mb="barcode"` | barcode element using `data-mb-data` or node text |
| translate, scale, rotate, orthogonal `matrix()` | baked geometry and rotation |
| paths, polygons, polylines, diagonal lines, skew, `<use>`, foreign objects | skipped with a warning |

CSS classes and external stylesheets are not resolved. Put important paint and
font properties directly on an element or in its inline `style` attribute.
Text laid out with complex `<tspan>` positioning is flattened to one editable
text element. A relative image `href` remains relative; use a data URI when the
converted JSON needs to be self-contained.

Unsupported elements do not abort the conversion: `import-svg` writes the
representable layout, logs each skipped feature, prints a warning count, and
exits successfully. Review the warnings and preview the JSON before printing:

```sh
mbprint import-svg artwork.svg -o imported.json
mbprint preview -l imported.json -o preview/
```

## Records, fields, and copies

The SVG command accepts the same layout/data options as `print`, `pdf`, and
`preview`: `--csv`, `--data`, `--map`, `--filter`, `--limit`, `--copies`, and
`--copies-from`. Required-field checks also run before writing files.

When one record is rendered and `--out` ends in `.svg`, that exact path is
used. Otherwise `--out` is a directory and files are named with a stable
numeric prefix, such as `001-AG-EX-0001.svg`.

## The other direction

An SVG file can also be a layout: `mbprint print -l label.svg` fills its
`{{placeholders}}` per record and rasterizes the document as drawn. See
[SVG labels as templates](svg-templates.md).

## Choosing SVG, JSON, or PDF

Use SVG when individual labels should remain editable or be consumed by a
vector workflow. Use `label.json` when the layout should remain editable in
mbprint's element model and retain data templates. Use exact-size PDF for a
portable multi-page label batch, or `--sheet` PDF for labels tiled on office
paper. Printers still consume raster protocols; SVG export does not send vector
commands directly to Brother or Phomemo hardware.
