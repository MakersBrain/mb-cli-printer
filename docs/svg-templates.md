# SVG labels as templates

Any command that takes `-l`/`--label` also accepts an SVG file. Draw the label
in Inkscape, Illustrator, or by hand, leave `{{placeholders}}` in the text, and
`mbprint` fills them per record:

```sh
mbprint fields  -l label.svg -c inventory.csv
mbprint preview -l label.svg -c inventory.csv -o preview/
mbprint pdf     -l label.svg -c inventory.csv -o labels.pdf
mbprint print   -l label.svg -c inventory.csv --model m110
```

Unlike a `label.json` layout, which `mbprint` rebuilds element by element, an
SVG layout stays the document you drew: paths, gradients, clip paths, masks and
filters all survive, because the finished file is handed to a real SVG renderer.
The cost is that renderer — see [Renderers](#renderers) below.

## Label size

The root `<svg>` decides the physical label size and the dot grid:

```xml
<svg xmlns="http://www.w3.org/2000/svg"
     width="30mm" height="20mm" viewBox="0 0 240 160">
```

- `width` and `height` give the physical size. `mm`, `cm`, `in`, `pt`, `pc`, and
  `px` are understood; a unitless number is CSS pixels (1/96 inch).
- The `viewBox` sets how many user units span the label. Here 240 units cover
  30 mm, which is 8 dots per millimetre — the same grid `label.json` uses, so
  one user unit is one printer dot.
- With a `viewBox` but no width/height, the viewBox is read as CSS pixels.
- Without either, the file is rejected: a label needs a physical size.

`<title>` names the label in `mbprint fields`. Two optional attributes on the
root mirror the `label.json` flags: `data-mb-round="true"` for a round die-cut
label and `data-mb-continuous="true"` for continuous stock.

Nothing else has to change when the printer changes. `mbprint` rasterizes at the
selected model's resolution, so the same file prints at 203 dpi on a Phomemo and
at 300 dpi on a Brother QL.

## Placeholders

`{{name}}`, filters such as `{{price|num:2}}`, and optional `[[ / {{batch}}]]`
segments work exactly as they do in `label.json`, and are documented in
[Data, templates, and filters](data-and-templates.md).

Placeholders are filled in text nodes and in attribute values, so they can drive
artwork as well as words:

```xml
<text x="8" y="30" font-size="18">{{name|truncate:22}}</text>
<text x="8" y="60" font-size="26" font-weight="bold">{{price_short}}€</text>
<text x="8" y="120" font-size="12">{{ref}}[[ / {{batch}}]]</text>
<rect x="0" y="150" width="240" height="10" fill="{{color|default:black}}"/>
```

Values are XML-escaped on the way in, so an `&` or `<` in a product name is
safe. `mbprint fields -l label.svg` lists what a file needs, and the
missing-field gate that guards `print`, `pdf`, and `preview` applies unchanged.

## QR codes and barcodes

An SVG cannot draw its own QR code, so mark a box and `mbprint` replaces it:

```xml
<rect data-mb="qr" data-mb-data="{{qr}}" x="140" y="40" width="90" height="90"/>
<rect data-mb="barcode" data-mb-data="{{sku}}"
      data-mb-symbology="code128" x="8" y="100" width="120" height="40"/>
```

- `data-mb` selects `qr` or `barcode`.
- `data-mb-data` is the template for its content; the element's text is used
  when the attribute is absent. An empty result drops the element, which makes
  a QR code optional through `[[...]]` or an empty field.
- The element's `x`, `y`, `width`, and `height` reserve the area, in user units.
  `cx`/`cy` with `r` or `rx`/`ry` work too, so a circle can hold a QR code.
- Any other `data-mb-*` attribute becomes an option for the generator, named in
  camel case: `data-mb-error-correction="H"`, `data-mb-margin="2"`,
  `data-mb-symbology="ean13"`, `data-mb-write-text="false"`.
- A `transform` on the marked element is kept, so a rotated placeholder yields a
  rotated code.

QR codes are generated as a vector path. Barcodes come from the same Pillow
renderer the JSON layouts use and are embedded as PNG data, and need the
`barcode` extra (`uv sync --extra barcode`).

The marker keeps its place in the drawing order, so anything layered over the
box in the design stays layered over the generated code.

## Renderers

Rasterizing needs an SVG renderer. The first one available is used:

| Backend | Install |
|---|---|
| `cairosvg` | `uv sync --extra svg` (needs system cairo) |
| `resvg` | in the nix dev shell; otherwise `cargo install resvg` or a distro package |
| `rsvg-convert` | librsvg |
| `inkscape` | Inkscape 1.x |

Set `MBPRINT_SVG_RENDERER` to one of those names to pin a backend. With none
installed, the SVG commands stop with an error naming these options; `mbprint
svg` and `mbprint fields` still work, because neither rasterizes.

Fonts are resolved by the renderer, not by `mbprint`. `resvg` has no fontconfig,
so `mbprint` tells it which real families `sans-serif`, `serif`, and `monospace`
mean on this machine. Even so, a font installed here may be missing on another
machine: converting text to paths in the design tool makes a template render
identically everywhere, at the price of losing `{{placeholders}}` in the
converted text. Convert the decorative text and leave the templated text live.

Relative `href`s — an external logo next to the label file — resolve against the
label's own directory. Embedding the image as a `data:` URI keeps the template
self-contained.

## Round trips with `mbprint svg`

`mbprint svg` is the other direction: it exports `label.json` layouts as
exact-size SVG, described in [SVG export](svg-export.md). Pointed at an SVG
layout it fills the original document instead of rebuilding one, so

```sh
mbprint svg -l label.svg -c inventory.csv -o vectors/
```

writes the finished artwork per record — useful to inspect what the renderer
will be given, or to hand a filled label to another vector workflow.

A `label.json` layout can also be exported once with `mbprint svg` and then
refined in a design tool. Keep the placeholders intact in the exported text and
the result works as a template.
