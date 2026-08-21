# mb-cli-printer

Command line printing for Phomemo label printers, in Python.

It implements the printer protocols and transports from
[transcriptionstream/phomymo](https://github.com/transcriptionstream/phomymo),
loads the same `label.json` layouts that designer exports, fills them from a
CSV, and either prints them over Bluetooth or writes a PDF.

- eight protocol families, from the M02 pocket printer to the Brother QL-1110NWB
- BLE, classic Bluetooth serial, USB, and a capture-to-file transport
- writes clamped to the negotiated link MTU, so no per-model tuning
- roller alignment: alignment plus dot-level offsets, saved per printer
- PDF output at the exact label size, or tiled on a sheet with cut marks
- templated fields with filters, defined per job or saved in config
- refuses to print blank fields unless you say so
- colored logs, a live progress bar, and a dry run that simulates the link

## Install

```sh
uv sync                       # runtime deps only
uv sync --extra tui           # + rich: colored logs and a live progress bar
uv sync --all-extras          # + pyusb (--transport usb) and python-barcode
```

Optional extras, all off by default so the core stays small:

| extra     | brings   | enables                                        |
|-----------|----------|------------------------------------------------|
| `tui`     | rich     | colored log levels and the live progress bar    |
| `usb`     | pyusb    | `--transport usb`                               |
| `barcode` | python-barcode | barcode elements in a layout              |

Everything runs through uv:

```sh
uv run mbprint --help
```

The examples below drop the `uv run` prefix for brevity. Keep it, or activate
the venv once with `source .venv/bin/activate`.

## Quick start

```sh
# 1. find the printer
mbprint scan

# 2. remember the printer and the derived fields, so no flags are needed later
mbprint config set model m110
mbprint config set device M110-0123456789
mbprint config set data.qr "https://shop.example/{{sku}}[[/{{batch}}]]"

# 3. check the layout picks up the right CSV columns
mbprint fields -l label.json -c "examples/inventory-sample.csv"

# 4. proof it as a PDF
mbprint pdf -l label.json -c "examples/inventory-sample.csv" \
  --copies-from "Quantity On Hand" -o labels.pdf

# 5. print it
mbprint print -l label.json -c "examples/inventory-sample.csv" \
  --copies-from "Quantity On Hand"
```

## Commands

| command    | what it does                                                      |
|------------|-------------------------------------------------------------------|
| `print`    | render records and send them to the printer                       |
| `pdf`      | render records to a PDF instead of printing                       |
| `preview`  | render records to PNG, optionally as the exact raster sent         |
| `fields`   | show the layout's placeholders and how CSV columns map to them     |
| `printers` | list every known model, its protocol, head width and dpi           |
| `scan`     | BLE scan, with model detection per device                          |
| `status`   | battery, paper, cover, firmware and the negotiated MTU             |
| `test`     | print a density ramp, eight strips from 1 to 8                     |
| `config`   | read and write persistent defaults                                 |

Every command also takes `-v` / `-vv` / `-q` / `--log-file` / `--plain`; see
[Logging and tracing](#logging-and-tracing). `--help` on any of them lists its
own options.

## Fields

`label.json` addresses data through `{{placeholders}}`. A record is built per
CSV row from three layers:

1. **Raw columns**, addressable by their own header: `{{Product Category}}`.
2. **Normalized fields**, mapped from an Odoo-style export automatically:

   | field         | column                                               |
   |---------------|------------------------------------------------------|
   | `name`        | Name                                                 |
   | `ref`, `sku`  | Internal Reference                                   |
   | `price`       | Sales Price                                          |
   | `qty`         | Quantity On Hand                                     |
   | `batch`       | Batch / Lot, empty when the CSV has no such column   |

   `price_short` is derived from `price`, dropping zero cents (`35.00` becomes
   `35`) with a comma as the decimal separator; `--decimal .` changes that.
   Remap anything with `--map batch="Lot/Serial Number"`.

3. **Derived fields**, which you define. Nothing is hardcoded:

```sh
mbprint print -l label.json -c inventory.csv \
  --data brand="Example Ceramics" \
  --data qr="https://shop.example/{{sku}}[[/{{batch}}]]"
```

`--data KEY=TEMPLATE` is repeatable and **evaluated in order**, so a later entry
can use an earlier one — `--data brand=... --data qr="…/{{brand}}#{{sku}}"`. A
template without placeholders is just a constant, which is what `--set` means
(kept as an alias). With no `--csv`, one label is printed from `--data` alone.

`[[...]]` marks an optional segment: it disappears when every field inside it is
empty. So `#{{sku}}[[/{{batch}}]]` yields `#AG-EX-0001` for a record with no
batch and `#AG-EX-0001/L7` for one with batch `L7`.

### Filters

A placeholder can pipe its value through filters, in layout text, QR data,
barcode data and `--data` alike:

| filter        | example                      | result                       |
|---------------|------------------------------|------------------------------|
| `num`         | `{{price|num}}`              | `49,5` — drops a zero fraction |
| `num:N`       | `{{price|num:2}}`            | `49,50` — fixed decimals     |
| `upper`       | `{{name|upper}}`             | `BETA WIDGET`               |
| `lower`       | `{{sku|lower}}`              | `bw-1`                       |
| `title`       | `{{name|title}}`             | `Beta Widget`               |
| `capitalize`  | `{{name|capitalize}}`        | `Beta widget`               |
| `trim`        | `{{name|trim}}`              | surrounding spaces removed   |
| `truncate:N`  | `{{name|truncate:8}}`        | `Beta Wi…`                   |
| `default:X`   | `{{batch|default:n/a}}`      | `n/a` when empty             |
| `slug`        | `{{name|slug}}`              | `beta-widget`, accents folded |
| `urlencode`   | `{{name|urlencode}}`         | `Beta%20Widget`             |
| `replace:a:b` | `{{sku|replace:-:_}}`        | `BW_1`                       |

They chain left to right: `{{name|truncate:8|upper}}` gives `BETA WI…`. `num`
uses your `--decimal` separator, and leaves anything unparseable untouched. An
unknown filter is an error listing the valid ones, rather than a silent pass.

`{{price_short}}` is the same thing as `{{price|num}}`, kept as a field because
existing layouts use it.

A `default:` filter also settles the [missing-field](#missing-fields) question
for that placeholder: it always has a value, so it never triggers the gate.

Define the templates once and forget them:

```sh
mbprint config set data.qr "https://shop.example/{{sku}}[[/{{batch}}]]"
mbprint config set data.brand "Example Ceramics"
```

Config templates are applied first, in the order you defined them, then any
`--data` given on the command line.

### Missing fields

Before rendering, every required placeholder is checked against every record. A
required placeholder is one that is not inside a `[[...]]` segment — an empty
value there is by design, not an omission. When something is missing you get a
count per field and a suggestion:

```
warning: qr: empty on 22 of 22 labels
warning: batch: empty on 22 of 22 labels
warning: give it a value with --data qr="...", point it at a column with
         --map qr=COLUMN, wrap it in [[...]] in the layout to make it optional,
         or silence it with --data qr=""
Print anyway? [y/N]
```

On a terminal it asks. Redirected or in a script it refuses and exits 1, so a
batch job never silently prints blank labels. Four ways forward:

- `--force` (or `--ignore-missing`) — print as-is, without asking
- `--data field="..."` — give it a value
- `--map field=COLUMN` — point it at a CSV column
- `--data field=""` — declare it deliberately empty, which silences the warning

Inspect all of this before printing anything:

```sh
mbprint fields -l label.json -c "examples/inventory-sample.csv"
```

It prints the label size, every placeholder, which are optional, the derived
field templates in effect, the resolved column per field, the first record's
values, and a count of what is missing.

Copies: `--copies N` for a flat count, `--copies-from "Quantity On Hand"` to
take it from a column, or both to multiply. `--filter Column=Value` and
`--limit N` narrow the batch down.

## PDF output

```sh
# one page per label, page size exactly 30x20mm
mbprint pdf -l label.json -c inventory.csv -o labels.pdf

# tiled on A4 with cut marks, one label per unit in stock
mbprint pdf -l label.json -c inventory.csv --sheet a4 \
  --copies-from "Quantity On Hand" --margin 10 --gap 2 -o sheet.pdf
```

Without `--sheet`, each page is exactly the label: a 30x20mm label yields a
85.04 x 56.69pt MediaBox with the artwork filling it edge to edge. Print such a
PDF at actual size, not "fit to page".

`--sheet a4|a5|letter|legal` tiles instead, filling as many rows and columns as
fit; `--columns` / `--rows` force a grid, `--no-marks` drops the cut outlines.

`--bilevel` applies the same halftoning the printer receives, so the PDF shows
the dots that will actually burn rather than smooth greys.

## Previews

```sh
mbprint preview -l label.json -c inventory.csv --limit 3 --zoom 4
mbprint preview -l label.json -c inventory.csv --model m110 --raster
```

`--raster` is the high-fidelity one: it renders exactly what goes over the wire,
including full head width, alignment, roller offsets, rotation on sideways
models and halftoning. `--bilevel` halftones without padding to the head, and
`--printer-scale` renders at the model's dpi instead of 203.

## Roller alignment

Media is rarely centred under the head. Three knobs, all in head space and all
applied after any protocol rotation, so they mean the same thing on every model:

- `--align left|center|right` — where the label sits across the head. Defaults
  to the model's own default: `right` for M110/M110S/M120 and M220, `center`
  elsewhere.
- `--offset-x N` — nudge across the head, in dots. 8 dots = 1mm at 203dpi.
- `--offset-y N` — nudge along the feed, in dots. Negative crops from the top.

TSPL printers also take `--tspl-offset-mm` (the TSPL `OFFSET` command) and
`--gap-mm` for the die-cut gap.

Check a change without wasting media:

```sh
mbprint preview -l label.json -c inventory.csv --limit 1 \
  --model m110 --raster --offset-x 6 --zoom 3
```

Then save what works:

```sh
mbprint config set offset_x 6
mbprint config set align right
mbprint config list
```

Config lives in `~/.config/mbprint/config.json` and supplies the default for any
flag you do not pass. Scalar keys: `model`, `transport`, `address`, `device`,
`density`, `feed`, `speed`, `offset_x`, `offset_y`, `align`, `dither`,
`continuous`, `gap_mm`, `tspl_offset_mm`, `label`, `media`, `host`. Plus
`data.<field>` for
[derived field templates](#fields), which keep the order you define them in.

## Density

Thermal density depends on the media. Print the ramp, pick a strip, save it:

```sh
mbprint test --model m110      # eight strips, density 1 to 8
mbprint config set density 7
```

## Logging and tracing

Every command takes the same three options, before or after the subcommand:

| flag              | effect                                                     |
|-------------------|------------------------------------------------------------|
| (default)         | INFO: what is printed, on which printer, over which link    |
| `-v`              | DEBUG: every protocol command, decoded and in hex           |
| `-vv`             | TRACE: every write, raster chunks included                  |
| `-q`              | warnings and errors only                                    |
| `--log-file PATH` | append a full trace to a file, whatever the console shows   |
| `--plain`         | no colors and no progress bar, even on a terminal           |

```sh
mbprint print -l label.json -c inventory.csv -v
```
```
DEBUG   layout     loaded label.json: 6 elements, 30mm x 20mm at 8 dots/mm
DEBUG   printers   model forced to 'm110'
DEBUG   protocol   packed 240x160 dots with auto dithering
DEBUG   protocol   fitted to head: 384x160 dots, align=right offset=(0,0)
INFO    cli        printer: M110 / M120 [m110] m110 203dpi head=384px
INFO    cli        transport: file mtu_payload=512B chunk=128B density=6 feed=32
DEBUG   protocol   -> M110 speed: 1b 4e 0d 05
DEBUG   protocol   -> GS v 0 raster header: 1d 76 30 00 30 00 a0 00
DEBUG   protocol   -> raster payload: 7680 bytes in 60 chunks of 128, 20ms apart
DEBUG   protocol   -> M110 footer: 1f f0 05 00 1f f0 03 00
```

`-vv` adds a `TRACE` line per write from the transport, so you can see exactly
what crossed the link and in what size, and it lets bleak's own debug output
through. `--log-file` always records at TRACE regardless of `-q`, which is the
one to reach for when a print misbehaves and you want the whole exchange:

```sh
mbprint print -l label.json -c inventory.csv --log-file /tmp/print.log
```

### Progress

With the `tui` extra installed, a batch print shows a live bar on a terminal:

```
INFO     printer: M110 / M120 [m110] m110 203dpi head=384px
INFO     transport: ble mtu_payload=244B chunk=128B density=6 feed=32
⠹ labels ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━  7/22 00:31
  AG-EX-0001 ━━━━━━━━━━━━━━━━━━━━━━━                 62%
```

The top bar counts labels with a time estimate, the second tracks the transfer
of the label in flight. Log lines print above it rather than through it.

The bar steps down gracefully, in this order:

1. rich installed, terminal attached, no `-v`/`-q`/`--plain` — the bars above
2. terminal but no rich — a single rewritten line, `[3/22] AG-EX-0001:  60%`
3. redirected output, `-q`, `-v`, `--plain`, or `NO_COLOR` set — one log line
   per label, so logs stay grep-able

A live bar and a byte-level trace cannot share a terminal usefully, which is why
any verbosity flag turns the bar off rather than interleaving with it.

## Printers, protocols and MTU

```sh
mbprint printers          # every known model
mbprint scan              # BLE scan, with model detection
mbprint status            # battery, paper, cover, firmware, MTU
```

Eight protocol families are implemented; seven match the phomymo reference
driver, and `brother` was added from the QL raster command language:

| protocol   | models                                   | notes                                        |
|------------|------------------------------------------|----------------------------------------------|
| `m-series` | M03, T02, M200, M250, M220, M221, M260   | ESC/POS raster                               |
| `m02`      | M02, M02S, M02X, M02 Pro                 | `10 FF FE 01` prefix, minimal feed           |
| `m04`      | M04S, M04AS (`m04s-53`, `m04s-80`, `m04s-110`) | 300dpi, `1F 11 xx` init, 256-byte chunks |
| `m110`     | M110, M110S, M120                        | speed/density/media commands, `1F F0` footer |
| `d-series` | D30, D35, D50, Q30                       | prints sideways, gap detection, cutter pad   |
| `p12`      | P12, P12 Pro, A30                        | tape, 6-packet handshake with reply waits    |
| `tspl`     | PM-241-BT                                | text protocol, inverted BITMAP               |
| `brother`  | QL-1100, QL-1110NWB, QL-1115NWB          | ESC/P raster on DK media, 300dpi, PackBits   |

Sideways models (D-series, P12) rotate the label 90° clockwise before it is
fitted to the head. A 30x20mm label therefore needs 20mm of tape, and P12 will
refuse it with a message saying so rather than printing garbage. 300dpi models
are rendered natively at 300dpi rather than upscaled from a 203dpi bitmap.

Every data write is clamped to `min(protocol chunk, link MTU payload)`. On BLE
that payload is the negotiated ATT MTU minus three bytes; BlueZ only reports the
real value once the write channel is acquired, which mbprint does on connect. A
printer that negotiates 517 gets 128-byte writes, one stuck at 23 gets 20-byte
writes, and neither needs configuring. `--mtu N` caps it further and
`--chunk-delay MS` changes the inter-chunk pacing.

## Brother QL

The QL-1100 series speaks Brother's ESC/P raster language rather than ESC/POS,
on DK media whose geometry is fixed per roll. Everything else — layouts, fields,
filters, PDF, previews — works the same.

```sh
# network, the usual link for a QL-1110NWB
mbprint print -l label.json -c inventory.csv \
  --model ql-1110nwb --transport tcp --host 192.168.1.50 --media 102x152

# USB
mbprint print -l label.json -c inventory.csv --model ql-1110nwb -t usb --media 62
```

`--media` names the loaded roll; `mbprint printers` lists the models and the
table below the DK ids. Omit it and the roll is inferred from the layout size,
which works when your label is drawn at the media's dimensions.

| id | roll |
|---|---|
| `12` `29` `38` `50` `54` `62` `102` `103` | continuous, width in mm |
| `17x54` `17x87` `23x23` `29x42` `29x90` `39x48` `39x90` `52x29` `60x86` `62x29` `62x100` `102x51` `102x152` `103x164` | die-cut |
| `d12` `d24` `d58` | round die-cut |

`102`, `103`, `102x51`, `102x152` and `103x164` need a wide model.

Three things differ from the Phomemo families:

- **Placement is by right margin.** Each roll has a printable area narrower
  than the tape, offset from the right edge of the 1296-dot head. `--offset-x`
  adds to that offset rather than replacing the alignment logic, and
  `--align` does not apply.
- **The label is fitted to the roll.** Die-cut media has a fixed printable
  rectangle, so the rendered label is scaled to fit and centred; continuous
  media fixes only the width. Layouts are rendered at 300 dpi natively.
- **Raster lines are PackBits-compressed** by default, which cuts a 4x6in job
  from 274KB to 15KB. `--no-compress` sends them raw.

Finishing: `--no-cut` leaves the labels joined, `--cut-every N` cuts every Nth.

Verification without hardware: our byte stream is compared against the
`brother_ql` project's output for the same image, byte for byte, across eight
media and compression combinations — including the rolls whose printable width
is not a whole number of bytes, where placement is easiest to get wrong.

## Transports

| flag                            | use                                          |
|---------------------------------|----------------------------------------------|
| `--transport ble` (default)     | `--device NAME` or `--address MAC`           |
| `--transport tcp`               | `--host IP [--tcp-port 9100]`, network printers |
| `--transport serial`            | `--port /dev/rfcomm0`, classic Bluetooth SPP |
| `--transport usb`               | `--usb-vid 0x0483 --usb-pid 0x5740`          |
| `--transport file --out job.bin`| capture the byte stream, print nothing       |

`--device` matches a case-insensitive substring of the advertised name. With
neither `--device` nor `--address`, mbprint scans and takes the first device
whose name matches a known model pattern.

### Dry runs

`--dry-run` runs the entire flow — render, halftone, fit to the head, frame for
the protocol, chunk to the MTU — against a simulated link instead of a printer.
Nothing is opened, nothing is sent, and it takes about as long as the real
print, because the protocol's inter-chunk pacing is honoured:

```sh
mbprint print -l label.json -c inventory.csv --model m110 --dry-run
```
```
INFO     printer: M110 / M120 [m110] m110 203dpi head=384px
WARNING  dry run: simulating the print, nothing will be sent to a printer
INFO     transport: file mtu_payload=244B chunk=128B density=6 feed=32
⠇ labels      ━━━━━━━━━━━━━━━━━━━━━━━━━━━━╺━━━━━━━━━━━━━━  2/3  00:03
  AG-EX-0001 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 100% 00:00
INFO     simulated 3 labels, 23040 raster bytes, 6.3s
```

That makes it the way to try the progress bar, to time a batch before
committing media, and to check protocol framing: add `--out job.bin` to keep
the bytes it would have sent, or `-vv` to watch every write.

The simulated link assumes a 244-byte MTU, what an M-series printer typically
negotiates; `--mtu N` models a different one and `--chunk-delay 0` runs the
whole batch instantly when you only care about the bytes.

## Troubleshooting

**It refuses to print, listing empty fields.** That is the missing-field gate;
see [Missing fields](#missing-fields). `--force` prints anyway.

**Nothing prints, and the header said `Generic M-series`.** The advertised name
matched no known model, so mbprint fell back to a generic 72-byte head. Many
units advertise a serial number rather than a model name. The raster width and
the protocol will both be wrong. Pass `--model m110` (see `mbprint printers`)
and save it with `mbprint config set model m110`. mbprint warns loudly when it
falls back, so this should never be silent.

**`mtu_payload=20B` and printing crawls.** The MTU was not negotiated: 20-byte
writes mean roughly 12 seconds a label instead of 2. mbprint acquires the BlueZ
MTU on connect; if you still see 20B, the firmware really is limited to it.

**The printer does not appear in `scan`.** It must be powered on and not
connected elsewhere — a printer paired to the phone app stops advertising. The
adapter must be up (`bluetoothctl power on`). BLE needs no pairing. Some units
expose only classic Bluetooth SPP and never appear here: bind them with
`rfcomm bind 0 <MAC> 1` and print with `--transport serial --port /dev/rfcomm0`.

**Output is offset or clipped on the label.** See
[roller alignment](#roller-alignment), and confirm with `preview --raster`
before spending media.

**A QR element refuses to render.** The element is smaller than the code's
module count. Enlarge it in the designer or shorten the data — a shorter
`--data qr=...` template drops a QR version and buys back modules.

**Text overflows its box.** The layout renderer wraps to the element width like
the designer does; `noWrap` allows overflow, `autoScale` shrinks to fit,
`clipOverflow` trims. All three come from the layout file.

## Reverse engineering a printer

`flake.nix` provides a shell for working out an unknown printer's protocol —
the Brother QL-1110NWB being the case in hand, whose raster language the
iPrint&Label Android app implements.

```sh
nix develop                       # adb, jadx, apktool, dex2jar, radare2, tshark, androguard
nix run .#pull-apk                # pull com.brother.ptouch.iprintandlabel off a device
nix run .#pull-apk -- OTHER.PKG   # any other package
nix run .#decompile-apk           # jadx + apktool over the pulled base.apk
nix develop .#native              # ghidra and friends, for lib/*.so
```

`pull-apk` resolves the install paths with `pm path` and pulls every piece, so
split APKs come down whole: `base.apk` carries the code, `split_config.*` the
per-ABI native libraries. `decompile-apk` runs both jadx (readable Java) and
apktool (smali plus resources), because they disagree on hard cases.

Two other sources are usually faster than reading decompiled code:

- **Bluetooth HCI snoop log** from the phone (Developer options → Enable
  Bluetooth HCI snoop log), pulled with `adb pull` and read with `tshark`. It
  shows the actual bytes the app sends, which is how several protocols in
  `printers.json` were pinned down.
- **USB capture** with `usbmon` and Wireshark, if the printer is wired.

Whatever the source, the target is a new flow in `protocol.py` plus an entry in
`printers.json` — see [the module map](#layout).

## Custom printers

Drop a `printers.json` in `~/.config/mbprint/` using the same schema as
`mbprint/printers.json`. Entries with an existing `id` override the built-in
one, so you can correct a head width or add a `namePatterns` entry for a unit
that advertises an unusual name:

```json
{
  "version": 1,
  "printers": [
    {
      "id": "m110",
      "name": "M110 (mine)",
      "protocol": "m110",
      "widthBytes": 48,
      "dpi": 203,
      "alignment": "right",
      "namePatterns": ["M110", "Q199"]
    }
  ]
}
```

## Option reference

Grouped by what they affect. Source and render options apply to `print`, `pdf`,
`preview` and `fields`; printer and transport options to `print`, `status` and
`test`.

**Global** (any command)

| option | meaning |
|---|---|
| `-v`, `-vv` (`--verbose`) | DEBUG: protocol commands; TRACE: every write |
| `-q` (`--quiet`) | warnings and errors only |
| `--log-file PATH` | append a full TRACE to a file |
| `--plain` | no color, no progress bar |

**Source**

| option | meaning |
|---|---|
| `-l`, `--label PATH` | layout file (default `label.json`, or config `label`) |
| `-c`, `--csv PATH` | records to print; without it, one label from `--data` |
| `--data KEY=TEMPLATE` | define a field, repeatable, evaluated in order |
| `--set KEY=VALUE` | alias for `--data`, for plain constants |
| `--map FIELD=COLUMN` | bind a field to a CSV column |
| `--force`, `--ignore-missing` | print despite empty required fields |
| `--decimal C` | decimal separator for `num` and prices (default `,`) |
| `--filter COLUMN=VALUE` | keep only matching rows |
| `--limit N` | only the first N records |
| `--copies N` | copies of every label |
| `--copies-from COLUMN` | copy count per record, from a column |

**Render**

| option | meaning |
|---|---|
| `--dither MODE` | `auto`, `none`, `threshold`, `floyd-steinberg`, `atkinson`, `ordered` |

**Printer and media**

| option | meaning |
|---|---|
| `-m`, `--model ID` | model id, see `mbprint printers`; default auto-detect |
| `--density 1-8` | thermal density |
| `--feed N` | dots fed after each label |
| `--speed N` | print speed (M110, TSPL) |
| `--continuous` | continuous media, no gap detection |
| `--align left\|center\|right` | where the label sits across the head |
| `--offset-x N`, `--offset-y N` | roller alignment, in dots |
| `--gap-mm N`, `--tspl-offset-mm N` | TSPL gap and offset |
| `--media ID` | Brother DK roll, see [Brother QL](#brother-ql) |
| `--no-cut`, `--cut-every N` | Brother cut behaviour |
| `--no-compress` | Brother: send raster lines raw |

**Transport**

| option | meaning |
|---|---|
| `-t`, `--transport KIND` | `ble`, `tcp`, `serial`, `usb`, `file` |
| `--device NAME`, `--address MAC` | which BLE printer |
| `--host IP`, `--tcp-port N` | network printer (default port 9100) |
| `--port PATH`, `--baud N` | serial / RFCOMM |
| `--usb-vid ID`, `--usb-pid ID` | USB device |
| `--mtu N` | cap the write size |
| `--chunk-delay MS` | inter-chunk pacing |
| `--dry-run` | simulate the link, send nothing |
| `-o`, `--out PATH` | with `-t file` or `--dry-run`, capture the bytes |

**`pdf` only**

| option | meaning |
|---|---|
| `-o`, `--out PATH` | output PDF (default `labels.pdf`) |
| `--sheet a4\|a5\|letter\|legal` | tile onto paper instead of label-sized pages |
| `--margin MM`, `--gap MM` | sheet margins and spacing |
| `--columns N`, `--rows N` | force a grid |
| `--no-marks` | omit cut marks |
| `--bilevel` | halftone, so the PDF shows printed dots |
| `--scale N` | render scale, 1 = 203dpi |

**`preview` only**

| option | meaning |
|---|---|
| `-o`, `--out DIR` | output directory (default `preview`) |
| `--raster` | the exact raster sent, head width included |
| `--bilevel` | halftone without padding to the head |
| `--printer-scale` | render at the model's dpi |
| `--zoom N` | upscale the PNG |

**Others**: `printers --json`, `scan --timeout SECONDS`,
`config list|get|set|unset KEY [VALUE]`.

## Layout

```
mbprint/
  cli.py           argparse front end, one function per command
  layout.py        label.json loader, Pillow renderer, {{field}} substitution
  data.py          CSV loading, column mapping, derived and templated fields
  raster.py        halftoning, bit packing, rotation, head fitting
  protocol.py      the seven print flows and their command builders
  printers.py      model definitions and detection
  printers.json    built-in model table
  media.py         Brother DK roll geometry and label fitting
  pdf.py           exact-size and tiled PDF output
  config.py        persistent defaults
  log.py           logger setup, TRACE level, hex dumps
  ui.py            console detection, progress bars, rich fallbacks
  transport/       ble.py, tcp.py, serial_port.py, usb.py, file.py
```

The pipeline is the same for every command: `data.build_records` turns CSV rows
into records and evaluates the `--data` templates, `layout.render` substitutes
placeholders and draws a PIL image, `protocol.prepare_raster` halftones it,
rotates it if the model prints sideways and fits it to the head, and
`protocol.print_raster` frames it for the model and streams it through a
transport that never exceeds its MTU. `pdf.py` taps the same images; `--dry-run`
swaps in a paced file transport instead of a printer.

## Tests

```sh
uv run pytest tests -q
```

Eighty-two tests covering templating, filters and optional segments, the
missing-field gate, column mapping and copy counts, raster packing, alignment,
offsets and rotation, the framing of every protocol, MTU clamping end to end,
model detection, PDF page geometry, the config data table, the progress
reporters, the logging setup including the command trace, and the Brother
stream checked byte for byte against brother_ql. No hardware
needed: the file transport captures what would have been sent, and `--dry-run`
exercises the whole flow.
