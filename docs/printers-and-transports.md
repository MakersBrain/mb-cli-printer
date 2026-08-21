# Printers and transports

## Printer protocols

Eight protocol families are implemented:

| Protocol | Models | Notes |
|---|---|---|
| `m-series` | M03, T02, M200, M250, M220, M221, M260 | ESC/POS raster |
| `m02` | M02, M02S, M02X, M02 Pro | `10 FF FE 01` prefix, minimal feed |
| `m04` | M04S, M04AS (`m04s-53`, `m04s-80`, `m04s-110`) | 300 dpi, 256-byte chunks |
| `m110` | M110, M110S, M120 | speed, density, media commands, `1F F0` footer |
| `d-series` | D30, D35, D50, Q30 | sideways raster, gap detection, cutter padding |
| `p12` | P12, P12 Pro, A30 | tape, six-packet handshake with reply waits |
| `tspl` | PM-241-BT | text protocol with inverted `BITMAP` data |
| `brother` | QL-1100, QL-1110NWB, QL-1115NWB | ESC/P raster on DK media, 300 dpi, PackBits |

Seven are based on the
[phomymo reference driver](https://github.com/transcriptionstream/phomymo).
Brother support uses the QL ESC/P raster language.

D-series and P12 models rotate the label 90 degrees clockwise before fitting it
to the head. A 30 × 20 mm layout therefore consumes 20 mm of tape. M04 and
Brother layouts are rendered natively at 300 dpi.

List the complete built-in table with:

```sh
mbprint printers
```

## Transports

| Option | Connection |
|---|---|
| `--transport ble` | BLE GATT; select with `--device NAME` or `--address MAC` |
| `--transport bluetooth` | classic Bluetooth RFCOMM/SPP; requires `--address MAC` |
| `--transport tcp` | network socket; requires `--host`, defaults to port 9100 |
| `--transport serial` | serial or bound RFCOMM device; requires `--port` |
| `--transport usb` | USB bulk endpoint; optionally select VID and PID |
| `--transport file` | capture the byte stream with `--out`, without printing |

BLE is the default. A device name is matched as a case-insensitive substring.
With no name or address, `mbprint` scans and selects the first advertisement
matching a known model pattern.

BLE and classic Bluetooth are different transports. Phomemo models generally
use BLE GATT. Brother QL Bluetooth models use classic RFCOMM/SPP and must first
be paired, for example with `bluetoothctl`. The direct `bluetooth` transport
does not require a `/dev/rfcomm` device; `serial` does.

On Linux, USB normally needs a udev rule granting access to the device. Known
vendor IDs include `04f9` for Brother and `0483` or `2e3c` for Phomemo. Install
the `usb` extra before using this transport.

`udev/99-mbprint.rules` covers all three vendors:

```sh
sudo cp udev/99-mbprint.rules /etc/udev/rules.d/
sudo udevadm control --reload && sudo udevadm trigger
```

On NixOS, add the rules to the system configuration instead:

```nix
services.udev.extraRules = builtins.readFile ./udev/99-mbprint.rules;
```

Replug the printer afterwards. pyusb also needs a libusb shared library on the
loader path; the flake's dev shell provides one, and elsewhere installing
`libusb1` system-wide is enough.

## MTU and pacing

Each data write is limited to the smaller of the protocol chunk size and the
transport payload limit. For BLE, the payload limit is the negotiated ATT MTU
minus three bytes. A 517-byte ATT MTU therefore still uses a protocol's
128-byte chunks, while an ATT MTU of 23 limits writes to 20 bytes.

`--mtu N` caps the payload further. `--chunk-delay MS` overrides the
protocol-specific pause between chunks.

## Alignment and density

Alignment controls are applied in printer-head space after any protocol
rotation:

- `--align left|center|right` positions the label across the head.
- `--offset-x N` moves it across the head in dots.
- `--offset-y N` moves it along the feed in dots; a negative value crops the
  top.

At 203 dpi, eight dots are approximately one millimetre. M110, M110S, M120, and
M220 default to right alignment; most other Phomemo models default to center.
Brother placement is media-defined, so `--align` does not apply there and
`--offset-x` adjusts the roll's right-margin placement.

Check an adjustment with a raster preview:

```sh
mbprint preview -l label.json -c inventory.csv --limit 1 \
  --model m110 --raster --offset-x 6 --zoom 3
```

Then save the active defaults:

```sh
mbprint config set offset_x 6
mbprint config set align right
```

Configuration is global, so changing to a different physical printer may
require changing these values. There are no per-printer profiles.

TSPL printers also accept `--gap-mm` and `--tspl-offset-mm`. To choose thermal
density, print and save a ramp:

```sh
mbprint test --model m110
mbprint config set density 7
```

## PDF and preview fidelity

Without `--sheet`, a PDF page has the exact physical dimensions of the label.
For example, a 30 × 20 mm label has an 85.04 × 56.69 point MediaBox. Print it at
actual size rather than fitting it to the page.

`--sheet a4|a5|letter|legal` tiles labels. `--columns` and `--rows` force a
grid, and `--no-marks` removes cut outlines. `--bilevel` applies the selected
halftone to the PDF. PDF rendering defaults to the layout's 203 dpi scale; it
does not reproduce the native 300 dpi dot grid of an M04 or Brother print.

For PNG previews:

- `--raster` applies model rotation, media fitting, head width, alignment,
  offsets, and halftoning.
- `--bilevel` applies halftoning without fitting to the printer head.
- `--printer-scale` renders ordinary previews at the model's DPI.
- `--zoom N` enlarges the saved PNG with nearest-neighbor scaling.

## Brother QL

The QL-1100 series uses Brother ESC/P raster commands and fixed DK roll
geometry. Network, classic Bluetooth, and USB printing have all been verified
on a QL-1110NWB.

```sh
# Network
mbprint print -l label.json -c inventory.csv \
  --model ql-1110nwb -t tcp --host 192.168.1.50

# Classic Bluetooth, after pairing
mbprint print -l label.json -c inventory.csv \
  --model ql-1110nwb -t bluetooth --address 74:97:79:16:1A:1E

# USB
mbprint print -l label.json -c inventory.csv \
  --model ql-1110nwb -t usb
```

`--media` is optional. Bluetooth, serial, and USB use the printer's raster
status block to identify the roll. TCP printing uses IPP on port 631 for status
because port 9100 does not return it. If no status is available, media is
inferred from the layout dimensions.

| IDs | Roll type |
|---|---|
| `12` `29` `38` `50` `54` `62` `102` `103` | continuous; ID is width in mm |
| `17x54` `17x87` `23x23` `29x42` `29x90` `39x48` `39x90` `52x29` `60x86` `62x29` `62x100` `102x51` `102x152` `103x164` | rectangular die-cut |
| `d12` `d24` `d58` | round die-cut |

The `102`, `103`, `102x51`, `102x152`, and `103x164` media require a wide
model. Some printers report a die-cut roll with its dimensions transposed;
media lookup accepts both orientations.

Die-cut layouts are scaled and centered within the roll's printable rectangle.
Continuous media fixes only the width. Raster lines are PackBits-compressed by
default; `--no-compress` sends raw lines. `--no-cut` leaves labels joined, and
`--cut-every N` changes the cut interval.

Query status over the configured connection:

```sh
mbprint status --model ql-1110nwb -t bluetooth \
  --address 74:97:79:16:1A:1E
mbprint status --model ql-1110nwb -t tcp --host 192.168.1.50
```

Network QLs can be discovered with Avahi:

```sh
avahi-browse -rtp _pdl-datastream._tcp | grep -i QL-
```

The Brother byte stream is tested against `brother_ql` across multiple media
and compression combinations. A captured QL-1110NWB status block is also part
of the test suite.

## Dry runs

`--dry-run` renders, halftones, fits, frames, chunks, and paces a print without
opening a printer connection:

```sh
mbprint print -l label.json -c inventory.csv --model m110 --dry-run
```

The simulated link uses a 244-byte payload limit. Use `--mtu N` to model a
different limit, `--chunk-delay 0` for an immediate run, and `--out job.bin` to
retain the bytes.

## Custom printer definitions

Place `printers.json` in `~/.config/mbprint/` (or the corresponding
`$XDG_CONFIG_HOME` directory). Entries with an existing ID override the bundled
definition:

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

## Troubleshooting

**`mtu_payload=20B` and printing is slow.** The BLE connection is using the
minimum ATT MTU. `mbprint` acquires BlueZ's negotiated write channel when it
connects; if the payload remains 20 bytes, the device or connection is limited
to it.

**A printer never appears in `scan`.** It may be connected to another app, or
it may support only classic Bluetooth. Use `bluetooth` after pairing, or bind an
RFCOMM device and use `serial`.

**A QR code does not fit.** Enlarge its element in the designer or shorten its
data so the encoder can select a smaller QR version.

**Text overflows.** The renderer supports the layout properties `noWrap`,
`autoScale`, and `clipOverflow`.
