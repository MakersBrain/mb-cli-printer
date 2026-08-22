"""Free compatible-font discovery hook for mb-cli-printer."""

from pathlib import Path


def font_directory() -> Path:
    return Path(__file__).with_name("fonts")
