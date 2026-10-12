"""Start tests/e2e/app_harness.py as a child process and read its output."""

from __future__ import annotations

import os
import queue
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class App:
    def __init__(self, data: Path, *extra: str):
        self.port = free_port()
        self.lines: "queue.Queue[str]" = queue.Queue()
        self.output: list = []
        env = dict(os.environ)
        env.pop("AGNI_TEST_MODE", None)            # the harness is the real app, not pytest mode
        env["PYTHONIOENCODING"] = "utf-8"
        self.proc = subprocess.Popen(
            [sys.executable, str(ROOT / "tests" / "e2e" / "app_harness.py"),
             "--data", str(data), "--cdp", str(self.port), *extra],
            cwd=str(ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace")
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        for line in self.proc.stdout:
            self.output.append(line)
            self.lines.put(line)

    def wait_for(self, marker: str, timeout: float = 90) -> str:
        end = time.time() + timeout
        for line in self.output:
            if marker in line:
                return line
        while time.time() < end:
            try:
                line = self.lines.get(timeout=0.5)
            except queue.Empty:
                if self.proc.poll() is not None:
                    break
                continue
            if marker in line:
                return line
        raise AssertionError(f"'{marker}' not seen. Output tail:\n{''.join(self.output[-40:])}")

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
