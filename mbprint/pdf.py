"""PDF output: one page per label, or labels tiled on a paper sheet."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

MM_PER_INCH = 25.4

PAGE_SIZES_MM = {
    "a4": (210.0, 297.0),
    "a5": (148.0, 210.0),
    "letter": (215.9, 279.4),
    "legal": (215.9, 355.6),
}


def _dpi(dots_per_mm: float) -> float:
    return dots_per_mm * MM_PER_INCH


def _prepare(images: list[Image.Image], bilevel: bool, dither: str) -> list[Image.Image]:
    if not bilevel:
        return [img.convert("RGB") for img in images]
    from mbprint.raster import to_bilevel

    return [to_bilevel(img, dither).convert("RGB") for img in images]


def write_labels(images: list[Image.Image], out_path: str | Path, dots_per_mm: float = 8.0,
                 bilevel: bool = False, dither: str = "auto", title: str = "") -> Path:
    """One page per label, page size exactly the label size."""
    if not images:
        raise SystemExit("nothing to write: no labels")
    pages = _prepare(images, bilevel, dither)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    dpi = _dpi(dots_per_mm)
    pages[0].save(
        out, "PDF", resolution=dpi, save_all=True, append_images=pages[1:], title=title or None
    )
    return out


def write_sheet(images: list[Image.Image], out_path: str | Path, dots_per_mm: float = 8.0,
                page: str = "a4", margin_mm: float = 10.0, gap_mm: float = 2.0,
                columns: int | None = None, rows: int | None = None,
                marks: bool = True, bilevel: bool = False, dither: str = "auto",
                title: str = "") -> Path:
    """Tile labels onto sheets of paper, with optional cut marks."""
    if not images:
        raise SystemExit("nothing to write: no labels")
    if page not in PAGE_SIZES_MM:
        raise SystemExit(f"unknown page size {page!r}; use one of {sorted(PAGE_SIZES_MM)}")

    prepared = _prepare(images, bilevel, dither)
    lw, lh = prepared[0].size
    page_w_mm, page_h_mm = PAGE_SIZES_MM[page]
    page_w = round(page_w_mm * dots_per_mm)
    page_h = round(page_h_mm * dots_per_mm)
    margin = round(margin_mm * dots_per_mm)
    gap = round(gap_mm * dots_per_mm)

    usable_w = page_w - 2 * margin
    usable_h = page_h - 2 * margin
    cols = columns or max(1, (usable_w + gap) // (lw + gap))
    rws = rows or max(1, (usable_h + gap) // (lh + gap))
    if cols * (lw + gap) - gap > usable_w or rws * (lh + gap) - gap > usable_h:
        raise SystemExit(
            f"{cols}x{rws} labels of {lw}x{lh} dots do not fit on {page} "
            f"with a {margin_mm}mm margin"
        )
    per_page = cols * rws

    pages: list[Image.Image] = []
    for start in range(0, len(prepared), per_page):
        sheet = Image.new("RGB", (page_w, page_h), "white")
        draw = ImageDraw.Draw(sheet)
        for slot, img in enumerate(prepared[start:start + per_page]):
            col, row = slot % cols, slot // cols
            x = margin + col * (lw + gap)
            y = margin + row * (lh + gap)
            sheet.paste(img, (x, y))
            if marks:
                draw.rectangle([x - 1, y - 1, x + lw, y + lh], outline=(190, 190, 190))
        pages.append(sheet)

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    pages[0].save(
        out, "PDF", resolution=_dpi(dots_per_mm), save_all=True,
        append_images=pages[1:], title=title or None,
    )
    return out
