"""1-bit raster conversion: dithering, bit packing, rotation, roller offsets."""

from __future__ import annotations

from dataclasses import dataclass

from PIL import Image

DITHER_MODES = ("auto", "none", "threshold", "floyd-steinberg", "atkinson", "ordered")

# 4x4 Bayer matrix, thresholds scaled to 0-255 (ordered dithering).
_BAYER4 = [
    [0, 8, 2, 10],
    [12, 4, 14, 6],
    [3, 11, 1, 9],
    [15, 7, 13, 5],
]


@dataclass
class Raster:
    """Packed 1bpp raster, MSB first, 1 = black dot."""

    data: bytearray
    width_bytes: int
    height: int
    # True image width, when it is not a whole number of bytes. Rows are padded
    # to a byte boundary, and those padding dots must not count as content when
    # the raster is positioned on the head.
    content_width: int | None = None

    @property
    def width_px(self) -> int:
        return self.width_bytes * 8

    @property
    def pixel_width(self) -> int:
        return self.content_width or self.width_bytes * 8


def _to_gray(img: Image.Image, gamma: float = 1.3):
    """Perceptual grayscale on white, with gamma lift for thermal midtones."""
    if img.mode != "RGB":
        img = img.convert("RGBA")
        flat = Image.new("RGBA", img.size, (255, 255, 255, 255))
        flat.alpha_composite(img)
        img = flat.convert("RGB")
    gray = img.convert("L")
    if gamma and gamma != 1.0:
        inv = 1.0 / gamma
        lut = [min(255, int(round(255.0 * ((i / 255.0) ** inv)))) for i in range(256)]
        gray = gray.point(lut)
    return gray


def _floyd_steinberg(gray: Image.Image) -> Image.Image:
    # Pillow's built-in error diffusion is Floyd-Steinberg.
    return gray.convert("1", dither=Image.Dither.FLOYDSTEINBERG)


def _atkinson(gray: Image.Image) -> Image.Image:
    w, h = gray.size
    buf = [float(v) for v in gray.getdata()]
    out = bytearray(w * h)
    for y in range(h):
        row = y * w
        for x in range(w):
            i = row + x
            old = buf[i]
            new = 255.0 if old >= 128 else 0.0
            out[i] = 255 if new else 0
            err = (old - new) / 8.0
            for dx, dy in ((1, 0), (2, 0), (-1, 1), (0, 1), (1, 1), (0, 2)):
                nx, ny = x + dx, y + dy
                if 0 <= nx < w and ny < h:
                    buf[ny * w + nx] += err
    res = Image.frombytes("L", (w, h), bytes(out))
    return res.convert("1", dither=Image.Dither.NONE)


def _ordered(gray: Image.Image) -> Image.Image:
    w, h = gray.size
    src = gray.load()
    out = Image.new("1", (w, h))
    dst = out.load()
    for y in range(h):
        brow = _BAYER4[y & 3]
        for x in range(w):
            threshold = (brow[x & 3] + 0.5) * (255.0 / 16.0)
            dst[x, y] = 255 if src[x, y] > threshold else 0
    return out


def _looks_like_photo(gray: Image.Image) -> bool:
    """Auto mode heuristic: many midtones means a photo, so dither it."""
    hist = gray.histogram()
    total = sum(hist) or 1
    mid = sum(hist[40:216])
    return (mid / total) > 0.15


def to_bilevel(img: Image.Image, dither: str = "auto") -> Image.Image:
    """Return a mode '1' image where black pixels are the ones to burn."""
    if dither not in DITHER_MODES:
        raise SystemExit(f"unknown dither mode {dither!r}; use one of {DITHER_MODES}")
    if dither in ("none", "threshold"):
        return _to_gray(img, gamma=1.0).point(lambda v: 255 if v >= 128 else 0).convert(
            "1", dither=Image.Dither.NONE
        )
    gray = _to_gray(img)
    if dither == "atkinson":
        return _atkinson(gray)
    if dither == "ordered":
        return _ordered(gray)
    if dither == "floyd-steinberg":
        return _floyd_steinberg(gray)
    if _looks_like_photo(gray):
        return _floyd_steinberg(gray)
    return _to_gray(img, gamma=1.0).point(lambda v: 255 if v >= 128 else 0).convert(
        "1", dither=Image.Dither.NONE
    )


def pack(img: Image.Image, dither: str = "auto") -> Raster:
    """Pack a PIL image into a raw 1bpp raster, one row per scan line."""
    bw = to_bilevel(img, dither)
    w, h = bw.size
    width_bytes = (w + 7) // 8
    data = bytearray(width_bytes * h)
    px = bw.load()
    for y in range(h):
        base = y * width_bytes
        for x in range(w):
            if not px[x, y]:  # 0 = black = burn
                data[base + (x >> 3)] |= 0x80 >> (x & 7)
    return Raster(data=data, width_bytes=width_bytes, height=h, content_width=w)


def fit(raster: Raster, width_bytes: int | None = None, alignment: str = "center",
        offset_x: int = 0, offset_y: int = 0) -> Raster:
    """Place a raster inside the print head width.

    `alignment` is where the label sits under the head; `offset_x` / `offset_y`
    are the roller alignment nudges in dots, positive meaning right and further
    down the feed. Both act in head space, after any protocol rotation.
    """
    out_width_bytes = width_bytes or raster.width_bytes
    if out_width_bytes < raster.width_bytes:
        raise SystemExit(
            f"the raster is {raster.width_px}px wide but the print head is only "
            f"{out_width_bytes * 8}px; use a narrower label or a wider model"
        )

    if alignment == "center":
        x_off = ((out_width_bytes - raster.width_bytes) // 2) * 8
    elif alignment == "right":
        x_off = (out_width_bytes - raster.width_bytes) * 8
    elif alignment == "left":
        x_off = 0
    else:
        raise SystemExit(f"unknown alignment {alignment!r}; use left, center or right")
    x_off += offset_x

    top_pad = max(0, offset_y)
    height = raster.height + top_pad
    out = bytearray(out_width_bytes * height)
    row_bits = out_width_bytes * 8
    src = raster.data
    for y in range(raster.height):
        dy = y + top_pad + min(0, offset_y)
        if dy < 0 or dy >= height:
            continue
        sbase = y * raster.width_bytes
        dbase = dy * out_width_bytes
        for x in range(raster.width_px):
            if not (src[sbase + (x >> 3)] >> (7 - (x & 7))) & 1:
                continue
            dx = x + x_off
            if 0 <= dx < row_bits:
                out[dbase + (dx >> 3)] |= 0x80 >> (dx & 7)
    return Raster(data=out, width_bytes=out_width_bytes, height=height)


def mirror(raster: Raster) -> Raster:
    """Reverse the bit order of every row.

    Brother's raster language transmits each line right-to-left, so a row that
    reads left-to-right on paper is sent with its bits flipped end for end.
    """
    width_bits = raster.width_px
    out = bytearray(len(raster.data))
    src = raster.data
    for y in range(raster.height):
        base = y * raster.width_bytes
        for x in range(width_bits):
            if (src[base + (x >> 3)] >> (7 - (x & 7))) & 1:
                dx = width_bits - 1 - x
                out[base + (dx >> 3)] |= 0x80 >> (dx & 7)
    return Raster(data=out, width_bytes=raster.width_bytes, height=raster.height)


def place(raster: Raster, width_bytes: int, right_margin_dots: int) -> Raster:
    """Put a raster into a head-width row, measured from the right edge.

    Brother positions artwork by its distance from the right side of the head,
    which is how the printable area of each DK roll is centred.
    """
    row_bits = width_bytes * 8
    content = raster.pixel_width
    left = row_bits - content - right_margin_dots
    if left < 0:
        raise SystemExit(
            f"the raster is {content} dots wide and needs {right_margin_dots} "
            f"dots of right margin, which does not fit a {row_bits}-dot head"
        )
    out = bytearray(width_bytes * raster.height)
    src = raster.data
    for y in range(raster.height):
        sbase = y * raster.width_bytes
        dbase = y * width_bytes
        for x in range(content):
            if (src[sbase + (x >> 3)] >> (7 - (x & 7))) & 1:
                dx = x + left
                out[dbase + (dx >> 3)] |= 0x80 >> (dx & 7)
    return Raster(data=out, width_bytes=width_bytes, height=raster.height)


def _rotate(raster: Raster, clockwise: bool) -> Raster:
    src_w = raster.width_px
    src_h = raster.height
    dst_w_bytes = (src_h + 7) // 8
    dst_h = src_w
    out = bytearray(dst_w_bytes * dst_h)
    src = raster.data
    sw = raster.width_bytes
    for y in range(src_h):
        row = y * sw
        for x in range(src_w):
            if not (src[row + (x >> 3)] >> (7 - (x & 7))) & 1:
                continue
            if clockwise:
                dx, dy = src_h - 1 - y, x
            else:
                dx, dy = y, src_w - 1 - x
            out[dy * dst_w_bytes + (dx >> 3)] |= 0x80 >> (dx & 7)
    return Raster(data=out, width_bytes=dst_w_bytes, height=dst_h)


def rotate_cw(raster: Raster) -> Raster:
    return _rotate(raster, True)


def rotate_ccw(raster: Raster) -> Raster:
    return _rotate(raster, False)


def pad_rows(raster: Raster, rows: int) -> Raster:
    """Append blank rows (used to bake feed into continuous-tape prints)."""
    if rows <= 0:
        return raster
    return Raster(
        data=raster.data + bytearray(raster.width_bytes * rows),
        width_bytes=raster.width_bytes,
        height=raster.height + rows,
    )


def to_image(raster: Raster) -> Image.Image:
    """Unpack back to a PIL image, for previewing exactly what gets sent."""
    img = Image.new("1", (raster.width_px, raster.height), 1)
    px = img.load()
    for y in range(raster.height):
        base = y * raster.width_bytes
        for x in range(raster.width_px):
            if (raster.data[base + (x >> 3)] >> (7 - (x & 7))) & 1:
                px[x, y] = 0
    return img
