"""CSV loading and record building for variable-data labels."""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path

from mbprint.layout import substitute
from mbprint.log import get_logger

log = get_logger(__name__)

# Default column aliases, in priority order. First header that matches
# (case-insensitively, ignoring accents/spacing) wins.
DEFAULT_ALIASES: dict[str, list[str]] = {
    "name": ["Name", "Product", "Product Name", "Nom", "Nom du produit", "Designation"],
    "ref": ["Internal Reference", "Reference", "Référence", "SKU", "Default Code", "Code"],
    "sku": ["Internal Reference", "SKU", "Reference", "Référence", "Default Code", "Code"],
    "price": ["Sales Price", "Price", "Prix", "List Price", "Sale Price"],
    "batch": ["Batch", "Lot", "Batch Number", "Lot/Serial Number", "Numero de lot"],
    "qty": ["Quantity On Hand", "Quantity", "Qty", "On Hand", "Quantité"],
    "currency": ["Currency", "Devise"],
    "category": ["Product Category", "Category", "Catégorie"],
    "unit": ["Unit", "Unité", "UoM"],
}

# Derived fields are not hardcoded: they come from --data KEY=TEMPLATE, or from
# the `data` table in the config file. This one is only ever used as an example
# in help text and documentation.
EXAMPLE_QR_TEMPLATE = "https://example.com/shop#{{sku}}[[/{{batch}}]]"


def _normalize(header: str) -> str:
    s = header.strip().lower()
    for a, b in (("é", "e"), ("è", "e"), ("ê", "e"), ("à", "a"), ("ç", "c"), ("û", "u")):
        s = s.replace(a, b)
    return re.sub(r"[^a-z0-9]", "", s)


def _format_price(raw: str, decimal_separator: str = ",") -> tuple[str, str]:
    """Return (price, price_short). Short form drops a zero fraction."""
    value = (raw or "").strip().replace(",", ".")
    try:
        number = float(value)
    except ValueError:
        return raw or "", raw or ""
    if abs(number - round(number)) < 1e-9:
        short = str(round(number))
    else:
        short = f"{number:.2f}".rstrip("0").rstrip(".").replace(".", decimal_separator)
    full = f"{number:.2f}".replace(".", decimal_separator)
    return full, short


@dataclass
class RecordSet:
    headers: list[str]
    records: list[dict]
    mapping: dict[str, str]


def build_mapping(headers: list[str], overrides: dict[str, str] | None = None) -> dict[str, str]:
    """Map logical field names (name, price, sku, ...) to CSV column names."""
    index = {_normalize(h): h for h in headers}
    mapping: dict[str, str] = {}
    for key, aliases in DEFAULT_ALIASES.items():
        for alias in aliases:
            column = index.get(_normalize(alias))
            if column:
                mapping[key] = column
                break
    log.debug(
        "auto-mapped %s", ", ".join(f"{k}<-{v}" for k, v in sorted(mapping.items())) or "nothing"
    )
    for key, column in (overrides or {}).items():
        if column not in headers:
            match = index.get(_normalize(column))
            if not match:
                raise SystemExit(
                    f"--map {key}={column}: no such column. Available: {', '.join(headers)}"
                )
            column = match
        mapping[key] = column
    return mapping


def load_csv(path: str | Path) -> tuple[list[str], list[dict]]:
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        raise SystemExit(f"CSV not found: {p}")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(text.splitlines(), dialect=dialect)
    headers = [h for h in (reader.fieldnames or []) if h is not None]
    rows = [{k: (v or "").strip() for k, v in row.items() if k is not None} for row in reader]
    rows = [r for r in rows if any(r.values())]
    log.debug("%s: %d columns, %d rows", p, len(headers), len(rows))
    if not headers:
        raise SystemExit(f"{p}: no header row found")
    return headers, rows


def apply_data(record: dict, entries: list[tuple[str, str]], decimal: str = ",") -> dict:
    """Evaluate `--data KEY=TEMPLATE` pairs in order, in place.

    Order matters: each entry can use the fields defined before it, so
    `--data sku={{ref}} --data qr=".../{{sku}}"` works.
    """
    for key, template in entries:
        record[key] = substitute(template, record, decimal)
    return record


def build_records(
    path: str | Path,
    data_entries: list[tuple[str, str]] | None = None,
    overrides: dict[str, str] | None = None,
    decimal_separator: str = ",",
    filters: list[tuple[str, str]] | None = None,
    limit: int | None = None,
) -> RecordSet:
    """Load a CSV into label records: raw columns plus normalized + derived fields."""
    headers, rows = load_csv(path)
    mapping = build_mapping(headers, overrides)
    records = []
    for row in rows:
        record = dict(row)  # raw column names stay usable as {{Column}}
        for key, column in mapping.items():
            record[key] = row.get(column, "")
        record.setdefault("batch", "")
        price, price_short = _format_price(record.get("price", ""), decimal_separator)
        record["price"] = price
        record["price_short"] = price_short
        record["sku"] = record.get("sku") or record.get("ref", "")
        record["ref"] = record.get("ref") or record.get("sku", "")
        apply_data(record, data_entries or [], decimal_separator)
        records.append(record)

    for column, wanted in filters or []:
        col = column
        if col not in headers:
            index = {_normalize(h): h for h in headers}
            col = index.get(_normalize(column), column)
        records = [r for r in records if str(r.get(col, "")).lower() == wanted.lower()]

    if limit is not None:
        records = records[:limit]
    log.debug(
        "built %d record(s); derived fields: %s",
        len(records),
        ", ".join(k for k, _ in data_entries or []) or "none",
    )
    return RecordSet(headers=headers, records=records, mapping=mapping)


def copies_for(record: dict, copies: int, copies_from: str | None) -> int:
    """How many times to print one record."""
    if not copies_from:
        return max(1, copies)
    raw = record.get(copies_from)
    if raw is None:
        index = {_normalize(k): k for k in record}
        key = index.get(_normalize(copies_from))
        raw = record.get(key, "") if key else ""
    try:
        n = int(float(str(raw).replace(",", ".") or 0))
    except ValueError:
        n = 0
    return max(0, n) * max(1, copies)
