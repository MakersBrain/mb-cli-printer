# CLI reference

`mbprint COMMAND --help` is the authoritative reference. This page groups the
options by purpose.

## Global options

These work before or after every subcommand.

| Option | Meaning |
|---|---|
| `-v`, `-vv` (`--verbose`) | DEBUG protocol commands; TRACE every write |
| `-q` (`--quiet`) | warnings and errors only |
| `--log-file PATH` | append a full TRACE log regardless of console verbosity |
| `--plain` | disable Rich/color output and use plain terminal reporting |

At normal verbosity, an interactive batch print uses Rich progress bars when
the `tui` extra is installed. Without Rich, with `--plain`, or with `NO_COLOR`,
it uses a single rewritten terminal line. Redirected, quiet, or verbose output
uses ordinary log lines instead.

## Source options

Layout and data options are accepted by `print`, `pdf`, `svg`, `preview`, and
`fields`.

| Option | Meaning |
|---|---|
| `-l`, `--label PATH` | layout file; defaults to `label.json` or config `label` |
| `-c`, `--csv PATH` | CSV records; without it, build one record from `--data` |
| `--data KEY=TEMPLATE` | define a field; repeatable and evaluated in order |
| `--set KEY=VALUE` | alias for `--data`, intended for constants |
| `--map FIELD=COLUMN` | bind a normalized field to a CSV column |
| `--force`, `--ignore-missing` | continue despite empty required fields |
| `--decimal C` | decimal separator for `num` and normalized prices; default `,` |
| `--filter COLUMN=VALUE` | keep matching rows; repeatable |
| `--limit N` | keep the first N records |
| `--copies N` | fixed copies per record |
| `--copies-from COLUMN` | copy count per record from a column |

`fields` currently uses the layout, CSV, data, map, and decimal options. It
accepts the other shared flags but reports the complete, unexpanded CSV rather
than applying filter, limit, or copy selection.

## Render options

`print`, `print-pdf`, `pdf`, and `preview` accept:

| Option | Meaning |
|---|---|
| `--dither MODE` | `auto`, `none`, `threshold`, `floyd-steinberg`, `atkinson`, or `ordered` |

## Printer and media options

These are accepted by `print`, `print-pdf`, `status`, and `test` unless noted
otherwise.

| Option | Meaning |
|---|---|
| `-m`, `--model ID` | model ID from `mbprint printers`; default is auto-detection |
| `--density 1-8` | thermal density |
| `--feed N` | dots fed after a label |
| `--speed N` | print speed for M110 and TSPL |
| `--continuous` | continuous media without gap detection |
| `--align left\|center\|right` | label position across a Phomemo head |
| `--offset-x N`, `--offset-y N` | alignment offsets in dots |
| `--gap-mm N`, `--tspl-offset-mm N` | TSPL gap and offset |
| `--media ID` | Brother DK roll |
| `--no-cut`, `--cut-every N` | Brother cutting behavior |
| `--no-compress` | send Brother raster lines uncompressed |

`preview --raster` separately accepts model, device, media, alignment, and
offset options.

## Transport options

These are accepted by `print`, `print-pdf`, `status`, and `test`.

| Option | Meaning |
|---|---|
| `-t`, `--transport KIND` | `ble`, `bluetooth`, `tcp`, `serial`, `usb`, or `file` |
| `--device NAME`, `--address MAC` | BLE selection; address also selects classic Bluetooth |
| `--host HOST`, `--tcp-port N` | network printer; port defaults to 9100 |
| `--rfcomm-channel N` | classic Bluetooth channel; default 1 |
| `--port PATH`, `--baud N` | serial or RFCOMM device; baud defaults to 115200 |
| `--usb-vid ID`, `--usb-pid ID` | USB vendor/product selection |
| `--usb-serial SERIAL` | select one USB printer by stable serial number |
| `--usb-bus N`, `--usb-address N` | select one USB connection by its current location |
| `--usb-interface N`, `--usb-alt N` | select a USB interface and alternate setting |
| `--mtu N` | cap the transport payload size |
| `--chunk-delay MS` | override inter-chunk pacing |
| `-o`, `--out PATH` | capture bytes for the file transport |

`print` and `print-pdf` additionally accept `--dry-run`; with `--out`, they
capture the bytes that would have been sent.

## Direct PDF printing

```text
mbprint print-pdf PDF [--pages 1,3-5] [--copies N] [--fit]
                  [--laposte-format FORMAT] [printer options]
```

Each selected PDF page is one label. Pages are rasterized at the selected
printer's native DPI and sent through the same protocol and transport as
`print`, including Brother QL and Phomemo models. All selected pages must have
the same physical size. On Brother QL printers the page must match the loaded
or selected DK media; a transposed page
is rotated automatically. `--fit` explicitly permits scaling a mismatched page.
`--dry-run --out job.bin` captures the resulting printer byte stream.

La Poste's Mon Timbre en Ligne downloads are A4 sheets rather than one-label
pages. `--laposte-format` extracts occupied stamps and prints them individually.
It accepts `SHEET`/`L24A_SHEET` for *Feuille blanche A4*, plus the adhesive-sheet
codes `L24A`, `L24B`, `L21A`, `L18A`, `L16A`, `L14A`, and `L12A`. Use the code
selected on La Poste's **Options d'impression** page.

## Extract La Poste labels to PDF

```text
mbprint extract-pdf PDF --laposte-format FORMAT [-o labels.pdf]
                    [--pages 1,3-5] [--dpi N | --model ID | --device NAME]
```

This performs the same occupied-slot extraction without printing. The output
contains one 63.5 x 33.9 mm stamp per PDF page. `--pages` selects source A4
pages. `--model` or `--device` uses that printer's native DPI; an explicit
`--dpi` takes precedence. The default is 254 DPI. Page dimensions remain exact
at every resolution.

## PDF options

| Option | Meaning |
|---|---|
| `-o`, `--out PATH` | output path; default `labels.pdf` |
| `-m`, `--model ID`, `--device NAME` | render at the selected model's native DPI |
| `--sheet a4\|a5\|letter\|legal` | tile onto a paper sheet |
| `--margin MM`, `--gap MM` | sheet margin and spacing |
| `--columns N`, `--rows N` | force a grid |
| `--no-marks` | omit cut marks |
| `--bilevel` | apply print halftoning |
| `--scale N` | explicit render scale, overriding automatic model DPI |

Without `--model` or `--device`, the layout's `dotsPerMm` controls PDF artwork
resolution. Selecting a model uses its exact native DPI and does not change the
physical PDF page size. See [PDF generation and direct printing](pdf-workflows.md)
for the complete workflow and media-safety behavior.

## Preview options

| Option | Meaning |
|---|---|
| `-o`, `--out DIR` | output directory; default `preview` |
| `--raster` | fit and render the printer raster including head width |
| `--bilevel` | apply halftoning without head fitting |
| `--printer-scale` | render at the selected model's DPI |
| `--zoom N` | enlarge the PNG |

## SVG options

```text
mbprint svg [source options] [--out PATH_OR_DIRECTORY]
```

One exact-size SVG is written per expanded record. A single record may target a
specific `.svg` path; batches use an output directory. Text, shapes, QR codes,
rotation, and clipping remain vectors. Images and barcodes are embedded PNG
data. See [SVG export and import](svg-export.md) for fidelity, round-trip
behavior, third-party SVG mapping, and naming details.

An SVG file is also accepted as a layout: `-l label.svg` fills the document's
`{{placeholders}}` per record and rasterizes it for `print`, `pdf`, and
`preview`, which needs an SVG renderer (cairosvg, resvg, rsvg-convert, or
inkscape). See [SVG labels as templates](svg-templates.md).

## Other commands

```text
mbprint printers [--json]
mbprint scan [--timeout SECONDS]
mbprint import-svg SVG [-o label.json]
mbprint config list
mbprint config get KEY
mbprint config set KEY VALUE
mbprint config unset KEY
mbprint wifi scan [--scan-wait SECONDS] [--raw] [transport options]
mbprint wifi status [--raw] [transport options]
mbprint wifi [configure] --ssid NAME [--password-stdin] [transport options]
mbprint usb-list [--usb-vid ID] [--usb-pid ID]
mbprint usb-info [--usb-serial SERIAL | --usb-bus N --usb-address N]
mbprint usb-report [--json] [--out PATH] [--usb-serial SERIAL | --usb-bus N --usb-address N]
```

Supported scalar config keys are `model`, `transport`, `address`, `device`,
`density`, `feed`, `speed`, `offset_x`, `offset_y`, `align`, `dither`,
`continuous`, `gap_mm`, `tspl_offset_mm`, `label`, `media`, and `host`.
Derived templates use `data.<field>` keys.

`import-svg` converts an SVG document to an editable JSON layout. The `-o` path
defaults to `label.json` and is overwritten if it exists. An mbprint-exported
SVG restores its embedded source layout exactly. Other SVGs use the supported
element mapping and report skipped features described in
[SVG export and import](svg-export.md#import-svg-to-labeljson).

## Logging and tracing

The default INFO output identifies the printer, transport, and label progress.
`-v` logs decoded protocol commands and their bytes. `-vv` adds a TRACE entry
for each transport write, including raster chunks, and enables Bleak's debug
output.

`--log-file` always records TRACE detail even when console output is quiet:

```sh
mbprint print -l label.json -c inventory.csv \
  --log-file /tmp/print.log
```
