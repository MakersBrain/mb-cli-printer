"""Persistent defaults (~/.config/mbprint/config.json).

Holds the things you calibrate once per printer: model, transport, density and
the roller alignment offsets.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

# The config file is free-form JSON: scalars plus the nested `data` table.
Config = dict[str, Any]

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "mbprint"
CONFIG_PATH = CONFIG_DIR / "config.json"

KNOWN_KEYS: dict[str, type] = {
    "model": str,
    "transport": str,
    "address": str,
    "device": str,
    "density": int,
    "feed": int,
    "speed": int,
    "offset_x": int,
    "offset_y": int,
    "align": str,
    "dither": str,
    "continuous": bool,
    "gap_mm": float,
    "tspl_offset_mm": float,
    "label": str,
    "media": str,
    "host": str,
}

# Derived field templates live under "data" as a table: data.qr, data.brand, ...
NESTED_PREFIX = "data."


def load() -> Config:
    try:
        loaded: Config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return loaded
    except FileNotFoundError:
        return {}
    except ValueError as exc:
        raise SystemExit(f"{CONFIG_PATH} is not valid JSON: {exc}")


def save(data: Config) -> Path:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    # Scalars sorted for readability; the `data` table keeps insertion order,
    # because derived fields are evaluated in the order they were defined.
    ordered = {k: data[k] for k in sorted(data) if k != "data"}
    if data.get("data"):
        ordered["data"] = data["data"]
    CONFIG_PATH.write_text(json.dumps(ordered, indent=2) + "\n", encoding="utf-8")
    return CONFIG_PATH


def data_templates(config: Config | None = None) -> list[tuple[str, str]]:
    """The `data` table as ordered (key, template) pairs."""
    table = (config if config is not None else load()).get("data") or {}
    return [(k, str(v)) for k, v in table.items()]


def set_key(config: Config, key: str, value: str) -> Config:
    """Set a scalar key, or a `data.<name>` template."""
    if key.startswith(NESTED_PREFIX):
        name = key[len(NESTED_PREFIX) :]
        if not name:
            raise SystemExit("config key 'data.' needs a field name, e.g. data.qr")
        config.setdefault("data", {})[name] = value
    else:
        config[key] = coerce(key, value)
    return config


def unset_key(config: Config, key: str) -> Config:
    if key.startswith(NESTED_PREFIX):
        config.get("data", {}).pop(key[len(NESTED_PREFIX) :], None)
        if not config.get("data"):
            config.pop("data", None)
    else:
        config.pop(key, None)
    return config


def flatten(config: Config) -> Config:
    """Config as flat `key = value` pairs, `data` included as data.<name>."""
    flat = {k: v for k, v in config.items() if k != "data"}
    for name, template in (config.get("data") or {}).items():
        flat[f"{NESTED_PREFIX}{name}"] = template
    return flat


def coerce(key: str, value: str) -> Any:
    kind = KNOWN_KEYS.get(key)
    if kind is None:
        raise SystemExit(
            f"unknown config key {key!r}; known keys: {', '.join(sorted(KNOWN_KEYS))}, "
            f"plus data.<field> for derived field templates"
        )
    if kind is bool:
        return str(value).lower() in ("1", "true", "yes", "on")
    try:
        return kind(value)
    except ValueError:
        raise SystemExit(f"config key {key} expects {kind.__name__}, got {value!r}")
