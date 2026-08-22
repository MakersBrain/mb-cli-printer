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
