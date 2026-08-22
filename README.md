# mb-cli-printer

Render `label.json` layouts with CSV data, then print them on Phomemo and
Brother label printers or export them as PDF and PNG.

`mbprint` supports BLE, classic Bluetooth, TCP, serial, USB, and capture-to-file
transports. It includes eight printer protocol families, automatic BLE MTU
handling, reusable field templates, label alignment controls, exact-size and
tiled PDFs, and hardware-free dry runs.

## Install

The project uses [uv](https://docs.astral.sh/uv/):

```sh
uv sync --no-dev                    # runtime dependencies
uv sync --no-dev --extra tui        # + colored logs and live progress bars
uv sync --no-dev --all-extras       # + USB and barcode support
```

Optional extras are off by default:

| Extra | Adds | Enables |
|---|---|---|
| `tui` | `rich` | colored logs and live progress bars |
| `usb` | `pyusb` | `--transport usb` |
| `barcode` | `python-barcode` | barcode elements in layouts |

Run the command through uv:

```sh
uv run mbprint --help
```

The examples below omit `uv run` for readability. Keep the prefix, or activate
the environment first with `source .venv/bin/activate`.

## Quick start

```sh
# Find a BLE printer.
mbprint scan

# Save the model, advertised device name, and a derived QR field.
mbprint config set model m110
mbprint config set device M110-0123456789
mbprint config set data.qr "https://shop.example/{{sku}}[[/{{batch}}]]"

# Inspect how the layout maps to the CSV.
mbprint fields -l label.json -c examples/inventory-sample.csv

# Create a proof, using stock quantity as the copy count.
mbprint pdf -l label.json -c examples/inventory-sample.csv \
  --copies-from "Quantity On Hand" -o labels.pdf

# Print the same labels.
mbprint print -l label.json -c examples/inventory-sample.csv \
  --copies-from "Quantity On Hand"
```

## How data reaches a label

A layout uses placeholders such as `{{name}}`, `{{sku}}`, and `{{price|num}}`.
For each CSV row, `mbprint` combines:

1. the original CSV columns;
2. normalized names such as `name`, `sku`, `price`, `qty`, and `batch`;
3. derived values supplied with repeatable `--data KEY=TEMPLATE` options or
   saved as `data.<field>` config entries.

For example:

```sh
mbprint print -l label.json -c inventory.csv \
  --data brand="Example Ceramics" \
  --data qr="https://example.com/items/{{sku}}[[/{{batch}}]]"
```

The optional segment `[[/...]]` disappears when its fields are empty. Derived
fields are evaluated in order, so a later `--data` entry can use a value defined
by an earlier one.

Before rendering, required placeholders are checked across the records. On a
terminal, `mbprint` asks before continuing with empty fields; in a script it
exits with an error. Use `--data`, `--map`, an optional segment, or `--force` to
resolve that deliberately.

See [Data, templates, and filters](docs/data-and-templates.md) for mappings,
filters, optional fields, copy counts, and the missing-field gate.

## Common workflows

Create one PDF page per label:

```sh
mbprint pdf -l label.json -c inventory.csv -o labels.pdf
```

Tile labels on A4 with cut marks:

```sh
mbprint pdf -l label.json -c inventory.csv --sheet a4 \
  --margin 10 --gap 2 -o sheet.pdf
```

Preview ordinary artwork or the fitted printer raster:

```sh
mbprint preview -l label.json -c inventory.csv --limit 3 --zoom 4
mbprint preview -l label.json -c inventory.csv --model m110 --raster
```

Test a complete print flow without opening hardware:

```sh
mbprint print -l label.json -c inventory.csv --model m110 \
  --dry-run --chunk-delay 0
```

Capture the framed bytes by adding `--out job.bin`. Details about PDF fidelity,
printer raster previews, alignment, protocols, media, and connection methods are
in [Printers and transports](docs/printers-and-transports.md).

## Commands

| Command | Purpose |
|---|---|
| `print` | render records and send them to a printer |
| `pdf` | render records to a PDF |
| `preview` | render records to PNG, optionally as the fitted printer raster |
| `fields` | inspect placeholders and CSV mappings |
| `printers` | list known models, protocols, head widths, and resolutions |
| `scan` | scan for BLE devices and detect printer models |
| `status` | query printer and media status where the protocol supports it |
| `test` | print a density ramp from 1 through 8 |
| `wifi` | scan, inspect, or configure a Brother QL's wireless settings |
| `usb-list` | list attached supported USB printers and stable selectors |
| `usb-info` | show USB descriptors, IEEE 1284 device ID, and port status |
| `config` | inspect and change persistent defaults |

Every command accepts `-v`, `-vv`, `-q`, `--log-file`, and `--plain`, before or
after the subcommand. Run `mbprint COMMAND --help` for command-specific help, or
see the [CLI reference](docs/cli-reference.md).

## Configuration

Persistent defaults live in `~/.config/mbprint/config.json` (or under
`$XDG_CONFIG_HOME`). Common examples:

```sh
mbprint config set model m110
mbprint config set align right
mbprint config set offset_x 6
mbprint config set density 7
mbprint config set data.brand "Example Ceramics"
mbprint config list
```

The configuration is one active set of defaults, not a collection of per-model
profiles. Command-line values override it.

## Troubleshooting

**The command refuses to render because fields are empty.** Run `mbprint
fields`, then provide the value with `--data`, map a CSV column with `--map`,
make the segment optional with `[[...]]`, or explicitly continue with `--force`.

**The printer is detected as `Generic M-series`.** Its advertised name did not
match a known model. Pass `--model ID` after checking `mbprint printers`, then
save that model in config.

**Output is offset or clipped.** Check a raster preview and adjust
`--align`, `--offset-x`, and `--offset-y` before spending media.

**A BLE printer does not appear in `scan`.** Make sure it is powered on, the
adapter is enabled, and the printer is not connected to another app. BLE does
not require pairing. Classic Bluetooth printers use the `bluetooth` or `serial`
transport instead.

More model- and transport-specific guidance is in
[Printers and transports](docs/printers-and-transports.md#troubleshooting).

## Documentation

- [Data, templates, and filters](docs/data-and-templates.md)
- [Printers and transports](docs/printers-and-transports.md)
- [CLI reference](docs/cli-reference.md)
- [Brother wireless configuration](docs/brother-wireless-config.md)
- [Development and reverse engineering](docs/development.md)

## Development

Install the development group and run the test suite:

```sh
uv sync
uv run pytest tests -q
```

The suite currently contains 88 hardware-free tests. See
[Development and reverse engineering](docs/development.md) for the module map,
test coverage, and the Nix-based printer research tools.
