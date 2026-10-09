"""
Midway Traders — optional log file for a bot run.

With LOG=1 set, everything the bot shows in the terminal is also written to
logs/<TEAM_ID>_<timestamp>.log: our ORDER/FILL lines, the engine's log
messages, its console output (session banners, tick/rank lines, portfolio
tables) and any crash traceback. The terminal output is unchanged.

    make trader BOT=midway_traders_trader_1 LOG=1
    LOG=1 TEAM_ID=midway_traders_trader_1 python -m team.trader
"""

from __future__ import annotations

import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import IO

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


class _Tee:
    """Write to the terminal as-is and to the log file as timestamped,
    colour-free lines. Everything else (isatty, encoding, ...) is the
    terminal's, so rich keeps its colours."""

    def __init__(self, terminal: IO[str], log: IO[str]) -> None:
        self._terminal = terminal
        self._log = log
        self._partial = ""

    def write(self, text: str) -> int:
        self._terminal.write(text)
        *lines, self._partial = (self._partial + _ANSI.sub("", text)).split("\n")
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for line in lines:
            self._log.write(f"{stamp} {line}\n" if line.strip() else "\n")
        self._log.flush()
        return len(text)

    def flush(self) -> None:
        self._terminal.flush()
        self._log.flush()

    def __getattr__(self, name):
        return getattr(self._terminal, name)


def setup() -> Path | None:
    """Start writing the bot's output to a log file if LOG is set; return
    the file's path."""
    if os.environ.get("LOG", "").lower() not in ("1", "true", "yes"):
        return None

    # Same console format as arena's run(). Configuring it here first makes
    # run()'s own basicConfig a no-op, so the file handler below survives.
    # Its console handler keeps the real stderr, so log lines are not
    # written to the file twice once stderr is teed below.
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S")

    LOG_DIR.mkdir(exist_ok=True)
    bot = os.environ.get("TEAM_ID") or "bot"
    path = LOG_DIR / f"{bot}_{datetime.now():%Y%m%d_%H%M%S}.log"
    log = open(path, "a", encoding="utf-8")

    handler = logging.StreamHandler(log)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    logging.getLogger().addHandler(handler)

    # The engine prints straight to stdout (rich console, print()), and
    # crash tracebacks go to stderr; neither passes through logging.
    sys.stdout = _Tee(sys.stdout, log)
    sys.stderr = _Tee(sys.stderr, log)

    logging.getLogger(__name__).info("Logging to %s", path)
    return path
