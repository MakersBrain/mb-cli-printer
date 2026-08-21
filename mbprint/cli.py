"""mbprint command line interface."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

from mbprint import __version__
from mbprint import config as cfg
from mbprint import log as mblog
from mbprint import ui
from mbprint import data as datamod
from mbprint import layout, media as mediamod, pdf, printers, protocol
from mbprint import raster as R
from mbprint.transport import build as build_transport

BASE_DPI = 203.0

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


# --- shared option groups --------------------------------------------------


def logging_parser() -> argparse.ArgumentParser:
    """Options shared by every subcommand, so `-v` works before or after it."""
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("-v", "--verbose", action="count", default=0,
                   help="-v traces each protocol command, -vv every write")
    p.add_argument("-q", "--quiet", action="store_true", help="warnings and errors only")
    p.add_argument("--log-file", default=None,
                   help="append a full trace to this file, whatever the console shows")
    p.add_argument("--plain", action="store_true",
                   help="no colors and no progress bar, even on a terminal")
    return p


def add_source_options(p: argparse.ArgumentParser) -> None:
    p.add_argument("--label", "-l", default=None, help="label.json layout file")
    p.add_argument("--csv", "-c", default=None, help="CSV of records to print")
    p.add_argument("--data", action="append", metavar="KEY=TEMPLATE",
                   help="define a field from a template (repeatable, evaluated in "
                        "order, may reference earlier fields, filters allowed): "
                        f'--data qr="{datamod.EXAMPLE_QR_TEMPLATE}". '
                        "[[...]] segments vanish when their fields are empty")
    p.add_argument("--set", action="append", metavar="KEY=VALUE", dest="data",
                   help="alias for --data, for plain constants")
    p.add_argument("--map", action="append", metavar="FIELD=COLUMN",
                   help="map a label field to a CSV column (repeatable)")
    p.add_argument("--force", "--ignore-missing", action="store_true", dest="force",
                   help="print even when fields are missing, without asking")
    p.add_argument("--decimal", default=",", help="decimal separator for prices (default ',')")
    p.add_argument("--filter", action="append", metavar="COLUMN=VALUE",
                   help="keep only rows where COLUMN equals VALUE (repeatable)")
    p.add_argument("--limit", type=int, default=None, help="only the first N records")
    p.add_argument("--copies", type=int, default=1, help="copies of each label (default 1)")
    p.add_argument("--copies-from", default=None, metavar="COLUMN",
                   help="take the copy count from this CSV column, e.g. 'Quantity On Hand'")


def add_render_options(p: argparse.ArgumentParser) -> None:
    p.add_argument("--dither", default=None, choices=list(R.DITHER_MODES),
                   help="halftoning for the 1-bit conversion (default auto)")


def add_printer_options(p: argparse.ArgumentParser) -> None:
    p.add_argument("--model", "-m", default=None,
                   help="printer model id (see 'mbprint printers'); default auto-detect")
    p.add_argument("--transport", "-t", default=None,
                   choices=["ble", "tcp", "serial", "usb", "file"],
                   help="transport (default ble; tcp for network printers)")
    p.add_argument("--host", default=None, help="printer hostname or IP, for --transport tcp")
    p.add_argument("--tcp-port", type=int, default=9100, help="TCP port (default 9100)")
    p.add_argument("--address", default=None, help="BLE MAC address")
    p.add_argument("--device", default=None, help="BLE device name (substring match)")
    p.add_argument("--port", default=None, help="serial/rfcomm port, e.g. /dev/rfcomm0")
    p.add_argument("--baud", type=int, default=115200, help="serial baud rate")
    p.add_argument("--usb-vid", default=None, help="USB vendor id, e.g. 0x0483")
    p.add_argument("--usb-pid", default=None, help="USB product id")
    p.add_argument("--out", "-o", default=None,
                   help="with --transport file: write the byte stream here")
    p.add_argument("--density", type=int, default=None, help="print density 1-8 (default 6)")
    p.add_argument("--feed", type=int, default=None, help="feed after each label in dots")
    p.add_argument("--speed", type=int, default=None, help="print speed (M110/TSPL)")
    p.add_argument("--continuous", action="store_true", default=None,
                   help="continuous media: no gap detection")
    p.add_argument("--align", default=None, choices=["left", "center", "right"],
                   help="roller alignment: where the label sits under the head")
    p.add_argument("--offset-x", type=int, default=None,
                   help="roller alignment nudge across the head, in dots")
    p.add_argument("--offset-y", type=int, default=None,
                   help="roller alignment nudge along the feed, in dots")
    p.add_argument("--media", default=None,
                   help="Brother DK media id, e.g. 62 or 102x152 "
                        "(default: inferred from the label size)")
    p.add_argument("--no-cut", action="store_true", help="Brother: do not cut after printing")
    p.add_argument("--cut-every", type=int, default=1, help="Brother: cut every N labels")
    p.add_argument("--no-compress", action="store_true",
                   help="Brother: send raster lines uncompressed")
    p.add_argument("--gap-mm", type=float, default=None, help="TSPL gap between labels in mm")
    p.add_argument("--tspl-offset-mm", type=float, default=None, help="TSPL OFFSET in mm")
    p.add_argument("--mtu", type=int, default=None,
                   help="cap the write size in bytes (default: negotiated link MTU)")
    p.add_argument("--chunk-delay", type=int, default=None,
                   help="ms between data chunks (default per protocol)")


# --- resolution helpers ----------------------------------------------------


def _pick(args, key, default):
    value = getattr(args, key, None)
    if value is not None:
        return value
    stored = cfg.load().get(key)
    return default if stored is None else stored


def _resolve_label(args) -> layout.Label:
    path = _pick(args, "label", None) or "label.json"
    return layout.Label.load(path)


def _data_entries(args) -> list[tuple[str, str]]:
    """Field templates: the config `data` table first, then --data / --set."""
    entries = cfg.data_templates()
    if entries:
        log.debug("config data templates: %s", ", ".join(k for k, _ in entries))
    return entries + _pairs(getattr(args, "data", None), "--data")


def _resolve_records(args) -> list[dict]:
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
                record["price"], args.decimal)
        # Re-evaluate so templates can use the normalized fields above.
        datamod.apply_data(record, entries, args.decimal)
        records = [record]

    expanded: list[dict] = []
    for record in records:
        for _ in range(datamod.copies_for(record, args.copies, args.copies_from)):
            expanded.append(record)
    if not expanded:
        raise SystemExit("no records to print (check --filter / --copies-from)")
    return expanded


def _check_missing(label: layout.Label, records: list[dict], args) -> None:
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
        log.warning("%s: empty on %d of %d label%s", field, n, total,
                    "" if total == 1 else "s")
    example = sorted(counts)[0]
    log.warning('give it a value with --data %s="...", point it at a column with '
                "--map %s=COLUMN, wrap it in [[...]] in the layout to make it "
                'optional, or silence it with --data %s=""',
                example, example, example)

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


def _resolve_media(args, label: layout.Label, printer):
    """Which DK roll a Brother job prints on; None for every other family."""
    if printer.protocol != "brother":
        return None
    media = mediamod.resolve(_pick(args, "media", None),
                             label.width_mm, label.height_mm, printer.id)
    log.info("media: %s (%s), printable %dx%s dots, %d dots right margin",
             media.id, media.name, media.dots_printable[0],
             media.dots_printable[1] or "endless", media.offset_r)
    return media


def _render_scale(label: layout.Label, printer, media=None) -> float:
    """Render straight at the head's resolution instead of upscaling a 203dpi bitmap."""
    if media is not None:
        return mediamod.render_scale(media, label.width_px)
    if not label.dots_per_mm:
        return 1.0
    return printer.dpi / BASE_DPI * (8.0 / label.dots_per_mm)


def _render_all(label: layout.Label, records: list[dict], scale: float = 1.0,
                decimal: str = ","):
    return [layout.render(label, record, scale=scale, decimal=decimal) for record in records]


def _print_options(args, printer, label=None, media=None) -> protocol.PrintOptions:
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


def _make_transport(args, printer):
    kind = _pick(args, "transport", "ble")
    mtu = args.mtu
    if getattr(args, "dry_run", False):
        # Same protocol flow, same chunking, same pacing, no hardware.
        return build_transport("file", path=args.out or os.devnull,
                               max_write=mtu or SIMULATED_MTU, pace=True)
    if kind == "ble":
        return build_transport(
            "ble",
            address=_pick(args, "address", None),
            device_name=_pick(args, "device", None),
            max_write=mtu,
        )
    if kind == "tcp":
        host = args.host or cfg.load().get("host")
        if not host:
            raise SystemExit("tcp transport needs --host, e.g. --host 192.168.1.50")
        return build_transport("tcp", host=host, port=args.tcp_port,
                               max_write=mtu or 4096)
    if kind == "serial":
        port = args.port or cfg.load().get("port")
        if not port:
            raise SystemExit("serial transport needs --port, e.g. --port /dev/rfcomm0")
        return build_transport("serial", port=port, baudrate=args.baud,
                               max_write=mtu or 512)
    if kind == "usb":
        vid = int(args.usb_vid, 0) if args.usb_vid else None
        pid = int(args.usb_pid, 0) if args.usb_pid else None
        return build_transport("usb", vid=vid, pid=pid, max_write=mtu or 512)
    if kind == "file":
        return build_transport("file", path=args.out or "-", max_write=mtu or 512)
    raise SystemExit(f"unknown transport {kind!r}")


# --- commands --------------------------------------------------------------


def cmd_printers(args) -> int:
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
    print("\nWrite chunking per protocol:",
          ", ".join(f"{k}={v}B" for k, v in printers.PROTOCOL_CHUNK.items()))
    return 0


def cmd_scan(args) -> int:
    from mbprint.transport.ble import scan

    found = asyncio.run(scan(args.timeout))
    if not found:
        print("no BLE devices found")
        return 1
    for address, name in sorted(found, key=lambda x: x[1] or ""):
        d = printers.detect(name)
        tag = f"-> {d.id} ({d.protocol}, {d.width_bytes * 8 if d.width_bytes else '?'}px)" if d else ""
        print(f"{address}  {name or '(unnamed)':<24} {tag}")
    return 0


def cmd_fields(args) -> int:
    label = _resolve_label(args)
    optional = [f for f in label.placeholders() if f not in label.placeholders(True)]
    print(f"label: {label.name}  {label.width_mm}x{label.height_mm}mm "
          f"({label.width_px}x{label.height_px} dots at {label.dots_per_mm}/mm)")
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
            args.csv, data_entries=entries,
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


def cmd_preview(args) -> int:
    label = _resolve_label(args)
    records = _resolve_records(args)
    _check_missing(label, records, args)
    printer = printers.resolve(_pick(args, "model", None), _pick(args, "device", None))
    preview_media = _resolve_media(args, label, printer) if args.raster else None
    scale = _render_scale(label, printer, preview_media) if preview_media else (
        (printer.dpi / BASE_DPI) if args.printer_scale else 1.0)
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


def cmd_pdf(args) -> int:
    label = _resolve_label(args)
    records = _resolve_records(args)
    _check_missing(label, records, args)
    images = _render_all(label, records, scale=args.scale, decimal=args.decimal)
    dots_per_mm = label.dots_per_mm * args.scale
    out = args.out or "labels.pdf"
    dither = _pick(args, "dither", "auto")
    if args.sheet:
        path = pdf.write_sheet(
            images, out, dots_per_mm=dots_per_mm, page=args.sheet,
            margin_mm=args.margin, gap_mm=args.gap, columns=args.columns, rows=args.rows,
            marks=not args.no_marks, bilevel=args.bilevel, dither=dither, title=label.name,
        )
    else:
        path = pdf.write_labels(
            images, out, dots_per_mm=dots_per_mm, bilevel=args.bilevel,
            dither=dither, title=label.name,
        )
    print(f"{path}  ({len(images)} label{'s' if len(images) != 1 else ''})")
    return 0


def cmd_print(args) -> int:
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

    log.info("printer: %s [%s] %s %ddpi head=%s%s", printer.name, printer.id,
             printer.protocol, printer.dpi,
             f"{printer.width_px}px" if printer.width_bytes else "auto",
             " rotated" if printer.rotated else "")
    log.info("labels: %d x %dx%d dots, align=%s offset=(%d,%d) dither=%s",
             len(rasters), rasters[0].width_px, rasters[0].height,
             opts.align, opts.offset_x, opts.offset_y, dither)
    if args.dry_run:
        log.warning("dry run: simulating the print, nothing will be sent to a printer")

    async def run() -> None:
        transport = _make_transport(args, printer)
        started = time.monotonic()
        async with transport:
            chunk = protocol.effective_chunk(printer, transport)
            log.info("transport: %s mtu_payload=%dB chunk=%dB density=%d feed=%d",
                     transport.name, transport.max_write, chunk, opts.density, opts.feed)
            # A live bar would fight a byte-level trace, so verbose runs log instead.
            show_bar = not args.quiet and not args.verbose
            with ui.progress(len(rasters), show_bar, args.plain) as bar:
                for i, rst in enumerate(rasters, 1):
                    record = records[i - 1]
                    label_id = record.get("ref") or record.get("sku") or str(i)
                    log.debug("label %d/%d: %s", i, len(rasters), label_id)
                    bar.label(i, len(rasters), label_id)
                    await protocol.print_raster(transport, printer, rst, opts, bar.chunk)
                    bar.finish_label()
                    if not show_bar:
                        log.info("[%d/%d] %s %s", i, len(rasters), label_id,
                                 "simulated" if args.dry_run else "printed")
            elapsed = time.monotonic() - started
            sent = sum(len(r.data) for r in rasters)
            log.info("%s %d label%s, %d raster bytes, %.1fs",
                     "simulated" if args.dry_run else "printed", len(rasters),
                     "" if len(rasters) == 1 else "s", sent, elapsed)

    asyncio.run(run())
    return 0


def cmd_test(args) -> int:
    printer = printers.resolve(_pick(args, "model", None), _pick(args, "device", None))

    async def run() -> None:
        transport = _make_transport(args, printer)
        async with transport:
            log.info("density test on %s via %s (mtu payload %dB)",
                     printer.name, transport.name, transport.max_write)
            await protocol.print_density_test(transport, printer)

    asyncio.run(run())
    return 0


def cmd_status(args) -> int:
    async def run() -> None:
        transport = _make_transport(args, None)
        if transport.name != "ble":
            raise SystemExit("status queries need the BLE transport")
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


def cmd_config(args) -> int:
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


# --- parser ----------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    common = logging_parser()
    p = argparse.ArgumentParser(
        prog="mbprint",
        description="Print label.json layouts on Phomemo printers, or export them as PDF.",
        parents=[common],
    )
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("print", help="render records and print them", parents=[common])
    add_source_options(sp)
    add_render_options(sp)
    add_printer_options(sp)
    sp.add_argument("--dry-run", action="store_true",
                    help="run the whole flow against a simulated link, sending nothing "
                         "to a printer; with --out, capture the bytes it would send")
    sp.set_defaults(func=cmd_print)

    sp = sub.add_parser("pdf", help="render records to a PDF instead of printing", parents=[common])
    add_source_options(sp)
    add_render_options(sp)
    sp.add_argument("--out", "-o", default="labels.pdf", help="output PDF path")
    sp.add_argument("--sheet", default=None, choices=sorted(pdf.PAGE_SIZES_MM),
                    help="tile labels on a paper sheet instead of one page per label")
    sp.add_argument("--margin", type=float, default=10.0, help="sheet margin in mm")
    sp.add_argument("--gap", type=float, default=2.0, help="gap between labels in mm")
    sp.add_argument("--columns", type=int, default=None, help="labels per row")
    sp.add_argument("--rows", type=int, default=None, help="label rows per sheet")
    sp.add_argument("--no-marks", action="store_true", help="omit cut marks in sheet mode")
    sp.add_argument("--bilevel", action="store_true",
                    help="apply the print halftoning so the PDF matches the printed dots")
    sp.add_argument("--scale", type=float, default=1.0, help="render scale (1 = 203dpi)")
    sp.set_defaults(func=cmd_pdf)

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
    sp.add_argument("--raster", action="store_true",
                    help="show the exact raster sent to the printer, head width included")
    sp.add_argument("--bilevel", action="store_true", help="apply print halftoning")
    sp.add_argument("--printer-scale", action="store_true",
                    help="render at the printer's dpi instead of 203")
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

    sp = sub.add_parser("status", help="query a BLE printer (battery, paper, firmware)", parents=[common])
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

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    mblog.configure(getattr(args, "verbose", 0), getattr(args, "quiet", False),
                    getattr(args, "log_file", None), getattr(args, "plain", False))
    if log.isEnabledFor(mblog.logging.DEBUG):
        given = {k: v for k, v in sorted(vars(args).items())
                 if v not in (None, False, 0) and k not in ("func", "command")}
        log.debug("mbprint %s %s %s", __version__, args.command,
                  " ".join(f"{k}={v!r}" for k, v in given.items()))
    try:
        return args.func(args)
    except KeyboardInterrupt:
        log.warning("interrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
