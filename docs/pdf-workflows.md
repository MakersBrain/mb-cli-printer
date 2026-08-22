# PDF generation and direct printing

`mbprint` supports two complementary PDF workflows:

1. `mbprint pdf` renders a `label.json` layout into a PDF. Each page is one
   label unless `--sheet` is used.
2. `mbprint print-pdf` rasterizes an existing PDF and sends each selected page
   through the normal Brother or Phomemo printer protocol.

The printer never receives PDF syntax. `print-pdf` renders each page to pixels
at the selected model's native DPI, applies the normal media placement,
halftoning, framing, chunking, and transport logic, then sends printer-native
bytes.

## Generate exact-size label pages

Without `--sheet`, the PDF page has the physical dimensions declared by the
layout. A 62 × 29 mm layout therefore produces a 62 × 29 mm PDF page.

Select a model to render the artwork at its exact native resolution:

```sh
# Brother QL: 300 DPI artwork on an exact-size page
mbprint pdf -l label.json -c inventory.csv \
  --model ql-1110nwb -o labels.pdf

# Phomemo M110: 203 DPI artwork on the same physical page
mbprint pdf -l label.json -c inventory.csv \
  --model m110 -o labels.pdf
```

`--device NAME` can detect the model from a printer name instead. A saved
`model` or `device` configuration is also honored. With no selected model, the
layout's own `dotsPerMm` controls the PDF image resolution.

The physical page size does not change when a model is selected. Only the
number of pixels used to represent the artwork changes. For example, 300 DPI
is exactly `300 / 25.4` dots/mm.

An explicit `--scale N` overrides automatic model DPI. It multiplies the
layout's pixel dimensions and `dotsPerMm` while retaining the physical page
size.

## Print PDF pages as labels

Use `print-pdf` when the input PDF already contains one label per page:

```sh
# Brother over USB
mbprint print-pdf labels.pdf --model ql-1110nwb --transport usb

# Phomemo over USB
mbprint print-pdf labels.pdf --model m110 --transport usb

# Brother over the network
mbprint print-pdf labels.pdf --model ql-1110nwb \
  --transport tcp --host 192.168.1.50
```

All supported print transports and stable USB selectors remain available. For
example, select one of several attached printers with `--usb-serial SERIAL`.

Select pages and copies without modifying the PDF:

```sh
mbprint print-pdf labels.pdf --pages 1,3-5 --copies 2 \
  --model m110 --transport usb
```

Page numbers are one-based. All selected pages must have the same physical
size. Duplicate page numbers in `--pages` are ignored; use `--copies` when
duplicates are wanted.

## Brother media validation

For Brother QL printers, `print-pdf` checks the PDF page dimensions against the
loaded or explicitly selected DK media before printing. USB, Bluetooth, and
serial connections query the printer's raster status; TCP uses IPP. If status
is unavailable, media is inferred from the PDF page size.

A transposed page matching the media is rotated automatically. A real size
mismatch is rejected to prevent wasting labels:

```text
PDF page 1 is 40.00x30.00mm, but 62x29 media is 62x29mm
```

Use `--media ID` to select a DK roll explicitly. Use `--fit` only when scaling
a differently sized PDF is intentional. Phomemo jobs are fitted to the model's
print head, but those printers do not provide the same DK-style physical media
table for preflight validation.

## End-to-end round trip

An exact-size PDF generated for a model can be printed back through that model:

```sh
mbprint pdf -l label.json -c inventory.csv \
  --model ql-1110nwb -o labels.pdf

mbprint print-pdf labels.pdf \
  --model ql-1110nwb --transport usb
```

The first command chooses the embedded artwork resolution. The second command
rasterizes at the printer's native DPI again, validates media, and sends the
printer protocol. PDF page dimensions remain the source of physical label
size.

## Sheet PDFs are different

`mbprint pdf --sheet a4` creates ordinary paper sheets containing several
labels. Those A4/A5/Letter/Legal pages are intended for a desktop or office
printer and are not one-label-per-page input for `print-pdf`.

```sh
mbprint pdf -l label.json -c inventory.csv --sheet a4 \
  --margin 10 --gap 2 -o sheets.pdf
```

## Safe validation without printing

Use a dry run to exercise PDF rendering, media fitting, protocol framing, and
chunking without opening a printer connection:

```sh
mbprint print-pdf labels.pdf --model ql-1110nwb \
  --media 62x29 --dry-run --chunk-delay 0
```

Add `--out job.bin` to capture the printer-native byte stream. The capture is
not a PDF and should only be sent to the same printer family and media setup.

## Common problems

- If a Brother page does not match the loaded roll, regenerate it with the
  correct layout dimensions or select the intended `--media`; avoid `--fit`
  unless scaling is deliberate.
- If artwork is clipped on a Phomemo, verify the model and PDF page width. The
  model determines the printable head width.
- If output is unexpectedly soft, generate with `--model` so vector/text
  artwork is first rendered at native DPI, and avoid unnecessary `--scale`
  overrides.
- Print exact-size PDFs at 100% in third-party viewers. Their “fit to page”
  option changes physical dimensions; `mbprint print-pdf` does not use that
  dialog and preserves the PDF page geometry itself.
