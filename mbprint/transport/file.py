"""Capture transport: writes the exact byte stream to a file instead of a printer."""

from __future__ import annotations

import os
from pathlib import Path
from typing import BinaryIO

from mbprint.log import get_logger, hexdump, trace, tracing
from mbprint.transport import Transport

log = get_logger(__name__)


class FileTransport(Transport):
    """Writes the byte stream to a file (or nowhere) instead of to a printer.

    With `pace` set it also honours the protocol's inter-chunk delays, so a dry
    run takes about as long as the real print and exercises the progress bar.
    """

    name = "file"

    def __init__(
        self, path: str = "-", max_write: int = 512, quiet: bool = True, pace: bool = False
    ):
        self.path = path
        self.max_write = max_write
        self.quiet = quiet
        self.pace = pace
        self._fh: BinaryIO | None = None
        self.bytes_written = 0

    async def open(self) -> None:
        if self.path == "-":
            self._fh = None
        else:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            self._fh = open(self.path, "wb")  # noqa: SIM115 - closed in close()

    async def close(self) -> None:
        if self.path not in ("-", os.devnull):
            log.info("captured %d bytes to %s", self.bytes_written, self.path)
        else:
            log.debug("discarded %d bytes", self.bytes_written)
        if self._fh:
            self._fh.close()
            self._fh = None

    async def send(self, data: bytes) -> None:
        if tracing(log):
            trace(log, "-> write %d bytes: %s", len(data), hexdump(data))
        self.bytes_written += len(data)
        if self._fh:
            self._fh.write(data)

    async def delay(self, ms: int) -> None:
        # Instant unless asked to imitate a real link's pacing.
        if self.pace:
            await super().delay(ms)
