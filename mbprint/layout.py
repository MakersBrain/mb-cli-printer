"""label.json layout loader and renderer.

Reads the label format exported by the phomymo designer (version 3) and draws
it with Pillow at printer resolution. Geometry matches the reference canvas
renderer: element coordinates are dots at `dotsPerMm` (8 dots/mm = 203 dpi),
x/y is the element's top-left corner, and rotation is clockwise about its centre.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import csv
import io
import json
import os
import re
import shutil
import subprocess
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import metadata
from pathlib import Path
from typing import Any
from urllib.parse import quote

from PIL import Image, ImageDraw, ImageFont

from mbprint.log import get_logger

log = get_logger(__name__)

# A record is the flat {field: value} map a template renders against; an element
# is one entry of a label.json `elements` array, whose keys vary by type.
Record = dict[str, str]
Element = dict[str, Any]
Font = ImageFont.FreeTypeFont | ImageFont.ImageFont

BASE_DPI = 203.0
FIELD_PATTERN = re.compile(r"\{\{([^}]+)\}\}")
# Optional segment: [[ ... ]] disappears when every field inside it is empty.
OPTIONAL_PATTERN = re.compile(r"\[\[(.*?)\]\]", re.DOTALL)

_FONT_CANDIDATES = {
    "sans": ["DejaVuSans.ttf", "LiberationSans-Regular.ttf", "NotoSans-Regular.ttf", "Arial.ttf"],
    "sans-bold": [
        "DejaVuSans-Bold.ttf",
        "LiberationSans-Bold.ttf",
        "NotoSans-Bold.ttf",
        "Arial-Bold.ttf",
    ],
    "sans-italic": ["DejaVuSans-Oblique.ttf", "LiberationSans-Italic.ttf"],
    "sans-bolditalic": ["DejaVuSans-BoldOblique.ttf", "LiberationSans-BoldItalic.ttf"],
    "serif": ["DejaVuSerif.ttf", "LiberationSerif-Regular.ttf", "NotoSerif-Regular.ttf"],
    "serif-bold": ["DejaVuSerif-Bold.ttf", "LiberationSerif-Bold.ttf", "NotoSerif-Bold.ttf"],
    "serif-italic": ["DejaVuSerif-Italic.ttf", "LiberationSerif-Italic.ttf"],
    "serif-bolditalic": ["DejaVuSerif-BoldItalic.ttf", "LiberationSerif-BoldItalic.ttf"],
    "mono": ["DejaVuSansMono.ttf", "LiberationMono-Regular.ttf", "NotoSansMono-Regular.ttf"],
    "mono-bold": ["DejaVuSansMono-Bold.ttf", "LiberationMono-Bold.ttf"],
    "mono-italic": ["DejaVuSansMono-Oblique.ttf", "LiberationMono-Italic.ttf"],
    "mono-bolditalic": ["DejaVuSansMono-BoldOblique.ttf", "LiberationMono-BoldItalic.ttf"],
}
_FC_QUERY = {"sans": "sans-serif", "serif": "serif", "mono": "monospace"}
_GENERIC_FAMILIES = {
    "sans": "sans",
    "sans-serif": "sans",
    "serif": "serif",
    "mono": "mono",
    "monospace": "mono",
}
_FREE_FONT_SUBSTITUTIONS = {
    "arial": "Liberation Sans",
    "helvetica": "Liberation Sans",
    "georgia": "Gelasio",
    "timesnewroman": "Liberation Serif",
    "couriernew": "Liberation Mono",
    "impact": "Anton",
    "comicsansms": "Comic Neue",
}
_FONT_SUFFIXES = {".ttf", ".otf", ".ttc"}
_FONT_SEARCH_DIRS: tuple[Path, ...] = ()
_ALLOW_FONT_FALLBACK = True
_FONT_BUNDLE_ENTRY_POINT = "mbprint.font_bundles"

# Extra room around an element's box so unwrapped text can overflow like it
# does on the HTML canvas.
_OVERFLOW_PAD = 96


def _family_of(name: str | None) -> str:
    n = (name or "sans").lower()
    if "mono" in n or "courier" in n:
        return "mono"
    if "serif" in n and "sans" not in n:
        return "serif"
    if "georgia" in n or "times" in n:
        return "serif"
    return "sans"


def _font_key(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def _style_flags(style: str) -> tuple[bool, bool]:
    value = style.casefold()
    bold = any(word in value for word in ("bold", "semibold", "demibold", "black", "heavy"))
    italic = "italic" in value or "oblique" in value
    return bold, italic


def _primary_font_family(value: str) -> str:
    """Return the primary family from a CSS font-family stack."""
    try:
        names = next(csv.reader([value], skipinitialspace=True))
    except (csv.Error, StopIteration):
        names = [value]
    family = (names[0] if names else value).strip().strip("'\"")
    return family or "sans"


def configure_fonts(
    source: str | Path | None = None,
    font_dirs: list[str] | None = None,
    allow_fallback: bool = True,
) -> None:
    """Configure exact font lookup for one label-rendering command.

    Explicit directories, ``MBPRINT_FONT_DIR``, a ``fonts`` directory beside
    the label, installed font-bundle add-ons, and the package's own ``fonts``
    directory form a portable font bundle search path, in that order.
    """
    global _ALLOW_FONT_FALLBACK, _FONT_SEARCH_DIRS

    directories: list[Path] = []
    explicit = [Path(path) for path in font_dirs or []]
    env_dirs = [
        Path(path) for path in os.environ.get("MBPRINT_FONT_DIR", "").split(os.pathsep) if path
    ]
    for directory in explicit + env_dirs:
        if not directory.is_dir():
            raise SystemExit(f"font directory not found: {directory}")
        directories.append(directory.resolve())
    if source is not None:
        beside_label = Path(source).resolve().parent / "fonts"
        if beside_label.is_dir():
            directories.append(beside_label)
    directories.extend(_installed_font_bundle_dirs())
    packaged = Path(__file__).with_name("fonts")
    if packaged.is_dir():
        directories.append(packaged)

    _FONT_SEARCH_DIRS = tuple(dict.fromkeys(directories))
    _ALLOW_FONT_FALLBACK = allow_fallback
    _bundled_faces.cache_clear()
    _font_path.cache_clear()
    _load_font.cache_clear()


@lru_cache(maxsize=1)
def _installed_font_bundle_dirs() -> tuple[Path, ...]:
    """Load font directories advertised by optional Python packages."""
    directories: list[Path] = []
    entry_points = metadata.entry_points().select(group=_FONT_BUNDLE_ENTRY_POINT)

    for entry_point in entry_points:
        try:
            provider = entry_point.load()
            provided = provider() if callable(provider) else provider
            values = [provided] if isinstance(provided, (str, os.PathLike)) else list(provided)
            for value in values:
                directory = Path(value).resolve()
                if not directory.is_dir():
                    log.warning(
                        "font bundle %s returned a missing directory: %s",
                        entry_point.name,
                        directory,
                    )
                    continue
                directories.append(directory)
        except Exception as exc:  # add-ons must not prevent unrelated commands starting
            log.warning("cannot load font bundle %s: %s", entry_point.name, exc)
    return tuple(dict.fromkeys(directories))


def _face_keys(family: str, style: str, path: Path) -> tuple[str, ...]:
    """Every name a bundled face answers to.

    A face is indexed both by the family it declares and by its file name with
    the declared style removed. Nerd Font builds declare an abbreviated family
    ("JetBrainsMono NF") while their files -- and the people who write the
    name into a layout -- say "JetBrainsMono Nerd Font", so indexing only the
    declared name leaves a bundled face unreachable by the name it is asked
    for. Without this the lookup falls through to the host, which is exactly
    what a portable bundle is meant to avoid.
    """
    keys = [_font_key(family)]
    stem, style_key = _font_key(path.stem), _font_key(style)
    if style_key and stem.endswith(style_key):
        stem = stem[: -len(style_key)]
    if stem and stem not in keys:
        keys.append(stem)
    return tuple(keys)


@lru_cache(maxsize=16)
def _bundled_faces(
    directories: tuple[Path, ...],
) -> tuple[tuple[str, bool, bool, str], ...]:
    faces: list[tuple[str, bool, bool, str]] = []
    for directory in directories:
        for path in sorted(
            item for item in directory.rglob("*") if item.suffix.casefold() in _FONT_SUFFIXES
        ):
            try:
                font = ImageFont.truetype(str(path), 12)
                family, style = font.getname()
            except OSError:
                log.warning("cannot load bundled font %s", path)
                continue
            face_bold, face_italic = _style_flags(style or "")
            keys = _face_keys(family or path.stem, style or "", path)
            variations: list[str] = []
            # Not every face is a variable font, and Pillow only grows
            # get_variation_names() when it was built against a FreeType with
            # the variation API. Either way the face is still usable, just
            # without named instances.
            with contextlib.suppress(AttributeError, OSError):
                variations = [name.decode(errors="replace") for name in font.get_variation_names()]
            for key in keys:
                if variations:
                    for variation in variations:
                        variation_bold, variation_italic = _style_flags(variation)
                        faces.append(
                            (key, variation_bold, face_italic or variation_italic, str(path))
                        )
                else:
                    faces.append((key, face_bold, face_italic, str(path)))
    return tuple(faces)


def _fontconfig_path(family: str, bold: bool, italic: bool, exact: bool) -> str | None:
    fc = shutil.which("fc-match")
    if not fc:
        return None
    query = family
    if bold:
        query += ":weight=bold"
    if italic:
        query += ":slant=italic"
    try:
        output = subprocess.run(
            [fc, "-f", "%{family}\t%{style}\t%{file}", query],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        matched_family, style, filename = output.split("\t", 2)
    except ValueError:
        return None
    if exact:
        requested = _font_key(family)
        names = {_font_key(name) for name in matched_family.split(",")}
        if requested not in names:
            return None
        face_bold, face_italic = _style_flags(style)
        if (face_bold, face_italic) != (bold, italic):
            return None
    return filename if filename and Path(filename).is_file() else None


@lru_cache(maxsize=64)
def _font_path(family: str, bold: bool, italic: bool) -> str | None:
    generic = _GENERIC_FAMILIES.get(family.casefold())
    if generic is None:
        wanted = _font_key(family)
        for face_family, face_bold, face_italic, path in _bundled_faces(_FONT_SEARCH_DIRS):
            if face_family == wanted and (face_bold, face_italic) == (bold, italic):
                return path
        return _fontconfig_path(family, bold, italic, exact=True)

    suffix = ""
    if bold and italic:
        suffix = "-bolditalic"
    elif bold:
        suffix = "-bold"
    elif italic:
        suffix = "-italic"
    for candidate in _FONT_CANDIDATES.get(generic + suffix, []):
        for directory in _FONT_SEARCH_DIRS:
            bundled = directory / candidate
            if bundled.is_file():
                return str(bundled)
        try:
            ImageFont.truetype(candidate, 12)
            return candidate
        except OSError:
            pass
    return _fontconfig_path(_FC_QUERY[generic], bold, italic, exact=False)


@lru_cache(maxsize=512)
def _load_font(
    family: str, bold: bool, italic: bool, size: int
) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    path = _font_path(family, bold, italic)
    if not path:
        style = " ".join(part for part, enabled in (("bold", bold), ("italic", italic)) if enabled)
        description = f"{family} {style}".strip()
        if not _ALLOW_FONT_FALLBACK:
            locations = ", ".join(str(path) for path in _FONT_SEARCH_DIRS) or "no font bundle"
            raise SystemExit(
                f"font {description!r} is not available ({locations}); install it, put its "
                ".ttf/.otf file in a fonts/ directory beside the label, pass --font-dir, "
                "or explicitly allow substitution with --font-fallback"
            )
        replacement = _FREE_FONT_SUBSTITUTIONS.get(_font_key(family))
        if replacement:
            path = _font_path(replacement, bold, italic)
            if path:
                log.warning("font %r is not available; substituting %s", description, replacement)
                return _open_font(path, size, bold)
        generic = _family_of(family)
        path = _font_path(generic, bold, italic)
        if path:
            log.warning("font %r is not available; substituting %s", description, path)
            return _open_font(path, size, bold)
        log.warning(
            "no %s font on this system; using the bitmap default, "
            "which ignores the layout's font size",
            description,
        )
        return ImageFont.load_default()
    log.debug("font %s %dpx -> %s", family, size, path)
    try:
        return _open_font(path, size, bold)
    except OSError as exc:
        if not _ALLOW_FONT_FALLBACK:
            raise SystemExit(f"cannot load font {family!r} from {path}: {exc}")
        log.warning("cannot load font %r from %s; using the bitmap default", family, path)
        return ImageFont.load_default()


def _open_font(path: str, size: int, bold: bool) -> ImageFont.FreeTypeFont:
    """Open a static face or select the requested named variable-font weight."""
    font = ImageFont.truetype(path, max(1, size))
    try:
        names = font.get_variation_names()
    except (AttributeError, OSError):
        return font
    wanted = b"Bold" if bold else b"Regular"
    if wanted in names:
        font.set_variation_by_name(wanted)
    return font


# --- template substitution -------------------------------------------------


def _parse_placeholder(body: str) -> tuple[str, list[tuple[str, str]]]:
    """`price|num:2` -> ("price", [("num", "2")])."""
    parts = body.split("|")
    name = parts[0].strip()
    filters = []
    for part in parts[1:]:
        filter_name, _, arg = part.strip().partition(":")
        filters.append((filter_name.strip(), arg))
    return name, filters


def _f_num(value: str, arg: str, decimal: str) -> str:
    """Format a number: `num` drops a zero fraction, `num:N` fixes the decimals."""
    try:
        number = float(str(value).replace(decimal, ".").replace(",", ".").strip())
    except ValueError:
        return value
    if arg:
        try:
            text = f"{number:.{int(arg)}f}"
        except ValueError:
            raise SystemExit(f"num: expected a digit count, got {arg!r}")
    else:
        text = f"{number:.6f}".rstrip("0").rstrip(".")
    return text.replace(".", decimal)


def _f_truncate(value: str, arg: str) -> str:
    try:
        width = int(arg)
    except ValueError:
        raise SystemExit(f"truncate: expected a length, got {arg!r}")
    if width <= 0 or len(value) <= width:
        return value
    return value[: max(1, width - 1)].rstrip() + "\u2026"


def _f_slug(value: str) -> str:
    text = unicodedata.normalize("NFKD", value)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", text.lower())).strip("-")


# Template filters. Each takes the current value and returns the next one; the
# signature is (value, arg, decimal) so every filter sees the same context.
def _f_replace(value: str, arg: str, _decimal: str) -> str:
    """`replace:a:b`; a missing second half deletes the match."""
    old, _, new = arg.partition(":")
    return value.replace(old, new)


FILTERS: dict[str, Callable[[str, str, str], str]] = {
    "upper": lambda v, a, d: v.upper(),
    "lower": lambda v, a, d: v.lower(),
    "title": lambda v, a, d: v.title(),
    "capitalize": lambda v, a, d: v.capitalize(),
    "trim": lambda v, a, d: v.strip(),
    "truncate": lambda v, a, d: _f_truncate(v, a),
    "default": lambda v, a, d: v if v.strip() else a,
    "num": _f_num,
    "slug": lambda v, a, d: _f_slug(v),
    "urlencode": lambda v, a, d: quote(v, safe=""),
    "replace": _f_replace,
}


def apply_filters(value: str, filters: list[tuple[str, str]], decimal: str = ",") -> str:
    for name, arg in filters:
        func = FILTERS.get(name)
        if func is None:
            raise SystemExit(
                f"unknown template filter {name!r}; available: {', '.join(sorted(FILTERS))}"
            )
        value = func(str(value), arg, decimal)
    return value


def substitute(text: str, record: Record, decimal: str = ",") -> str:
    """Render a template against a record.

    `{{field}}` is replaced from the record, optionally through a pipeline of
    filters: `{{price|num:2}}`, `{{name|truncate:18|upper}}`. Unknown fields are
    left as-is so typos stay visible. `[[optional]]` segments disappear when
    every field inside them is empty.
    """

    def sub_fields(s: str) -> str:
        def one(match: re.Match[str]) -> str:
            name, filters = _parse_placeholder(match.group(1))
            if name not in record and not any(f == "default" for f, _ in filters):
                return match.group(0)
            return apply_filters(str(record.get(name, "")), filters, decimal)

        return FIELD_PATTERN.sub(one, s)

    def sub_optional(m: re.Match[str]) -> str:
        inner = m.group(1)
        names = [_parse_placeholder(n)[0] for n in FIELD_PATTERN.findall(inner)]
        if names and all(str(record.get(n, "")).strip() == "" for n in names):
            return ""
        return sub_fields(inner)

    return sub_fields(OPTIONAL_PATTERN.sub(sub_optional, text))


def template_fields(text: str, required_only: bool = False) -> list[str]:
    """Placeholder names in a template.

    With `required_only`, names that appear solely inside `[[optional]]`
    segments are left out: an empty value there is a design decision, not a
    missing field.
    """
    text = text or ""
    if required_only:
        text = OPTIONAL_PATTERN.sub("", text)
    seen: list[str] = []
    for body in FIELD_PATTERN.findall(text):
        name = _parse_placeholder(body)[0]
        if name not in seen:
            seen.append(name)
    return seen


def missing_fields(text: str, record: Record) -> list[str]:
    """Required placeholders in `text` that this record leaves empty.

    A placeholder carrying a `default:` filter always has a value, so it never
    counts as missing.
    """
    stripped = OPTIONAL_PATTERN.sub("", text or "")
    missing: list[str] = []
    for body in FIELD_PATTERN.findall(stripped):
        name, filters = _parse_placeholder(body)
        if any(f == "default" and a.strip() for f, a in filters):
            continue
        if str(record.get(name, "")).strip() == "" and name not in missing:
            missing.append(name)
    return missing


# --- label model -----------------------------------------------------------


@dataclass
class Label:
    width_mm: float
    height_mm: float
    dots_per_mm: float = 8.0
    round: bool = False
    continuous: bool = False
    name: str = ""
    elements: list[Element] = field(default_factory=list)
    fields: list[Element] = field(default_factory=list)
    source: Path | None = None
    # Set for SVG layouts: the document is kept whole and rendered by an
    # external SVG renderer instead of element by element.
    svg_source: str | None = None

    @property
    def width_px(self) -> int:
        return round(self.width_mm * self.dots_per_mm)

    @property
    def height_px(self) -> int:
        return round(self.height_mm * self.dots_per_mm)

    def templates(self) -> list[str]:
        """Every template string in the layout: text, QR data and barcode data."""
        if self.svg_source is not None:
            from mbprint import svgtemplate

            return svgtemplate.templates(self.svg_source)
        return [
            el[key]
            for el in self.elements
            for key in ("text", "qrData", "barcodeData")
            if el.get(key)
        ]

    def placeholders(self, required_only: bool = False) -> list[str]:
        seen: list[str] = []
        for text in self.templates():
            for name in template_fields(text, required_only):
                if name not in seen:
                    seen.append(name)
        return seen

    def missing_for(self, record: Record) -> list[str]:
        """Required placeholders this record cannot fill."""
        seen: list[str] = []
        for text in self.templates():
            for name in missing_fields(text, record):
                if name not in seen:
                    seen.append(name)
        return seen

    @classmethod
    def load(cls, path: str | Path) -> Label:
        p = Path(path)
        if p.suffix.lower() == ".svg":
            from mbprint import svgtemplate

            return svgtemplate.load(p)
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise SystemExit(f"label file not found: {p}")
        except ValueError as exc:
            raise SystemExit(f"{p} is not valid JSON: {exc}")
        size = data.get("labelSize") or {}
        width = data.get("widthMm") or size.get("width")
        height = data.get("heightMm") or size.get("height")
        if not width or not height:
            raise SystemExit(f"{p}: missing widthMm/heightMm")
        log.debug(
            "loaded %s: %s elements, %smm x %smm at %s dots/mm",
            p,
            len(data.get("elements") or []),
            width,
            height,
            data.get("dotsPerMm") or 8,
        )
        return cls(
            width_mm=float(width),
            height_mm=float(height),
            dots_per_mm=float(data.get("dotsPerMm") or 8),
            round=bool(data.get("round") or size.get("round")),
            continuous=bool(data.get("continuous") or size.get("continuous")),
            name=data.get("name", p.stem),
            elements=list(data.get("elements") or []),
            fields=list(data.get("fields") or []),
            source=p,
        )


# --- element rendering -----------------------------------------------------


def _norm_text_style(el: Element) -> tuple[str, bool, bool, bool]:
    """(family, bold, italic, underline), accepting both the compact export
    keys (font/bold/italic) and the full designer keys (fontFamily/fontWeight)."""
    family = _primary_font_family(str(el.get("fontFamily") or el.get("font") or "sans"))
    weight = str(el.get("fontWeight") or "normal").casefold()
    bold = (
        bool(el.get("bold"))
        or weight in {"bold", "bolder"}
        or (weight.isdigit() and int(weight) >= 600)
    )
    italic = bool(el.get("italic")) or el.get("fontStyle") == "italic"
    underline = bool(el.get("underline")) or el.get("textDecoration") == "underline"
    return family, bold, italic, underline


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: Font, max_width: float) -> list[str]:
    lines: list[str] = []
    for paragraph in text.split("\n"):
        words = paragraph.split(" ")
        current = ""
        for word in words:
            trial = f"{current} {word}".strip()
            if current and draw.textlength(trial, font=font) > max_width:
                lines.append(current)
                current = word
            else:
                current = trial
        lines.append(current)
    return lines or [""]


def _auto_scale_size(
    draw: ImageDraw.ImageDraw,
    el: Element,
    text: str,
    w: float,
    h: float,
    family: str,
    bold: bool,
    italic: bool,
    base: float,
) -> float:
    """Largest size (down from `base`) whose wrapped block fits the box."""
    size = max(6.0, base)
    while size > 5:
        font = _load_font(family, bold, italic, round(size))
        lines = text.split("\n") if el.get("noWrap") else _wrap(draw, text, font, max(1.0, w - 8))
        widest = max((draw.textlength(ln, font=font) for ln in lines), default=0)
        if widest <= w - 8 and len(lines) * size * 1.2 <= h:
            return size
        size -= 1
    return 6.0


def _render_text(
    layer: Image.Image, el: Element, w: float, h: float, ox: float, oy: float, scale: float
) -> None:
    text = el.get("text") or ""
    draw = ImageDraw.Draw(layer)
    background = el.get("background")
    if background and background != "transparent":
        draw.rectangle([ox, oy, ox + w, oy + h], fill=background)
    if not text.strip():
        return

    family, bold, italic, underline = _norm_text_style(el)
    base_size = float(el.get("fontSize") or 16) * scale
    if el.get("autoScale"):
        base_size = _auto_scale_size(draw, el, text, w, h, family, bold, italic, base_size)
    font = _load_font(family, bold, italic, round(base_size))

    if el.get("noWrap"):
        lines = text.split("\n")
    else:
        lines = _wrap(draw, text, font, max(1.0, w - 8 * scale))

    line_height = base_size * 1.2
    total = len(lines) * line_height
    valign = el.get("verticalAlign") or "middle"
    if valign == "top":
        y = oy + line_height / 2 + 2 * scale
    elif valign == "bottom":
        y = oy + h - total + line_height / 2 - 2 * scale
    else:
        y = oy + h / 2 - total / 2 + line_height / 2

    align = el.get("align") or el.get("textAlign") or "center"
    if align == "left":
        x, anchor = ox + 4 * scale, "lm"
    elif align == "right":
        x, anchor = ox + w - 4 * scale, "rm"
    else:
        x, anchor = ox + w / 2, "mm"

    color = el.get("color") or "black"
    for line in lines:
        draw.text((x, y), line, font=font, fill=color, anchor=anchor)
        if underline:
            length = draw.textlength(line, font=font)
            ux = x if align == "left" else (x - length if align == "right" else x - length / 2)
            uy = y + base_size * 0.45
            draw.line([ux, uy, ux + length, uy], fill=color, width=max(1, round(base_size / 16)))
        y += line_height


def _pixels(img: Image.Image) -> Any:
    """`Image.load()` narrowed: Pillow only returns None for an unloadable image."""
    px = img.load()
    if px is None:  # pragma: no cover - defensive
        raise SystemExit("cannot access image pixels")
    return px


def _decode_data_uri(uri: str) -> bytes:
    if uri.startswith("data:"):
        _, _, payload = uri.partition(",")
        if ";base64" in uri.split(",", 1)[0]:
            return base64.b64decode(payload)
        return payload.encode("utf-8")
    return Path(uri).read_bytes()


def _render_image(
    layer: Image.Image, el: Element, w: float, h: float, ox: float, oy: float
) -> None:
    uri = el.get("imageData") or el.get("src")
    if not uri:
        return
    try:
        raw = _decode_data_uri(uri)
        img: Image.Image = Image.open(io.BytesIO(raw))
        img.load()
    except (OSError, ValueError, binascii.Error) as exc:
        raise SystemExit(f"cannot decode image element {el.get('id', '?')}: {exc}")
    img = img.convert("RGBA").resize((max(1, round(w)), max(1, round(h))), Image.Resampling.LANCZOS)
    brightness = el.get("brightness") or 0
    contrast = el.get("contrast") or 0
    if brightness or contrast:
        from PIL import ImageEnhance

        rgb = img.convert("RGB")
        if brightness:
            rgb = ImageEnhance.Brightness(rgb).enhance(1 + brightness / 100)
        if contrast:
            rgb = ImageEnhance.Contrast(rgb).enhance(1 + contrast / 100)
        img = rgb.convert("RGBA")
    layer.alpha_composite(img, (round(ox), round(oy)))


def _render_qr(layer: Image.Image, el: Element, w: float, h: float, ox: float, oy: float) -> None:
    data = (el.get("qrData") or "").strip()
    if not data:
        return
    import qrcode

    ec = {
        "L": qrcode.constants.ERROR_CORRECT_L,
        "M": qrcode.constants.ERROR_CORRECT_M,
        "Q": qrcode.constants.ERROR_CORRECT_Q,
        "H": qrcode.constants.ERROR_CORRECT_H,
    }.get(str(el.get("errorCorrection") or "M").upper(), qrcode.constants.ERROR_CORRECT_M)
    qr = qrcode.QRCode(error_correction=ec, border=int(el.get("margin", 1)), box_size=1)
    qr.add_data(data)
    qr.make(fit=True)
    modules = qr.get_matrix()
    n = len(modules)
    side = max(1, round(min(w, h)))
    if side < n:
        raise SystemExit(
            f"QR element {el.get('id', '?')} is {side} dots wide but the code needs "
            f"at least {n}; enlarge the element or shorten its data"
        )
    # Draw one pixel per module, then scale to the size set in the editor. Nearest
    # neighbour keeps the modules hard-edged; the box fills exactly as designed.
    grid = Image.new("1", (n, n), 1)
    px = _pixels(grid)
    for y, row in enumerate(modules):
        for x, dark in enumerate(row):
            if dark:
                px[x, y] = 0
    rendered = grid.resize((side, side), Image.Resampling.NEAREST).convert("RGBA")
    dx = ox + (w - side) / 2
    dy = oy + (h - side) / 2
    layer.alpha_composite(rendered, (round(dx), round(dy)))


def _render_barcode(
    layer: Image.Image, el: Element, w: float, h: float, ox: float, oy: float
) -> None:
    data = (el.get("barcodeData") or "").strip()
    if not data:
        return
    try:
        import barcode
        from barcode.writer import ImageWriter
    except ImportError:
        raise SystemExit("barcode elements need python-barcode: pip install python-barcode")
    fmt = (el.get("barcodeFormat") or "code128").lower().replace("_", "")
    aliases = {
        "code128": "code128",
        "code39": "code39",
        "ean13": "ean13",
        "ean8": "ean8",
        "upca": "upca",
        "itf": "itf",
    }
    try:
        cls = barcode.get_barcode_class(aliases.get(fmt, fmt))
    except Exception:
        raise SystemExit(f"unsupported barcode format {fmt!r}")
    buf = io.BytesIO()
    cls(data, writer=ImageWriter()).write(
        buf, options={"write_text": bool(el.get("showText", True)), "quiet_zone": 1}
    )
    buf.seek(0)
    img = Image.open(buf).convert("RGBA")
    img = img.resize((max(1, round(w)), max(1, round(h))), Image.Resampling.LANCZOS)
    layer.alpha_composite(img, (round(ox), round(oy)))


def _render_shape(
    layer: Image.Image, el: Element, w: float, h: float, ox: float, oy: float, scale: float
) -> None:
    draw = ImageDraw.Draw(layer)
    shape = el.get("shapeType") or el.get("shape") or "rectangle"
    fill = el.get("fill") if el.get("fill") not in (None, "transparent", "none") else None
    stroke = el.get("stroke") or ("black" if not fill else None)
    width = max(1, round(float(el.get("strokeWidth") or 1) * scale))
    box = [ox, oy, ox + w, oy + h]
    if shape in ("rectangle", "rect", "square"):
        draw.rectangle(box, fill=fill, outline=stroke, width=width)
    elif shape in ("ellipse", "circle"):
        draw.ellipse(box, fill=fill, outline=stroke, width=width)
    elif shape in ("line", "hline"):
        draw.line([ox, oy + h / 2, ox + w, oy + h / 2], fill=stroke or "black", width=width)
    elif shape == "vline":
        draw.line([ox + w / 2, oy, ox + w / 2, oy + h], fill=stroke or "black", width=width)
    else:
        draw.rectangle(box, fill=fill, outline=stroke, width=width)


def render(
    label: Label, record: Record | None = None, scale: float = 1.0, decimal: str = ","
) -> Image.Image:
    """Render one label to an RGB image. `scale` > 1 renders for higher-dpi heads."""
    if label.svg_source is not None:
        from mbprint import svgtemplate

        return svgtemplate.render(label, record, scale, decimal)
    record = record or {}
    width = max(1, round(label.width_px * scale))
    height = max(1, round(label.height_px * scale))
    canvas = Image.new("RGBA", (width, height), (255, 255, 255, 255))

    for el in label.elements:
        el = dict(el)
        for key in ("text", "qrData", "barcodeData"):
            if el.get(key):
                el[key] = substitute(el[key], record, decimal)

        ew = float(el.get("width") or 0) * scale
        eh = float(el.get("height") or 0) * scale
        ex = float(el.get("x") or 0) * scale
        ey = float(el.get("y") or 0) * scale
        if ew <= 0 or eh <= 0:
            continue

        etype = el.get("type")
        pad = 0 if el.get("clipOverflow") or etype != "text" else round(_OVERFLOW_PAD * scale)
        layer = Image.new("RGBA", (round(ew) + 2 * pad, round(eh) + 2 * pad), (0, 0, 0, 0))
        ox = oy = pad

        if etype == "text":
            _render_text(layer, el, ew, eh, ox, oy, scale)
        elif etype == "image":
            _render_image(layer, el, ew, eh, ox, oy)
        elif etype == "qr":
            _render_qr(layer, el, ew, eh, ox, oy)
        elif etype == "barcode":
            _render_barcode(layer, el, ew, eh, ox, oy)
        elif etype == "shape":
            _render_shape(layer, el, ew, eh, ox, oy, scale)
        else:
            log.debug("skipping element %s of unknown type %r", el.get("id", "?"), etype)
            continue

        rotation = float(el.get("rotation") or 0)
        cx, cy = ex + ew / 2, ey + eh / 2
        if rotation:
            # Canvas rotates clockwise for positive degrees; PIL rotates the other way.
            layer = layer.rotate(-rotation, resample=Image.Resampling.BICUBIC, expand=True)
        canvas.alpha_composite(layer, (round(cx - layer.width / 2), round(cy - layer.height / 2)))

    if label.round:
        mask = Image.new("L", (width, height), 0)
        ImageDraw.Draw(mask).ellipse([0, 0, width - 1, height - 1], fill=255)
        white = Image.new("RGBA", (width, height), (255, 255, 255, 255))
        canvas = Image.composite(canvas, white, mask)

    return canvas.convert("RGB")
