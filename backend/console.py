"""Terminal output for the server: ordinary log lines, plus one progress line at the
bottom that updates in place ("SEC: 237/1028 (23%) · downloading AAPL").

Several jobs can show progress at once; their texts share the line. When the output
isn't a terminal (e.g. the macOS background service writing to a log file), there is
no line to update, so progress appears as an ordinary line only at milestones.
"""
from __future__ import annotations

import shutil
import sys
import threading

_lock = threading.Lock()
_status: dict[str, str] = {}   # job -> its progress text
_tty = sys.stdout.isatty()


def _clear() -> None:
    if _tty and _status:
        sys.stdout.write("\r\033[K")


def _draw() -> None:
    if _tty and _status:
        width = shutil.get_terminal_size((100, 20)).columns - 1
        line = "   |   ".join(_status.values())
        sys.stdout.write(line[:width])
    sys.stdout.flush()


def log(msg: str, error: bool = False) -> None:
    """An ordinary line (printed above the progress line)."""
    with _lock:
        _clear()
        sys.stdout.flush()
        (sys.stderr if error else sys.stdout).write(msg + "\n")
        (sys.stderr if error else sys.stdout).flush()
        _draw()


def progress(job: str, text: str, milestone: bool = False) -> None:
    """Update a job's progress line. Not a terminal: printed only when milestone is True."""
    if not _tty:
        if milestone:
            log(text)
        return
    with _lock:
        _clear()
        _status[job] = text
        _draw()


def done(job: str, msg: str | None = None) -> None:
    """Remove a job's progress line, optionally leaving a final message."""
    with _lock:
        _clear()
        _status.pop(job, None)
        if msg:
            sys.stdout.write(msg + "\n")
        _draw()
