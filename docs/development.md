# Development and reverse engineering

## Setup and tests

`uv sync` installs runtime dependencies and the default development group.
Run the hardware-free suite with:

```sh
uv run pytest tests -q
```

The tests cover templates, filters, optional segments, missing fields,
column mapping, copies, raster packing, alignment, rotation, all protocol
frames, MTU clamping, model detection, exact-DPI PDF generation, direct PDF
printing through Brother and Phomemo flows, vector SVG output, config ordering,
progress, logging, Brother streams compared with `brother_ql`, and a status
block captured from a QL-1110NWB.

Useful additional checks are:

```sh
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

## Module map

```text
mbprint/
  cli.py           argparse front end and command orchestration
  layout.py        label loader, Pillow renderer, placeholder substitution
  data.py          CSV loading, mapping, derived fields, record selection
  raster.py        halftoning, packing, rotation, and head fitting
  protocol.py      printer command builders and print flows
  printers.py      model definitions and detection
  printers.json    bundled model table
  media.py         Brother DK geometry and fitting
  pdf.py           exact-size PDF output plus page selection and rasterization
  svg.py           exact-size hybrid vector/raster SVG export
  svgimport.py     SVG metadata round trips and SVG-to-layout element mapping
  config.py        persistent defaults
  log.py           logger setup, TRACE level, and hex dumps
  ui.py            terminal detection and progress reporters
  ipp.py           minimal IPP client for Brother media status
  transport/       BLE, Bluetooth, TCP, serial, USB, and file transports
```

The main print pipeline is:

```text
CSV + --data
    -> data.build_records
    -> layout.render
    -> protocol.prepare_raster
    -> protocol.print_raster
    -> selected transport
```

`print-pdf` enters the same pipeline at the image stage: PDFium rasterizes each
page at the printer's native DPI, then `prepare_raster` and `print_raster` handle
media placement, printer framing, and transport delivery.

`prepare_raster` halftones, rotates when required, and fits the bitmap to the
head. `print_raster` frames it for the selected protocol. The transport clamps
writes to its payload limit. PDF output branches from the rendered images, and
dry-run printing substitutes a paced file transport.

## Reverse-engineering environment

`flake.nix` provides tools for researching an unknown printer protocol:

```sh
nix develop                       # adb, jadx, apktool, dex2jar, radare2, tshark, androguard
nix run .#pull-apk                # pull Brother iPrint&Label from a device
nix run .#pull-apk -- OTHER.PKG   # pull another package
nix run .#decompile-apk           # run jadx and apktool over base.apk
nix develop .#native              # Ghidra and native-code tools
```

`pull-apk` uses `pm path` and downloads all pieces of a split APK.
`decompile-apk` runs both jadx and apktool because their output is useful in
different failure cases.

Two capture sources are often faster than reading decompiled code:

- Android Bluetooth HCI snoop logs reveal the bytes sent by the app.
- `usbmon` and Wireshark reveal traffic from a wired device.

On Pixel devices, set Bluetooth HCI snoop logging to **Enabled**, not
**Filtered**; filtered captures truncate packets. Restart the Bluetooth stack
after enabling it. A bug report normally contains the log at
`FS/data/misc/bluetooth/logs/btsnoop_hci.log`. Reassemble HCI ACL into L2CAP and
then RFCOMM UIH payloads to recover a classic serial stream.

[Brother wireless configuration](brother-wireless-config.md) records what the
iPrint&Label app sends to put a QL on a network, as a worked example of reading
a protocol out of a decompiled app.

A new printer normally requires a flow in `protocol.py` and a definition in
`printers.json`. Add framing, raster, detection, and MTU tests before relying on
hardware validation.
