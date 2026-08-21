"""Logging and protocol tracing.

Three levels of detail:

  default   INFO  what is being printed, on which printer, over which link
  -v        DEBUG every command sent, decoded, with its bytes
  -vv       TRACE every write, including raster chunks, as a hex dump

TRACE is a custom level below DEBUG so that a full byte-level trace never
drowns the ordinary debug output.
"""

from __future__ import annotations

import logging
import sys

TRACE = 5
logging.addLevelName(TRACE, "TRACE")

MAX_HEX_BYTES = 32


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def trace(log: logging.Logger, msg: str, *args: object) -> None:
    """Log at TRACE. Callers should guard expensive arguments with `tracing()`."""
    if log.isEnabledFor(TRACE):
        log.log(TRACE, msg, *args)


def tracing(log: logging.Logger) -> bool:
    return log.isEnabledFor(TRACE)


def hexdump(data: bytes | bytearray, limit: int = MAX_HEX_BYTES) -> str:
    """Compact hex for a byte string, truncated so a raster chunk stays readable."""
    head = bytes(data[:limit]).hex(" ")
    if len(data) > limit:
        return f"{head} ... ({len(data)} bytes)"
    return head or "(empty)"


class _ShortName(logging.Filter):
    """Expose the module part of the logger name, for the verbose formats."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.shortname = record.name.replace("mbprint.", "")
        return True


def _console_handler(verbosity: int, plain: bool) -> logging.Handler:
    from mbprint import ui

    console = ui.console(plain)
    if console is not None:
        from rich.highlighter import NullHighlighter
        from rich.logging import RichHandler

        handler: logging.Handler = RichHandler(
            console=console,
            # Level color only: repr highlighting mangles hex dumps and model names.
            highlighter=NullHighlighter(),
            show_time=verbosity > 0,
            show_path=False,
            omit_repeated_times=False,
            markup=False,
            rich_tracebacks=True,
            log_time_format="[%H:%M:%S]",
        )
        handler.addFilter(_ShortName())
        handler.setFormatter(
            logging.Formatter("%(shortname)-10s %(message)s" if verbosity else "%(message)s")
        )
        return handler
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_Formatter(verbose=verbosity > 0))
    return handler


class _Formatter(logging.Formatter):
    """Plain lines for normal use, level-tagged once anything verbose is on."""

    def __init__(self, verbose: bool):
        super().__init__()
        self.verbose = verbose

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        if record.exc_info:
            message = f"{message}\n{self.formatException(record.exc_info)}"
        if not self.verbose:
            if record.levelno >= logging.WARNING:
                return f"{record.levelname.lower()}: {message}"
            return message
        name = record.name.replace("mbprint.", "")
        return f"{record.levelname:<7} {name:<18} {message}"


def configure(
    verbosity: int = 0, quiet: bool = False, log_file: str | None = None, plain: bool = False
) -> None:
    """Set up the mbprint logger. Called once, from the CLI.

    With rich installed and a terminal attached, log lines are colored by level
    and render above any live progress bar; otherwise they are plain stderr text.
    """
    if quiet:
        level = logging.WARNING
    elif verbosity >= 2:
        level = TRACE
    elif verbosity == 1:
        level = logging.DEBUG
    else:
        level = logging.INFO

    root = logging.getLogger("mbprint")
    root.setLevel(level)
    root.handlers.clear()
    root.propagate = False

    stream = _console_handler(verbosity, plain)
    stream.setLevel(level)
    root.addHandler(stream)

    if log_file:
        # The file always gets the full trace, whatever the console shows.
        handler = logging.FileHandler(log_file, encoding="utf-8")
        handler.setLevel(TRACE)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s %(message)s"))
        root.setLevel(min(level, TRACE))
        root.addHandler(handler)

    # bleak is chatty at DEBUG; only let it through on a full trace.
    logging.getLogger("bleak").setLevel(logging.DEBUG if verbosity >= 2 else logging.WARNING)
