"""Phomemo print protocols.

One flow per protocol family, ported from the phomymo reference driver:

  m-series  generic ESC/POS raster (M03, T02, M200, M250, M220, M221, M260)
  m02       M02/M02S/M02X/M02 Pro, needs the 10 FF FE 01 prefix
  m04       M04S/M04AS, 300 dpi, 1F 11 xx init sequence
  m110      M110/M110S/M120, phomemo-tools speed/density/media commands
  d-series  D30/D35/D50/Q30, rotated raster with gap detection
  p12       P12/P12 Pro/A30 tape, proprietary init handshake, rotated raster
  tspl      PM-241 and friends, text-based TSPL with an inverted BITMAP
  brother   Brother QL-1100/1110NWB/1115NWB, ESC/P raster on DK media

Every data write is chunked to min(protocol chunk, transport MTU payload).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from PIL import Image

from mbprint import media as mediamod
from mbprint import raster as R
from mbprint.log import get_logger, hexdump, trace, tracing
from mbprint.printers import PrinterDef
from mbprint.transport import Transport

ProgressFn = Callable[[int], None] | None

log = get_logger(__name__)


async def _cmd(transport: Transport, name: str, data: bytes) -> None:
    """Send one command, tracing what it is and the bytes it puts on the wire."""
    log.debug("-> %s: %s", name, hexdump(data))
    await transport.send(data)


@dataclass
class PrintOptions:
    density: int = 6  # 1-8, mapped per protocol
    feed: int = 32  # dots fed after the label
    continuous: bool = False  # continuous media: disable gap detection
    speed: int = 5  # m110 / tspl print speed
    copies: int = 1
    gap_mm: float = 3.0  # tspl gap between die-cut labels
    tspl_offset_mm: float = -3.0  # tspl roller alignment (negative shifts down)
    chunk_delay_ms: int | None = None  # override inter-chunk pacing
    # Roller alignment, in head space (applied after any protocol rotation).
    align: str = "center"
    offset_x: int = 0
    offset_y: int = 0
    # Physical label size, used by TSPL's SIZE command.
    label_width_mm: float | None = None
    label_height_mm: float | None = None
    # Brother: which DK roll is loaded, and how to finish the job.
    media: mediamod.Media | None = None
    cut: bool = True
    cut_every: int = 1
    compress: bool = True
    high_quality: bool = True


Flow = Callable[[Transport, PrinterDef, R.Raster, PrintOptions, ProgressFn], Awaitable[None]]


# --- density mapping -------------------------------------------------------

_HEAT_TIMES = [40, 60, 80, 100, 120, 140, 160, 200]


def density_to_heat_time(density: int) -> int:
    return _HEAT_TIMES[max(0, min(7, density - 1))]


# --- command builders ------------------------------------------------------

INIT = bytes([0x1B, 0x40])  # ESC @


def feed_cmd(dots: int) -> bytes:  # ESC J n
    return bytes([0x1B, 0x4A, dots & 0xFF])


def density_cmd(level: int) -> bytes:  # GS | n
    return bytes([0x1D, 0x7C, level & 0xFF])


def heat_settings(max_dots: int, heat_time: int, heat_interval: int) -> bytes:
    return bytes([0x1B, 0x37, max_dots & 0xFF, heat_time & 0xFF, heat_interval & 0xFF])


def line_spacing(dots: int) -> bytes:  # ESC 3 n
    return bytes([0x1B, 0x33, dots & 0xFF])


def raster_header(width_bytes: int, height: int) -> bytes:  # GS v 0
    return bytes(
        [
            0x1D,
            0x76,
            0x30,
            0x00,
            width_bytes % 256,
            width_bytes // 256,
            height % 256,
            height // 256,
        ]
    )


def media_type(kind: int) -> bytes:  # 1F 11 0A gaps / 0B continuous
    return bytes([0x1F, 0x11, kind & 0xFF])


M02_PREFIX = bytes([0x10, 0xFF, 0xFE, 0x01])

D_END = bytes([0x1B, 0x64, 0x00])  # print, no feed (gap detect)

M110_FOOTER = bytes([0x1F, 0xF0, 0x05, 0x00, 0x1F, 0xF0, 0x03, 0x00])

M04_INIT = bytes([0x1F, 0x11, 0x0B])
M04_FEED = bytes([0x1B, 0x64, 0x02])


def M110_SPEED(speed: int) -> bytes:
    return bytes([0x1B, 0x4E, 0x0D, speed & 0xFF])


def M110_DENSITY(density: int) -> bytes:
    return bytes([0x1B, 0x4E, 0x04, density & 0xFF])


def M04_DENSITY(density: int) -> bytes:
    return bytes([0x1F, 0x11, 0x02, density & 0xFF])


def M04_HEAT(points: int) -> bytes:
    return bytes([0x1F, 0x11, 0x37, points & 0xFF])


def M04_COMPRESSION(mode: int) -> bytes:
    return bytes([0x1F, 0x11, 0x35, mode & 0xFF])


P12_INIT_SEQUENCE = [
    bytes([0x1F, 0x11, 0x38]),
    bytes([0x1F, 0x11, 0x11, 0x1F, 0x11, 0x12, 0x1F, 0x11, 0x09, 0x1F, 0x11, 0x13]),
    bytes([0x1F, 0x11, 0x09]),
    bytes([0x1F, 0x11, 0x19, 0x1F, 0x11, 0x11]),
    bytes([0x1F, 0x11, 0x19]),
    bytes([0x1F, 0x11, 0x07]),
]
P12_FEED = bytes([0x1B, 0x64, 0x0D])

# D30 head-to-cutter distance in dots at 203 dpi.
D_CUTTER_OFFSET = 56


# --- Brother QL raster language (ESC/P) -----------------------------------

BROTHER_SWITCH_RASTER = bytes([0x1B, 0x69, 0x61, 0x01])  # ESC i a 1
BROTHER_STATUS = bytes([0x1B, 0x69, 0x53])  # ESC i S


def brother_print_information(
    media: mediamod.Media, rows: int, page: int, high_quality: bool = True
) -> bytes:
    """ESC i z: media type, width, length and how many raster lines follow."""
    flags = 0x80 | (1 << 1) | (1 << 2) | (1 << 3) | (int(high_quality) << 6)
    length_mm = 0 if media.continuous else int(media.length_mm)
    return (
        bytes(
            [
                0x1B,
                0x69,
                0x7A,
                flags,
                mediamod.MEDIA_TYPE_BYTE[media.form] & 0xFF,
                int(media.width_mm) & 0xFF,
                length_mm & 0xFF,
            ]
        )
        + rows.to_bytes(4, "little")
        + bytes([0 if page == 0 else 1, 0x00])
    )


# 32-byte status block the printer returns for ESC i S and after each page.
BROTHER_ERRORS_1 = {
    0: "no media",
    1: "end of media",
    2: "cutter jam",
    4: "unit in use",
    5: "printer off",
    7: "fan failure",
}
BROTHER_ERRORS_2 = {
    0: "replace media",
    1: "expansion buffer full",
    2: "transmission error",
    4: "cover opened while printing",
    6: "media cannot be fed",
    7: "system error",
}
BROTHER_MEDIA_TYPES = {0x00: "no media", 0x0A: "continuous", 0x0B: "die-cut"}
BROTHER_STATUS_TYPES = {
    0x00: "reply to status request",
    0x01: "printing completed",
    0x02: "error",
    0x05: "notification",
    0x06: "phase change",
}
BROTHER_PHASES = {0x00: "waiting to receive", 0x01: "printing"}


def brother_parse_status(data: bytes) -> dict[str, Any]:
    """Decode the 32-byte status block: loaded media, phase and errors."""
    if not data or len(data) < 32:
        raise SystemExit(
            f"short status reply from the printer ({len(data or b'')} bytes); expected 32"
        )
    if not data.startswith(bytes([0x80, 0x20, 0x42])):
        raise SystemExit(f"unexpected status header: {data[:4].hex(' ')}")
    errors = [name for bit, name in BROTHER_ERRORS_1.items() if data[8] & (1 << bit)]
    errors += [name for bit, name in BROTHER_ERRORS_2.items() if data[9] & (1 << bit)]
    return {
        "media_width_mm": data[10],
        "media_length_mm": data[17],
        "media_type": BROTHER_MEDIA_TYPES.get(data[11], f"unknown 0x{data[11]:02x}"),
        "status_type": BROTHER_STATUS_TYPES.get(data[18], f"unknown 0x{data[18]:02x}"),
        "phase": BROTHER_PHASES.get(data[19], f"unknown 0x{data[19]:02x}"),
        "errors": errors,
    }


async def brother_query_status(
    transport: Transport, printer: PrinterDef, timeout_ms: int = 3000
) -> dict[str, Any] | None:
    """Ask a QL what media is loaded and whether it is happy."""
    await _cmd(transport, "invalidate", bytes(printer.invalidate_bytes))
    await _cmd(transport, "ESC @ init", INIT)
    # The printer only answers status requests once it is in raster mode.
    await _cmd(transport, "switch to raster mode", BROTHER_SWITCH_RASTER)
    await _cmd(transport, "ESC i S status request", BROTHER_STATUS)
    reply = await transport.wait_for_response(timeout_ms)
    log.debug("<- status: %s", hexdump(reply or b"", 32))
    if not reply:
        return None
    return brother_parse_status(reply)


def brother_autocut(enabled: bool) -> bytes:  # ESC i M
    return bytes([0x1B, 0x69, 0x4D, (int(enabled) << 6) & 0xFF])


def brother_cut_every(n: int) -> bytes:  # ESC i A
    return bytes([0x1B, 0x69, 0x41, n & 0xFF])


def brother_expanded_mode(
    cut_at_end: bool, dpi_600: bool = False, two_color: bool = False
) -> bytes:  # ESC i K
    flags = (int(two_color) << 0) | (int(cut_at_end) << 3) | (int(dpi_600) << 6)
    return bytes([0x1B, 0x69, 0x4B, flags & 0xFF])


def brother_margins(dots: int) -> bytes:  # ESC i d
    return bytes([0x1B, 0x69, 0x64]) + (dots & 0xFFFF).to_bytes(2, "little")


def brother_compression(enabled: bool) -> bytes:  # M
    return bytes([0x4D, (int(enabled) << 1) & 0xFF])


BROTHER_PRINT_LAST = bytes([0x1A])  # print and eject
BROTHER_PRINT_PAGE = bytes([0x0C])  # print, more pages follow


def packbits(data: bytes) -> bytes:
    """TIFF PackBits, the run-length coding Brother uses for raster lines.

    A literal run is `n-1` followed by n bytes; a repeat is `257-n` followed by
    the byte to repeat n times. Both cap at 127 here rather than the format's
    128, which is what the reference encoders emit and what keeps our output
    byte-identical to them.
    """
    if len(data) < 2:
        return (b"\x00" + data) if data else b""

    MAX_RUN = 127
    result = bytearray()
    literal = bytearray()
    repeat = 0
    pos = 0
    in_repeat = False

    def flush_literal() -> None:
        if literal:
            result.append(len(literal) - 1)
            result.extend(literal)
            literal.clear()

    def flush_repeat() -> None:
        result.append(257 - repeat)
        result.append(data[pos])

    while pos < len(data) - 1:
        if data[pos] == data[pos + 1]:
            if not in_repeat:
                flush_literal()
                in_repeat, repeat = True, 1
            else:
                if repeat == MAX_RUN:
                    flush_repeat()
                    repeat = 0
                repeat += 1
        else:
            if in_repeat:
                repeat += 1
                flush_repeat()
                in_repeat, repeat = False, 0
            else:
                if len(literal) == MAX_RUN:
                    flush_literal()
                literal.append(data[pos])
        pos += 1

    if in_repeat:
        repeat += 1
        flush_repeat()
    else:
        literal.append(data[pos])
        flush_literal()
    return bytes(result)


def brother_raster_lines(rst: R.Raster, compress: bool) -> bytes:
    """One `g` command per scan line, mirrored because Brother sends right to left."""
    mirrored = R.mirror(rst)
    out = bytearray()
    width = rst.width_bytes
    for y in range(rst.height):
        row = bytes(mirrored.data[y * width : (y + 1) * width])
        if compress:
            row = packbits(row)
        out += bytes([0x67, 0x00, len(row)]) + row
    return bytes(out)


async def _print_brother(
    transport: Transport,
    printer: PrinterDef,
    rst: R.Raster,
    opts: PrintOptions,
    on_progress: ProgressFn,
) -> None:
    media = opts.media
    if media is None:
        raise SystemExit("the Brother protocol needs to know the media; pass --media")
    compress = opts.compress and printer.compression

    if printer.min_rows and media.continuous and rst.height < printer.min_rows:
        raise SystemExit(
            f"{printer.name} needs at least {printer.min_rows} raster lines, this job "
            f"has {rst.height}; use a longer label or continuous media"
        )
    if printer.max_rows and rst.height > printer.max_rows:
        raise SystemExit(
            f"{printer.name} accepts at most {printer.max_rows} raster lines, "
            f"this job has {rst.height}"
        )

    # Job preamble: clear whatever state the printer was left in.
    await _cmd(transport, "switch to raster mode", BROTHER_SWITCH_RASTER)
    await _cmd(transport, "invalidate", bytes(printer.invalidate_bytes))
    await _cmd(transport, "ESC @ init", INIT)
    await _cmd(transport, "switch to raster mode", BROTHER_SWITCH_RASTER)

    await _cmd(transport, "ESC i S status request", BROTHER_STATUS)
    await _cmd(
        transport,
        "ESC i z print information",
        brother_print_information(media, rst.height, 0, opts.high_quality),
    )
    if opts.cut:
        await _cmd(transport, "ESC i M autocut", brother_autocut(True))
        await _cmd(transport, "ESC i A cut every", brother_cut_every(opts.cut_every))
    await _cmd(transport, "ESC i K expanded mode", brother_expanded_mode(opts.cut))
    await _cmd(transport, "ESC i d margins", brother_margins(media.feed_margin))
    if compress:
        await _cmd(transport, "M compression", brother_compression(True))

    log.debug(
        "-> %d raster lines of %d bytes%s",
        rst.height,
        rst.width_bytes,
        ", packbits" if compress else "",
    )
    await _send_data(transport, printer, brother_raster_lines(rst, compress), opts, on_progress)
    await _cmd(transport, "print", BROTHER_PRINT_LAST)


def tspl(line: str) -> bytes:
    return (line + "\r\n").encode("ascii")


# --- chunked transfer ------------------------------------------------------


def effective_chunk(printer: PrinterDef, transport: Transport) -> int:
    """Largest write that respects both the protocol and the link MTU."""
    return max(1, min(printer.chunk_size, transport.max_write))


async def _send_data(
    transport: Transport,
    printer: PrinterDef,
    data: bytes | bytearray,
    opts: PrintOptions,
    on_progress: ProgressFn,
) -> None:
    chunk = effective_chunk(printer, transport)
    delay = printer.chunk_delay_ms if opts.chunk_delay_ms is None else opts.chunk_delay_ms
    total = len(data)
    count = (total + chunk - 1) // chunk
    log.debug(
        "-> raster payload: %d bytes in %d chunks of %d, %dms apart", total, count, chunk, delay
    )
    for index, i in enumerate(range(0, total, chunk), 1):
        piece = bytes(data[i : i + chunk])
        if tracing(log):
            trace(log, "-> chunk %d/%d @%d: %s", index, count, i, hexdump(piece))
        await transport.send(piece)
        await transport.delay(delay)
        if on_progress:
            on_progress(round((i + len(piece)) / total * 100))


# --- per-protocol flows ----------------------------------------------------


async def _print_m_series(
    transport: Transport,
    printer: PrinterDef,
    rst: R.Raster,
    opts: PrintOptions,
    on_progress: ProgressFn,
) -> None:
    await _cmd(transport, "ESC @ init", INIT)
    await transport.delay(100)
    await _cmd(transport, "ESC 7 heat", heat_settings(7, density_to_heat_time(opts.density), 2))
    await transport.delay(30)
    await _cmd(transport, "GS | density", density_cmd(opts.density))
    await transport.delay(50)
    await _cmd(transport, "GS v 0 raster header", raster_header(rst.width_bytes, rst.height))
    await _send_data(transport, printer, rst.data, opts, on_progress)
    await transport.delay(300)
    await _cmd(transport, "ESC J feed", feed_cmd(opts.feed))
    await transport.delay(800)


async def _print_m02(
    transport: Transport,
    printer: PrinterDef,
    rst: R.Raster,
    opts: PrintOptions,
    on_progress: ProgressFn,
) -> None:
    await _cmd(transport, "M02 prefix", M02_PREFIX)
    await transport.delay(50)
    await _cmd(transport, "ESC @ init", INIT)
    await transport.delay(100)
    await _cmd(transport, "ESC 7 heat", heat_settings(7, density_to_heat_time(opts.density), 2))
    await transport.delay(30)
    await _cmd(transport, "GS v 0 raster header", raster_header(rst.width_bytes, rst.height))
    await _send_data(transport, printer, rst.data, opts, on_progress)
    await transport.delay(300)
    # Continuous paper: feed only enough to clear the head.
    await _cmd(transport, "ESC J feed", feed_cmd(min(opts.feed, 8)))
    await transport.delay(500)


async def _print_m04(
    transport: Transport,
    printer: PrinterDef,
    rst: R.Raster,
    opts: PrintOptions,
    on_progress: ProgressFn,
) -> None:
    await _cmd(transport, "M04 density", M04_DENSITY(round(opts.density / 8 * 15)))
    await transport.delay(30)
    await _cmd(transport, "M04 heat", M04_HEAT(round(100 + (opts.density - 1) * 50 / 3)))
    await transport.delay(30)
    await _cmd(transport, "M04 init (continuous media)", M04_INIT)
    await transport.delay(30)
    await _cmd(transport, "M04 compression", M04_COMPRESSION(0x00))
    await transport.delay(30)
    await _cmd(transport, "GS v 0 raster header", raster_header(rst.width_bytes, rst.height))
    await _send_data(transport, printer, rst.data, opts, on_progress)
    await transport.delay(300)
    for _ in range(max(1, round(opts.feed / 16))):
        await _cmd(transport, "M04 feed", M04_FEED)
        await transport.delay(30)
    await transport.delay(500)


async def _print_m110(
    transport: Transport,
    printer: PrinterDef,
    rst: R.Raster,
    opts: PrintOptions,
    on_progress: ProgressFn,
) -> None:
    await _cmd(transport, "M110 speed", M110_SPEED(opts.speed))
    await transport.delay(30)
    await _cmd(transport, "M110 density", M110_DENSITY(round(5 + opts.density * 1.25)))
    await transport.delay(30)
    await _cmd(transport, "media type", media_type(0x0B if opts.continuous else 0x0A))
    await transport.delay(30)
    await _cmd(transport, "GS v 0 raster header", raster_header(rst.width_bytes, rst.height))
    await _send_data(transport, printer, rst.data, opts, on_progress)
    await transport.delay(300)
    await _cmd(transport, "M110 footer", M110_FOOTER)
    await transport.delay(500)


async def _print_d_series(
    transport: Transport,
    printer: PrinterDef,
    rst: R.Raster,
    opts: PrintOptions,
    on_progress: ProgressFn,
) -> None:
    rot = rst
    if opts.continuous and opts.feed > 0:
        # ESC J is ignored in continuous mode, so bake the feed into the raster:
        # enough blank rows to clear the cutter, plus the requested margin.
        rot = R.pad_rows(rot, D_CUTTER_OFFSET + opts.feed)
    await _cmd(transport, "ESC 7 heat", heat_settings(7, density_to_heat_time(opts.density), 2))
    await transport.delay(30)
    await _cmd(transport, "media type", media_type(0x0B if opts.continuous else 0x0A))
    await transport.delay(30)
    await _cmd(
        transport,
        "ESC @ init + GS v 0 raster header",
        INIT + raster_header(rot.width_bytes, rot.height),
    )
    await _send_data(transport, printer, rot.data, opts, on_progress)
    await transport.delay(100)
    await _cmd(transport, "ESC d 0 print + gap detect", D_END)


async def _print_p12(
    transport: Transport,
    printer: PrinterDef,
    rst: R.Raster,
    opts: PrintOptions,
    on_progress: ProgressFn,
) -> None:
    rot = rst
    for cmd in P12_INIT_SEQUENCE:
        await _cmd(transport, "P12 init packet", cmd)
        await transport.wait_for_response(500)
    await _cmd(
        transport,
        "ESC @ init + GS v 0 raster header",
        INIT + raster_header(rot.width_bytes, rot.height),
    )
    await _send_data(transport, printer, rot.data, opts, on_progress)
    await transport.delay(100)
    await _cmd(transport, "P12 feed", P12_FEED)
    await transport.delay(50)
    await _cmd(transport, "P12 feed", P12_FEED)


async def _print_tspl(
    transport: Transport,
    printer: PrinterDef,
    rst: R.Raster,
    opts: PrintOptions,
    on_progress: ProgressFn,
) -> None:
    dpmm = 8 * (printer.dpi / 203.0)
    width_mm = round(opts.label_width_mm or rst.width_px / dpmm, 1)
    height_mm = round(opts.label_height_mm or rst.height / dpmm, 1)
    await _cmd(transport, "TSPL", tspl(f"SIZE {width_mm:g} mm, {height_mm:g} mm"))
    await transport.delay(50)
    await _cmd(transport, "TSPL", tspl(f"GAP {opts.gap_mm:g} mm, 0 mm"))
    await transport.delay(50)
    await _cmd(transport, "TSPL", tspl(f"OFFSET {opts.tspl_offset_mm:g} mm"))
    await transport.delay(50)
    await _cmd(transport, "TSPL", tspl(f"DENSITY {round(opts.density / 8 * 15)}"))
    await transport.delay(50)
    await _cmd(transport, "TSPL", tspl(f"SPEED {opts.speed}"))
    await transport.delay(50)
    await _cmd(transport, "TSPL", tspl("DIRECTION 0"))
    await transport.delay(50)
    await _cmd(transport, "TSPL", tspl("CLS"))
    await transport.delay(50)
    bx, by = max(0, opts.offset_x), max(0, opts.offset_y)
    await transport.send(f"BITMAP {bx},{by},{rst.width_bytes},{rst.height},0,".encode("ascii"))
    # TSPL BITMAP is 0 = black, the inverse of the ESC/POS raster convention.
    inverted = bytes(b ^ 0xFF for b in rst.data)
    await _send_data(transport, printer, inverted, opts, on_progress)
    await _cmd(transport, "bitmap terminator", b"\r\n")
    await transport.delay(50)
    await _cmd(transport, "TSPL", tspl(f"PRINT {opts.copies}"))
    await transport.delay(50)
    await _cmd(transport, "TSPL", tspl("END"))


_FLOWS: dict[str, Flow] = {
    "m-series": _print_m_series,
    "m02": _print_m02,
    "m04": _print_m04,
    "m110": _print_m110,
    "d-series": _print_d_series,
    "p12": _print_p12,
    "tspl": _print_tspl,
    "brother": _print_brother,
}


def prepare_raster(
    img: Image.Image, printer: PrinterDef, opts: PrintOptions | None = None, dither: str = "auto"
) -> R.Raster:
    """Turn a rendered label image into the exact raster the printer receives.

    Rotated models (D-series, P12 tape) print sideways, so the label is rotated
    first and only then fitted to the head; that keeps `align` and the roller
    offsets meaning the same thing on every model. TSPL positions the bitmap
    itself, so it keeps the raw raster.
    """
    opts = opts or PrintOptions()
    if dither == "auto" and printer.protocol == "tspl":
        # Shipping labels need crisp barcodes, not halftones.
        dither = "threshold"
    rst = R.pack(img, dither)
    log.debug("packed %dx%d dots with %s dithering", rst.width_px, rst.height, dither)
    if printer.protocol == "brother":
        media = opts.media
        if media is None:
            raise SystemExit("the Brother protocol needs to know the media; pass --media")
        margin = media.offset_r + printer.additional_offset_r + opts.offset_x
        placed = R.place(rst, printer.width_px // 8, margin)
        log.debug(
            "placed on %s: %d dots from the right edge (%d media + %d model%s), head %d dots",
            media.name,
            margin,
            media.offset_r,
            printer.additional_offset_r,
            f" + {opts.offset_x} offset" if opts.offset_x else "",
            placed.width_px,
        )
        return placed
    if printer.rotated:
        rst = R.rotate_cw(rst)
        log.debug("rotated 90 CW for %s: now %dx%d dots", printer.id, rst.width_px, rst.height)
    if printer.protocol == "tspl":
        log.debug("tspl places the bitmap itself, keeping the raw raster")
        return rst
    if printer.rotated and printer.width_bytes and rst.width_bytes > printer.width_bytes:
        head_mm = printer.width_px / 8
        label_mm = rst.width_px / 8
        raise SystemExit(
            f"{printer.name} prints sideways on {head_mm:g}mm media, but this label "
            f"is {label_mm:g}mm along that axis; use a shorter label or a wider model"
        )
    fitted = R.fit(rst, printer.width_bytes, opts.align, opts.offset_x, opts.offset_y)
    log.debug(
        "fitted to head: %dx%d dots, align=%s offset=(%d,%d)",
        fitted.width_px,
        fitted.height,
        opts.align,
        opts.offset_x,
        opts.offset_y,
    )
    return fitted


async def print_raster(
    transport: Transport,
    printer: PrinterDef,
    rst: R.Raster,
    opts: PrintOptions | None = None,
    on_progress: ProgressFn = None,
) -> None:
    """Send one raster to the printer using its protocol flow."""
    opts = opts or PrintOptions()
    flow = _FLOWS.get(printer.protocol)
    if flow is None:
        raise SystemExit(f"unsupported protocol {printer.protocol!r}")
    # TSPL sends its own copy count; every other protocol repeats the raster.
    repeats = 1 if printer.protocol == "tspl" else opts.copies
    log.debug(
        "%s flow: %dx%d dots (%d bytes), density=%d feed=%d continuous=%s copies=%d",
        printer.protocol,
        rst.width_px,
        rst.height,
        len(rst.data),
        opts.density,
        opts.feed,
        opts.continuous,
        repeats,
    )
    for copy in range(max(1, repeats)):
        if repeats > 1:
            log.debug("copy %d/%d", copy + 1, repeats)
        await flow(transport, printer, rst, opts, on_progress)


async def print_density_test(transport: Transport, printer: PrinterDef) -> None:
    """Eight solid strips, density 1 to 8, to pick a density for the media."""
    log.info("printing density ramp on %s", printer.name)
    strip_height, strip_width_bytes, gap = 30, 40, 8
    strip = bytes([0xFF] * (strip_width_bytes * strip_height))
    opts = PrintOptions()
    for density in range(1, 9):
        log.debug("strip at density %d (heat time %d)", density, density_to_heat_time(density))
        await _cmd(transport, "ESC @ init", INIT)
        await transport.delay(50)
        await _cmd(transport, "ESC 7 heat", heat_settings(7, density_to_heat_time(density), 2))
        await transport.delay(30)
        await _cmd(transport, "GS | density", density_cmd(density))
        await transport.delay(30)
        await _cmd(
            transport, "GS v 0 raster header", raster_header(strip_width_bytes, strip_height)
        )
        await _send_data(transport, printer, strip, opts, None)
        if density < 8:
            await transport.delay(200)
            await _cmd(transport, "ESC J feed", feed_cmd(gap))
            await transport.delay(300)
    await transport.delay(300)
    await _cmd(transport, "ESC J feed", feed_cmd(48))
    await transport.delay(500)
