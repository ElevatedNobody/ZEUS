"""
STT manager: runs Whisper in a supervised worker process and chooses the
backend from what actually works on this machine right now.

    ZEUS -> SpeechToText -> STTManager -> Whisper worker process -> CTranslate2

The main process never imports CTranslate2 in this mode. A native crash in
the worker (access violation, CUDA fault, a failed allocation that aborts)
ends only the worker; the manager records the exit code and stderr, classifies
the failure, blacklists that backend for the session and moves to the next.

Backends, in order (see build_chain):
    PRIMARY       large model on CUDA float16          (when a GPU is present)
    FALLBACK      CUDA int8_float16, then the CPU int8, then a smaller Whisper
                  model (models/whisper-small) on CUDA int8_float16
    LAST_RESORT   the smaller Whisper model on the CPU

Each one is loaded only if the commit headroom for it exists (core/sysmem.py);
if it does not, idle Ollama models are unloaded first. A backend that CRASHES
is not retried in this session - retrying a native crash in a loop is how a
machine ends up unusable.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, List, Optional

from core.logger import get_logger

logger = get_logger("stt.manager")

ROOT = Path(__file__).resolve().parent.parent

# Windows NTSTATUS exit codes, as a crashed process reports them.
_NTSTATUS = {
    0xC0000005: ("access_violation", "native access violation (a NULL or invalid pointer was read)"),
    0xC0000409: ("fail_fast", "native fail-fast / stack buffer overrun (a library aborted)"),
    0xC0000374: ("heap_corruption", "native heap corruption"),
    0xC00000FD: ("stack_overflow", "native stack overflow"),
    0xC0000135: ("dll_not_found", "a required DLL was not found"),
    0xC000001D: ("illegal_instruction", "illegal CPU instruction (a binary built for another CPU)"),
}

# Commit (host) memory each backend needs, measured on this project's
# reference laptop with large-v3-turbo: CUDA float16 peaked at 4.1 GB private,
# CPU int8 at 4.8 GB. Smaller models are scaled from their file size.
_NEED_GB = {("cuda", "float16"): 4.3, ("cuda", "int8_float16"): 4.0, ("cpu", "int8"): 5.0}
# ...and with whisper-small (models/whisper-small, 0.45 GiB model.bin), load plus
# transcription, 2026-09-27: CUDA float16 2.49, CUDA int8_float16 2.19, CPU
# int8 3.22 GiB. Most of it is fixed cost - importing CTranslate2 alone commits
# 0.78 GiB - so a smaller model is interpolated between the two measurements,
# never scaled by file size: that rule budgeted 1.8 GB for whisper-small on the
# CPU, which really peaks at 3.2 and could have been loaded without room for it.
_SMALL_NEED_GB = {("cuda", "float16"): 2.5, ("cuda", "int8_float16"): 2.2, ("cpu", "int8"): 3.25}
_LARGE_MODEL_GIB, _SMALL_MODEL_GIB = 1.51, 0.45


def need_gb(device: str, compute_type: str, model_gib: float) -> float:
    """Commit to budget for this model file size on this backend (+0.3 GB)."""
    large = _NEED_GB.get((device, compute_type), 4.5)
    small = _SMALL_NEED_GB.get((device, compute_type), large)
    position = max(0.0, (model_gib - _SMALL_MODEL_GIB) / (_LARGE_MODEL_GIB - _SMALL_MODEL_GIB))
    return round(small + (large - small) * position + 0.3, 2)

_blacklist: Dict[tuple, str] = {}          # (model_dir, device, compute_type) -> reason


def classify_failure(exit_code: Optional[int], text: str) -> tuple:
    """(classification, human explanation) for a worker failure."""
    if exit_code is not None and exit_code not in (0, 1):
        code = exit_code & 0xFFFFFFFF
        if code in _NTSTATUS:
            name, why = _NTSTATUS[code]
            return name, f"{why} (exit 0x{code:08X})"
    lowered = (text or "").lower()
    if any(m in lowered for m in ("mkl_malloc", "failed to allocate", "memoryerror",
                                  "out of memory", "paging file", "bad_alloc")):
        return "out_of_memory", "the runtime could not allocate memory"
    if any(m in lowered for m in ("cudnn", "cublas", "cuda driver", "cuda failed",
                                  "no cuda", "cuda error", "cudaerror")):
        return "cuda_runtime", "the CUDA runtime failed"
    if any(m in lowered for m in ("dll load failed", "modulenotfounderror", "importerror",
                                  "no module named")):
        return "runtime_missing", "a runtime library is missing or will not load"
    if exit_code is None:
        return "error", (text or "worker error")[:200]
    return "crash", f"the worker exited with code {exit_code}"


class WorkerCrash(RuntimeError):
    def __init__(self, message: str, exit_code: Optional[int] = None,
                 classification: str = "crash", stderr_tail: str = ""):
        super().__init__(message)
        self.exit_code = exit_code
        self.classification = classification
        self.stderr_tail = stderr_tail


class WhisperWorker:
    """One worker process and its line protocol."""

    def __init__(self, python: Optional[str] = None, log_path: Optional[Path] = None):
        self.python = python or sys.executable
        self.log_path = log_path
        self.process: Optional[subprocess.Popen] = None
        self._replies: "queue.Queue" = queue.Queue()
        self._stderr: deque = deque(maxlen=200)
        self._next_id = 1
        self._lock = threading.Lock()
        self.busy = False

    @property
    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start(self, timeout: float = 60.0):
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.process = subprocess.Popen(
            [self.python, "-m", "voice.stt_worker"], cwd=str(ROOT), env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1, creationflags=flags)
        threading.Thread(target=self._pump_stdout, daemon=True, name="stt-worker-out").start()
        threading.Thread(target=self._pump_stderr, daemon=True, name="stt-worker-err").start()
        ready = self._await(0, timeout)
        if not ready.get("ready"):
            raise self._crash("the worker did not start")

    def _pump_stdout(self):
        process = self.process
        for line in process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                self._replies.put(json.loads(line))
            except ValueError:
                self._stderr.append(f"[stdout] {line}")
        self._replies.put(None)                 # EOF: the process is gone

    def _pump_stderr(self):
        process = self.process
        handle = None
        if self.log_path is not None:
            try:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                handle = self.log_path.open("a", encoding="utf-8")
                handle.write(f"\n--- worker pid {process.pid} started "
                             f"{time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            except Exception:
                handle = None
        for line in process.stderr:
            self._stderr.append(line.rstrip())
            if handle is not None:
                try:
                    handle.write(line)
                    handle.flush()
                except Exception:
                    pass
        if handle is not None:
            handle.close()

    def stderr_tail(self, lines: int = 25) -> str:
        return "\n".join(list(self._stderr)[-lines:])

    def _crash(self, what: str) -> WorkerCrash:
        process = self.process
        code = None
        if process is not None:
            try:
                code = process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                code = None
        tail = self.stderr_tail()
        if process is None or code == 0:
            # A clean exit: the worker was stopped on purpose (released to
            # free memory, or closed). That is not a failure of the backend.
            return WorkerCrash(f"{what}: the worker was stopped", exit_code=code,
                               classification="stopped", stderr_tail=tail)
        name, why = classify_failure(code, tail)
        return WorkerCrash(f"{what}: {why}", exit_code=code, classification=name,
                           stderr_tail=tail)

    def _await(self, request_id: int, timeout: float) -> dict:
        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                self.stop(force=True)
                raise WorkerCrash(f"the worker did not answer within {int(timeout)} s",
                                  classification="hung", stderr_tail=self.stderr_tail())
            try:
                reply = self._replies.get(timeout=min(remaining, 1.0))
            except queue.Empty:
                if not self.alive:
                    raise self._crash("the worker process ended")
                continue
            if reply is None:
                raise self._crash("the worker process ended")
            if reply.get("id") == request_id:
                return reply

    def request(self, command: str, timeout: float = 300.0, **fields) -> dict:
        with self._lock:
            if not self.alive:
                raise WorkerCrash("the worker is not running", classification="stopped")
            request_id = self._next_id
            self._next_id += 1
            self.busy = True
            try:
                self.process.stdin.write(json.dumps({"id": request_id, "cmd": command, **fields}) + "\n")
                self.process.stdin.flush()
            except (BrokenPipeError, OSError):
                self.busy = False
                raise self._crash("the worker process ended")
            try:
                return self._await(request_id, timeout)
            finally:
                self.busy = False

    def stop(self, force: bool = False):
        process, self.process = self.process, None
        if process is None:
            return
        try:
            if not force and process.poll() is None:
                process.stdin.write(json.dumps({"id": -1, "cmd": "quit"}) + "\n")
                process.stdin.flush()
                process.wait(timeout=5)
        except Exception:
            pass
        if process.poll() is None:
            process.kill()
            try:
                process.wait(timeout=5)
            except Exception:
                pass


@dataclass
class Backend:
    role: str
    model_dir: str
    device: str
    compute_type: str
    need_gb: float
    label: str = ""

    @property
    def key(self) -> tuple:
        return (self.model_dir, self.device, self.compute_type)

    def describe(self) -> str:
        return f"{self.label or Path(self.model_dir).name} {self.device}/{self.compute_type}"


def _smaller_model_dirs(models_root: Path, primary: Path) -> List[Path]:
    """Other complete Whisper model folders (models/whisper-small, ...), smallest first.

    config.ini [voice] stt_fallback_model chooses which: `auto` (every complete
    models/whisper-* folder), a size such as `small` (models/whisper-small only),
    or `none`. Installed by `scripts/setup_models.py --whisper-fallback <size>`."""
    try:
        from voice.offline import config_str
        wanted = (config_str("stt_fallback_model", "auto") or "auto").strip().lower()
    except Exception:
        wanted = "auto"
    if wanted in ("none", "off", "false", "0", "no"):
        return []
    found = []
    for folder in sorted(models_root.glob("whisper*")):
        if (not folder.is_dir() or folder.resolve() == primary.resolve()
                or not (folder / "model.bin").is_file()):
            continue
        if wanted != "auto" and folder.name.lower() != f"whisper-{wanted}":
            continue
        found.append(folder)
    return sorted(found, key=lambda f: (f / "model.bin").stat().st_size)


def build_chain(model_dir: Path, device_pref: str = "auto", compute_pref: str = "auto",
                has_gpu: Optional[bool] = None, model_label: str = "") -> List[Backend]:
    """PRIMARY / FALLBACK / LAST_RESORT for this machine."""
    if has_gpu is None:
        try:
            from core.hardware import profile
            has_gpu = bool(profile().gpus)
        except Exception:
            has_gpu = False
    size_gb = (model_dir / "model.bin").stat().st_size / 1024 ** 3 if (model_dir / "model.bin").is_file() else 1.5
    candidates: List[tuple] = []
    if has_gpu and device_pref in ("auto", "cuda"):
        first = compute_pref if compute_pref not in ("auto", "", "int8") else "float16"
        candidates.append(("cuda", first))
        if first != "int8_float16":
            candidates.append(("cuda", "int8_float16"))
    if device_pref in ("auto", "cpu", "cuda"):
        candidates.append(("cpu", "int8"))
    chain: List[Backend] = []
    for device, compute in candidates:
        chain.append(Backend("", str(model_dir), device, compute, need_gb(device, compute, size_gb), model_label))
    for folder in _smaller_model_dirs(model_dir.parent, model_dir):
        folder_gb = (folder / "model.bin").stat().st_size / 1024 ** 3
        # A smaller model on the GPU at int8_float16 commits the least host
        # memory of any backend (measured: 2.2 GiB for whisper-small), so under
        # memory pressure it comes first; the CPU stays the last resort.
        smaller = ([("cuda", "int8_float16")] if has_gpu and device_pref in ("auto", "cuda") else []) + [("cpu", "int8")]
        for device, compute in smaller:
            chain.append(Backend("", str(folder), device, compute, need_gb(device, compute, folder_gb), folder.name))
    for index, backend in enumerate(chain):
        backend.role = ("PRIMARY" if index == 0 else
                        "LAST_RESORT" if index == len(chain) - 1 and len(chain) > 2 else "FALLBACK")
    return chain


class STTManager:
    """Owns the worker and the choice of backend for one SpeechToText."""

    def __init__(self, chain: List[Backend], cpu_threads: int = 0,
                 log_path: Optional[Path] = None, python: Optional[str] = None):
        self.chain = chain
        self.cpu_threads = cpu_threads
        self.log_path = log_path
        self.python = python
        self.worker: Optional[WhisperWorker] = None
        self.active: Optional[Backend] = None
        self.attempts: List[dict] = []
        self.last_error = ""
        self._lock = threading.Lock()
        self._tmp = Path(tempfile.mkdtemp(prefix="agni_stt_"))
        try:
            from core.model_lifecycle import register_releaser
            register_releaser("whisper-worker", self.release_if_idle)
        except Exception:
            pass

    # ------------------------------------------------------------ memory --

    @staticmethod
    def _room_for(backend: Backend) -> tuple:
        try:
            from core.sysmem import has_headroom
            ok, reason = has_headroom(backend.need_gb, margin_gb=0.5)
            if ok:
                return True, reason
            try:
                from core.model_lifecycle import active_in_this_process, unload_all
                if not active_in_this_process():
                    return ok, reason
                dropped = unload_all()
                if dropped:
                    logger.info(f"unloaded {', '.join(dropped)} to make room for Whisper")
            except Exception:
                pass
            return has_headroom(backend.need_gb, margin_gb=0.5)
        except Exception as error:
            return True, f"headroom not measured ({error})"

    # ----------------------------------------------------------- loading --

    def ensure_loaded(self) -> Backend:
        with self._lock:
            if self.worker is not None and self.worker.alive and self.active is not None:
                return self.active
            self._stop_worker()
            reasons = []
            for backend in self.chain:
                if backend.key in _blacklist:
                    reasons.append(f"{backend.describe()}: blacklisted this session "
                                   f"({_blacklist[backend.key]})")
                    continue
                room, why = self._room_for(backend)
                if not room:
                    self.attempts.append({"backend": backend.describe(), "role": backend.role,
                                          "ok": False, "classification": "insufficient_memory",
                                          "error": why})
                    reasons.append(f"{backend.describe()}: {why}")
                    continue
                worker = WhisperWorker(self.python, self.log_path)
                try:
                    worker.start()
                    reply = worker.request("load", timeout=600, model_dir=backend.model_dir,
                                           device=backend.device, compute_type=backend.compute_type,
                                           cpu_threads=self.cpu_threads if backend.device == "cpu" else 0)
                    if not reply.get("ok"):
                        name, why = classify_failure(None, reply.get("error", ""))
                        raise WorkerCrash(f"load failed: {reply.get('error', '')}",
                                          classification=name, stderr_tail=worker.stderr_tail())
                except WorkerCrash as crash:
                    worker.stop(force=True)
                    self._record_failure(backend, crash)
                    reasons.append(f"{backend.describe()}: {crash}")
                    continue
                self.worker, self.active = worker, backend
                self.attempts.append({"backend": backend.describe(), "role": backend.role,
                                      "ok": True, "load_seconds": reply.get("seconds"),
                                      "peak_private_mb": reply.get("peak_private_mb")})
                logger.info(f"STT worker ready: {backend.role} {backend.describe()} "
                            f"(pid {worker.process.pid}, load {reply.get('seconds')} s)")
                self.last_error = ""
                return backend
            self.last_error = "no Whisper backend could be loaded: " + "; ".join(reasons)
            raise RuntimeError(self.last_error)

    def _record_failure(self, backend: Backend, crash: WorkerCrash):
        entry = {"backend": backend.describe(), "role": backend.role, "ok": False,
                 "classification": crash.classification, "exit_code": crash.exit_code,
                 "error": str(crash)[:300], "stderr_tail": crash.stderr_tail[-800:]}
        self.attempts.append(entry)
        # A memory shortage is a condition of the moment, and a worker that
        # was stopped on purpose did not fail - neither is blacklisted. Every
        # other failure is, for this session.
        if crash.classification not in ("insufficient_memory", "stopped"):
            _blacklist[backend.key] = f"{crash.classification}: {str(crash)[:120]}"
        logger.error(f"STT backend {backend.role} {backend.describe()} failed - "
                     f"{crash.classification}: {crash}")
        try:
            from security import events
            events.record("stt_backend_failed", f"{backend.describe()}: {crash.classification}",
                          severity="warning", persist=False)
        except Exception:
            pass

    # ------------------------------------------------------ transcription --

    def transcribe(self, audio, language: Optional[str] = None,
                   options: Optional[dict] = None) -> dict:
        """audio: a file path, or a float32 mono 16 kHz array. One retry on
        the next backend if the active one crashes mid-transcription."""
        path, temporary = self._as_path(audio)
        try:
            for _attempt in range(3):
                backend = self.ensure_loaded()
                # The worker may be released (to free memory for a language
                # model) between ensure_loaded() and this call - take one
                # reference and treat "stopped" as "load again", not a failure.
                worker = self.worker
                if worker is None:
                    continue
                try:
                    reply = worker.request("transcribe", timeout=600, path=str(path),
                                           language=language, options=options or {})
                except WorkerCrash as crash:
                    if crash.classification != "stopped":
                        self._record_failure(backend, crash)
                    self._stop_worker()
                    continue
                if reply.get("ok"):
                    return reply
                name, why = classify_failure(None, reply.get("error", ""))
                if name in ("out_of_memory", "cuda_runtime"):
                    self._record_failure(backend, WorkerCrash(reply.get("error", ""),
                                                              classification=name))
                    self._stop_worker()
                    continue
                raise RuntimeError(reply.get("error", "transcription failed"))
            raise RuntimeError(self.last_error or "every Whisper backend failed")
        finally:
            if temporary:
                try:
                    path.unlink()
                except OSError:
                    pass

    def _as_path(self, audio):
        if isinstance(audio, (str, Path)):
            return Path(audio), False
        import numpy as np
        path = self._tmp / f"clip_{time.time_ns()}.npy"
        np.save(path, np.asarray(audio, dtype=np.float32).reshape(-1))
        return path, True

    # ---------------------------------------------------------- lifecycle --

    def _stop_worker(self):
        worker, self.worker, self.active = self.worker, None, None
        if worker is not None:
            worker.stop()

    def release_if_idle(self) -> bool:
        """Called by core/model_lifecycle.py when a language model needs the
        memory. Frees the worker unless it is transcribing right now."""
        worker = self.worker
        if worker is None or worker.busy:
            return False
        with self._lock:
            self._stop_worker()
        logger.info("released the idle Whisper worker to free memory")
        return True

    def status(self) -> dict:
        return {"isolation": "process",
                "active": (f"{self.active.role} {self.active.describe()}" if self.active else None),
                "worker_pid": self.worker.process.pid if self.worker and self.worker.alive else None,
                "chain": [f"{b.role} {b.describe()} (needs ~{b.need_gb} GB)" for b in self.chain],
                "blacklisted": {" ".join(k[1:]) + f" {Path(k[0]).name}": v for k, v in _blacklist.items()},
                "attempts": self.attempts[-10:], "last_error": self.last_error}

    def close(self):
        self._stop_worker()
        try:
            from core.model_lifecycle import unregister_releaser
            unregister_releaser("whisper-worker", self.release_if_idle)
        except Exception:
            pass


def segments_of(reply: dict):
    """A faster-whisper-shaped (segments, info) pair from a worker reply, so
    code written against WhisperModel.transcribe keeps working."""
    segments = [SimpleNamespace(text=text) for text in reply.get("segments") or []]
    if not segments and reply.get("text"):
        segments = [SimpleNamespace(text=reply["text"])]
    info = SimpleNamespace(language=reply.get("language"), duration=reply.get("duration"))
    return segments, info
