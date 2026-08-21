"""Brother DK media.

Every DK roll has a fixed geometry: the tape is wider than the printable area,
and the printable area is not centred under the head, so each entry carries the
right-hand offset needed to land the artwork straight. Dots are at 300 dpi
(11.811 dots/mm), the native resolution of the QL series.

Figures follow Brother's media table as implemented by the brother_ql project.
"""

from __future__ import annotations

from dataclasses import dataclass

from PIL import Image

DIE_CUT = "die-cut"
ENDLESS = "endless"
ROUND_DIE_CUT = "round"

WIDE_MODELS = ["ql-1050", "ql-1060n", "ql-1100", "ql-1110nwb", "ql-1115nwb"]


@dataclass(frozen=True)
class Media:
    id: str
    width_mm: float
    length_mm: float          # 0 for continuous rolls
    form: str
    dots_total: tuple[int, int]
    dots_printable: tuple[int, int]
    offset_r: int             # dots of margin on the right, to centre the print
    feed_margin: int = 0
    models: tuple[str, ...] = ()   # empty means every model

    @property
    def continuous(self) -> bool:
        return self.form == ENDLESS

    @property
    def name(self) -> str:
        if self.form == ROUND_DIE_CUT:
            return f"{self.width_mm:g}mm round die-cut"
        if self.form == DIE_CUT:
            return f"{self.width_mm:g}x{self.length_mm:g}mm die-cut"
        return f"{self.width_mm:g}mm continuous"

    def works_with(self, printer_id: str) -> bool:
        return not self.models or printer_id in self.models


ALL_MEDIA: tuple[Media, ...] = (
    Media("12", 12, 0, ENDLESS, (142, 0), (106, 0), 29, 35),
    Media("29", 29, 0, ENDLESS, (342, 0), (306, 0), 6, 35),
    Media("38", 38, 0, ENDLESS, (449, 0), (413, 0), 12, 35),
    Media("50", 50, 0, ENDLESS, (590, 0), (554, 0), 12, 35),
    Media("54", 54, 0, ENDLESS, (636, 0), (590, 0), 0, 35),
    Media("62", 62, 0, ENDLESS, (732, 0), (696, 0), 12, 35),
    Media("102", 102, 0, ENDLESS, (1200, 0), (1164, 0), 12, 35, tuple(WIDE_MODELS)),
    Media("103", 104, 0, ENDLESS, (1224, 0), (1200, 0), 12, 35, tuple(WIDE_MODELS)),
    Media("17x54", 17, 54, DIE_CUT, (201, 636), (165, 566), 0),
    Media("17x87", 17, 87, DIE_CUT, (201, 1026), (165, 956), 0),
    Media("23x23", 23, 23, DIE_CUT, (272, 272), (202, 202), 42),
    Media("29x42", 29, 42, DIE_CUT, (342, 495), (306, 425), 6),
    Media("29x90", 29, 90, DIE_CUT, (342, 1061), (306, 991), 6),
    Media("39x90", 38, 90, DIE_CUT, (449, 1061), (413, 991), 12),
    Media("39x48", 39, 48, DIE_CUT, (461, 565), (425, 495), 6),
    Media("52x29", 52, 29, DIE_CUT, (614, 341), (578, 271), 0),
    Media("60x86", 60, 87, DIE_CUT, (708, 1024), (672, 954), 18),
    Media("62x29", 62, 29, DIE_CUT, (732, 341), (696, 271), 12),
    Media("62x100", 62, 100, DIE_CUT, (732, 1179), (696, 1109), 12),
    Media("102x51", 102, 51, DIE_CUT, (1200, 596), (1164, 526), 12, 0, tuple(WIDE_MODELS)),
    Media("102x152", 102, 153, DIE_CUT, (1200, 1804), (1164, 1660), 12, 0, tuple(WIDE_MODELS)),
    Media("103x164", 104, 164, DIE_CUT, (1224, 1941), (1200, 1822), 12, 0,
          ("ql-1100", "ql-1110nwb")),
    Media("d12", 12, 12, ROUND_DIE_CUT, (142, 142), (94, 94), 113, 35),
    Media("d24", 24, 24, ROUND_DIE_CUT, (284, 284), (236, 236), 42),
    Media("d58", 58, 58, ROUND_DIE_CUT, (688, 688), (618, 618), 51),
)

# Media type byte for the print-information command.
MEDIA_TYPE_BYTE = {DIE_CUT: 0x0B, ROUND_DIE_CUT: 0x0B, ENDLESS: 0x0A}


def by_id(media_id: str) -> Media | None:
    for m in ALL_MEDIA:
        if m.id == media_id:
            return m
    return None


def for_printer(printer_id: str) -> list[Media]:
    return [m for m in ALL_MEDIA if m.works_with(printer_id)]


def match_size(width_mm: float, height_mm: float, printer_id: str,
               tolerance: float = 1.5) -> Media | None:
    """Find the media a label of this size is meant for.

    Die-cut rolls must match both dimensions; a continuous roll only needs the
    width, since the length is whatever you print.
    """
    best = None
    for m in for_printer(printer_id):
        if abs(m.width_mm - width_mm) > tolerance:
            continue
        if m.continuous:
            candidate = (2, m)
        elif abs(m.length_mm - height_mm) <= tolerance:
            candidate = (1, m)
        else:
            continue
        if best is None or candidate[0] < best[0]:
            best = candidate
    return best[1] if best else None


def resolve(media_id: str | None, label_width_mm: float, label_height_mm: float,
            printer_id: str) -> Media:
    """Pick the media explicitly, or infer it from the label size."""
    if media_id:
        m = by_id(media_id)
        if m is None:
            raise SystemExit(
                f"unknown media {media_id!r}; available for {printer_id}: "
                + ", ".join(x.id for x in for_printer(printer_id))
            )
        if not m.works_with(printer_id):
            raise SystemExit(f"{m.id} media does not fit {printer_id}")
        return m
    m = match_size(label_width_mm, label_height_mm, printer_id)
    if m is None:
        raise SystemExit(
            f"no DK media matches a {label_width_mm:g}x{label_height_mm:g}mm label; "
            f"pass --media ID, one of: "
            + ", ".join(x.id for x in for_printer(printer_id))
        )
    return m


def render_scale(media: Media, label_width_px: int) -> float:
    """Scale that makes a label fill the printable width of this roll."""
    return media.dots_printable[0] / max(1, label_width_px)


def fit(img: Image.Image, media: Media, min_rows: int = 0) -> Image.Image:
    """Size a rendered label to the roll's printable area.

    Die-cut rolls have a fixed printable rectangle, slightly smaller than the
    label itself, so the artwork is scaled to fit and centred. Continuous rolls
    fix only the width; the length is however long the label came out, padded up
    to the printer's minimum number of raster lines if it is very short.
    """
    printable_w, printable_h = media.dots_printable
    if media.continuous:
        if img.width != printable_w:
            height = max(1, round(img.height * printable_w / img.width))
            img = img.resize((printable_w, height), Image.LANCZOS)
        if min_rows and img.height < min_rows:
            padded = Image.new("RGB", (printable_w, min_rows), "white")
            padded.paste(img.convert("RGB"), (0, 0))
            img = padded
        return img

    scale = min(printable_w / img.width, printable_h / img.height)
    sized = img.resize((max(1, round(img.width * scale)),
                        max(1, round(img.height * scale))), Image.LANCZOS)
    canvas = Image.new("RGB", (printable_w, printable_h), "white")
    canvas.paste(sized.convert("RGB"),
                 ((printable_w - sized.width) // 2, (printable_h - sized.height) // 2))
    return canvas
