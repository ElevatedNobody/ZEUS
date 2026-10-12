"""
The Chatterbox bridge, kept loaded between sentences.

Why this exists
---------------
voice/providers.py used to start voice/chatterbox_bridge.py once per
SENTENCE. Every sentence re-imported torch and re-read ~3 GB of weights before
it could say a word. Measured on the RTX 5060 laptop on 2026-09-25, the same
sentence took 43 s one-shot and 22 s from a process that was already loaded
(CPU); the load was half of every sentence, and a reply of three sentences
paid it three times. The bridge always had a `--serve` mode (one JSON request
per line, model resident) - nothing used it.

This class owns one such process: start it, send a request, read the one-line
reply with a deadline, and report a crash with the process's own evidence
instead of hanging. The same shape as voice/stt_manager.WhisperWorker.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional

from core.logger import get_logger

logger = get_logger("voice.chatterbox_server")

ROOT = Path(__file__).resolve().parent.parent
BRIDGE = Path(__file__).resolve().parent / "chatterbox_bridge.py"


class BridgeDied(RuntimeError):
    """The bridge process ended (or never answered) while a request was open."""

    def __init__(self, message: str, exit_code: Optional[int] = None, stderr: str = ""):
        super().__init__(message)
        self.exit_code = exit_code
        self.stderr = stderr


class ChatterboxServer:
    """One `chatterbox_bridge.py --serve` process and its line protocol."""

    def __init__(self, python: Path, log_path: Optional[Path] = None):
        self.python = Path(python)
        self.log_path = log_path
        self.process: Optional[subprocess.Popen] = None
        self._replies: "queue.Queue" = queue.Queue()
        self._stderr: deque = deque(maxlen=200)
        self._lock = threading.Lock()
        self.busy = False
        self.started_at = 0.0
        self.last_used = 0.0
        self.device = ""
        self.requests = 0

    # -- lifecycle -----------------------------------------------------------

    @property
    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    @property
    def pid(self) -> Optional[int]:
        return self.process.pid if self.process is not None else None

    def start(self) -> None:
        env = dict(os.environ)
        env.setdefault("PYTHONIOENCODING", "utf-8")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        # stderr is PIPED and drained by a thread: torch and transformers write
        # a lot there, and an undrained pipe fills and blocks the bridge
        # mid-sentence. stdout carries only the JSON replies.
        self.process = subprocess.Popen(
            [str(self.python), str(BRIDGE), "--serve"], cwd=str(ROOT), env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1, creationflags=flags)
        self.started_at = time.time()
        self.last_used = self.started_at
        threading.Thread(target=self._pump_stdout, name="chatterbox-stdout", daemon=True).start()
        threading.Thread(target=self._pump_stderr, name="chatterbox-stderr", daemon=True).start()

    def _pump_stdout(self) -> None:
        process = self.process
        try:
            for line in process.stdout:
                line = line.strip()
                if line:
                    self._replies.put(line)
        except Exception:
            pass
        finally:
            self._replies.put(None)          # the process has gone

    def _pump_stderr(self) -> None:
        process = self.process
        handle = None
        if self.log_path is not None:
            try:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                handle = self.log_path.open("a", encoding="utf-8")
                handle.write(f"\n--- chatterbox bridge pid {process.pid} started "
                             f"{time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            except Exception:
                handle = None
        try:
            for line in process.stderr:
                self._stderr.append(line.rstrip())
                if handle is not None:
                    try:
                        handle.write(line)
                        handle.flush()
                    except Exception:
                        pass
        except Exception:
            pass
        finally:
            if handle is not None:
                try:
                    handle.close()
                except Exception:
                    pass

    def stderr_tail(self, lines: int = 6) -> str:
        return " | ".join(list(self._stderr)[-lines:])[:600]

    def stop(self) -> None:
        process, self.process = self.process, None
        if process is None:
            return
        try:
            if process.stdin:
                process.stdin.close()       # the --serve loop ends on EOF
        except Exception:
            pass
        try:
            process.wait(timeout=5)
        except Exception:
            try:
                process.kill()
                process.wait(timeout=5)
            except Exception:
                pass

    # -- use -----------------------------------------------------------------

    def request(self, payload: dict, timeout: float = 120.0) -> dict:
        """Send one request and wait for its one-line reply.

        Raises BridgeDied if the process ends first, TimeoutError if it does
        not answer in time (the process is then stopped: a bridge that has
        stopped answering cannot be trusted with the next sentence)."""
        with self._lock:
            if not self.alive:
                raise BridgeDied("the Chatterbox bridge is not running",
                                 self.process.returncode if self.process else None,
                                 self.stderr_tail())
            self.busy = True
            try:
                while True:                     # a stale reply must not answer this request
                    try:
                        self._replies.get_nowait()
                    except queue.Empty:
                        break
                try:
                    self.process.stdin.write(json.dumps(payload) + "\n")
                    self.process.stdin.flush()
                except Exception as error:
                    raise BridgeDied(f"could not reach the Chatterbox bridge: {error}",
                                     self._exit_code(), self.stderr_tail())
                deadline = time.time() + timeout
                while True:
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        self.stop()
                        raise TimeoutError(f"Chatterbox did not answer within {timeout:.0f} s")
                    try:
                        line = self._replies.get(timeout=min(remaining, 0.5))
                    except queue.Empty:
                        continue
                    if line is None:
                        code = self._exit_code()
                        raise BridgeDied("the Chatterbox bridge ended while speaking",
                                         code, self.stderr_tail())
                    try:
                        answer = json.loads(line)
                    except ValueError:
                        # A stray line on stdout (a library print) - not our reply.
                        self._stderr.append(f"[stdout] {line[:200]}")
                        continue
                    self.requests += 1
                    if answer.get("device"):
                        self.device = answer["device"]
                    return answer
            finally:
                self.busy = False
                self.last_used = time.time()

    def _exit_code(self) -> Optional[int]:
        process = self.process
        if process is None:
            return None
        try:
            return process.wait(timeout=3)
        except Exception:
            return process.poll()
