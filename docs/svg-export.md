# SVG export

`mbprint svg` renders `label.json` layouts as exact-size SVG files. One file is
written for each expanded CSV record or copy.

```sh
# One record to one named file
mbprint svg -l label.json --data name="Sample" -o sample.svg

# A CSV batch to a directory of numbered files
mbprint svg -l label.json -c inventory.csv -o vectors/
```

The root SVG carries physical `width` and `height` values in millimetres plus a
`viewBox` in the layout's dot coordinate system. It therefore preserves the
same label geometry without requiring a printer model or DPI selection.

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

## Records, fields, and copies

The SVG command accepts the same layout/data options as `print`, `pdf`, and
`preview`: `--csv`, `--data`, `--map`, `--filter`, `--limit`, `--copies`, and
`--copies-from`. Required-field checks also run before writing files.

When one record is rendered and `--out` ends in `.svg`, that exact path is
used. Otherwise `--out` is a directory and files are named with a stable
numeric prefix, such as `001-AG-EX-0001.svg`.

## SVG versus PDF

Use SVG when individual labels should remain editable or be consumed by a
vector workflow. Use exact-size PDF for a portable multi-page label batch, or
`--sheet` PDF for labels tiled on office paper. Printers still consume raster
protocols; SVG export does not send vector commands directly to Brother or
Phomemo hardware.
