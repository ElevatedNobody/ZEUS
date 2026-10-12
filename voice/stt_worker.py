"""
Whisper worker process - the only place ZEUS loads faster-whisper/CTranslate2
when [voice] stt_isolation = process (the default).

Why a separate process: CTranslate2, cuBLAS, cuDNN and MKL are native code.
When one of them fails - an allocation that returns NULL, a driver fault - it
does not raise a Python exception, it kills the process. In ZEUS's own process
that would be the whole application; here it is one worker, whose exit code
the STT manager (voice/stt_manager.py) records, classifies and replaces.

Protocol: one JSON object per line.
    stdin   {"id": 1, "cmd": "probe"}
            {"id": 2, "cmd": "decode", "path": "a.wav"}
            {"id": 3, "cmd": "load", "model_dir": "...", "device": "cuda",
             "compute_type": "float16", "cpu_threads": 8}
            {"id": 4, "cmd": "transcribe", "path": "a.wav" | "a.npy",
             "language": "en", "options": {...}}
            {"id": 5, "cmd": "ping"}      {"id": 6, "cmd": "quit"}
    stdout  {"id": n, "ok": true, ...}  or  {"id": n, "ok": false,
             "error": "...", "error_type": "..."}

stdout carries ONLY protocol lines: anything a library prints is sent to
stderr, which the manager keeps as crash evidence.

    python -m voice.stt_worker
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _peak_private_mb() -> int:
    try:
        import psutil
        info = psutil.Process().memory_info()
        return int(getattr(info, "peak_pagefile", 0) or getattr(info, "rss", 0)) // (1024 * 1024)
    except Exception:
        return 0


class Worker:
    def __init__(self, out):
        self.out = out
        self.model = None
        self.loaded = {}

    def reply(self, payload: dict):
        self.out.write(json.dumps(payload) + "\n")
        self.out.flush()

    # ---------------------------------------------------------- commands --

    def probe(self, _request):
        import ctranslate2
        import faster_whisper
        info = {"ctranslate2": ctranslate2.__version__,
                "faster_whisper": getattr(faster_whisper, "__version__", ""),
                "python": sys.version.split()[0],
                "cpu_compute_types": sorted(ctranslate2.get_supported_compute_types("cpu"))}
        try:
            info["cuda_devices"] = int(ctranslate2.get_cuda_device_count())
        except Exception as error:
            info["cuda_devices"], info["cuda_error"] = 0, str(error)
        if info["cuda_devices"]:
            try:
                from voice.cuda_dlls import register_cuda_dlls
                register_cuda_dlls()
                info["cuda_compute_types"] = sorted(ctranslate2.get_supported_compute_types("cuda"))
            except Exception as error:
                info["cuda_error"] = str(error)
        try:
            import av
            info["audio_decoder"] = f"PyAV {av.__version__}"
        except Exception as error:
            info["audio_decoder"] = f"unavailable: {error}"
        return info

    def decode(self, request):
        from faster_whisper import decode_audio
        audio = decode_audio(request["path"], sampling_rate=16000)
        rms = float((audio ** 2).mean() ** 0.5) if audio.size else 0.0
        return {"samples": int(audio.shape[0]), "seconds": round(audio.shape[0] / 16000, 2),
                "rms": round(rms, 5)}

    def load(self, request):
        device = request.get("device", "cpu")
        if device == "cuda":
            from voice.cuda_dlls import register_cuda_dlls
            register_cuda_dlls()
        from faster_whisper import WhisperModel
        started = time.time()
        self.model = WhisperModel(request["model_dir"], device=device,
                                  compute_type=request.get("compute_type", "int8"),
                                  local_files_only=True,
                                  cpu_threads=int(request.get("cpu_threads", 0) or 0),
                                  num_workers=1)
        self.loaded = {k: request.get(k) for k in ("model_dir", "device", "compute_type")}
        return {"seconds": round(time.time() - started, 2), "loaded": self.loaded}

    def transcribe(self, request):
        if self.model is None:
            raise RuntimeError("no model loaded - send 'load' first")
        path = request["path"]
        audio = path
        if str(path).lower().endswith(".npy"):
            import numpy as np
            audio = np.load(path).astype("float32").reshape(-1)
        options = dict(request.get("options") or {})
        if request.get("language"):
            options["language"] = request["language"]
        started = time.time()
        try:
            segments, info = self.model.transcribe(audio, **options)
        except TypeError:
            segments, info = self.model.transcribe(audio, beam_size=1)
        pieces = [segment.text for segment in segments]
        return {"text": " ".join(p.strip() for p in pieces).strip(),
                "segments": [p.strip() for p in pieces],
                "language": getattr(info, "language", None),
                "duration": round(float(getattr(info, "duration", 0.0) or 0.0), 2),
                "seconds": round(time.time() - started, 2)}

    def handle(self, request: dict) -> bool:
        command = request.get("cmd", "")
        base = {"id": request.get("id")}
        if command == "quit":
            self.reply({**base, "ok": True})
            return False
        if command == "ping":
            self.reply({**base, "ok": True, "loaded": self.loaded})
            return True
        if command == "crash_test" and os.environ.get("AGNI_STT_ALLOW_CRASH_TEST") == "1":
            # Deliberate native NULL read (a real 0xC0000005, not a Python
            # exception - ctypes would turn one into OSError), used ONLY by
            # TEST_AGNI.bat and the test suite to prove a crash here cannot
            # take ZEUS down.
            import faulthandler
            faulthandler._read_null()
        handler = {"probe": self.probe, "decode": self.decode, "load": self.load,
                   "transcribe": self.transcribe}.get(command)
        if handler is None:
            self.reply({**base, "ok": False, "error": f"unknown command {command!r}",
                        "error_type": "protocol"})
            return True
        try:
            result = handler(request)
            self.reply({**base, "ok": True, **result, "peak_private_mb": _peak_private_mb()})
        except MemoryError as error:
            self.reply({**base, "ok": False, "error": f"MemoryError: {error}",
                        "error_type": "out_of_memory", "peak_private_mb": _peak_private_mb()})
        except Exception as error:
            text = f"{type(error).__name__}: {error}"
            self.reply({**base, "ok": False, "error": text[:600],
                        "error_type": type(error).__name__,
                        "traceback": traceback.format_exc()[-1500:],
                        "peak_private_mb": _peak_private_mb()})
        return True


def harden_crash_behaviour() -> None:
    """A native crash must END this process, visibly, with its exit code.

    Without this, Windows shows the modal "python.exe - Application Error ...
    Click on OK to terminate the program" box (the third screenshot of the
    2026-09-24 report) and the dying process waits for someone to click it -
    so a supervisor sees a hung worker, not a crashed one. faulthandler writes
    the Python stack of the crashing thread to stderr, which the manager
    keeps as the evidence of WHICH call crashed."""
    if os.name == "nt":
        try:
            import ctypes
            # SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX | SEM_NOOPENFILEERRORBOX
            ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002 | 0x8000)
        except Exception:
            pass
    try:
        import faulthandler
        faulthandler.enable(file=sys.stderr, all_threads=True)
    except Exception:
        pass


def main() -> int:
    harden_crash_behaviour()
    # Keep the protocol channel clean: libraries that print go to stderr.
    protocol = sys.stdout
    sys.stdout = sys.stderr
    try:
        from voice.offline import enforce_offline
        enforce_offline()
    except Exception:
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
    worker = Worker(protocol)
    worker.reply({"id": 0, "ok": True, "ready": True, "pid": os.getpid()})
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            worker.reply({"id": None, "ok": False, "error": "not JSON", "error_type": "protocol"})
            continue
        if not worker.handle(request):
            break
    return 0


if __name__ == "__main__":
    sys.exit(main())
