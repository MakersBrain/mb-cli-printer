"""PDF output: one page per label, or labels tiled on a paper sheet."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw

MM_PER_INCH = 25.4

PAGE_SIZES_MM = {
    "a4": (210.0, 297.0),
    "a5": (148.0, 210.0),
    "letter": (215.9, 279.4),
    "legal": (215.9, 355.6),
}


@dataclass(frozen=True)
class RenderedPage:
    number: int
    width_mm: float
    height_mm: float
    image: Image.Image


def page_indices(spec: str | None, count: int) -> list[int]:
    """Parse one-based PDF page numbers/ranges into zero-based indices."""
    if count < 1:
        raise SystemExit("the PDF has no pages")
    if not spec:
        return list(range(count))
    selected: list[int] = []
    for token in spec.split(","):
        token = token.strip()
        try:
            if "-" in token:
                first_text, last_text = token.split("-", 1)
                first, last = int(first_text), int(last_text)
                if last < first:
                    raise ValueError
                numbers: Iterable[int] = range(first, last + 1)
            else:
                numbers = [int(token)]
        except ValueError:
            raise SystemExit(f"invalid page selection {spec!r}; use values like 1,3-5")
        for number in numbers:
            if not 1 <= number <= count:
                raise SystemExit(f"PDF page {number} is outside the available range 1-{count}")
            index = number - 1
            if index not in selected:
                selected.append(index)
    if not selected:
        raise SystemExit("no PDF pages selected")
    return selected


def render_pages(
    path: str | Path, dpi: int, pages: str | None = None, password: str | None = None
) -> list[RenderedPage]:
    """Rasterize selected PDF pages at printer resolution on a white background."""
    try:
        import pypdfium2 as pdfium
    except ImportError:
        raise SystemExit("PDF printing needs pypdfium2: pip install pypdfium2")

    source = Path(path)
    if not source.is_file():
        raise SystemExit(f"PDF not found: {source}")
    if dpi <= 0:
        raise SystemExit("PDF render DPI must be positive")
    try:
        document = pdfium.PdfDocument(source, password=password)
    except Exception as exc:
        raise SystemExit(f"cannot open PDF {source}: {exc}")

    rendered: list[RenderedPage] = []
    try:
        for index in page_indices(pages, len(document)):
            page = document[index]
            try:
                width_pt, height_pt = page.get_size()
                bitmap = page.render(scale=dpi / 72.0, fill_color=(255, 255, 255, 255))
                try:
                    image = bitmap.to_pil().convert("RGB").copy()
                finally:
                    bitmap.close()
            finally:
                page.close()
            rendered.append(
                RenderedPage(
                    number=index + 1,
                    width_mm=float(width_pt) * MM_PER_INCH / 72.0,
                    height_mm=float(height_pt) * MM_PER_INCH / 72.0,
                    image=image,
                )
            )
    except SystemExit:
        raise
    except Exception as exc:
        raise SystemExit(f"cannot render PDF {source}: {exc}")
    finally:
        document.close()
    return rendered


def _dpi(dots_per_mm: float) -> float:
    return dots_per_mm * MM_PER_INCH


def _prepare(images: list[Image.Image], bilevel: bool, dither: str) -> list[Image.Image]:
    if not bilevel:
        return [img.convert("RGB") for img in images]
    from mbprint.raster import to_bilevel

    return [to_bilevel(img, dither).convert("RGB") for img in images]


def write_labels(
    images: list[Image.Image],
    out_path: str | Path,
    dots_per_mm: float = 8.0,
    bilevel: bool = False,
    dither: str = "auto",
    title: str = "",
) -> Path:
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


def write_sheet(
    images: list[Image.Image],
    out_path: str | Path,
    dots_per_mm: float = 8.0,
    page: str = "a4",
    margin_mm: float = 10.0,
    gap_mm: float = 2.0,
    columns: int | None = None,
    rows: int | None = None,
    marks: bool = True,
    bilevel: bool = False,
    dither: str = "auto",
    title: str = "",
) -> Path:
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
        for slot, img in enumerate(prepared[start : start + per_page]):
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
        out,
        "PDF",
        resolution=_dpi(dots_per_mm),
        save_all=True,
        append_images=pages[1:],
        title=title or None,
    )
    return out
