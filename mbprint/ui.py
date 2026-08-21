"""Terminal presentation: colored logging and the live progress bar.

Both are provided by `rich`, an optional dependency (`uv sync --extra tui`).
Rich renders log lines above a live bar rather than fighting it, which matters
because printing emits protocol traces while a transfer is in flight.

Without rich installed, or when output is not a terminal, everything degrades
to the plain single-line reporter and the plain log formatter. Nothing here is
required for printing to work.
"""

from __future__ import annotations

import os
import sys

_console = None
_console_checked = False


def rich_available() -> bool:
    try:
        import rich  # noqa: F401

        return True
    except ImportError:
        return False


def color_enabled(plain: bool = False) -> bool:
    """Rich output unless disabled, piped, or the library is missing."""
    if plain or os.environ.get("NO_COLOR"):
        return False
    if not sys.stderr.isatty():
        return False
    return rich_available()


def console(plain: bool = False):
    """The one stderr Console shared by the log handler and the progress bar."""
    global _console, _console_checked
    if not _console_checked:
        _console_checked = True
        if color_enabled(plain):
            from rich.console import Console

            _console = Console(stderr=True)
    return _console


def reset() -> None:
    """Forget the cached console. For tests, and after changing options."""
    global _console, _console_checked
    _console = None
    _console_checked = False


class Progress:
    """No-op reporter: used when logging is verbose or output is redirected."""

    def start(self) -> None:
        pass

    def label(self, index: int, total: int, name: str) -> None:
        pass

    def chunk(self, percent: int) -> None:
        pass

    def finish_label(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()


class PlainProgress(Progress):
    """One rewritten line on stderr, for terminals without rich."""

    def __init__(self):
        self._index = 0
        self._total = 0
        self._name = ""
        self._last = -10

    def label(self, index: int, total: int, name: str) -> None:
        self._index, self._total, self._name, self._last = index, total, name, -10

    def chunk(self, percent: int) -> None:
        if percent - self._last < 5 and percent < 100:
            return
        self._last = percent
        print(f"\r[{self._index}/{self._total}] {self._name}: {percent:3d}%",
              end="", file=sys.stderr, flush=True)

    def finish_label(self) -> None:
        print(f"\r[{self._index}/{self._total}] {self._name}: done      ", file=sys.stderr)


def _AmountColumn():
    """Labels count as `7/22`, the in-flight transfer as `62%`."""
    from rich.progress import ProgressColumn
    from rich.text import Text

    class AmountColumn(ProgressColumn):
        def render(self, task):
            if task.fields.get("unit") == "percent":
                return Text(f"{task.percentage:>3.0f}%", style="green")
            return Text(f"{int(task.completed)}/{int(task.total or 0)}", style="green")

    return AmountColumn()


class RichProgress(Progress):
    """Two bars: labels completed overall, and bytes sent for the current label."""

    def __init__(self, total: int, plain: bool = False):
        from rich.progress import (BarColumn, Progress as RProgress, SpinnerColumn,
                                   TextColumn, TimeRemainingColumn)

        self._total = total
        self._progress = RProgress(
            SpinnerColumn(style="cyan"),
            TextColumn("[bold]{task.description}"),
            BarColumn(bar_width=None, complete_style="cyan", finished_style="green"),
            _AmountColumn(),
            TimeRemainingColumn(compact=True),
            console=console(plain),
            transient=False,
        )
        self._labels = None
        self._current = None

    def start(self) -> None:
        self._progress.start()
        self._labels = self._progress.add_task("labels", total=self._total, unit="count")
        self._current = self._progress.add_task(
            "waiting", total=100, visible=False, unit="percent")

    def label(self, index: int, total: int, name: str) -> None:
        self._progress.update(self._current, description=name, completed=0, visible=True)

    def chunk(self, percent: int) -> None:
        self._progress.update(self._current, completed=percent)

    def finish_label(self) -> None:
        self._progress.update(self._current, completed=100)
        self._progress.advance(self._labels)

    def stop(self) -> None:
        if self._current is not None:
            self._progress.update(self._current, visible=False)
        self._progress.stop()


def progress(total: int, enabled: bool = True, plain: bool = False) -> Progress:
    """Pick the best reporter available for this terminal and verbosity."""
    if not enabled or not sys.stderr.isatty():
        return Progress()
    if color_enabled(plain):
        return RichProgress(total, plain)
    return PlainProgress()
