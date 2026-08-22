"""DejaVu font bundle discovery hook for mb-cli-printer."""

from pathlib import Path


def font_directory() -> Path:
    """Return the installed directory containing this add-on's font files."""
    return Path(__file__).with_name("fonts")
