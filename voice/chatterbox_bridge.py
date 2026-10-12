"""
The script that runs INSIDE the dedicated Chatterbox environment.

This file is executed by that environment's Python, not by ZEUS's. It is the
only code in the project that imports Chatterbox, and it deliberately imports
nothing from ZEUS - it has to run in an interpreter where `core`, `ui` and the
rest of the project are not importable at all.

Why a separate process
----------------------
chatterbox-tts pins `torch==2.6.0`, `transformers==5.2.0`, `diffusers==0.29.0`
and `gradio==6.8.0`. Those are hard equality pins, and installing them into
ZEUS's own environment would drag half its dependency tree to whatever those
versions require. The subprocess boundary buys complete isolation: nothing
Chatterbox pins can disturb Ollama, PySide6 or the RAG stack. What it must
NOT cost is a model load per sentence - a fresh process re-imports torch and
re-reads the weights every time (measured 2026-09-25: 43 s one-shot vs 22 s
warm for the same sentence on the CPU). So ZEUS keeps one `--serve` process
loaded while you talk (voice/chatterbox_server.py) and releases it when idle.

    ZEUS  ->  ChatterboxProvider  ->  [ this file, in the voice venv ]  ->  WAV

Why Chatterbox rather than F5-TTS
---------------------------------
F5-TTS declares `torchcodec` as a dependency, which loads FFmpeg shared
libraries at import time. Windows ships no FFmpeg, so the install completed and
then failed at the first sentence with a missing DLL three frames from its
cause. Chatterbox pins torch 2.6, which is below the release where torchaudio
delegates decoding to torchcodec, so that entire class of failure does not
exist here.

Protocol
--------
One JSON object in on stdin, one JSON object out on stdout. Everything else -
torch's warnings, model loading chatter, progress bars - goes to stderr, so
stdout stays parseable.

    in : {"text": "...", "output": "...wav", "device": "auto",
          "reference": "...wav", "exaggeration": 0.5, "cfg_weight": 0.5}
    out: {"ok": true, "path": "...", "seconds": 3.4, "device": "cpu",
          "synthesis_seconds": 12.1}
    or : {"ok": false, "error": "...", "detail": "...", "fix": "..."}

With --serve the same objects travel one per line, and a request of
{"op": "load", "model_dir": "...", "device": "auto", "warm": true} loads the
model (and speaks one word into nothing) before the first real sentence.
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
from pathlib import Path

# Everything a library prints on import must go to stderr. torch and
# transformers both write banners and progress bars, and a single stray
# character on stdout makes the reply unparseable on the ZEUS side.
_REAL_STDOUT = sys.stdout
sys.stdout = sys.stderr

# A native crash in torch must END this process with its exit code. On
# 2026-09-24 torch's c10.dll read a NULL pointer here under memory pressure and
# Windows showed the modal "python.exe - Application Error ... Click on OK"
# box; the dying process then sat waiting for a click while ZEUS waited for
# its reply. SetErrorMode suppresses that box; faulthandler writes the Python
# stack of the crash to stderr, which the provider logs as evidence.
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

# No runtime downloads. These are set before any ML library is imported,
# because huggingface_hub decides its offline behaviour at import time.
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("GRADIO_ANALYTICS_ENABLED", "False")

_MODEL = None
_MODEL_DEVICE = ""
# The voice reference the model's conditionals were last prepared from.
# generate(audio_prompt_path=...) re-reads and re-embeds the reference WAV on
# EVERY sentence; preparing it once and reusing it gives the identical voice.
_CONDS_REFERENCE = ""
_DEFAULT_CONDS = None


class ModelMissing(Exception):
    """The checkpoint directory is absent. Raised, not exit()ed, so a --serve
    process survives it and says so for every request."""


def reply(payload: dict) -> None:
    _REAL_STDOUT.write(json.dumps(payload))
    _REAL_STDOUT.flush()


def fail(error: str, detail: str = "", fix: str = "") -> None:
    reply({"ok": False, "error": error, "detail": detail, "fix": fix})
    sys.exit(1)


def pick_device(requested: str) -> str:
    """Choose a device, and never claim an accelerator that is not there."""
    try:
        import torch
    except ImportError:
        return "cpu"
    requested = (requested or "auto").lower()
    if requested not in ("auto", ""):
        return requested
    if torch.cuda.is_available():
        return "cuda"
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def describe_environment() -> dict:
    """What this environment actually is. Used by --probe and the installer."""
    info = {
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "torch": None, "torch_cuda": None, "cuda_available": False,
        "gpu_name": None, "vram_gb": None, "chatterbox": False, "device": "cpu",
    }
    try:
        import torch
        info["torch"] = torch.__version__
        info["torch_cuda"] = getattr(torch.version, "cuda", None)
        info["cuda_available"] = bool(torch.cuda.is_available())
        if info["cuda_available"]:
            info["gpu_name"] = torch.cuda.get_device_name(0)
            info["vram_gb"] = round(
                torch.cuda.get_device_properties(0).total_memory / (1024 ** 3), 1)
    except Exception as error:
        info["torch_error"] = str(error)
    try:
        import chatterbox                                   # noqa: F401
        info["chatterbox"] = True
    except Exception as error:
        info["chatterbox_error"] = str(error)
    info["device"] = pick_device("auto")
    return info


def load(model_dir: Path, device: str):
    """Load Chatterbox once and keep it resident for the life of this process.

    `from_local` rather than `from_pretrained`: the latter reaches Hugging Face
    on a cache miss, which would break the offline guarantee at exactly the
    moment somebody is relying on it.
    """
    global _MODEL, _MODEL_DEVICE
    if _MODEL is not None:
        return _MODEL

    from chatterbox.tts import ChatterboxTTS

    if not model_dir.is_dir():
        raise ModelMissing(f"No Chatterbox model directory at {model_dir}")

    try:
        _MODEL = ChatterboxTTS.from_local(str(model_dir), device)
    except TypeError:
        # Older releases take the directory as a keyword.
        _MODEL = ChatterboxTTS.from_local(ckpt_dir=str(model_dir), device=device)
    _MODEL_DEVICE = device
    global _DEFAULT_CONDS
    _DEFAULT_CONDS = getattr(_MODEL, "conds", None)
    return _MODEL


def use_voice(model, reference: str, exaggeration) -> None:
    """Point the model at the voice for the next sentence, preparing the
    reference only when it differs from the one already prepared."""
    global _CONDS_REFERENCE
    if reference:
        if reference != _CONDS_REFERENCE:
            model.prepare_conditionals(reference, exaggeration=float(
                0.5 if exaggeration is None else exaggeration))
            _CONDS_REFERENCE = reference
    elif _CONDS_REFERENCE:
        if _DEFAULT_CONDS is not None:
            model.conds = _DEFAULT_CONDS           # back to the built-in speaker
        _CONDS_REFERENCE = ""


def save_wav(path: Path, wav, sample_rate: int) -> None:
    """16-bit PCM WAV via soundfile.

    torchaudio.save wrote 32-bit float WAV, which the standard `wave` module
    cannot read (so "seconds" was always None), and from torchaudio 2.9 on
    save() goes through torchcodec/FFmpeg - the very dependency this bridge
    exists to avoid. torchaudio stays as the fallback."""
    try:
        import soundfile as sf
        data = wav.detach().cpu().numpy()
        if data.ndim == 2:
            data = data.T if data.shape[0] < data.shape[1] else data
            if data.shape[1] == 1:
                data = data[:, 0]
        sf.write(str(path), data, int(sample_rate), subtype="PCM_16")
    except ImportError:
        import torchaudio
        torchaudio.save(str(path), wav, sample_rate)


def _release_gpu_cache(error: Exception) -> None:
    """After a CUDA out-of-memory error, hand the cached blocks back so the
    retry (after ZEUS unloads its language models) has room."""
    if "out of memory" not in str(error).lower():
        return
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


def warm_up(request: dict) -> dict:
    """Load the model now, and optionally say one short word into nothing so
    the GPU kernels are compiled before the first real sentence."""
    model_dir = Path(request.get("model_dir") or ".")
    device = pick_device(request.get("device", "auto"))
    started = time.time()
    try:
        model = load(model_dir, device)
        loaded = time.time()
        reference = request.get("reference") or ""
        if reference and not Path(reference).is_file():
            reference = ""
        use_voice(model, reference, request.get("exaggeration"))
        if request.get("warm"):
            model.generate("Ready.")
    except ModelMissing as error:
        return {"ok": False, "error": "model_missing", "detail": str(error),
                "fix": "Run: SETUP_VOICE.bat"}
    except Exception as error:
        _release_gpu_cache(error)
        return {"ok": False, "error": "load_failed",
                "detail": f"{type(error).__name__}: {error}",
                "traceback": traceback.format_exc()[-1500:]}
    return {"ok": True, "device": _MODEL_DEVICE or device,
            "load_seconds": round(loaded - started, 2),
            "warm_seconds": round(time.time() - loaded, 2)}


def synthesise(request: dict) -> dict:
    text = (request.get("text") or "").strip()
    if not text:
        return {"ok": False, "error": "empty_text",
                "detail": "There was nothing to say."}

    model_dir = Path(request.get("model_dir") or ".")
    device = pick_device(request.get("device", "auto"))
    output = Path(request["output"])
    output.parent.mkdir(parents=True, exist_ok=True)

    reference = request.get("reference") or ""
    if reference and not Path(reference).is_file():
        reference = ""          # an absent clip is not a reason to go silent

    started = time.time()
    try:
        model = load(model_dir, device)
        options = {}
        use_voice(model, reference, request.get("exaggeration"))
        for name, key in (("exaggeration", "exaggeration"),
                          ("cfg_weight", "cfg_weight"),
                          ("temperature", "temperature")):
            value = request.get(key)
            if value is not None:
                options[name] = float(value)

        wav = model.generate(text, **options)
        save_wav(output, wav, model.sr)
    except ModelMissing as error:
        return {"ok": False, "error": "model_missing", "detail": str(error),
                "fix": "Run: SETUP_VOICE.bat"}
    except Exception as error:
        _release_gpu_cache(error)
        return {"ok": False, "error": "synthesis_failed",
                "detail": f"{type(error).__name__}: {error}",
                "traceback": traceback.format_exc()[-1500:]}

    if not output.is_file() or output.stat().st_size == 0:
        return {"ok": False, "error": "no_audio",
                "detail": "Chatterbox reported success but wrote no audio."}

    seconds = None
    try:
        import wave
        with wave.open(str(output), "rb") as handle:
            seconds = round(handle.getnframes() / float(handle.getframerate()), 2)
    except Exception:
        pass

    return {"ok": True, "path": str(output), "seconds": seconds,
            "device": _MODEL_DEVICE or device, "bytes": output.stat().st_size,
            "synthesis_seconds": round(time.time() - started, 2)}


def main() -> int:
    if "--probe" in sys.argv:
        reply({"ok": True, "environment": describe_environment()})
        return 0

    if "--serve" in sys.argv:
        # One request per line, model stays loaded. This is what makes the
        # second and later utterances fast.
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
                if request.get("op") == "load":
                    reply(warm_up(request))
                else:
                    reply(synthesise(request))
                _REAL_STDOUT.write("\n")
                _REAL_STDOUT.flush()
            except Exception as error:
                reply({"ok": False, "error": "bridge_error", "detail": str(error)})
                _REAL_STDOUT.write("\n")
                _REAL_STDOUT.flush()
        return 0

    try:
        request = json.loads(sys.stdin.read() or "{}")
    except ValueError as error:
        fail("bad_request", f"Could not parse the request: {error}")
        return 1
    reply(synthesise(request))
    return 0


if __name__ == "__main__":
    sys.exit(main())
