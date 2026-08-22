# Data, templates, and filters

## Record construction

`label.json` addresses data through `{{placeholders}}`. One record is built per
CSV row from three layers.

First, every original column remains addressable by its header, for example
`{{Product Category}}`.

Second, common Odoo-style columns are exposed under normalized field names:

| Field | Automatically recognized columns |
|---|---|
| `name` | Name, Product, Product Name, Nom, and related aliases |
| `ref`, `sku` | Internal Reference, Reference, SKU, Default Code, Code |
| `price` | Sales Price, Price, List Price, Prix |
| `qty` | Quantity On Hand, Quantity, Qty, On Hand |
| `batch` | Batch, Lot, Lot/Serial Number, and related aliases |
| `currency` | Currency, Devise |
| `category` | Product Category, Category |
| `unit` | Unit, UoM |

Matching ignores case, spacing, punctuation, and common accents. Override a
mapping explicitly when needed:

```sh
mbprint print -l label.json -c inventory.csv \
  --map batch="Lot/Serial Number"
```

`price` is normalized to two decimals. `price_short` drops a zero fractional
part, so `35.00` becomes `35`; otherwise it removes trailing zeroes. The default
decimal separator is a comma, and `--decimal .` changes it.

Finally, derived fields come from repeatable `--data KEY=TEMPLATE` options:

```sh
mbprint print -l label.json -c inventory.csv \
  --data brand="Example Ceramics" \
  --data qr="https://example.com/{{brand}}/{{sku}}[[/{{batch}}]]"
```

Entries are evaluated in order, so `qr` above can use `brand`. A template with
no placeholders is a constant. `--set KEY=VALUE` is retained as an alias for
that case. With no `--csv`, one record is built from `--data` alone.

Save reusable templates in config:

```sh
mbprint config set data.brand "Example Ceramics"
mbprint config set data.qr "https://example.com/{{sku}}[[/{{batch}}]]"
```

Config templates are evaluated first in definition order, followed by command
line entries.

Placeholders, filters, and optional segments work the same way in an SVG
layout, in text and in attribute values. See
[SVG labels as templates](svg-templates.md).

## Fonts in label.json

`fontFamily` (or the compact `font` key) is resolved as an exact installed or
bundled family. Rendering stops when that family and requested bold/italic face
is unavailable; this prevents silent changes to wrapping and alignment.

Portable label bundles can keep font files beside the label:

```text
product-label/
  label.json
  fonts/
    ExampleSans-Regular.ttf
    ExampleSans-Bold.ttf
```

The `fonts/` directory is scanned recursively for `.ttf`, `.otf`, and `.ttc`
files. The same files can be shipped application-wide under `mbprint/fonts/`,
or supplied from repeatable `--font-dir PATH` options. `MBPRINT_FONT_DIR`
accepts the platform path separator for configured directories.

Install every maintained redistributable font add-on for a runtime checkout
with:

```console
uv sync --no-dev --extra fonts
```

Developers should omit `--no-dev` so the test and lint dependencies remain
installed:

```console
uv sync --extra fonts
```

The `fonts` extra installs the DejaVu, Phomymo, Nerd, and compatible-font
wheels together. Installation registers all four automatically; labels do not
need `--font-dir` to find them.

Separately distributed font wheels can advertise one or more directories
through the `mbprint.font_bundles` entry point;
`packages/mbprint-fonts-dejavu` is a packaging example.

Smaller installations can select one bundle:

```console
uv sync --extra fonts-dejavu
uv sync --extra fonts-phomymo
uv sync --extra fonts-nerd
uv sync --extra fonts-compatible
```

The Phomymo bundle contains Inter, Roboto, Open Sans, Lato, Montserrat,
Oswald, Playfair Display, Merriweather, Roboto Mono, and Source Code Pro. CSS
family stacks exported by Phomymo, such as `Inter, sans-serif`, resolve their
primary family exactly. Its proprietary OS-font choices (Arial, Helvetica,
Georgia, Times New Roman, Courier New, Impact, and Comic Sans MS) work when
installed locally but are not redistributed. The compatible-font add-on
provides these explicit `--font-fallback` mappings:

| Missing requested family | Free substitute |
|---|---|
| Arial or Helvetica | Liberation Sans |
| Georgia | Gelasio |
| Times New Roman | Liberation Serif |
| Courier New | Liberation Mono |
| Impact | Anton |
| Comic Sans MS | Comic Neue |

Liberation is metric-compatible with Arial, Times New Roman, and Courier New.
The remaining mappings are visually similar rather than metric-compatible.
Strict mode never applies these aliases.

The Nerd bundle contains JetBrainsMono Nerd Font in its four common styles.
Any other Nerd Font can be installed through another add-on or supplied using
the normal adjacent `fonts/`, `--font-dir`, or `MBPRINT_FONT_DIR` mechanisms.

Include every style used by the layout. If a label requests bold, having only
the regular face is an error. Font licenses must permit redistribution when
files are committed or packaged.

Font fallback is enabled by default for `print`, `pdf`, `svg`, and `preview`.
Missing families use a compatible mapping above when available, then a matching
generic sans-serif, serif, or monospace font. Every substitution emits a
warning.

Require exact matching for one command with:

```sh
mbprint pdf -l product-label/label.json --no-font-fallback -o labels.pdf
```

Make strict matching persistent with:

```sh
mbprint config set font_fallback false
```

`--font-fallback` overrides that configured false value for one command.
`mbprint config unset font_fallback` returns to the built-in fallback-enabled
default.

## Optional segments

`[[...]]` marks a segment that disappears when every field inside it is empty:

```text
#{{sku}}[[/{{batch}}]]
```

This becomes `#AG-EX-0001` without a batch and `#AG-EX-0001/L7` with batch
`L7`. Optional segments work in layout text, QR data, barcode data, and derived
templates.

## Filters

Filters can be used anywhere placeholders are supported:

| Filter | Example | Result |
|---|---|---|
| `num` | `{{price|num}}` | `49,5`, with insignificant zeroes removed |
| `num:N` | `{{price|num:2}}` | `49,50`, with fixed decimals |
| `upper` | `{{name|upper}}` | `BETA WIDGET` |
| `lower` | `{{sku|lower}}` | `bw-1` |
| `title` | `{{name|title}}` | `Beta Widget` |
| `capitalize` | `{{name|capitalize}}` | `Beta widget` |
| `trim` | `{{name|trim}}` | surrounding spaces removed |
| `truncate:N` | `{{name|truncate:8}}` | `Beta Wi…` |
| `default:X` | `{{batch|default:n/a}}` | `n/a` when empty |
| `slug` | `{{name|slug}}` | `beta-widget`, with accents folded |
| `urlencode` | `{{name|urlencode}}` | `Beta%20Widget` |
| `replace:a:b` | `{{sku|replace:-:_}}` | `BW_1` |

Filters chain from left to right. For example,
`{{name|truncate:8|upper}}` produces `BETA WI…`. `num` uses the selected
decimal separator and leaves unparseable values unchanged. Unknown filters are
reported as errors.

`{{price_short}}` is equivalent to `{{price|num}}` and remains available for
existing layouts.

## Missing fields

Before `print`, `pdf`, `svg`, or `preview` renders output, every required placeholder
is checked against every expanded record. Placeholders used only inside an
optional segment are not required. A `default:` filter also satisfies the
check.

When a field is empty, an interactive terminal asks whether to proceed. A
redirected or scripted command exits with status 1. Resolve it in one of four
ways:

- `--data field="..."` supplies a value.
- `--map field=COLUMN` maps a CSV column.
- `[[...]]` makes a layout segment optional.
- `--force` (also `--ignore-missing`) continues with the empty value.

An explicit `--data field=""` declares the field deliberately empty and
silences its warning.

Inspect the layout and source before rendering:

```sh
mbprint fields -l label.json -c examples/inventory-sample.csv
```

The report includes label geometry, placeholders, optional placeholders,
derived templates, column mappings, first-record values, and missing counts.
The current `fields` report always analyzes the complete CSV; `--filter`,
`--limit`, and copy options affect output commands but not that report.

## Selecting records and copies

For `print`, `pdf`, `svg`, and `preview`:

- `--filter COLUMN=VALUE` keeps matching rows and is repeatable.
- `--limit N` keeps the first N records after filtering.
- `--copies N` creates a fixed number of copies per record.
- `--copies-from COLUMN` reads the copy count from each record.
- Combining both copy options multiplies their values.

The filter comparison is case-insensitive. A missing, invalid, or zero
`--copies-from` value produces no copies for that row.
