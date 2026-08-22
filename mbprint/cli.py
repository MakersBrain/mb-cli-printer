"""mbprint command line interface."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

from PIL import Image

from mbprint import __version__, brother, ipp, layout, pdf, printers, protocol, svg, ui, wireless
from mbprint import config as cfg
from mbprint import data as datamod
from mbprint import log as mblog
from mbprint import media as mediamod
from mbprint import raster as R
from mbprint.transport import Transport
from mbprint.transport import build as build_transport

# Every command takes the parsed argparse namespace; `_pick` reads attributes
# that only some subparsers define, falling back to the config file.
Args = argparse.Namespace

log = mblog.get_logger("mbprint.cli")


def _pairs(values: list[str] | None, flag: str) -> list[tuple[str, str]]:
    """KEY=VALUE arguments, keeping the order they were given in."""
    out: list[tuple[str, str]] = []
    for item in values or []:
        if "=" not in item:
            raise SystemExit(f"{flag} expects KEY=VALUE, got {item!r}")
        key, _, value = item.partition("=")
        out.append((key.strip(), value))
    return out


def _kv(values: list[str] | None, flag: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in values or []:
        if "=" not in item:
            raise SystemExit(f"{flag} expects KEY=VALUE, got {item!r}")
        key, _, value = item.partition("=")
        out[key.strip()] = value.strip()
    return out


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


# --- shared option groups --------------------------------------------------


def logging_parser() -> argparse.ArgumentParser:
    """Options shared by every subcommand, so `-v` works before or after it."""
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="-v traces each protocol command, -vv every write",
    )
    p.add_argument("-q", "--quiet", action="store_true", help="warnings and errors only")
    p.add_argument(
        "--log-file",
        default=None,
        help="append a full trace to this file, whatever the console shows",
    )
    p.add_argument(
        "--plain", action="store_true", help="no colors and no progress bar, even on a terminal"
    )
    return p


def add_source_options(p: argparse.ArgumentParser) -> None:
    p.add_argument("--label", "-l", default=None, help="label.json layout file")
    p.add_argument("--csv", "-c", default=None, help="CSV of records to print")
    p.add_argument(
        "--data",
        action="append",
        metavar="KEY=TEMPLATE",
        help="define a field from a template (repeatable, evaluated in "
        "order, may reference earlier fields, filters allowed): "
        f'--data qr="{datamod.EXAMPLE_QR_TEMPLATE}". '
        "[[...]] segments vanish when their fields are empty",
    )
    p.add_argument(
        "--set",
        action="append",
        metavar="KEY=VALUE",
        dest="data",
        help="alias for --data, for plain constants",
    )
    p.add_argument(
        "--map",
        action="append",
        metavar="FIELD=COLUMN",
        help="map a label field to a CSV column (repeatable)",
    )
    p.add_argument(
        "--force",
        "--ignore-missing",
        action="store_true",
        dest="force",
        help="print even when fields are missing, without asking",
    )
    p.add_argument("--decimal", default=",", help="decimal separator for prices (default ',')")
    p.add_argument(
        "--filter",
        action="append",
        metavar="COLUMN=VALUE",
        help="keep only rows where COLUMN equals VALUE (repeatable)",
    )
    p.add_argument("--limit", type=int, default=None, help="only the first N records")
    p.add_argument("--copies", type=int, default=1, help="copies of each label (default 1)")
    p.add_argument(
        "--copies-from",
        default=None,
        metavar="COLUMN",
        help="take the copy count from this CSV column, e.g. 'Quantity On Hand'",
    )


def add_render_options(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--dither",
        default=None,
        choices=list(R.DITHER_MODES),
        help="halftoning for the 1-bit conversion (default auto)",
    )


def add_printer_options(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--model",
        "-m",
        default=None,
        help="printer model id (see 'mbprint printers'); default auto-detect",
    )
    p.add_argument(
        "--transport",
        "-t",
        default=None,
        choices=["ble", "bluetooth", "tcp", "serial", "usb", "file"],
        help="transport (default ble; the wifi command defaults to usb)",
    )
    p.add_argument("--host", default=None, help="printer hostname or IP, for --transport tcp")
    p.add_argument(
        "--rfcomm-channel",
        type=int,
        default=1,
        help="RFCOMM channel for --transport bluetooth (default 1)",
    )
    p.add_argument("--tcp-port", type=int, default=9100, help="TCP port (default 9100)")
    p.add_argument("--address", default=None, help="BLE MAC address")
    p.add_argument("--device", default=None, help="BLE device name (substring match)")
    p.add_argument("--port", default=None, help="serial/rfcomm port, e.g. /dev/rfcomm0")
    p.add_argument("--baud", type=int, default=115200, help="serial baud rate")
    p.add_argument("--usb-vid", default=None, help="USB vendor id, e.g. 0x0483")
    p.add_argument("--usb-pid", default=None, help="USB product id")
    p.add_argument("--usb-serial", default=None, help="select one USB printer by serial number")
    p.add_argument("--usb-bus", type=int, default=None, help="select a USB bus number")
    p.add_argument("--usb-address", type=int, default=None, help="select a USB device address")
    p.add_argument("--usb-interface", type=int, default=0, help="USB interface number (default 0)")
    p.add_argument("--usb-alt", type=int, default=0, help="USB alternate setting (default 0)")
    p.add_argument(
        "--out",
        "-o",
        default=None,
        help="capture path for --transport file or a wifi --dry-run",
    )
    p.add_argument("--density", type=int, default=None, help="print density 1-8 (default 6)")
    p.add_argument("--feed", type=int, default=None, help="feed after each label in dots")
    p.add_argument("--speed", type=int, default=None, help="print speed (M110/TSPL)")
    p.add_argument(
        "--continuous", action="store_true", default=None, help="continuous media: no gap detection"
    )
    p.add_argument(
        "--align",
        default=None,
        choices=["left", "center", "right"],
        help="roller alignment: where the label sits under the head",
    )
    p.add_argument(
        "--offset-x", type=int, default=None, help="roller alignment nudge across the head, in dots"
    )
    p.add_argument(
        "--offset-y", type=int, default=None, help="roller alignment nudge along the feed, in dots"
    )
    p.add_argument(
        "--media",
        default=None,
        help="Brother DK media id, e.g. 62 or 102x152 (default: inferred from the label size)",
    )
    p.add_argument("--no-cut", action="store_true", help="Brother: do not cut after printing")
    p.add_argument("--cut-every", type=int, default=1, help="Brother: cut every N labels")
    p.add_argument(
        "--no-compress", action="store_true", help="Brother: send raster lines uncompressed"
    )
    p.add_argument("--gap-mm", type=float, default=None, help="TSPL gap between labels in mm")
    p.add_argument("--tspl-offset-mm", type=float, default=None, help="TSPL OFFSET in mm")
    p.add_argument(
        "--mtu",
        type=int,
        default=None,
        help="cap the write size in bytes (default: negotiated link MTU)",
    )
    p.add_argument(
        "--chunk-delay",
        type=int,
        default=None,
        help="ms between data chunks (default per protocol)",
    )


# --- resolution helpers ----------------------------------------------------


def _pick(args: Args, key: str, default: Any) -> Any:
    value = getattr(args, key, None)
    if value is not None:
        return value
    stored = cfg.load().get(key)
    return default if stored is None else stored


def _resolve_label(args: Args) -> layout.Label:
    path = _pick(args, "label", None) or "label.json"
    return layout.Label.load(path)


def _data_entries(args: Args) -> list[tuple[str, str]]:
    """Field templates: the config `data` table first, then --data / --set."""
    entries = cfg.data_templates()
    if entries:
        log.debug("config data templates: %s", ", ".join(k for k, _ in entries))
    return entries + _pairs(getattr(args, "data", None), "--data")


def _resolve_records(args: Args) -> list[datamod.Record]:
    entries = _data_entries(args)
    if args.csv:
        rs = datamod.build_records(
            args.csv,
            data_entries=entries,
            overrides=_kv(getattr(args, "map", None), "--map"),
            decimal_separator=args.decimal,
            filters=[tuple(f.split("=", 1)) for f in (args.filter or []) if "=" in f],
            limit=args.limit,
        )
        records = rs.records
    else:
        record: dict[str, str] = {"batch": ""}
        for key, template in entries:
            record[key] = layout.substitute(template, record, args.decimal)
        record["sku"] = record.get("sku") or record.get("ref", "")
        record["ref"] = record.get("ref") or record.get("sku", "")
        if record.get("price"):
            record["price"], record["price_short"] = datamod._format_price(
                record["price"], args.decimal
            )
        # Re-evaluate so templates can use the normalized fields above.
        datamod.apply_data(record, entries, args.decimal)
        records = [record]

    expanded: list[datamod.Record] = []
    for record in records:
        for _ in range(datamod.copies_for(record, args.copies, args.copies_from)):
            expanded.append(record)
    if not expanded:
        raise SystemExit("no records to print (check --filter / --copies-from)")
    return expanded


def _check_missing(label: layout.Label, records: list[datamod.Record], args: Args) -> None:
    """Warn about fields no record can fill, and ask before printing them blank."""
    # A field defined explicitly, even as an empty string, is a deliberate choice.
    defined = {k for k, _ in _data_entries(args)}
    counts: dict[str, int] = {}
    for record in records:
        for field in label.missing_for(record):
            if field not in defined:
                counts[field] = counts.get(field, 0) + 1
    if not counts:
        return

    total = len(records)
    for field, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        log.warning("%s: empty on %d of %d label%s", field, n, total, "" if total == 1 else "s")
    example = sorted(counts)[0]
    log.warning(
        'give it a value with --data %s="...", point it at a column with '
        "--map %s=COLUMN, wrap it in [[...]] in the layout to make it "
        'optional, or silence it with --data %s=""',
        example,
        example,
        example,
    )

    if getattr(args, "force", False):
        log.warning("proceeding anyway (--force)")
        return
    if not sys.stdin.isatty():
        raise SystemExit(
            "refusing to print labels with missing fields; pass --force to proceed anyway"
        )
    if not _ask("Print anyway?"):
        raise SystemExit("cancelled")


def _ask(question: str) -> bool:
    console = ui.console()
    try:
        if console is not None:
            from rich.prompt import Confirm

            return Confirm.ask(f"[yellow]{question}[/yellow]", default=False, console=console)
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


def _reported_size(args: Args, printer: printers.PrinterDef) -> tuple[float, float] | None:
    """Ask the printer itself what roll is loaded, however we can reach it.

    Over Bluetooth, serial or USB the raster status block answers directly.
    Networked QLs stay silent on port 9100 but do answer IPP on 631.
    """
    transport_kind = _pick(args, "transport", "ble")
    if transport_kind == "tcp":
        host = getattr(args, "host", None) or cfg.load().get("host")
        info = ipp.loaded_media(host) if host else None
        if info and info.get("reasons"):
            log.warning("printer reports: %s", ", ".join(info["reasons"]))
        return info.get("size_mm") if info else None
    if transport_kind not in ("bluetooth", "serial", "usb"):
        return None
    if getattr(args, "dry_run", False):
        # A dry run touches no hardware, so there is nobody to ask.
        log.debug("dry run: taking the media from the label size, not the printer")
        return None

    async def query() -> dict[str, Any] | None:
        transport = _make_transport(args, printer)
        async with transport:
            return await protocol.brother_query_status(transport, printer)

    try:
        status = asyncio.run(query())
    except SystemExit as exc:
        log.debug("status query failed, falling back to the label size: %s", exc)
        return None
    if not status or not status.get("media_width_mm"):
        return None
    if status.get("errors"):
        log.warning("printer reports: %s", ", ".join(status["errors"]))
    return float(status["media_width_mm"]), float(status["media_length_mm"])


def _printer_media(args: Args, printer: printers.PrinterDef) -> mediamod.Media | None:
    """Which roll the printer says is loaded, mapped to a media table entry.

    The printer knows better than the layout does, and printing on the wrong
    roll wastes labels, so this wins over inferring from the label size.
    """
    size = _reported_size(args, printer)
    if not size:
        return None
    width, length = size
    media = mediamod.from_size(width, length, printer.id)
    if media is None:
        log.warning(
            "the printer reports %gx%gmm media, which is not in the media table; "
            "pass --media to choose the closest roll",
            width,
            length,
        )
        return None
    log.info("printer reports %s loaded (%gx%gmm)", media.id, width, length)
    return media


def _resolve_media(
    args: Args, label: layout.Label, printer: printers.PrinterDef
) -> mediamod.Media | None:
    """Which DK roll a Brother job prints on; None for every other family."""
    return _resolve_media_size(args, label.width_mm, label.height_mm, printer)


def _resolve_media_size(
    args: Args, width_mm: float, height_mm: float, printer: printers.PrinterDef
) -> mediamod.Media | None:
    """Resolve Brother media from an arbitrary physical page size."""
    if printer.protocol != "brother":
        return None
    explicit = _pick(args, "media", None)
    media = mediamod.resolve(explicit, width_mm, height_mm, printer.id) if explicit else None
    media = media or _printer_media(args, printer)
    if media is None:
        media = mediamod.resolve(None, width_mm, height_mm, printer.id)
    log.info(
        "media: %s (%s), printable %dx%s dots, %d dots right margin",
        media.id,
        media.name,
        media.dots_printable[0],
        media.dots_printable[1] or "endless",
        media.offset_r,
    )
    return media


def _render_scale(
    label: layout.Label, printer: printers.PrinterDef, media: mediamod.Media | None = None
) -> float:
    """Render straight at the head's resolution instead of upscaling a 203dpi bitmap."""
    if media is not None:
        return mediamod.render_scale(media, label.width_px)
    if not label.dots_per_mm:
        return 1.0
    return printer.dpi / (pdf.MM_PER_INCH * label.dots_per_mm)


def _render_all(
    label: layout.Label,
    records: list[datamod.Record],
    scale: float = 1.0,
    decimal: str = ",",
) -> list[Image.Image]:
    return [layout.render(label, record, scale=scale, decimal=decimal) for record in records]


def _print_options(
    args: Args,
    printer: printers.PrinterDef,
    label: layout.Label | None = None,
    media: mediamod.Media | None = None,
) -> protocol.PrintOptions:
    return protocol.PrintOptions(
        density=int(_pick(args, "density", 6)),
        feed=int(_pick(args, "feed", 32)),
        continuous=bool(_pick(args, "continuous", label.continuous if label else False)),
        speed=int(_pick(args, "speed", 5)),
        copies=1,  # copies are already expanded into the record list
        gap_mm=float(_pick(args, "gap_mm", 3.0)),
        tspl_offset_mm=float(_pick(args, "tspl_offset_mm", -3.0)),
        chunk_delay_ms=args.chunk_delay,
        align=str(_pick(args, "align", printer.alignment if printer else "center")),
        offset_x=int(_pick(args, "offset_x", 0)),
        offset_y=int(_pick(args, "offset_y", 0)),
        label_width_mm=label.width_mm if label else None,
        label_height_mm=label.height_mm if label else None,
        media=media,
        cut=not getattr(args, "no_cut", False),
        cut_every=getattr(args, "cut_every", 1),
        compress=not getattr(args, "no_compress", False),
    )


# What an unconfigured BLE link typically negotiates, used to time dry runs.
SIMULATED_MTU = 244


def _make_transport(args: Args, printer: printers.PrinterDef | None) -> Transport:
    kind = _pick(args, "transport", "ble")
    mtu = args.mtu
    if getattr(args, "dry_run", False):
        # Same protocol flow, same chunking, same pacing, no hardware.
        return build_transport(
            "file", path=args.out or os.devnull, max_write=mtu or SIMULATED_MTU, pace=True
        )
    if kind == "ble":
        return build_transport(
            "ble",
            address=_pick(args, "address", None),
            device_name=_pick(args, "device", None),
            max_write=mtu,
        )
    if kind == "bluetooth":
        address = _pick(args, "address", None)
        if not address:
            raise SystemExit(
                "bluetooth transport needs --address, e.g. --address 74:97:79:16:1A:1E"
            )
        return build_transport(
            "bluetooth",
            address=address,
            channel=getattr(args, "rfcomm_channel", 1),
            max_write=mtu or 1024,
        )
    if kind == "tcp":
        host = args.host or cfg.load().get("host")
        if not host:
            raise SystemExit("tcp transport needs --host, e.g. --host 192.168.1.50")
        return build_transport("tcp", host=host, port=args.tcp_port, max_write=mtu or 4096)
    if kind == "serial":
        port = args.port or cfg.load().get("port")
        if not port:
            raise SystemExit("serial transport needs --port, e.g. --port /dev/rfcomm0")
        return build_transport("serial", port=port, baudrate=args.baud, max_write=mtu or 512)
    if kind == "usb":
        vid = int(args.usb_vid, 0) if args.usb_vid else None
        pid = int(args.usb_pid, 0) if args.usb_pid else None
        return build_transport(
            "usb",
            vid=vid,
            pid=pid,
            max_write=mtu or 512,
            interface=getattr(args, "usb_interface", 0),
            alternate=getattr(args, "usb_alt", 0),
            serial=getattr(args, "usb_serial", None),
            bus=getattr(args, "usb_bus", None),
            address=getattr(args, "usb_address", None),
        )
    if kind == "file":
        return build_transport("file", path=args.out or "-", max_write=mtu or 512)
    raise SystemExit(f"unknown transport {kind!r}")


# --- commands --------------------------------------------------------------


def cmd_printers(args: Args) -> int:
    defs = printers.all_definitions()
    if args.json:
        import json

        print(json.dumps([d.__dict__ for d in defs], indent=2))
        return 0
    width = max(len(d.id) for d in defs)
    group = None
    for d in sorted(defs, key=lambda x: (x.group, x.id)):
        if d.group != group:
            group = d.group
            print(f"\n{group or 'Other'}")
        head = f"{d.width_bytes * 8}px" if d.width_bytes else "auto"
        print(f"  {d.id:<{width}}  {d.protocol:<9} {head:>6} {d.dpi}dpi  {d.name}")
    print(
        "\nWrite chunking per protocol:",
        ", ".join(f"{k}={v}B" for k, v in printers.PROTOCOL_CHUNK.items()),
    )
    return 0


def cmd_scan(args: Args) -> int:
    from mbprint.transport.ble import scan

    found = asyncio.run(scan(args.timeout))
    if not found:
        print("no BLE devices found")
        return 1
    for address, name in sorted(found, key=lambda x: x[1] or ""):
        d = printers.detect(name)
        tag = (
            f"-> {d.id} ({d.protocol}, {d.width_bytes * 8 if d.width_bytes else '?'}px)"
            if d
            else ""
        )
        print(f"{address}  {name or '(unnamed)':<24} {tag}")
    return 0


def cmd_fields(args: Args) -> int:
    label = _resolve_label(args)
    optional = [f for f in label.placeholders() if f not in label.placeholders(True)]
    print(
        f"label: {label.name}  {label.width_mm}x{label.height_mm}mm "
        f"({label.width_px}x{label.height_px} dots at {label.dots_per_mm}/mm)"
    )
    print("placeholders:", ", ".join(label.placeholders()) or "(none)")
    if optional:
        print("optional:", ", ".join(optional), "(inside [[...]] segments)")

    entries = _data_entries(args)
    if entries:
        print("\nderived fields:")
        for key, template in entries:
            print(f"  {key:<12} = {template}")

    if args.csv:
        rs = datamod.build_records(
            args.csv,
            data_entries=entries,
            overrides=_kv(getattr(args, "map", None), "--map"),
            decimal_separator=args.decimal,
        )
        print("\ncolumns:", ", ".join(rs.headers))
        print("mapping:")
        for key in sorted(rs.mapping):
            print(f"  {key:<10} <- {rs.mapping[key]}")
        if rs.records:
            print(f"\nfirst record ({len(rs.records)} total):")
            for key in label.placeholders():
                print(f"  {key:<12} = {rs.records[0].get(key, '')!r}")
            missing: dict[str, int] = {}
            for record in rs.records:
                for field in label.missing_for(record):
                    missing[field] = missing.get(field, 0) + 1
            if missing:
                print("\nmissing:")
                for field, n in sorted(missing.items(), key=lambda kv: -kv[1]):
                    print(f"  {field:<12} empty on {n}/{len(rs.records)} labels")
                print(f'  define one with: --data {sorted(missing)[0]}="..."')
    return 0


def cmd_preview(args: Args) -> int:
    label = _resolve_label(args)
    records = _resolve_records(args)
    _check_missing(label, records, args)
    printer = printers.resolve(_pick(args, "model", None), _pick(args, "device", None))
    preview_media = _resolve_media(args, label, printer) if args.raster else None
    scale = (
        _render_scale(label, printer, preview_media)
        if preview_media
        else (_render_scale(label, printer) if args.printer_scale else 1.0)
    )
    out_dir = Path(args.out or "preview")
    out_dir.mkdir(parents=True, exist_ok=True)
    dither = _pick(args, "dither", "auto")
    for i, record in enumerate(records, 1):
        img = layout.render(label, record, scale=scale, decimal=args.decimal)
        if args.raster:
            opts = protocol.PrintOptions(
                align=str(_pick(args, "align", printer.alignment)),
                offset_x=int(_pick(args, "offset_x", 0)),
                offset_y=int(_pick(args, "offset_y", 0)),
                label_width_mm=label.width_mm,
                label_height_mm=label.height_mm,
                media=preview_media,
            )
            if preview_media is not None:
                img = mediamod.fit(img, preview_media, printer.min_rows)
            img = R.to_image(protocol.prepare_raster(img, printer, opts, dither))
        elif args.bilevel:
            img = R.to_bilevel(img, dither)
        if args.zoom > 1:
            img = img.resize((img.width * args.zoom, img.height * args.zoom), 0)
        name = (record.get("ref") or record.get("sku") or f"label{i}").replace("/", "-")
        path = out_dir / f"{i:03d}-{name}.png"
        img.save(path)
        print(path)
    return 0


def cmd_pdf(args: Args) -> int:
    label = _resolve_label(args)
    records = _resolve_records(args)
    _check_missing(label, records, args)
    model = _pick(args, "model", None)
    device = _pick(args, "device", None)
    printer = printers.resolve(model, device) if model or device else None
    scale = (
        args.scale
        if args.scale is not None
        else (_render_scale(label, printer) if printer else 1.0)
    )
    if scale <= 0:
        raise SystemExit("PDF render scale must be positive")
    images = _render_all(label, records, scale=scale, decimal=args.decimal)
    dots_per_mm = label.dots_per_mm * scale
    if printer is not None:
        log.info("PDF raster: %s [%s] at %ddpi", printer.name, printer.id, printer.dpi)
    out = args.out or "labels.pdf"
    dither = _pick(args, "dither", "auto")
    if args.sheet:
        path = pdf.write_sheet(
            images,
            out,
            dots_per_mm=dots_per_mm,
            page=args.sheet,
            margin_mm=args.margin,
            gap_mm=args.gap,
            columns=args.columns,
            rows=args.rows,
            marks=not args.no_marks,
            bilevel=args.bilevel,
            dither=dither,
            title=label.name,
        )
    else:
        path = pdf.write_labels(
            images,
            out,
            dots_per_mm=dots_per_mm,
            bilevel=args.bilevel,
            dither=dither,
            title=label.name,
        )
    print(f"{path}  ({len(images)} label{'s' if len(images) != 1 else ''})")
    return 0


def cmd_svg(args: Args) -> int:
    """Write one exact-size SVG file per expanded label record."""
    label = _resolve_label(args)
    records = _resolve_records(args)
    _check_missing(label, records, args)
    destination = Path(args.out or "svg")
    single_file = len(records) == 1 and destination.suffix.lower() == ".svg"
    if not single_file:
        destination.mkdir(parents=True, exist_ok=True)
    for index, record in enumerate(records, 1):
        name = (record.get("ref") or record.get("sku") or f"label{index}").replace("/", "-")
        path = destination if single_file else destination / f"{index:03d}-{name}.svg"
        svg.write(label, record, path, decimal=args.decimal)
        print(path)
    return 0


def cmd_print(args: Args) -> int:
    label = _resolve_label(args)
    records = _resolve_records(args)
    _check_missing(label, records, args)
    printer = printers.resolve(_pick(args, "model", None), _pick(args, "device", None))
    media = _resolve_media(args, label, printer)
    scale = _render_scale(label, printer, media)
    dither = _pick(args, "dither", "auto")
    opts = _print_options(args, printer, label, media)

    rasters = []
    for record in records:
        img = layout.render(label, record, scale=scale, decimal=args.decimal)
        if media is not None:
            img = mediamod.fit(img, media, printer.min_rows)
        rasters.append(protocol.prepare_raster(img, printer, opts, dither))

    label_ids = [
        record.get("ref") or record.get("sku") or str(i) for i, record in enumerate(records, 1)
    ]
    return _send_rasters(args, printer, opts, rasters, label_ids, dither)


def _send_rasters(
    args: Args,
    printer: printers.PrinterDef,
    opts: protocol.PrintOptions,
    rasters: list[R.Raster],
    label_ids: list[str],
    dither: str,
) -> int:
    """Send already prepared labels through the common transport/progress flow."""
    if not rasters:
        raise SystemExit("nothing to print")
    log.info(
        "printer: %s [%s] %s %ddpi head=%s%s",
        printer.name,
        printer.id,
        printer.protocol,
        printer.dpi,
        f"{printer.width_px}px" if printer.width_bytes else "auto",
        " rotated" if printer.rotated else "",
    )
    log.info(
        "labels: %d x %dx%d dots, align=%s offset=(%d,%d) dither=%s",
        len(rasters),
        rasters[0].width_px,
        rasters[0].height,
        opts.align,
        opts.offset_x,
        opts.offset_y,
        dither,
    )
    if args.dry_run:
        log.warning("dry run: simulating the print, nothing will be sent to a printer")

    async def run() -> None:
        transport = _make_transport(args, printer)
        started = time.monotonic()
        async with transport:
            chunk = protocol.effective_chunk(printer, transport)
            log.info(
                "transport: %s mtu_payload=%dB chunk=%dB density=%d feed=%d",
                transport.name,
                transport.max_write,
                chunk,
                opts.density,
                opts.feed,
            )
            # A live bar would fight a byte-level trace, so verbose runs log instead.
            show_bar = not args.quiet and not args.verbose
            with ui.progress(len(rasters), show_bar, args.plain) as bar:
                for i, rst in enumerate(rasters, 1):
                    label_id = label_ids[i - 1]
                    log.debug("label %d/%d: %s", i, len(rasters), label_id)
                    bar.label(i, len(rasters), label_id)
                    await protocol.print_raster(transport, printer, rst, opts, bar.chunk)
                    bar.finish_label()
                    if not show_bar:
                        log.info(
                            "[%d/%d] %s %s",
                            i,
                            len(rasters),
                            label_id,
                            "simulated" if args.dry_run else "printed",
                        )
            elapsed = time.monotonic() - started
            sent = sum(len(r.data) for r in rasters)
            log.info(
                "%s %d label%s, %d raster bytes, %.1fs",
                "simulated" if args.dry_run else "printed",
                len(rasters),
                "" if len(rasters) == 1 else "s",
                sent,
                elapsed,
            )

    asyncio.run(run())
    return 0


def _pdf_page_on_media(
    page: pdf.RenderedPage, media: mediamod.Media, printer: printers.PrinterDef, allow_fit: bool
) -> Image.Image:
    """Validate page geometry, auto-rotate a transposed page, then fit printable margins."""
    tolerance = 1.5
    width, height = page.width_mm, page.height_mm
    direct = abs(width - media.width_mm) <= tolerance and (
        media.continuous or abs(height - media.length_mm) <= tolerance
    )
    transposed = abs(height - media.width_mm) <= tolerance and (
        media.continuous or abs(width - media.length_mm) <= tolerance
    )
    image = page.image
    if not direct and transposed:
        image = image.transpose(Image.Transpose.ROTATE_90)
        direct = True
    if not direct and not allow_fit:
        expected = (
            f"{media.width_mm:g}mm wide"
            if media.continuous
            else f"{media.width_mm:g}x{media.length_mm:g}mm"
        )
        raise SystemExit(
            f"PDF page {page.number} is {width:.2f}x{height:.2f}mm, but {media.id} media is "
            f"{expected}; use a correctly sized PDF or pass --fit"
        )
    return mediamod.fit(image, media, printer.min_rows)


def cmd_print_pdf(args: Args) -> int:
    """Rasterize an exact-size PDF and print each selected page as one label."""
    printer = printers.resolve(_pick(args, "model", None), _pick(args, "device", None))
    pages = pdf.render_pages(args.pdf_file, printer.dpi, args.pages)
    first = pages[0]
    for page in pages[1:]:
        if abs(page.width_mm - first.width_mm) > 0.5 or abs(page.height_mm - first.height_mm) > 0.5:
            raise SystemExit(
                f"PDF pages must have one label size; page 1 is "
                f"{first.width_mm:.2f}x{first.height_mm:.2f}mm and page {page.number} is "
                f"{page.width_mm:.2f}x{page.height_mm:.2f}mm"
            )
    media = _resolve_media_size(args, first.width_mm, first.height_mm, printer)
    opts = _print_options(args, printer, media=media)
    opts.label_width_mm = first.width_mm
    opts.label_height_mm = first.height_mm
    dither = _pick(args, "dither", "auto")
    rasters: list[R.Raster] = []
    label_ids: list[str] = []
    for page in pages:
        image = (
            _pdf_page_on_media(page, media, printer, args.fit) if media is not None else page.image
        )
        raster = protocol.prepare_raster(image, printer, opts, dither)
        for copy in range(1, args.copies + 1):
            rasters.append(raster)
            label_ids.append(f"page {page.number}" + (f" copy {copy}" if args.copies > 1 else ""))
    log.info(
        "PDF: %s, pages=%s, size=%.2fx%.2fmm at %ddpi",
        args.pdf_file,
        ",".join(str(page.number) for page in pages),
        first.width_mm,
        first.height_mm,
        printer.dpi,
    )
    return _send_rasters(args, printer, opts, rasters, label_ids, dither)


def cmd_test(args: Args) -> int:
    printer = printers.resolve(_pick(args, "model", None), _pick(args, "device", None))

    async def run() -> None:
        transport = _make_transport(args, printer)
        async with transport:
            log.info(
                "density test on %s via %s (mtu payload %dB)",
                printer.name,
                transport.name,
                transport.max_write,
            )
            await protocol.print_density_test(transport, printer)

    asyncio.run(run())
    return 0


def cmd_status(args: Args) -> int:
    printer = printers.resolve(_pick(args, "model", None), _pick(args, "device", None))

    async def run_brother() -> None:
        transport = _make_transport(args, printer)
        async with transport:
            info = await protocol.brother_query_status(transport, printer)
            print(f"printer: {printer.name} [{printer.id}] via {transport.name}")
            if info is None:
                # Network QLs stay silent on 9100, but answer IPP on 631.
                host = getattr(args, "host", None) or cfg.load().get("host")
                ipp_info = ipp.loaded_media(host) if host else None
                if ipp_info:
                    width, length = ipp_info.get("size_mm") or (0, 0)
                    guess = mediamod.from_size(width, length, printer.id)
                    print(f"model:   {ipp_info.get('model')} (over IPP)")
                    print(
                        f"media:   {ipp_info['keyword']}"
                        + (
                            f"  ->  --media {guess.id} ({guess.name})"
                            if guess
                            else "  (no entry in the media table)"
                        )
                    )
                    print(f"state:   {ipp_info['state']}")
                    print(
                        "issues:  "
                        + (", ".join(ipp_info["reasons"]) if ipp_info["reasons"] else "none")
                    )
                    return
                print("media:   unknown, the printer sent no status reply")
                print(
                    "         network QLs take print jobs on port 9100 but report "
                    "status only over USB, Bluetooth or IPP on port 631;"
                )
                print("         pass --media to say which roll is loaded")
                return
            media_mm = info["media_width_mm"]
            length = info["media_length_mm"]
            print(
                f"media:   {info['media_type']} {media_mm}mm" + (f" x {length}mm" if length else "")
            )
            guess = mediamod.match_size(media_mm, length or 0, printer.id)
            if guess:
                print(f"         looks like --media {guess.id} ({guess.name})")
            print(f"status:  {info['status_type']}, {info['phase']}")
            print("errors:  " + (", ".join(info["errors"]) if info["errors"] else "none"))

    if printer.protocol == "brother":
        asyncio.run(run_brother())
        return 0

    async def run() -> None:
        from mbprint.transport.ble import BLETransport

        transport = _make_transport(args, None)
        # Only BLE carries the notify channel these queries are answered on.
        if not isinstance(transport, BLETransport):
            raise SystemExit("status queries need the BLE or TCP transport")
        async with transport:
            print(f"device: {transport.resolved_name} [{transport.address}]")
            print(f"mtu payload: {transport.max_write} bytes")
            d = printers.detect(transport.resolved_name)
            if d:
                print(f"detected: {d.name} [{d.id}] {d.protocol} {d.dpi}dpi")
            for what in ("battery", "paper", "cover", "firmware", "serial"):
                reply = await transport.query(what)
                print(f"{what:<9}: {reply.hex(' ') if reply else '(no reply)'}")

    asyncio.run(run())
    return 0


def cmd_config(args: Args) -> int:
    data = cfg.load()
    flat = cfg.flatten(data)
    if args.action == "list":
        if not flat:
            print(f"no config yet ({cfg.CONFIG_PATH})")
        for key in sorted(flat):
            print(f"{key} = {flat[key]}")
        return 0
    if args.action == "get":
        if not args.key:
            raise SystemExit("config get needs a key")
        print(flat.get(args.key, ""))
        return 0
    if args.action == "set":
        if not args.key or args.value is None:
            raise SystemExit("config set needs KEY VALUE")
        cfg.set_key(data, args.key, args.value)
        print(f"{cfg.save(data)}: {args.key} = {cfg.flatten(data)[args.key]}")
        return 0
    if args.action == "unset":
        cfg.unset_key(data, args.key)
        print(f"{cfg.save(data)}: removed {args.key}")
        return 0
    raise SystemExit(f"unknown config action {args.action!r}")


def cmd_wifi(args: Args) -> int:
    """Inspect or configure Brother's native Wi-Fi settings protocol."""
    if args.action in ("scan", "status"):

        async def query() -> None:
            transport = _make_transport(args, None)
            async with transport:
                if args.action == "scan":
                    await transport.send(wireless.wifi_scan_start_command())
                    await transport.delay(round(args.scan_wait * 1000))
                    await transport.send(wireless.wifi_scan_result_command())
                    reply = await _collect_response(transport, 3000)
                    points = wireless.parse_access_points(reply)
                    if points:
                        print("SSID                             CH  POWER  SECURITY")
                        for point in points:
                            security = (
                                "enterprise"
                                if point.enterprise
                                else "encrypted"
                                if point.encrypted
                                else "open"
                            )
                            print(
                                f"{point.ssid[:32]:<32} {point.channel:>3} {point.power:>6}  {security}"
                            )
                    else:
                        print("no access points decoded (the printer may not support this query)")
                else:
                    oids = [
                        "458867",  # connected
                        "458967.2",  # IPv4
                        "458877",  # SSID
                        "458880",  # encryption
                        "458881",  # authentication
                        "459138.2",  # infrastructure mode
                        "459138.3",  # Wireless Direct
                    ]
                    replies: dict[str, bytes] = {}
                    for oid in oids:
                        await transport.send(wireless.inquire_command(oid))
                        replies[oid] = await _collect_response(transport, 3000)
                    reply = b"".join(replies.values())
                    connected = wireless.parse_wifi_status(replies["458867"])
                    address = wireless.parse_ip_address(replies["458967.2"])
                    state = (
                        "connected"
                        if connected
                        else "disconnected"
                        if connected is False
                        else "unknown"
                    )
                    print(f"wifi: {state}")
                    print(f"ipv4: {address or 'unknown'}")
                    ssid = wireless.parse_oid_value(replies["458877"], "458877")
                    encryption = wireless.parse_oid_value(replies["458880"], "458880")
                    authentication = wireless.parse_oid_value(replies["458881"], "458881")
                    encryption_names = {
                        str(value): name for name, value in wireless.ENCRYPTIONS.items()
                    }
                    authentication_names = {
                        str(value): name for name, value in wireless.AUTHENTICATIONS.items()
                    }
                    print(f"ssid: {ssid or 'unknown'}")
                    print(
                        "encryption: "
                        + (
                            encryption_names.get(encryption, encryption)
                            if encryption
                            else "unknown"
                        )
                    )
                    print(
                        "authentication: "
                        + (
                            authentication_names.get(authentication, authentication)
                            if authentication
                            else "unknown"
                        )
                    )
                    infrastructure = wireless.parse_oid_value(replies["459138.2"], "459138.2")
                    wireless_direct = wireless.parse_oid_value(replies["459138.3"], "459138.3")
                    print(f"infrastructure: {_enabled(infrastructure)}")
                    print(f"wireless direct: {_enabled(wireless_direct)}")
                if args.raw:
                    print(f"raw ({len(reply)} bytes): {reply.hex(' ') if reply else '(no reply)'}")

        asyncio.run(query())
        return 0

    if not args.ssid:
        raise SystemExit("wifi configure needs --ssid NAME")
    password = args.password
    if args.password_stdin:
        password = sys.stdin.readline().rstrip("\r\n")
    elif password is None and args.authentication != "open":
        if not sys.stdin.isatty():
            raise SystemExit("password required; use --password-stdin or --password")
        password = getpass.getpass("Wi-Fi password: ")

    settings = wireless.WirelessSettings(
        ssid=args.ssid,
        password=password or "",
        encryption=args.encryption,
        authentication=args.authentication,
        infrastructure=not args.no_infrastructure,
        wireless_direct=args.wireless_direct,
        reboot=not args.no_reboot,
    )
    try:
        command = settings.command()
    except ValueError as exc:
        raise SystemExit(str(exc))

    if args.dry_run:
        Path(args.out or "brother-wifi.bin").write_bytes(command)
        print(f"wrote {len(command)} credential-bearing bytes to {args.out or 'brother-wifi.bin'}")
        return 0
    if not args.yes and not _ask(
        f"Configure {args.ssid!r} and reboot the printer? This changes its network settings."
    ):
        raise SystemExit("cancelled")

    async def send() -> None:
        transport = _make_transport(args, None)
        async with transport:
            await transport.send(command)
        print(f"sent {len(command)} bytes via {transport.name}; the printer may now reboot")

    asyncio.run(send())
    return 0


async def _collect_response(
    transport: Transport, first_timeout_ms: int, idle_timeout_ms: int = 250
) -> bytes:
    """Collect a possibly packetized response until the receive side goes idle."""
    chunks: list[bytes] = []
    reply = await transport.wait_for_response(first_timeout_ms)
    while reply:
        chunks.append(reply)
        reply = await transport.wait_for_response(idle_timeout_ms)
    return b"".join(chunks)


def _enabled(value: str | None) -> str:
    return "enabled" if value == "1" else "disabled" if value == "0" else "unknown"


def cmd_usb_info(args: Args) -> int:
    """Show descriptors and standard Printer Class information without changing settings."""
    from mbprint.transport.usb import USBTransport

    async def query() -> None:
        transport = _make_transport(args, None)
        if not isinstance(transport, USBTransport):
            raise SystemExit("usb-info requires --transport usb")
        async with transport:
            info = transport.device_info
            print(f"device:       {info['vid']:04x}:{info['pid']:04x}")
            print(f"location:     bus {info['bus']} address {info['address']}")
            print(f"manufacturer: {info['manufacturer'] or 'unknown'}")
            print(f"product:      {info['product'] or 'unknown'}")
            print(f"serial:       {info['serial'] or 'unknown'}")
            print(
                f"interface:    {info['interface']} alt {info['alternate']} "
                f"protocol {info['protocol']}"
            )
            endpoint_in = info["in_endpoint"]
            print(
                f"endpoints:    OUT 0x{info['out_endpoint']:02x}, "
                + (f"IN 0x{endpoint_in:02x}" if endpoint_in is not None else "IN none")
                + f", {info['packet_size']}-byte packets"
            )
            device_id = await transport.get_device_id()
            port = await transport.get_port_status()
            print(f"device ID:    {device_id or 'unavailable'}")
            if port is None:
                print("port status:  unavailable")
            else:
                print(
                    "port status:  "
                    + ("selected" if port["selected"] else "not selected")
                    + (", paper empty" if port["paper_empty"] else ", paper present")
                    + (", error" if port["error"] else ", no error")
                )

    asyncio.run(query())
    return 0


def cmd_usb_report(args: Args) -> int:
    """Fetch the Brother read-only printer configuration/system report."""
    from mbprint.transport.usb import USBTransport

    async def query() -> None:
        transport = _make_transport(args, None)
        if not isinstance(transport, USBTransport):
            raise SystemExit("usb-report requires --transport usb")
        async with transport:
            if transport.device_info["vid"] != 0x04F9:
                raise SystemExit("usb-report is only supported for Brother printers")
            await transport.send(brother.SYSTEM_REPORT_COMMAND)
            response = await _collect_response(transport, 3000)
        if not response:
            raise SystemExit("printer did not return a system report")
        try:
            if args.json:
                output = json.dumps(brother.parse_system_report(response), indent=2)
            else:
                output = brother.decode_system_report(response)
        except ValueError as exc:
            raise SystemExit(str(exc))
        if args.out:
            Path(args.out).write_text(output + "\n", encoding="utf-8")
            print(f"wrote system report to {args.out}")
        else:
            print(output)

    asyncio.run(query())
    return 0


def cmd_usb_list(args: Args) -> int:
    """List supported USB printers without claiming their interfaces."""
    from mbprint.transport.usb import describe_usb_device, find_usb_devices

    vid = int(args.usb_vid, 0) if args.usb_vid else None
    pid = int(args.usb_pid, 0) if args.usb_pid else None
    devices = [describe_usb_device(dev) for dev in find_usb_devices(vid, pid)]
    if not devices:
        print("no supported USB printers found")
        return 0
    print("BUS  ADDR  USB ID     SERIAL                    PRODUCT")
    for info in devices:
        bus = str(info["bus"]) if info["bus"] is not None else "?"
        address = str(info["address"]) if info["address"] is not None else "?"
        serial = info["serial"] or "-"
        product = info["product"] or info["manufacturer"] or "unknown"
        print(
            f"{bus:>3}  {address:>4}  {info['vid']:04x}:{info['pid']:04x}  "
            f"{serial[:24]:<24}  {product}"
        )
    return 0


# --- parser ----------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    common = logging_parser()
    p = argparse.ArgumentParser(
        prog="mbprint",
        description="Print label.json layouts, or export them as PDF, SVG, and PNG.",
        parents=[common],
    )
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("print", help="render records and print them", parents=[common])
    add_source_options(sp)
    add_render_options(sp)
    add_printer_options(sp)
    sp.add_argument(
        "--dry-run",
        action="store_true",
        help="run the whole flow against a simulated link, sending nothing "
        "to a printer; with --out, capture the bytes it would send",
    )
    sp.set_defaults(func=cmd_print)

    sp = sub.add_parser(
        "print-pdf", help="print each exact-size PDF page as one label", parents=[common]
    )
    sp.add_argument("pdf_file", metavar="PDF", help="PDF whose pages are individual labels")
    add_render_options(sp)
    add_printer_options(sp)
    sp.add_argument("--pages", help="one-based pages and ranges, e.g. 1,3-5 (default all)")
    sp.add_argument("--copies", type=_positive_int, default=1, help="copies of each page")
    sp.add_argument(
        "--fit",
        action="store_true",
        help="allow a PDF page whose physical size differs from Brother media",
    )
    sp.add_argument(
        "--dry-run",
        action="store_true",
        help="render and simulate printing; with --out, capture the printer bytes",
    )
    sp.set_defaults(func=cmd_print_pdf)

    sp = sub.add_parser("pdf", help="render records to a PDF instead of printing", parents=[common])
    add_source_options(sp)
    add_render_options(sp)
    sp.add_argument("--model", "-m", default=None, help="render at this printer model's native DPI")
    sp.add_argument("--device", default=None, help="device name used to detect a printer model")
    sp.add_argument("--out", "-o", default="labels.pdf", help="output PDF path")
    sp.add_argument(
        "--sheet",
        default=None,
        choices=sorted(pdf.PAGE_SIZES_MM),
        help="tile labels on a paper sheet instead of one page per label",
    )
    sp.add_argument("--margin", type=float, default=10.0, help="sheet margin in mm")
    sp.add_argument("--gap", type=float, default=2.0, help="gap between labels in mm")
    sp.add_argument("--columns", type=int, default=None, help="labels per row")
    sp.add_argument("--rows", type=int, default=None, help="label rows per sheet")
    sp.add_argument("--no-marks", action="store_true", help="omit cut marks in sheet mode")
    sp.add_argument(
        "--bilevel",
        action="store_true",
        help="apply the print halftoning so the PDF matches the printed dots",
    )
    sp.add_argument(
        "--scale",
        type=float,
        default=None,
        help="explicit render scale (overrides the selected model's native DPI)",
    )
    sp.set_defaults(func=cmd_pdf)

    sp = sub.add_parser("svg", help="render records to exact-size SVG files", parents=[common])
    add_source_options(sp)
    sp.add_argument(
        "--out",
        "-o",
        default="svg",
        help="output directory, or a .svg path when rendering one record",
    )
    sp.set_defaults(func=cmd_svg)

    sp = sub.add_parser("preview", help="render records to PNG files", parents=[common])
    add_source_options(sp)
    add_render_options(sp)
    sp.add_argument("--model", "-m", default=None, help="printer model, for raster preview")
    sp.add_argument("--device", default=None, help="BLE device name, for model auto-detect")
    sp.add_argument("--align", default=None, choices=["left", "center", "right"])
    sp.add_argument("--media", default=None, help="Brother DK media id, for --raster")
    sp.add_argument("--offset-x", type=int, default=None)
    sp.add_argument("--offset-y", type=int, default=None)
    sp.add_argument("--out", "-o", default="preview", help="output directory")
    sp.add_argument(
        "--raster",
        action="store_true",
        help="show the exact raster sent to the printer, head width included",
    )
    sp.add_argument("--bilevel", action="store_true", help="apply print halftoning")
    sp.add_argument(
        "--printer-scale", action="store_true", help="render at the printer's dpi instead of 203"
    )
    sp.add_argument("--zoom", type=int, default=1, help="upscale the PNG for inspection")
    sp.set_defaults(func=cmd_preview)

    sp = sub.add_parser("fields", help="show label placeholders and CSV mapping", parents=[common])
    add_source_options(sp)
    sp.set_defaults(func=cmd_fields)

    sp = sub.add_parser("printers", help="list known printer models", parents=[common])
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_printers)

    sp = sub.add_parser("scan", help="scan for BLE printers", parents=[common])
    sp.add_argument("--timeout", type=float, default=6.0)
    sp.set_defaults(func=cmd_scan)

    sp = sub.add_parser(
        "status", help="query a BLE printer (battery, paper, firmware)", parents=[common]
    )
    add_printer_options(sp)
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("test", help="print a density test pattern (8 strips)", parents=[common])
    add_printer_options(sp)
    sp.set_defaults(func=cmd_test)

    sp = sub.add_parser("config", help="read or write persistent defaults", parents=[common])
    sp.add_argument("action", choices=["list", "get", "set", "unset"])
    sp.add_argument("key", nargs="?", help="a scalar key, or data.<field> for a template")
    sp.add_argument("value", nargs="?")
    sp.set_defaults(func=cmd_config)

    sp = sub.add_parser("wifi", help="inspect or configure Brother QL Wi-Fi", parents=[common])
    add_printer_options(sp)
    sp.add_argument(
        "action", nargs="?", choices=["configure", "scan", "status"], default="configure"
    )
    sp.add_argument("--ssid", help="network name (configure action)")
    secret = sp.add_mutually_exclusive_group()
    secret.add_argument("--password", help="network password (visible in the process list)")
    secret.add_argument(
        "--password-stdin", action="store_true", help="read one password line from stdin"
    )
    sp.add_argument("--encryption", choices=sorted(wireless.ENCRYPTIONS), default="tkip-aes")
    sp.add_argument("--authentication", choices=sorted(wireless.AUTHENTICATIONS), default="wpa-psk")
    sp.add_argument("--no-infrastructure", action="store_true")
    sp.add_argument("--wireless-direct", action="store_true")
    sp.add_argument("--no-reboot", action="store_true")
    sp.add_argument("--yes", action="store_true", help="send without confirmation")
    sp.add_argument("--scan-wait", type=float, default=5.0, help="seconds to wait for AP scan")
    sp.add_argument("--raw", action="store_true", help="also print the unparsed response bytes")
    sp.add_argument(
        "--dry-run", action="store_true", help="write the command to --out without sending it"
    )
    sp.set_defaults(func=cmd_wifi, transport="usb")

    sp = sub.add_parser("usb-list", help="list supported USB printers", parents=[common])
    add_printer_options(sp)
    sp.set_defaults(func=cmd_usb_list, transport="usb")

    sp = sub.add_parser("usb-info", help="show read-only USB printer information", parents=[common])
    add_printer_options(sp)
    sp.set_defaults(func=cmd_usb_info, transport="usb")

    sp = sub.add_parser(
        "usb-report", help="fetch a Brother configuration/system report", parents=[common]
    )
    add_printer_options(sp)
    sp.add_argument("--json", action="store_true", help="emit parsed sections as JSON")
    sp.set_defaults(func=cmd_usb_report, transport="usb")

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    mblog.configure(
        getattr(args, "verbose", 0),
        getattr(args, "quiet", False),
        getattr(args, "log_file", None),
        getattr(args, "plain", False),
    )
    if log.isEnabledFor(logging.DEBUG):
        given = {
            k: v
            for k, v in sorted(vars(args).items())
            if v not in (None, False, 0) and k not in ("func", "command")
        }
        log.debug(
            "mbprint %s %s %s",
            __version__,
            args.command,
            " ".join(f"{k}={v!r}" for k, v in given.items()),
        )
    try:
        code: int = args.func(args)
        return code
    except KeyboardInterrupt:
        log.warning("interrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
