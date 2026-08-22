"""Unit tests: templating, record building, raster maths and protocol framing."""

import asyncio
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw

from mbprint import brother, data, layout, pdf, printers, protocol, wireless
from mbprint import raster as R
from mbprint.transport.file import FileTransport
from mbprint.transport.usb import (
    decode_device_id,
    decode_port_status,
    select_usb_device,
)

CSV = """Name,Internal Reference,Sales Price,Quantity On Hand
Alpha Gadget,AG-EX-0001,40.00,3
Beta Widget,BW-EX-0002,49.50,2
"""


@pytest.fixture
def csv_path(tmp_path):
    p = tmp_path / "inv.csv"
    p.write_text(CSV, encoding="utf-8")
    return p


# --- templating ------------------------------------------------------------


def test_optional_segment_drops_when_field_empty():
    tpl = "https://shop.example/x#{{sku}}[[/{{batch}}]]"
    assert layout.substitute(tpl, {"sku": "AG-1", "batch": ""}) == ("https://shop.example/x#AG-1")
    assert layout.substitute(tpl, {"sku": "AG-1", "batch": "L7"}) == (
        "https://shop.example/x#AG-1/L7"
    )


def test_unknown_placeholder_is_left_visible():
    assert layout.substitute("{{nope}}", {}) == "{{nope}}"


def test_price_short_drops_zero_cents():
    assert data._format_price("40.00", ",") == ("40,00", "40")
    assert data._format_price("49.50", ",") == ("49,50", "49,5")
    assert data._format_price("", ",") == ("", "")


# --- records ---------------------------------------------------------------


def test_records_map_odoo_columns(csv_path):
    rs = data.build_records(csv_path)
    assert rs.mapping["ref"] == "Internal Reference"
    first = rs.records[0]
    assert first["name"] == "Alpha Gadget"
    assert first["price_short"] == "40"
    assert "qr" not in first  # nothing is derived unless --data says so


def test_data_templates_are_evaluated_in_order(csv_path):
    rs = data.build_records(
        csv_path,
        data_entries=[
            ("brand", "Ceramics"),
            ("qr", "https://shop.example/{{brand}}#{{sku}}[[/{{batch}}]]"),
        ],
    )
    first = rs.records[0]
    assert first["brand"] == "Ceramics"
    assert first["qr"] == "https://shop.example/Ceramics#AG-EX-0001"


def test_data_template_can_reference_a_raw_csv_column(csv_path):
    rs = data.build_records(csv_path, data_entries=[("qr", "x/{{Internal Reference}}")])
    assert rs.records[0]["qr"] == "x/AG-EX-0001"


def test_copies_from_quantity_column(csv_path):
    rs = data.build_records(csv_path)
    assert [data.copies_for(r, 1, "Quantity On Hand") for r in rs.records] == [3, 2]
    assert data.copies_for(rs.records[0], 2, None) == 2


def test_filter_and_limit(csv_path):
    rs = data.build_records(csv_path, filters=[("Internal Reference", "BW-EX-0002")])
    assert len(rs.records) == 1
    assert len(data.build_records(csv_path, limit=1).records) == 1


# --- layout ----------------------------------------------------------------


def test_label_geometry_and_placeholders(tmp_path):
    label_file = tmp_path / "l.json"
    label_file.write_text(
        '{"widthMm":30,"heightMm":20,"dotsPerMm":8,"elements":['
        '{"type":"text","x":0,"y":0,"width":100,"height":20,"text":"{{name}}","fontSize":12},'
        '{"type":"qr","x":100,"y":0,"width":80,"height":80,"qrData":"{{qr}}"}]}',
        encoding="utf-8",
    )
    label = layout.Label.load(label_file)
    assert (label.width_px, label.height_px) == (240, 160)
    assert label.placeholders() == ["name", "qr"]
    img = layout.render(label, {"name": "Beta", "qr": "https://x/y"})
    assert img.size == (240, 160)
    assert img.convert("L").getextrema()[0] < 128  # something was drawn


# --- raster ----------------------------------------------------------------


def _dot_image(w=16, h=8, dot=(0, 0)):
    img = Image.new("RGB", (w, h), "white")
    img.putpixel(dot, (0, 0, 0))
    return img


def test_pack_sets_the_msb_first_bit():
    rst = R.pack(_dot_image(), "threshold")
    assert rst.width_bytes == 2
    assert rst.data[0] == 0x80


def test_fit_centers_and_offsets_in_head_space():
    rst = R.pack(_dot_image(), "threshold")
    centered = R.fit(rst, 6, "center")
    assert centered.width_bytes == 6
    assert centered.data[2] == 0x80  # (6-2)//2 = 2 bytes of left padding

    nudged = R.fit(rst, 6, "left", offset_x=1, offset_y=2)
    assert nudged.height == rst.height + 2
    assert nudged.data[2 * 6] == 0x40  # shifted one dot right, two rows down


def test_fit_rejects_labels_wider_than_the_head():
    rst = R.pack(_dot_image(w=64), "threshold")
    with pytest.raises(SystemExit):
        R.fit(rst, 4)


def test_rotate_cw_swaps_axes_and_moves_the_corner():
    rst = R.pack(_dot_image(w=16, h=8, dot=(0, 0)), "threshold")
    rot = R.rotate_cw(rst)
    assert (rot.width_px, rot.height) == (8, 16)
    img = R.to_image(rot)
    assert img.getpixel((7, 0)) == 0  # top-left dot lands top-right


def test_round_trip_through_to_image():
    src = _dot_image(dot=(5, 3))
    img = R.to_image(R.pack(src, "threshold"))
    assert img.getpixel((5, 3)) == 0  # black dot
    assert img.getpixel((6, 3)) != 0  # its neighbour stayed white


# --- protocol --------------------------------------------------------------


def _capture(printer, img, opts=None, dither="threshold"):
    opts = opts or protocol.PrintOptions()
    rst = protocol.prepare_raster(img, printer, opts, dither)
    transport = FileTransport(path="-")
    chunks: list[bytes] = []

    async def run():
        async with transport:
            transport.send = lambda d: chunks.append(bytes(d)) or asyncio.sleep(0)
            await protocol.print_raster(transport, printer, rst, opts)

    asyncio.run(run())
    return rst, b"".join(chunks)


def test_m_series_stream_has_init_heat_and_raster_header():
    printer = printers.by_id("m200")
    rst, stream = _capture(printer, _dot_image(48 * 8, 4))
    assert stream.startswith(bytes([0x1B, 0x40]))  # ESC @
    assert bytes([0x1B, 0x37]) in stream  # ESC 7 heat
    header = bytes([0x1D, 0x76, 0x30, 0x00, rst.width_bytes, 0x00, rst.height, 0x00])
    assert header in stream
    assert stream.endswith(bytes([0x1B, 0x4A, 32]))  # ESC J feed


def test_m02_stream_is_prefixed():
    printer = printers.by_id("m02")
    _, stream = _capture(printer, _dot_image(48 * 8, 4))
    assert stream.startswith(bytes([0x10, 0xFF, 0xFE, 0x01]))


def test_d_series_rotates_and_ends_with_gap_detect():
    printer = printers.by_id("d-series")
    rst, stream = _capture(printer, _dot_image(80, 40))
    assert (rst.width_px, rst.height) == (40, 80)  # rotated
    assert bytes([0x1F, 0x11, 0x0A]) in stream  # gap media
    assert stream.endswith(bytes([0x1B, 0x64, 0x00]))


def test_d_series_continuous_bakes_the_feed_into_the_raster():
    printer = printers.by_id("d-series")
    opts = protocol.PrintOptions(continuous=True, feed=16)
    rst, stream = _capture(printer, _dot_image(80, 40), opts)
    assert bytes([0x1F, 0x11, 0x0B]) in stream  # continuous media
    rows = protocol.D_CUTTER_OFFSET + opts.feed + rst.height
    assert bytes([0x1D, 0x76, 0x30, 0x00, rst.width_bytes, 0x00, rows % 256, rows // 256]) in stream


def test_tspl_uses_label_millimetres_and_inverts_the_bitmap():
    printer = printers.by_id("pm241")
    opts = protocol.PrintOptions(label_width_mm=30, label_height_mm=20, offset_x=8)
    _, stream = _capture(printer, _dot_image(240, 160), opts)
    assert b"SIZE 30 mm, 20 mm\r\n" in stream
    assert b"BITMAP 8,0,30,160,0," in stream
    assert stream.rstrip().endswith(b"END")


def test_p12_handshake_precedes_the_raster():
    printer = printers.by_id("p12")
    _, stream = _capture(printer, _dot_image(96, 40))
    for cmd in protocol.P12_INIT_SEQUENCE:
        assert cmd in stream
    assert stream.endswith(protocol.P12_FEED)


def test_p12_rejects_a_label_wider_than_the_tape():
    printer = printers.by_id("p12")
    with pytest.raises(SystemExit):
        protocol.prepare_raster(_dot_image(240, 160), printer, protocol.PrintOptions())


def test_chunking_never_exceeds_the_link_mtu():
    printer = printers.by_id("m200")  # 128-byte protocol chunk
    transport = FileTransport(path="-", max_write=20)
    assert protocol.effective_chunk(printer, transport) == 20
    transport.max_write = 512
    assert protocol.effective_chunk(printer, transport) == 128


def test_writes_respect_the_mtu_end_to_end():
    printer = printers.by_id("m200")
    rst = protocol.prepare_raster(
        _dot_image(48 * 8, 30), printer, protocol.PrintOptions(), "threshold"
    )
    transport = FileTransport(path="-", max_write=23)
    sizes: list[int] = []

    async def run():
        async with transport:
            transport.send = lambda d: sizes.append(len(d)) or asyncio.sleep(0)
            await protocol.print_raster(transport, printer, rst, protocol.PrintOptions())

    asyncio.run(run())
    assert max(sizes) <= 23


# --- printers / pdf --------------------------------------------------------


def test_detect_matches_the_longest_pattern_first():
    assert printers.detect("M02 Pro 1234").id == "m02-pro"
    assert printers.detect("M110-abc").id == "m110"
    assert printers.detect("Nothing") is None
    assert printers.resolve(None, "Nothing").protocol == "m-series"


def test_pdf_page_is_exactly_the_label_size(tmp_path):
    out = pdf.write_labels(
        [Image.new("RGB", (240, 160), "white")] * 2, tmp_path / "l.pdf", dots_per_mm=8
    )
    blob = out.read_bytes()
    assert b"/MediaBox [ 0 0 85.03937007874016 56.69291338582678 ]" in blob


def test_pdf_sheet_tiles_labels(tmp_path):
    out = pdf.write_sheet(
        [Image.new("RGB", (240, 160), "white")] * 5, tmp_path / "s.pdf", dots_per_mm=8, page="a4"
    )
    assert out.exists() and out.stat().st_size > 0


@pytest.mark.parametrize(
    ("model", "dpi", "expected_size"),
    [("m110", 203, (240, 160)), ("ql-1110nwb", 300, (354, 236))],
)
def test_pdf_selected_model_uses_exact_native_dpi(tmp_path, monkeypatch, model, dpi, expected_size):
    from mbprint import cli

    label_file = tmp_path / "label.json"
    label_file.write_text(
        '{"widthMm":30,"heightMm":20,"dotsPerMm":8,"elements":[]}', encoding="utf-8"
    )
    captured = {}

    def write_labels(images, out_path, **kwargs):
        captured["size"] = images[0].size
        captured["dots_per_mm"] = kwargs["dots_per_mm"]
        return out_path

    monkeypatch.setattr(pdf, "write_labels", write_labels)
    assert (
        cli.main(["pdf", "--label", str(label_file), "--model", model, "--out", "labels.pdf"]) == 0
    )
    assert captured["size"] == expected_size
    assert captured["dots_per_mm"] * pdf.MM_PER_INCH == pytest.approx(dpi)


def test_pdf_explicit_scale_overrides_selected_model(tmp_path, monkeypatch):
    from mbprint import cli

    label_file = tmp_path / "label.json"
    label_file.write_text(
        '{"widthMm":30,"heightMm":20,"dotsPerMm":8,"elements":[]}', encoding="utf-8"
    )
    captured = {}

    def write_labels(images, out_path, **kwargs):
        captured["size"] = images[0].size
        return out_path

    monkeypatch.setattr(pdf, "write_labels", write_labels)
    assert (
        cli.main(
            [
                "pdf",
                "--label",
                str(label_file),
                "--model",
                "ql-1110nwb",
                "--scale",
                "2",
            ]
        )
        == 0
    )
    assert captured["size"] == (480, 320)


def test_pdf_page_selection_and_native_dpi_rendering(tmp_path):
    source = pdf.write_labels(
        [Image.new("RGB", (300, 200), "white"), Image.new("RGB", (300, 200), "black")],
        tmp_path / "labels.pdf",
        dots_per_mm=10,
    )
    pages = pdf.render_pages(source, dpi=300, pages="2")
    assert [(page.number, round(page.width_mm), round(page.height_mm)) for page in pages] == [
        (2, 30, 20)
    ]
    assert pages[0].image.size == (355, 237)
    assert pages[0].image.getextrema() == ((0, 0), (0, 0), (0, 0))


def test_pdf_page_range_parser_validates_and_deduplicates():
    assert pdf.page_indices("1,3-5,3", 5) == [0, 2, 3, 4]
    with pytest.raises(SystemExit, match="outside the available range"):
        pdf.page_indices("6", 5)
    with pytest.raises(SystemExit, match="invalid page selection"):
        pdf.page_indices("3-1", 5)


def test_print_pdf_dry_run_uses_existing_brother_pipeline(tmp_path):
    from mbprint import cli

    source = pdf.write_labels(
        [Image.new("RGB", (620, 290), "white")],
        tmp_path / "label.pdf",
        dots_per_mm=10,
    )
    capture = tmp_path / "job.bin"
    result = cli.main(
        [
            "print-pdf",
            str(source),
            "--model",
            "ql-1110nwb",
            "--media",
            "62x29",
            "--dry-run",
            "--out",
            str(capture),
            "--chunk-delay",
            "0",
            "--plain",
        ]
    )
    assert result == 0
    stream = capture.read_bytes()
    assert stream.startswith(b"\x1bia")  # Brother raster mode
    assert b"\x1biz" in stream  # Brother print-information command


def test_print_pdf_dry_run_uses_existing_phomemo_pipeline(tmp_path):
    from mbprint import cli

    source = pdf.write_labels(
        [Image.new("RGB", (400, 300), "white")],
        tmp_path / "phomemo-label.pdf",
        dots_per_mm=10,
    )
    capture = tmp_path / "phomemo-job.bin"
    result = cli.main(
        [
            "print-pdf",
            str(source),
            "--model",
            "m110",
            "--dry-run",
            "--out",
            str(capture),
            "--chunk-delay",
            "0",
            "--plain",
        ]
    )
    assert result == 0
    stream = capture.read_bytes()
    assert stream.startswith(protocol.M110_SPEED(5))
    assert b"\x1dv0\x00" in stream  # GS v 0 raster command


def test_print_pdf_parser_accepts_pages_and_copies():
    from mbprint import cli

    args = cli.build_parser().parse_args(
        ["print-pdf", "labels.pdf", "--pages", "2-4", "--copies", "3"]
    )
    assert args.pdf_file == "labels.pdf"
    assert args.pages == "2-4"
    assert args.copies == 3


def test_print_pdf_validates_brother_media_and_rotates_transposed_page():
    from mbprint import cli
    from mbprint import media as M

    printer = printers.by_id("ql-1110nwb")
    media = M.by_id("62x29")
    assert media is not None
    transposed = pdf.RenderedPage(1, 29, 62, Image.new("RGB", (290, 620), "white"))
    fitted = cli._pdf_page_on_media(transposed, media, printer, allow_fit=False)
    assert fitted.size == media.dots_printable

    wrong = pdf.RenderedPage(1, 40, 30, Image.new("RGB", (400, 300), "white"))
    with pytest.raises(SystemExit, match="correctly sized PDF or pass --fit"):
        cli._pdf_page_on_media(wrong, media, printer, allow_fit=False)


# --- logging and tracing ---------------------------------------------------


def test_configure_sets_levels_and_a_single_handler():
    import logging

    from mbprint import log as mblog

    mblog.configure(verbosity=0)
    root = logging.getLogger("mbprint")
    assert root.level == logging.INFO
    assert len(root.handlers) == 1

    mblog.configure(verbosity=1)
    assert root.level == logging.DEBUG
    mblog.configure(verbosity=2)
    assert root.level == mblog.TRACE
    mblog.configure(quiet=True)
    assert root.level == logging.WARNING
    assert len(root.handlers) == 1  # reconfiguring never stacks handlers


def test_log_file_captures_the_full_trace_even_when_quiet(tmp_path):
    import logging

    from mbprint import log as mblog

    path = tmp_path / "mb.log"
    mblog.configure(quiet=True, log_file=str(path))
    log = mblog.get_logger("mbprint.test")
    mblog.trace(log, "wire %s", "detail")
    log.warning("shown on the console too")
    logging.getLogger("mbprint").handlers[-1].flush()
    written = path.read_text()
    assert "wire detail" in written and "shown on the console too" in written
    mblog.configure()  # leave global state clean for the other tests


def test_hexdump_truncates_long_payloads():
    from mbprint import log as mblog

    assert mblog.hexdump(bytes([0x1B, 0x40])) == "1b 40"
    assert mblog.hexdump(b"") == "(empty)"
    dumped = mblog.hexdump(bytes(100), limit=4)
    assert dumped.endswith("... (100 bytes)")


def test_protocol_traces_every_command_and_chunk(caplog):
    from mbprint import log as mblog

    printer = printers.by_id("m110")
    with caplog.at_level(mblog.TRACE, logger="mbprint.protocol"):
        _capture(printer, _dot_image(48 * 8, 8))
    messages = [r.getMessage() for r in caplog.records]
    assert any("M110 speed: 1b 4e 0d" in m for m in messages)
    assert any("GS v 0 raster header" in m for m in messages)
    assert any("raster payload" in m and "chunks of" in m for m in messages)
    assert any(m.startswith("-> chunk 1/") for m in messages)


def test_unknown_device_name_is_warned_about(caplog):
    import logging

    with caplog.at_level(logging.WARNING, logger="mbprint.printers"):
        assert printers.resolve(None, "M110-0123456789").id == "generic"
    warnings = " ".join(r.getMessage() for r in caplog.records)
    assert "matches no known model" in warnings
    assert "--model" in warnings


# --- terminal ui -----------------------------------------------------------


def test_progress_is_a_noop_when_disabled_or_redirected():
    from mbprint import ui

    # Disabled explicitly, and (under pytest) stderr is not a tty either.
    bar = ui.progress(10, enabled=False)
    assert type(bar) is ui.Progress
    with bar:
        bar.label(1, 10, "AG-1")
        bar.chunk(50)
        bar.finish_label()  # must not raise or write anything


def test_progress_picks_plain_when_rich_is_unavailable(monkeypatch):
    from mbprint import ui

    ui.reset()
    monkeypatch.setattr(ui.sys.stderr, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(ui, "rich_available", lambda: False)
    assert isinstance(ui.progress(3, enabled=True), ui.PlainProgress)
    assert ui.console() is None
    ui.reset()


def test_plain_progress_throttles_and_finishes(capsys):
    from mbprint import ui

    bar = ui.PlainProgress()
    bar.label(2, 5, "AG-2")
    for pct in (1, 2, 3, 50, 100):
        bar.chunk(pct)
    bar.finish_label()
    err = capsys.readouterr().err
    assert "[2/5] AG-2:  50%" in err
    assert "[2/5] AG-2:   1%" in err  # the first update always shows
    assert "  2%" not in err and "  3%" not in err  # then sub-5% steps are dropped
    assert err.rstrip().endswith("done")


def test_no_color_env_disables_rich(monkeypatch):
    from mbprint import ui

    ui.reset()
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(ui.sys.stderr, "isatty", lambda: True, raising=False)
    assert ui.color_enabled() is False
    ui.reset()


def test_plain_flag_keeps_the_stdlib_handler():
    import logging

    from mbprint import log as mblog
    from mbprint import ui

    ui.reset()
    mblog.configure(verbosity=1, plain=True)
    handler = logging.getLogger("mbprint").handlers[0]
    assert isinstance(handler, logging.StreamHandler)
    assert handler.__class__.__module__.startswith("logging")
    mblog.configure()
    ui.reset()


# --- dry run ---------------------------------------------------------------


def test_file_transport_paces_only_when_asked():
    import time

    async def elapsed(pace):
        t = FileTransport(path="-", pace=pace)
        start = time.monotonic()
        await t.delay(60)
        return time.monotonic() - start

    assert asyncio.run(elapsed(False)) < 0.01
    assert asyncio.run(elapsed(True)) >= 0.05


def test_dry_run_walks_the_whole_flow_without_hardware(tmp_path, capsys):
    from mbprint.cli import main

    label = tmp_path / "l.json"
    label.write_text(
        '{"widthMm":30,"heightMm":20,"dotsPerMm":8,"elements":['
        '{"type":"text","x":0,"y":0,"width":200,"height":40,"text":"{{ref}}","fontSize":20}]}',
        encoding="utf-8",
    )
    out = tmp_path / "job.bin"
    code = main(
        [
            "print",
            "-l",
            str(label),
            "--set",
            "ref=AG-1",
            "--model",
            "m110",
            "--dry-run",
            "--chunk-delay",
            "0",
            "--out",
            str(out),
            "-q",
        ]
    )
    assert code == 0
    # The capture holds a real M110 job: header, raster and footer.
    stream = out.read_bytes()
    assert stream.startswith(bytes([0x1B, 0x4E, 0x0D]))
    assert bytes([0x1D, 0x76, 0x30, 0x00]) in stream
    assert stream.endswith(protocol.M110_FOOTER)


def test_dry_run_uses_a_simulated_ble_mtu(tmp_path):
    import argparse

    from mbprint import cli

    args = argparse.Namespace(
        transport="file",
        mtu=None,
        out=None,
        dry_run=True,
        port=None,
        baud=115200,
        usb_vid=None,
        usb_pid=None,
        address=None,
        device=None,
    )
    transport = cli._make_transport(args, printers.by_id("m110"))
    assert transport.max_write == cli.SIMULATED_MTU
    assert transport.pace is True


# --- derived fields and the missing-field gate -----------------------------


def test_template_fields_separates_required_from_optional():
    tpl = "{{sku}}[[/{{batch}}]]"
    assert layout.template_fields(tpl) == ["sku", "batch"]
    assert layout.template_fields(tpl, required_only=True) == ["sku"]
    assert layout.missing_fields(tpl, {"sku": "", "batch": ""}) == ["sku"]
    assert layout.missing_fields(tpl, {"sku": "A", "batch": ""}) == []


def test_label_reports_what_a_record_cannot_fill(tmp_path):
    label_file = tmp_path / "l.json"
    label_file.write_text(
        '{"widthMm":30,"heightMm":20,"elements":['
        '{"type":"text","x":0,"y":0,"width":100,"height":20,"text":"{{name}}"},'
        '{"type":"text","x":0,"y":20,"width":100,"height":20,"text":"[[{{batch}}]]"},'
        '{"type":"qr","x":0,"y":40,"width":80,"height":80,"qrData":"{{qr}}"}]}',
        encoding="utf-8",
    )
    label = layout.Label.load(label_file)
    assert label.placeholders() == ["name", "batch", "qr"]
    assert label.placeholders(required_only=True) == ["name", "qr"]
    assert label.missing_for({"name": "Beta"}) == ["qr"]
    assert label.missing_for({"name": "Beta", "qr": "x"}) == []


def test_config_data_table_keeps_definition_order(tmp_path, monkeypatch):
    from mbprint import config as cfgmod

    monkeypatch.setattr(cfgmod, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cfgmod, "CONFIG_PATH", tmp_path / "config.json")
    stored = {}
    cfgmod.set_key(stored, "data.brand", "Ceramics")
    cfgmod.set_key(stored, "data.qr", "https://x/{{brand}}")
    cfgmod.set_key(stored, "density", "7")
    cfgmod.save(stored)

    loaded = cfgmod.load()
    assert loaded["density"] == 7
    assert cfgmod.data_templates(loaded) == [
        ("brand", "Ceramics"),
        ("qr", "https://x/{{brand}}"),
    ]
    assert cfgmod.flatten(loaded)["data.qr"] == "https://x/{{brand}}"
    cfgmod.unset_key(loaded, "data.brand")
    assert cfgmod.data_templates(loaded) == [("qr", "https://x/{{brand}}")]


def test_unknown_config_key_mentions_the_data_table():
    from mbprint import config as cfgmod

    with pytest.raises(SystemExit) as exc:
        cfgmod.coerce("nonsense", "x")
    assert "data.<field>" in str(exc.value)


def _gate_args(tmp_path, force=False, data=None):
    import argparse

    return argparse.Namespace(force=force, data=data, csv=None, decimal=",")


def test_missing_fields_abort_without_a_tty(tmp_path, monkeypatch):
    from mbprint import cli

    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False, raising=False)
    monkeypatch.setattr(cli.cfg, "data_templates", lambda *a, **k: [])
    label_file = tmp_path / "l.json"
    label_file.write_text(
        '{"widthMm":30,"heightMm":20,"elements":['
        '{"type":"qr","x":0,"y":0,"width":80,"height":80,"qrData":"{{qr}}"}]}',
        encoding="utf-8",
    )
    label = layout.Label.load(label_file)
    with pytest.raises(SystemExit) as exc:
        cli._check_missing(label, [{"name": "x"}], _gate_args(tmp_path))
    assert "--force" in str(exc.value)

    # --force proceeds, and so does defining the field, even as an empty string.
    cli._check_missing(label, [{"name": "x"}], _gate_args(tmp_path, force=True))
    cli._check_missing(label, [{"name": "x"}], _gate_args(tmp_path, data=["qr="]))


# --- template filters ------------------------------------------------------


@pytest.mark.parametrize(
    "template,expected",
    [
        ("{{price|num}}", "49,5"),
        ("{{price|num:2}}", "49,50"),
        ("{{price|num:0}}", "50"),
        ("{{whole|num}}", "35"),
        ("{{name|upper}}", "BETA WIDGET"),
        ("{{name|lower}}", "beta widget"),
        ("{{sku|title}}", "Bw-1"),
        ("{{name|truncate:8}}", "Beta Wi…"),
        ("{{name|truncate:40}}", "Beta Widget"),
        ("{{name|slug}}", "beta-widget"),
        ("{{accented|slug}}", "creme-brulee"),
        ("{{name|urlencode}}", "Beta%20Widget"),
        ("{{batch|default:n/a}}", "n/a"),
        ("{{sku|default:n/a}}", "BW-1"),
        ("{{padded|trim}}", "spaces"),
        ("{{sku|replace:-:_}}", "BW_1"),
        ("{{name|truncate:8|upper}}", "BETA WI…"),  # pipelines chain
    ],
)
def test_filters(template, expected):
    record = {
        "price": "49.50",
        "whole": "35.00",
        "name": "Beta Widget",
        "sku": "BW-1",
        "batch": "",
        "accented": "Crème Brûlée",
        "padded": "  spaces  ",
    }
    assert layout.substitute(template, record) == expected


def test_num_respects_the_decimal_separator():
    assert layout.substitute("{{p|num:2}}", {"p": "49.5"}, decimal=".") == "49.50"
    assert layout.substitute("{{p|num:2}}", {"p": "49,5"}, decimal=",") == "49,50"


def test_num_leaves_non_numbers_alone():
    assert layout.substitute("{{p|num}}", {"p": "n/a"}) == "n/a"


def test_unknown_filter_fails_loudly():
    with pytest.raises(SystemExit) as exc:
        layout.substitute("{{p|nope}}", {"p": "x"})
    assert "unknown template filter" in str(exc.value)
    assert "truncate" in str(exc.value)  # lists what is available


def test_filters_do_not_confuse_field_extraction():
    tpl = "{{price|num:2}} {{name|upper}}[[/{{batch|lower}}]]"
    assert layout.template_fields(tpl) == ["price", "name", "batch"]
    assert layout.template_fields(tpl, required_only=True) == ["price", "name"]


def test_a_default_filter_means_the_field_is_never_missing():
    assert layout.missing_fields("{{batch}}", {}) == ["batch"]
    assert layout.missing_fields("{{batch|default:n/a}}", {}) == []
    assert layout.substitute("{{missing|default:-}}", {}) == "-"


def test_filters_work_in_data_templates(csv_path):
    rs = data.build_records(
        csv_path,
        data_entries=[
            ("qr", "https://shop.example/{{name|slug}}#{{sku|lower}}"),
            ("tag", "{{price|num}} EUR"),
        ],
    )
    assert rs.records[0]["qr"] == "https://shop.example/alpha-gadget#ag-ex-0001"
    assert rs.records[0]["tag"] == "40 EUR"


# --- Brother QL ------------------------------------------------------------


def _brother_image(media_id, model="ql-1110nwb"):
    """An image already at the roll's printable size, so fit() is a no-op."""
    from mbprint import media as M

    m = M.by_id(media_id)
    w = m.dots_printable[0]
    h = m.dots_printable[1] or 500
    img = Image.new("RGB", (w, h), "white")
    draw = ImageDraw.Draw(img)
    draw.rectangle([10, 10, w // 2, h // 3], fill="black")
    draw.line([0, h - 5, w, h - 5], fill="black", width=3)
    return m, img


def _brother_stream(media_id, compress=True, cut=True, model="ql-1110nwb"):
    from mbprint import media as M

    m, img = _brother_image(media_id, model)
    printer = printers.by_id(model)
    opts = protocol.PrintOptions(media=m, compress=compress, cut=cut)
    rst = protocol.prepare_raster(M.fit(img, m, printer.min_rows), printer, opts, "threshold")
    chunks: list[bytes] = []
    transport = FileTransport(path="-", max_write=1 << 20)

    async def run():
        async with transport:
            transport.send = lambda d: chunks.append(bytes(d)) or asyncio.sleep(0)
            await protocol.print_raster(transport, printer, rst, opts)

    asyncio.run(run())
    return img, b"".join(chunks)


def test_packbits_matches_the_reference_encoder():
    ref = pytest.importorskip("packbits")
    import random

    random.seed(7)
    cases = [b"", b"\x01", b"\x00" * 300, b"\xff" * 1000, bytes(range(60))]
    for _ in range(200):
        n = random.randint(1, 200)
        cases.append(bytes(random.choice([0, 255, random.randint(0, 255)]) for _ in range(n)))
    for case in cases:
        assert protocol.packbits(case) == ref.encode(case), case[:32].hex(" ")


@pytest.mark.parametrize(
    "media_id,compress",
    [
        ("102x152", True),
        ("102x152", False),
        ("62", True),
        ("102", True),
        ("62x29", True),
        ("d58", True),
        ("29x90", False),
        ("103x164", True),
    ],
)
def test_brother_stream_matches_brother_ql(media_id, compress):
    """Our byte stream must equal what brother_ql produces for the same image.

    This is the only verification available without the hardware, so it covers
    byte-unaligned printable widths (1164, 618, 306 dots) where placement is
    easiest to get wrong, and both compressed and raw raster lines.
    """
    pytest.importorskip("brother_ql")
    from brother_ql.conversion import convert
    from brother_ql.models import ModelsManager
    from brother_ql.raster import BrotherQLRaster

    if "QL-1110NWB" not in [m.identifier for m in ModelsManager().iter_elements()]:
        pytest.skip("installed brother_ql predates the QL-1100 series")

    img, ours = _brother_stream(media_id, compress=compress)
    qlr = BrotherQLRaster("QL-1110NWB")
    qlr.exception_on_warning = True
    theirs = convert(
        qlr,
        [img],
        media_id,
        cut=True,
        compress=compress,
        dither=False,
        threshold=70,
        hq=True,
        rotate=0,
    )
    assert ours == theirs


def test_brother_preamble_and_print_information():
    _, stream = _brother_stream("62x29")
    assert stream.startswith(bytes([0x1B, 0x69, 0x61, 0x01]))  # switch to raster
    assert bytes(200) in stream  # invalidate
    assert bytes([0x1B, 0x40]) in stream  # ESC @
    assert bytes([0x1B, 0x69, 0x53]) in stream  # status request
    # ESC i z: flags, die-cut media, 62mm wide, 29mm long
    assert bytes([0x1B, 0x69, 0x7A, 0xCE, 0x0B, 62, 29]) in stream
    assert bytes([0x1B, 0x69, 0x4D, 0x40]) in stream  # autocut on
    assert stream.endswith(bytes([0x1A]))  # print and eject


def test_brother_continuous_media_reports_no_length():
    _, stream = _brother_stream("62")
    # Continuous media type 0x0A, and a length of zero.
    assert bytes([0x1B, 0x69, 0x7A, 0xCE, 0x0A, 62, 0]) in stream
    assert bytes([0x1B, 0x69, 0x64, 35, 0]) in stream  # 35 dot feed margin


def test_brother_compression_command_only_when_compressing():
    _, compressed = _brother_stream("62x29", compress=True)
    _, raw = _brother_stream("62x29", compress=False)
    assert bytes([0x4D, 0x02]) in compressed
    assert bytes([0x4D]) not in raw.split(bytes([0x1B, 0x69, 0x64]))[1][:4]
    assert len(raw) > len(compressed)


def test_brother_places_the_label_by_its_right_margin():
    from mbprint import media as M

    printer = printers.by_id("ql-1110nwb")
    m = M.by_id("62x29")
    img = Image.new("RGB", m.dots_printable, "white")
    img.putpixel((0, 0), (0, 0, 0))  # single dot, top-left of the label
    opts = protocol.PrintOptions(media=m)
    rst = protocol.prepare_raster(img, printer, opts, "threshold")

    assert rst.width_bytes == 162  # 1296 dot head
    # left = 1296 - 696 printable - (12 media + 44 model) = 544
    unpacked = R.to_image(rst)
    assert unpacked.getpixel((544, 0)) == 0
    assert unpacked.getpixel((543, 0)) != 0


def test_place_uses_the_true_width_not_the_padded_one():
    # 1164 dots is not a whole number of bytes: the 4 padding dots must not
    # push the content left, which is exactly what a byte-wide raster would do.
    img = Image.new("RGB", (1164, 4), "white")
    img.putpixel((1163, 0), (0, 0, 0))  # rightmost dot of the content
    rst = R.pack(img, "threshold")
    assert rst.width_bytes == 146 and rst.pixel_width == 1164
    placed = R.place(rst, 162, right_margin_dots=56)
    assert R.to_image(placed).getpixel((1296 - 56 - 1, 0)) == 0


def test_media_resolution_and_fitting():
    from mbprint import media as M

    assert M.resolve(None, 102, 153, "ql-1110nwb").id == "102x152"
    assert M.resolve("62", 0, 0, "ql-1110nwb").id == "62"
    with pytest.raises(SystemExit):
        M.resolve("102x152", 0, 0, "m110")  # wide media, narrow printer
    with pytest.raises(SystemExit):
        M.resolve("nonsense", 0, 0, "ql-1110nwb")

    m = M.by_id("102x152")
    fitted = M.fit(Image.new("RGB", (816, 1216), "white"), m)
    assert fitted.size == m.dots_printable  # die-cut is exact

    endless = M.by_id("62")
    fitted = M.fit(Image.new("RGB", (400, 100), "white"), endless, min_rows=301)
    assert fitted.width == endless.dots_printable[0]
    assert fitted.height == 301  # padded up to the minimum


def test_media_lookup_accepts_a_transposed_size():
    """A printer may name a die-cut roll from the label's side, not the tape's.

    This QL-1110NWB reports DK-1209 as 29x62mm where the media table calls it
    62x29: same roll, and the two readings need opposite raster dimensions, so
    picking the wrong one makes the printer error after printing.
    """
    from mbprint import media as M

    direct = M.from_size(62, 29, "ql-1110nwb")
    transposed = M.from_size(29, 62, "ql-1110nwb")
    assert direct is not None and direct.id == "62x29"
    assert transposed is direct
    assert direct.dots_printable == (696, 271)
    # A size that is in the table only one way round still resolves.
    assert M.from_size(90, 29, "ql-1110nwb").id == "29x90"
    assert M.from_size(11, 13, "ql-1110nwb") is None


def test_brother_status_block_from_real_hardware():
    """A status block captured from a QL-1110NWB over Bluetooth.

    Taken from an HCI snoop of the Brother app printing to the same DK-1209
    roll this driver was tested on. The printer reports the media as 62mm wide
    with 29mm labels, which is the reading the media table uses.
    """
    block = bytes.fromhex(
        "80 20 42 34 44 30 00 00 00 00 3e 0b 00 00 03 00"
        "00 1d 00 00 00 00 00 00 00 00 00 00 00 00 00 00".replace(" ", "")
    )
    status = protocol.brother_parse_status(block)
    assert status["media_width_mm"] == 62
    assert status["media_length_mm"] == 29
    assert status["media_type"] == "die-cut"
    assert status["status_type"] == "reply to status request"
    assert status["phase"] == "waiting to receive"
    assert status["errors"] == []


def test_brother_status_rejects_a_foreign_reply():
    with pytest.raises(SystemExit):
        protocol.brother_parse_status(bytes(32))  # no 80 20 42 header
    with pytest.raises(SystemExit):
        protocol.brother_parse_status(bytes([0x80, 0x20, 0x42]))  # too short


def test_brother_wireless_command_matches_reversed_native_format():
    command = wireless.WirelessSettings(ssid="Maker WiFi", password="secret").command()
    assert command.startswith(wireless.PJL_HEADER)
    assert b'DEFAULT OBJBRNET="458877:-4d-61-6b-65-72-20-57-69-46-69"' in command
    assert b'DEFAULT OBJBRNET="458880:8"' in command
    assert b'DEFAULT OBJBRNET="458881:3"' in command
    assert b'DEFAULT OBJBRNET="459138.2:1"' in command
    encrypted = wireless.xor_password(b"secret")
    assert b'DEFAULT OBJBRNET="99458890:' + encrypted + b'"' in command
    assert command.endswith(wireless.PJL_FOOTER + wireless.REBOOT_COMMAND)


def test_brother_open_wireless_command_omits_password():
    command = wireless.WirelessSettings(
        ssid="Guest", password="", encryption="none", authentication="open"
    ).command()
    assert b"99458890" not in command
    assert b"99458889.1" not in command
    assert b'DEFAULT OBJBRNET="458880:1"' in command
    assert b'DEFAULT OBJBRNET="458881:1"' in command


def test_brother_wireless_password_xor_is_reversible():
    assert wireless.xor_password(wireless.xor_password(b"correct horse")) == b"correct horse"


def test_brother_wireless_read_commands_match_native_pjl():
    assert wireless.wifi_scan_start_command() == (
        wireless.PJL_HEADER + b'@PJL DEFAULT OBJBRNET="458845:31-3a"\r\n' + wireless.PJL_FOOTER
    )
    assert wireless.wifi_scan_result_command() == (
        wireless.PJL_HEADER + b"@PJL INFO AVAILABLEWLAN\r\n" + wireless.PJL_FOOTER
    )
    assert wireless.wifi_status_command() == (
        wireless.PJL_HEADER
        + b'@PJL DEFAULT OBJBRNET="458867"\r\n'
        + b"@PJL INQUIRE OBJBRNET\r\n"
        + wireless.PJL_FOOTER
    )
    assert wireless.ip_address_command() == (
        wireless.PJL_HEADER
        + b'@PJL DEFAULT OBJBRNET="458967.2"\r\n'
        + b"@PJL INQUIRE OBJBRNET\r\n"
        + wireless.PJL_FOOTER
    )


def test_brother_wireless_response_decoders():
    reply = b'@PJL INFO OBJBRNET\r\n"458867:1"\r\n"458967.2:c0-a8-01-32"\r\n'
    assert wireless.parse_wifi_status(reply) is True
    assert wireless.parse_ip_address(reply) == "192.168.1.50"
    assert wireless.parse_wifi_status(b'"458867:0"') is False
    assert wireless.parse_ip_address(b'"458967.2:ffff-00-00-01"') is None
    assert (
        wireless.parse_oid_value(b'"458877:-4d-61-6b-65-72-20-57-69-46-69"', "458877")
        == "Maker WiFi"
    )
    assert wireless.parse_oid_value(b'"458877:4D-61-6B-65-72"', "458877") == "Maker"
    assert wireless.parse_oid_value(b'"458880:8"\r\n', "458880") == "8"
    assert wireless.parse_oid_value(b'"458881:3"\r\n', "missing") is None


def test_brother_wireless_inquire_rejects_non_numeric_oids():
    with pytest.raises(ValueError, match="invalid OBJBRNET OID"):
        wireless.inquire_command('458867"\r\n@PJL RESET')


def test_brother_access_point_decoder_ignores_unknown_rows():
    reply = b"header\r\nVAP,-4d-61-6b-65-72,ignored,ignored,11,87,0,2\r\nbad,row\r\n"
    assert wireless.parse_access_points(reply) == [
        wireless.AccessPoint("Maker", channel=11, power=87, enterprise=False, encrypted=True)
    ]


def test_usb_printer_class_response_decoders():
    identifier = b"MFG:Brother;MDL:QL-1110NWB;"
    assert (
        decode_device_id((len(identifier) + 2).to_bytes(2, "big") + identifier)
        == identifier.decode()
    )
    assert decode_device_id(b"\x00") is None
    assert decode_port_status(0x18) == {"selected": True, "paper_empty": False, "error": False}


def test_brother_system_report_command_and_decoder():
    response = (
        b"\x00\x12<<PRINTER CONFIGURATION>>\r\n"
        b"[Printer]\r\nPrinter =QL-1110NWB\r\nProgVer =V2.13\r\n"
        b"[WLAN]\r\nIP Address =192.0.2.7\r\nGateway Address =192.0.2.1\r\n"
    )
    assert bytes.fromhex("1b 69 58 47") == brother.SYSTEM_REPORT_COMMAND
    assert brother.decode_system_report(response).startswith("<<PRINTER CONFIGURATION>>")
    assert brother.parse_system_report(response) == {
        "Printer": {"Printer": "QL-1110NWB", "ProgVer": "V2.13"},
        "WLAN": {"IP Address": "192.0.2.7", "Gateway Address": "192.0.2.1"},
    }


def test_brother_system_report_rejects_unrelated_response():
    with pytest.raises(ValueError, match="not a Brother"):
        brother.parse_system_report(b"ordinary printer reply")


def test_usb_report_parser_defaults_to_usb_and_supports_json():
    args = (
        __import__("mbprint.cli", fromlist=["build_parser"])
        .build_parser()
        .parse_args(["usb-report", "--json", "--usb-serial", "QL-A", "--out", "report.json"])
    )
    assert args.transport == "usb"
    assert args.json is True
    assert args.usb_serial == "QL-A"
    assert args.out == "report.json"


def test_usb_selector_requires_one_unambiguous_device():
    first = SimpleNamespace(idVendor=0x04F9, idProduct=0x209B, bus=1, address=7)
    second = SimpleNamespace(idVendor=0x04F9, idProduct=0x209B, bus=1, address=9)
    serials = {id(first): "QL-A", id(second): "QL-B"}

    def read_serial(dev):
        return serials[id(dev)]

    assert select_usb_device([first, second], serial="QL-B", serial_reader=read_serial) is second
    assert select_usb_device([first, second], bus=1, address=7) is first
    with pytest.raises(SystemExit, match="multiple USB printers match"):
        select_usb_device([first, second])
    with pytest.raises(SystemExit, match="serial 'missing'"):
        select_usb_device([first, second], serial="missing", serial_reader=read_serial)
