"""
Timestamped run logs and console output for the operator BAT files.

Every run writes its OWN file - logs/<kind>_YYYYMMDD_HHMMSS.log - so the
evidence of one run is never overwritten by the next. (The old installer
rewrote logs/install.log each time; the run that produced the 2026-09-24
screenshots was lost that way, overwritten by an interrupted --repair run
eleven minutes later.)
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Optional, TextIO

ROOT = Path(__file__).resolve().parent.parent

_COLOURS = {"PASS": "\033[32m", "OK": "\033[32m", "FAIL": "\033[31m", "WARN": "\033[33m",
            "DEGRADED": "\033[33m", "UNAVAILABLE": "\033[35m", "INPUT_REQUIRED": "\033[36m",
            "NOT_TESTED": "\033[2m", "INFO": "\033[2m"}
_RESET = "\033[0m"


def _log_root() -> Path:
    try:
        from core.paths import log_dir
        return log_dir()
    except Exception:
        return ROOT / "logs"


def _enable_vt():
    """ANSI colours in a classic Windows console."""
    if os.name == "nt":
        try:
            import ctypes
            kernel = ctypes.windll.kernel32
            handle = kernel.GetStdHandle(-11)
            mode = ctypes.c_uint32()
            if kernel.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel.SetConsoleMode(handle, mode.value | 0x0004)
        except Exception:
            pass


class RunLog:
    """Console + a timestamped log file for one run of one BAT command."""

    def __init__(self, kind: str, quiet: bool = False):
        self.kind = kind
        self.quiet = quiet
        self.stamp = time.strftime("%Y%m%d_%H%M%S")
        folder = _log_root()
        folder.mkdir(parents=True, exist_ok=True)
        self.path = folder / f"{kind}_{self.stamp}.log"
        # Two runs in the same second (a BAT re-launched elevated) must not
        # share - and so overwrite - one file.
        counter = 1
        while self.path.exists():
            counter += 1
            self.path = folder / f"{kind}_{self.stamp}_{counter}.log"
        self._file: Optional[TextIO] = self.path.open("w", encoding="utf-8")
        self.colour = sys.stdout is not None and sys.stdout.isatty()
        if self.colour:
            _enable_vt()
        self.write_file(f"ZEUS {kind} run {time.strftime('%Y-%m-%d %H:%M:%S')} "
                        f"python={sys.executable}")

    def write_file(self, line: str):
        if self._file is not None:
            try:
                self._file.write(f"{time.strftime('%H:%M:%S')}  {line}\n")
                self._file.flush()
            except Exception:
                pass

    def say(self, line: str = ""):
        if not self.quiet:
            try:
                print(line, flush=True)
            except UnicodeEncodeError:
                # A console that cannot show the status marks still has to say
                # pass / fail - a bare '?' for both would hide the result.
                line = line.translate({0x2713: "OK", 0x2717: "X ", 0x2013: "- "})
                print(line.encode("ascii", "replace").decode("ascii"), flush=True)
        self.write_file(line)

    def heading(self, title: str):
        self.say("")
        self.say(f"== {title} " + "=" * max(0, 66 - len(title)))

    def status(self, state: str, label: str, detail: str = ""):
        tag = state
        if self.colour:
            tag = f"{_COLOURS.get(state, '')}{state}{_RESET}"
        pad = " " * max(0, 14 - len(state))
        text = f"  {tag}{pad} {label}" + (f"  - {detail}" if detail else "")
        if not self.quiet:
            try:
                print(text, flush=True)
            except UnicodeEncodeError:
                print(text.encode("ascii", "replace").decode("ascii"), flush=True)
        self.write_file(f"{state:<14} {label}" + (f"  - {detail}" if detail else ""))

    def result(self, result):
        """Print one agni_ops.status.Result in the operator format."""
        self.status(result.status, result.component, result.reason)
        for key, value in (("where", result.where), ("fallback", result.fallback),
                           ("fix attempted", result.fix_attempted),
                           ("current state", result.current_state)):
            if value and result.status != "PASS":
                self.say(f"                 {key}: {value}")
        if result.evidence and result.status not in ("PASS",):
            self.write_file(f"                 evidence: {result.evidence}")

    def close(self):
        if self._file is not None:
            try:
                self._file.close()
            finally:
                self._file = None
