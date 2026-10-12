"""
ZEUS - install everything, in one run. Hardware-aware.

    STEP 1 : INSTALL / INSTALL.bat          install all of it
    INSTALL.bat --check                    report what is present, change nothing
    INSTALL.bat --repair                   fix whatever is broken
    INSTALL.bat --skip-voice               everything except Chatterbox (3.2 GB)
    INSTALL.bat --voice-only               only Chatterbox
    INSTALL.bat --skip-models              do not pull Ollama models

Stages, in dependency order:

    1. ZEUS packages       the Python packages the application needs
    2. This machine         CPU / RAM / GPU / VRAM / disk -> the model plan
    3. Ollama models        pulls ONLY what the plan needs and is missing,
                            verifies each one with a real call, pins digests
    4. Whisper              listening - model sized for this machine, device
                            proven with a real transcription (GPU or CPU)
    5. Face login           InsightFace models (buffalo_l + liveness), each
                            file checked against its published SHA-256
    6. Chatterbox           speaking (optional; ~3.2 GB)
    7. Readiness            database, firewall scripts, knowledge pack, the
                            offline self-test, and logs/AGNI_READINESS.md

Nothing here is reimplemented. Each stage calls the script that already owns
it, so there is exactly one place where any of this logic lives:

    stage 4  ->  scripts/setup_models.py --whisper-only, scripts/verify_whisper.py
    stage 5  ->  scripts/setup_voice_engine.py
    stage 6  ->  scripts/install_knowledge_pack.py, scripts/offline_test.py

Resumable: every stage checks whether it is already done before doing
anything; models that are already pulled are never downloaded again.

Independent: a stage that fails does not stop the ones after it. The final
report says exactly what works. Nothing reports READY without being checked.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# One log per run. The single logs/install.log used to be rewritten by every
# run - the run behind the 2026-09-24 screenshots was lost that way, replaced
# by an interrupted --repair run eleven minutes later.
LOG_PATH = ROOT / "logs" / f"install_{time.strftime('%Y%m%d_%H%M%S')}.log"
READINESS_PATH = ROOT / "logs" / "AGNI_READINESS.md"
OLLAMA = "http://127.0.0.1:11434"

# The packages the application cannot start without, and the import name each
# one actually provides. `pip install PySide6` gives you `PySide6`, but
# `pip install faster-whisper` gives you `faster_whisper`, and checking the
# wrong name means reinstalling something that was already there.
CORE_IMPORTS = [
    ("PySide6", "the application window"),
    ("requests", "talking to Ollama"),
    ("psutil", "system monitoring"),
]

_log: list = []


class Say:
    GREEN, RED, AMBER, DIM, BOLD, OFF = (
        "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m")

    @staticmethod
    def banner(title: str):
        print("\n" + "=" * 68)
        print(f"  {title}")
        print("=" * 68)

    @classmethod
    def stage(cls, number: str, title: str, detail: str = ""):
        print(f"\n{cls.BOLD}[{number}] {title}{cls.OFF}")
        if detail:
            print(f"      {cls.DIM}{detail}{cls.OFF}")
        log(f"--- {number} {title}")

    @classmethod
    def ok(cls, message: str):
        print(f"  {cls.GREEN}OK{cls.OFF}    {message}")
        log(f"OK    {message}")

    @classmethod
    def warn(cls, message: str):
        print(f"  {cls.AMBER}WARN{cls.OFF}  {message}")
        log(f"WARN  {message}")

    @classmethod
    def fail(cls, message: str, fix: str = ""):
        print(f"  {cls.RED}FAIL{cls.OFF}  {message}")
        if fix:
            print(f"        {cls.DIM}{fix}{cls.OFF}")
        log(f"FAIL  {message} | {fix}")

    @classmethod
    def note(cls, message: str):
        print(f"        {cls.DIM}{message}{cls.OFF}")
        log(f"      {message}")


def log(line: str):
    _log.append(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {line}")


def write_log():
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        LOG_PATH.write_text("\n".join(_log) + "\n", encoding="utf-8")
    except Exception:
        pass


def run(command: list, timeout: int = 3600, stream: bool = True) -> bool:
    """Run one step of the install.

    `stream=True` lets pip and the download scripts print as they go. Long
    downloads with no output look like a hang, and people kill them.
    """
    log(f"RUN  {' '.join(str(part) for part in command)}")
    try:
        if stream:
            return subprocess.run(command, timeout=timeout, cwd=str(ROOT)).returncode == 0
        result = subprocess.run(command, capture_output=True, text=True,
                                timeout=timeout, cwd=str(ROOT))
        if result.returncode != 0:
            log("STDERR " + (result.stderr or "")[-2000:])
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        Say.fail(f"that step ran past {timeout // 60} minutes and was stopped",
                 "run the installer again - finished work is kept")
        return False
    except Exception as error:
        Say.fail(str(error))
        return False


def importable(module: str) -> bool:
    """Can THIS interpreter import it? Asked by running it, because a package
    can be installed for a different Python than the one about to use it."""
    try:
        return subprocess.run([sys.executable, "-c", f"import {module}"],
                              capture_output=True, timeout=120).returncode == 0
    except Exception:
        return False


# ================================================================= HTTP ==
# Setup talks to the local Ollama daemon with the standard library only. It
# must never depend on a package it is in the middle of installing: when pip
# could not install requirements.txt on a new Python, `import requests` here
# crashed the whole setup at stage 3.

def _http_json(url: str, body=None, timeout: float = 10.0):
    """(HTTP status, parsed JSON or None). Never uses a proxy - this is only
    ever called for the loopback Ollama daemon."""
    import urllib.error
    import urllib.request
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        url, data=data, method="POST" if data is not None else "GET",
        headers={"Content-Type": "application/json"} if data is not None else {})
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
            return response.status, (json.loads(raw) if raw.strip() else None)
    except urllib.error.HTTPError as error:
        try:
            raw = error.read().decode("utf-8", "replace")
            return error.code, (json.loads(raw) if raw.strip() else None)
        except Exception:
            return error.code, None


def _ollama_models(timeout: float = 8.0) -> list:
    """Names of the models the local daemon has. Raises if it is not running."""
    _status, data = _http_json(f"{OLLAMA}/api/tags", timeout=timeout)
    return [m.get("name", "") for m in (data or {}).get("models", [])]


def _requirement_lines(path: Path) -> list:
    """requirements.txt as individual pip arguments (markers kept, comments dropped)."""
    lines = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split(" #", 1)[0].split("\t#", 1)[0].strip()
        if line and not line.startswith(("#", "-")):
            lines.append(line)
    return lines


# ============================================ 1. the application's packages ==

def stage_dependencies(repair: bool, check_only: bool = False) -> dict:
    Say.stage("1/7", "ZEUS packages",
              "the libraries the application itself needs")
    state = {"name": "ZEUS packages", "ok": False, "detail": ""}

    missing = [(module, why) for module, why in CORE_IMPORTS
               if not importable(module)]
    if not missing and not repair:
        Say.ok("already installed")
        state.update(ok=True, detail="already installed")
        return state

    for module, why in missing:
        Say.note(f"missing: {module} ({why})")

    if check_only:
        # --check reports. It does not install. A status command with side
        # effects is one nobody can trust enough to run.
        state["detail"] = f"missing {', '.join(m for m, _ in missing)}"
        return state

    requirements = ROOT / "config" / "requirements.txt"
    if not requirements.is_file():
        requirements = ROOT / "requirements.txt"
    if not requirements.is_file():
        Say.fail("requirements.txt is not found in config/ or root folder",
                 "you may have unzipped only part of the package")
        state["detail"] = "requirements.txt missing"
        return state

    Say.note("installing from requirements.txt (a few minutes)")
    run([sys.executable, "-m", "pip", "install", "--upgrade", "pip", "-q"],
        timeout=600, stream=False)
    installed = run([sys.executable, "-m", "pip", "install", "-r",
                     str(requirements)], timeout=3600)
    if not installed:
        # pip installs NOTHING when a single requirement cannot be satisfied
        # (one package without a wheel for this Python is enough). Install the
        # rest one at a time so one gap cannot take the whole application down.
        Say.warn("pip could not install the whole list at once - installing each package on its own")
        failed = []
        for line in _requirement_lines(requirements):
            if not run([sys.executable, "-m", "pip", "install", line],
                       timeout=1800, stream=False):
                failed.append(line)
        for line in failed:
            Say.warn(f"could not install: {line}")
        if failed:
            state["failed_packages"] = failed
    # Packages installed by a child pip process are not seen by this running
    # interpreter until its import caches are refreshed.
    importlib.invalidate_caches()

    # pip's exit code is not the question - whether the packages IMPORT is.
    # pip can report success while a wheel silently went to a different
    # interpreter, and it can report failure over one optional package while
    # everything ZEUS actually needs installed fine.
    still_missing = [module for module, _ in CORE_IMPORTS if not importable(module)]
    if still_missing:
        Say.fail(f"these still will not import: {', '.join(still_missing)}",
                 "the pip output above says why - a wheel that will not build "
                 "for your Python version is the usual cause")
        state["detail"] = f"cannot import {', '.join(still_missing)}"
        return state

    if not installed:
        Say.warn("pip reported an error, but everything ZEUS needs imports")
        Say.note("an optional package failed; the application will still run")

    Say.ok("all required packages import")
    state.update(ok=True, detail="installed")
    return state


# ====================================================== 2. this machine ==

def stage_hardware() -> dict:
    """What this machine is, and the model plan that follows from it. Reads
    only; nothing is installed here."""
    Say.stage("2/7", "This machine", "CPU, RAM, GPU, VRAM, disk -> which models fit")
    state = {"name": "Hardware & model plan", "ok": True, "detail": "", "plan": None}

    version = sys.version_info
    if version[:2] not in ((3, 11), (3, 12)):
        Say.warn(f"Python {version.major}.{version.minor} - ZEUS is tested on 3.11 and 3.12")
        Say.note("PySide6, onnxruntime and faster-whisper wheels are most reliable there")

    if len(str(ROOT)) > 60:
        Say.warn(f"the install path is long ({len(str(ROOT))} characters): {ROOT}")
        Say.note("Windows' 260-character limit breaks some package installs deep in")
        Say.note("site-packages. Extracting ZEUS to a short folder such as C:\\ZEUS avoids it.")

    try:
        from core.hardware import model_plan, profile
        hw = profile(force=True)
        plan = model_plan(hw)
    except Exception as error:
        Say.warn(f"could not read the hardware ({error}) - using the CPU plan")
        state.update(detail="hardware not readable")
        return state

    gpu = ", ".join(f"{g.name} ({g.vram_total_gb:.0f} GB)" for g in hw.gpus) or "none (CPU inference)"
    Say.ok(f"CPU      {hw.cpu}  ({hw.cores} cores / {hw.threads} threads)")
    Say.ok(f"RAM      {hw.ram_total_gb:.1f} GB")
    Say.ok(f"GPU      {gpu}")
    Say.ok(f"Disk     {hw.disk_free_gb:.0f} GB free")
    Say.ok(f"Tier     {plan['tier']}")
    for slot, model in plan["slots"].items():
        Say.note(f"  {slot:<16} {model}")
    whisper = plan.get("whisper", {})
    Say.note(f"  {'speech input':<16} Whisper {whisper.get('model')} on {whisper.get('device')}")
    Say.note(f"required download: about {plan['download_gb']} GB of models")

    if hw.disk_free_gb and hw.disk_free_gb < plan["download_gb"] + 8:
        Say.warn(f"only {hw.disk_free_gb:.0f} GB free - models need about "
                 f"{plan['download_gb'] + 8:.0f} GB with room to work")
        state["ok"] = False
    state.update(detail=f"{plan['tier']}; {hw.threads} threads, "
                        f"{hw.ram_total_gb:.0f} GB RAM, GPU: {gpu}", plan=plan)
    return state


# ============================================================ 3. Ollama ==

_SLOT_REASON = {"fast": "quick replies", "classify": "request classification",
                "default": "conversation", "reasoning": "reasoning / findings",
                "json_structured": "structured output", "code": "the coding agent",
                "vision": "images and scanned pages", "embedding": "document search (RAG)"}


def _model_present(name: str, models: list) -> bool:
    """The same rule the model router uses, so a model the router will happily
    run (e.g. qwen2.5-coder:7b-instruct-q4_K_M for qwen2.5-coder:7b) is never
    downloaded a second time."""
    try:
        from core.ollama_client import OllamaClient
        return any(OllamaClient._model_matches(model, name) for model in models)
    except Exception:
        pass
    for model in models:
        if model == name or model == f"{name}:latest":
            return True
        if ":" not in name and model.split(":")[0] == name:
            return True
    return False


def _plan() -> dict:
    try:
        from core.hardware import model_plan
        return model_plan()
    except Exception:
        return {"slots": {"default": "qwen3:4b", "embedding": "bge-m3"},
                "required": ["bge-m3", "qwen3:4b"], "optional": []}


_model_reports: list = []


def _verify_model(name: str) -> tuple:
    """A real, TYPED test of one model (agni_ops/models.py), then UNLOAD it.

    What the old check got wrong on 2026-09-24, and why this replaced it:
      * bge-m3 was reported "NOT WORKING - 0-dim vector": Ollama had answered
        HTTP 500 (its runner could not allocate memory) and the embedding was
        read out of the error body. Now an embedding must be a valid vector
        that ranks a known document first; an HTTP error is reported as one.
      * qwen3-vl:8b was reported "NOT WORKING": num_predict=24 was spent
        entirely on reasoning, so the answer was empty - and it was never
        shown an image. Now a vision model must name the colour of a
        generated picture.
      * every verified model stayed loaded for five minutes; seven of them
        exhausted the machine's memory before Whisper even started. Each
        model is now unloaded after its test."""
    try:
        from agni_ops.models import test_model
    except Exception as error:
        return _verify_model_minimal(name, error)
    report = test_model(name)
    _model_reports.append(report)
    ok = report.status in ("PASS", "WARN")
    detail = report.function_detail if ok else (report.cause or report.error or report.status)
    return ok, f"{(report.type or 'model').lower()}: {detail}"


def _verify_model_minimal(name: str, why) -> tuple:
    """Standard-library fallback when agni_ops cannot be imported: the
    HTTP status is checked, the token budget allows for reasoning, and the
    model is unloaded (keep_alive 0) when the call returns."""
    log(f"typed model test unavailable ({why}); using the minimal check")
    try:
        started = time.time()
        if "bge" in name or "embed" in name:
            status, reply = _http_json(f"{OLLAMA}/api/embed", {"model": name, "keep_alive": 0,
                                                              "input": "pump discharge pressure"},
                                       timeout=300)
            if status != 200:
                return False, f"HTTP {status}: {(reply or {}).get('error', 'no detail')}"[:200]
            vectors = (reply or {}).get("embeddings") or []
            dims = len(vectors[0]) if vectors and vectors[0] else 0
            return dims > 0, f"{dims}-dim vector"
        body = {"model": name, "prompt": "Reply with the single word OK.", "stream": False,
                "options": {"num_predict": 768}, "keep_alive": 0}
        status, reply = _http_json(f"{OLLAMA}/api/generate", body, timeout=600)
        if status != 200:
            return False, f"HTTP {status}: {(reply or {}).get('error', 'no detail')}"[:200]
        text = ((reply or {}).get("response") or "").strip()
        return bool(text), f"answered in {time.time() - started:.1f} s"
    except Exception as error:
        return False, str(error)[:160]


def _start_ollama() -> bool:
    """Start the local daemon if its CLI is installed. Starting is not
    installing: without Ollama on this machine, nothing is started."""
    exe = shutil.which("ollama")
    if not exe:
        return False
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000008   # DETACHED_PROCESS
    env = dict(os.environ, OLLAMA_HOST="127.0.0.1:11434")
    try:
        subprocess.Popen([exe, "serve"], env=env, creationflags=flags,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        return False
    for _ in range(30):
        time.sleep(1)
        try:
            _ollama_models(timeout=2)
            return True
        except Exception:
            continue
    return False


def stage_ollama(pull: bool = False, repair: bool = False) -> dict:
    """Check the local AI runtime and the models this machine's plan needs.

    Ollama itself is never installed from here - it is a system application
    with its own installer. With pull=True, missing models are fetched with
    `ollama pull`, every required model is verified with a real call, and the
    digests are pinned into the trusted-model registry."""
    Say.stage("3/7", "Ollama and the AI models",
              "checked, not installed - Ollama has its own installer")
    state = {"name": "Ollama", "ok": False, "detail": ""}

    try:
        models = _ollama_models()
    except Exception:
        models = None
        if pull and _start_ollama():
            Say.ok("started the local Ollama service")
            try:
                models = _ollama_models()
            except Exception:
                models = None
    if models is None:
        Say.warn("Ollama is not running")
        Say.note("ZEUS needs it to think. Install it from https://ollama.com (Windows")
        Say.note("installer), then run this setup again - it starts the service and")
        Say.note("pulls the models itself. Manually, the commands are:")
        Say.note("    ollama serve")
        for name in _plan().get("required", ["qwen3:4b", "bge-m3"]):
            Say.note(f"    ollama pull {name}")
        state["detail"] = "not running"
        return state

    Say.ok(f"Ollama is running with {len(models)} model(s)")
    plan = _plan()
    reasons = {}
    for slot, name in plan.get("slots", {}).items():
        reasons.setdefault(name, _SLOT_REASON.get(slot, slot))
    wanted = {name: reasons.get(name, "") for name in plan.get("required", [])}
    # bge-m3 is singled out because RAG silently produces nothing without an
    # embedding model, and that failure is hard to diagnose from the symptom.
    wanted.setdefault("bge-m3", "document search (RAG)")

    absent = [name for name in wanted if not _model_present(name, models)]
    present = [name for name in wanted if name not in absent]
    for name in present:
        Say.ok(f"{name:<20} already pulled - not downloaded again")
    for name in absent:
        Say.warn(f"no {name} model - {wanted[name]} will not work until it is pulled")
        Say.note(f"    ollama pull {name}")

    if not pull:
        state.update(ok=not absent,
                     detail=f"{len(models)} models"
                            + (f", missing {', '.join(absent)}" if absent else ""))
        return state

    failed_pulls = []
    for name in absent:
        Say.note(f"pulling {name} (resumes if interrupted)")
        if not run(["ollama", "pull", name], timeout=4 * 3600):
            failed_pulls.append(name)
            Say.fail(f"{name} did not download", "run the setup again - Ollama resumes")

    try:                                   # the names as installed, after the pulls
        models = _ollama_models()
    except Exception:
        pass
    # Start from an empty runtime: models another program (or the running
    # ZEUS) left resident would otherwise be counted against each test.
    try:
        from core.model_lifecycle import unload_all
        dropped = unload_all()
        if dropped:
            Say.note(f"unloaded {', '.join(dropped)} before testing, to free memory")
    except Exception:
        pass
    verified, broken = [], []
    for name in wanted:
        if name in failed_pulls:
            continue
        installed = next((m for m in models if _model_present(name, [m])), name)
        ok, detail = _verify_model(installed)
        (verified if ok else broken).append(name)
        (Say.ok if ok else Say.warn)(f"{name:<20} {'verified' if ok else 'NOT VERIFIED'} - {detail}")
    if _model_reports:
        try:
            from agni_ops.models import save_health
            save_health(_model_reports)
        except Exception:
            pass

    try:
        from security.integrity import model_integrity
        registry = model_integrity.pin(by="setup")
        Say.ok(f"pinned {len(registry.get('models', {}))} model digest(s) in the trusted registry")
    except Exception as error:
        Say.warn(f"could not pin model digests: {error}")

    missing = failed_pulls + broken
    state.update(ok=not missing,
                 detail=f"{len(verified)}/{len(wanted)} required models verified"
                        + (f"; not ready: {', '.join(missing)}" if missing else ""))
    return state


# =========================================================== 4. listening ==

def _whisper_size() -> str:
    try:
        return _plan().get("whisper", {}).get("model") or "base"
    except Exception:
        return "base"


def verify_whisper_device(size: str) -> str:
    """Prove the device with a real transcription of known speech; returns
    what worked ("cuda/float16", "cpu/int8") or "".

    scripts/verify_whisper.py runs the staged test (agni_ops/voice.py): each
    stage in its own worker process, the CPU path before the GPU, so a native
    crash is one stage's recorded result instead of the end of setup. It
    rewrites models/whisper/agni_verified.json either way - a failed run no
    longer leaves an earlier "cuda OK" record standing."""
    try:
        result = subprocess.run([sys.executable, str(ROOT / "scripts" / "verify_whisper.py"),
                                 "--model", size], capture_output=True, text=True,
                                timeout=1800, cwd=str(ROOT))
        for line in (result.stdout or "").splitlines():
            Say.note(line)
        record = ROOT / "models" / "whisper" / "agni_verified.json"
        if result.returncode == 0 and record.is_file():
            data = json.loads(record.read_text(encoding="utf-8"))
            if data.get("ok"):
                return f"{data.get('device')}/{data.get('compute_type')}"
        if result.returncode != 0 and result.stderr:
            log("verify_whisper stderr: " + result.stderr[-1500:])
    except Exception as error:
        Say.note(f"verification did not run: {error}")
    return ""


def _whisper_record_is_current(target: Path) -> bool:
    """A verification record written by the staged test that succeeded."""
    try:
        data = json.loads((target / "agni_verified.json").read_text(encoding="utf-8"))
        return data.get("verified_by") == "agni_ops.voice" and bool(data.get("ok"))
    except Exception:
        return False


def _whisper_fallback(check_only: bool) -> None:
    """The low-memory recogniser config.ini [voice] stt_fallback_model names
    (models/whisper-small by default): used only when the primary model cannot
    get memory. Downloaded once and SHA-256 verified by setup_models.py, which
    downloads nothing when it is already present and intact. Never fails the
    stage - the primary model is what listening needs."""
    try:
        from voice.offline import WHISPER_REQUIRED_FILES, config_str, missing_files, resolve_models_dir
        size = (config_str("stt_fallback_model", "auto") or "auto").strip().lower()
        folder = resolve_models_dir() / f"whisper-{size}"
        present = not missing_files(folder, WHISPER_REQUIRED_FILES)
    except Exception:
        return
    if size in ("auto", "none", "off", "false", "0", "no", ""):
        return
    if present:
        Say.ok(f"low-memory fallback whisper-{size}: already downloaded - not downloaded again")
        return
    if check_only:
        Say.note(f"low-memory fallback whisper-{size}: not installed (STEP 1 : INSTALL installs it)")
        return
    Say.note(f"low-memory fallback: whisper-{size} (used only when the main model cannot get memory)")
    if not run([sys.executable, str(ROOT / "scripts" / "setup_models.py"), "--whisper-fallback", size],
               timeout=3600):
        Say.warn(f"the whisper-{size} fallback did not install - under memory pressure listening waits "
                 "for the main model")


def stage_whisper(repair: bool, check_only: bool = False) -> dict:
    size = _whisper_size()
    state = _stage_whisper(size, repair, check_only)
    if state.get("ok"):
        _whisper_fallback(check_only)
    return state


def _stage_whisper(size: str, repair: bool, check_only: bool = False) -> dict:
    Say.stage("4/7", "Whisper - listening", f"model '{size}', sized for this machine")
    state = {"name": "Listening (Whisper)", "ok": False, "detail": ""}

    try:
        from voice.offline import (WHISPER_REQUIRED_FILES, missing_files,
                                   resolve_whisper_dir)
        target = resolve_whisper_dir()
        absent = missing_files(target, WHISPER_REQUIRED_FILES)
    except Exception as error:
        Say.fail(f"could not check the Whisper model: {error}")
        state["detail"] = str(error)
        return state

    if not absent and not repair:
        Say.ok(f"already downloaded ({target.name}) - not downloaded again")
        state.update(ok=True, detail="already downloaded")
        # Downloaded is not verified: without a current record of a real
        # transcription, prove it now.
        if not check_only and not _whisper_record_is_current(target) and importable("faster_whisper"):
            device = verify_whisper_device(size)
            if device:
                Say.ok(f"verified by a real transcription on {device}")
                state["detail"] = f"already downloaded; verified on {device}"
            else:
                Say.warn("downloaded, but no backend transcribed the test speech - see the stages above")
                state.update(ok=False, detail="downloaded; transcription NOT verified")
        return state

    if check_only:
        Say.warn(f"not downloaded - missing: {', '.join(absent)}")
        Say.note("run STEP 1 : INSTALL to download it")
        state["detail"] = "not downloaded"
        return state

    if not importable("faster_whisper"):
        Say.note("installing faster-whisper")
        if not run([sys.executable, "-m", "pip", "install", "faster-whisper>=1.1.0"],
                   timeout=1800, stream=False):
            Say.fail("faster-whisper would not install",
                     "listening will be unavailable; speaking is unaffected")
            state["detail"] = "faster-whisper would not install"
            return state
    if _plan().get("whisper", {}).get("device") == "cuda":
        # CTranslate2's Windows wheels do not bundle cuBLAS / cuDNN.
        Say.note("installing the CUDA runtime libraries faster-whisper uses on the GPU")
        if not run([sys.executable, "-m", "pip", "install", "nvidia-cublas-cu12",
                    "nvidia-cudnn-cu12>=9,<10"], timeout=3600, stream=False):
            Say.warn("CUDA libraries did not install - listening will use the CPU")

    Say.note("downloading the speech-recognition model")
    if not run([sys.executable, str(ROOT / "scripts" / "setup_models.py"),
                "--whisper-only", "--whisper-size", size], timeout=3600):
        Say.fail("the Whisper download did not finish",
                 "run the installer again - it resumes")
        state["detail"] = "download failed"
        return state

    absent = missing_files(resolve_whisper_dir(), WHISPER_REQUIRED_FILES)
    if absent:
        Say.fail(f"still missing: {', '.join(absent)}")
        state["detail"] = f"missing {', '.join(absent)}"
        return state

    device = verify_whisper_device(size)
    if device:
        Say.ok(f"Whisper is ready - ZEUS can listen (runs on {device})")
        state.update(ok=True, detail=f"downloaded; runs on {device}")
    else:
        Say.warn("Whisper downloaded, but the test transcription did not run")
        state.update(ok=False, detail="downloaded; test transcription failed")
    return state


# ========================================================== 5. face login ==
# InsightFace models from the official release pages. Every file is checked
# against the SHA-256 GitHub publishes for it (copied here), so a corrupted or
# substituted download is refused, not installed.

FACE_ROOT = ROOT / "models" / "insightface"
FACE_PACKS = {
    "buffalo_l": ("https://github.com/deepinsight/insightface/releases/download/model-zoo/buffalo_l.zip",
                  "80ffe37d8a5940d59a7384c201a2a38d4741f2f3c51eef46ebb28218a7b0ca2f", 288621354),
    "buffalo_sc": ("https://github.com/deepinsight/insightface/releases/download/model-zoo/buffalo_sc.zip",
                   "57d31b56b6ffa911c8a73cfc1707c73cab76efe7f13b675a05223bf42de47c72", 14969382),
}
FACE_LIVENESS = ("https://github.com/deepinsight/insightface-model-addons/releases/download/addons/liveness.onnx",
                 "87a9ac1dbb16a61eec212957e5095e62a8769c1e188af9b0198f253302c4afdb", 1484006)


def _sha256_file(path: Path) -> str:
    import hashlib
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download_verified(url: str, target: Path, sha256: str, size: int) -> bool:
    """Download to a .part file, check size and SHA-256, then move into place."""
    import urllib.request
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".part")
    try:
        with urllib.request.urlopen(url, timeout=60) as response, part.open("wb") as out:
            done = 0
            while True:
                chunk = response.read(1 << 20)
                if not chunk:
                    break
                done += len(chunk)
                if done > size:
                    raise ValueError("larger than the published size")
                out.write(chunk)
        if part.stat().st_size != size or _sha256_file(part) != sha256:
            raise ValueError("SHA-256 does not match the published digest")
        os.replace(part, target)
        return True
    except Exception as error:
        Say.fail(f"{target.name}: {error}", "run the setup again")
        try:
            part.unlink()
        except OSError:
            pass
        return False


def _face_pack_name() -> str:
    tier = _plan().get("tier", "")
    return "buffalo_sc" if tier == "cpu_8gb" else "buffalo_l"


def stage_face(check_only: bool = False) -> dict:
    Say.stage("5/7", "Face login (InsightFace)",
              "optional second factor - models checked against published SHA-256")
    state = {"name": "Face login (InsightFace)", "ok": False, "detail": ""}
    pack = _face_pack_name()
    url, sha256, size = FACE_PACKS[pack]
    pack_dir = FACE_ROOT / "models" / pack
    liveness = FACE_ROOT / "addons" / "liveness.onnx"

    if not all(importable(m) for m in ("onnx", "skimage", "onnxruntime")):
        if check_only:
            Say.warn("face libraries (onnx, scikit-image) are not installed")
            state["detail"] = "libraries missing"
            return state
        Say.note("installing onnx, scikit-image, tqdm")
        run([sys.executable, "-m", "pip", "install", "onnx>=1.13", "scikit-image>=0.21",
             "tqdm>=4.60"], timeout=1800, stream=False)
        importlib.invalidate_caches()

    have_pack = pack_dir.is_dir() and any(pack_dir.glob("*.onnx"))
    have_live = liveness.is_file() and _sha256_file(liveness) == FACE_LIVENESS[1]
    if have_pack:
        Say.ok(f"{pack} already downloaded - not downloaded again")
    if have_live:
        Say.ok("liveness model present, SHA-256 verified")
    if check_only:
        state.update(ok=have_pack and have_live,
                     detail="ready" if have_pack and have_live else "models not downloaded")
        return state

    if not have_pack:
        Say.note(f"downloading {pack} ({size / 1e6:.0f} MB)")
        archive = FACE_ROOT / f"{pack}.zip"
        if _download_verified(url, archive, sha256, size):
            import zipfile
            with zipfile.ZipFile(archive) as bundle:
                bundle.extractall(pack_dir)
            archive.unlink()
            have_pack = any(pack_dir.glob("*.onnx"))
            Say.ok(f"{pack} downloaded, SHA-256 matched the published digest")
    if not have_live:
        Say.note("downloading the liveness model (1.5 MB)")
        have_live = _download_verified(FACE_LIVENESS[0], liveness, FACE_LIVENESS[1], FACE_LIVENESS[2])
        if have_live:
            Say.ok("liveness model downloaded, SHA-256 matched")

    if not (have_pack and have_live):
        state["detail"] = "download incomplete - face login stays unavailable"
        return state

    # A real detection on InsightFace's own sample photo, in a fresh process
    # (agni_ops/face.py). The old probe read ANY failure of that process - on
    # 2026-09-24 the machine was out of memory - as "found no face". Now a
    # probe that fails says so, with the reason; "no face" is reserved for a
    # detector that ran and found none.
    faces, failure = 0, ""
    try:
        from agni_ops.face import suite as face_suite
        checks = face_suite(camera=False)
        for check in checks:
            log(f"face {check.status} {check.component}: {check.reason}")
        functional = next((c for c in checks if c.component.startswith("MODEL_FUNCTIONAL")), None)
        broken = next((c for c in checks if c.status == "FAIL"), None)
        if functional is not None and functional.status == "PASS":
            faces = int(re.search(r"detected (\d+)", functional.reason).group(1))
        elif broken is not None:
            failure = f"{broken.component}: {broken.reason}"
    except Exception as error:
        failure = f"the face probe could not run: {type(error).__name__}: {error}"
    record_path = FACE_ROOT / "agni_verified.json"
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except Exception:
        record = {}
    record.update({"pack": pack, "pack_sha256": sha256, "liveness_sha256": FACE_LIVENESS[1],
                   "sources": [url, FACE_LIVENESS[0]], "verified_at": time.time(),
                   "probe_faces_on_sample": faces, "probe_failure": failure})
    record_path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    if faces > 0:
        Say.ok(f"face engine works - found {faces} faces in the sample photo")
        Say.note("turn face login on with [face] login = second_factor in config.ini,")
        Say.note("then enrol in ZEUS: Security > Face login > Enrol my face")
        state.update(ok=True, detail=f"{pack} + liveness, verified")
    elif failure:
        Say.warn(f"models are in place but the face engine did not run: {failure[:160]}")
        state["detail"] = f"engine not verified - {failure[:120]}"
    else:
        Say.warn("the face engine ran but found no face in the reference photo")
        state["detail"] = "detector ran, found no face in the reference sample"
    return state


# ============================================================ 6. speaking ==

def stage_voice(repair: bool, check_only: bool = False) -> dict:
    Say.stage("6/7", "Chatterbox - speaking (optional)",
              "about 3.2 GB; without it ZEUS speaks with the Windows voice")
    state = {"name": "Speaking (Chatterbox)", "ok": False, "detail": ""}

    installer = ROOT / "scripts" / "setup_voice_engine.py"
    if not installer.is_file():
        Say.fail("scripts/setup_voice_engine.py is missing")
        state["detail"] = "installer missing"
        return state

    arguments = ["--check"] if check_only else (["--repair"] if repair else [])
    ok = run([sys.executable, str(installer)] + arguments, timeout=14400)

    state["ok"] = ok
    state["detail"] = ("verified by a real test synthesis" if ok
                       else "see the voice report above")
    return state


# =========================================================== 6. readiness ==

def stage_readiness(check_only: bool = False) -> dict:
    """Database, security scaffolding, knowledge pack, and the offline
    self-test - the same checks, whose results go into AGNI_READINESS.md."""
    Say.stage("7/7", "Readiness", "database, firewall scripts, knowledge pack, self-test")
    state = {"name": "Readiness self-test", "ok": False, "detail": ""}
    lines = ["# ZEUS readiness report", "",
             f"Generated {time.strftime('%Y-%m-%d %H:%M:%S')} on this machine by the setup script.",
             "Every line below is the result of a check that ran; nothing is assumed.", ""]

    try:
        from memory.database import init_db, list_users
        init_db()
        users = list_users()
        from jarvis import store as jv_store
        with jv_store.connection():
            pass
        from crudex_net import store as net_store
        with net_store.connect():
            pass
        Say.ok(f"databases ready ({len(users)} account(s), ZEUS & Network sync initialized)")
        lines.append(f"- Databases: ready, {len(users)} account(s), ZEUS & Network stores initialized")
    except Exception as error:
        Say.fail(f"database: {error}")
        lines.append(f"- Database: FAILED - {error}")

    if not check_only:
        try:
            from security.firewall import include_ollama_default, write_scripts
            written = write_scripts(ROOT, include_ollama_default(ROOT))
            Say.note(f"    firewall script: {written.get('apply', 'scripts/firewall/apply_agni_firewall.ps1')}")
            # Say what IS in force, read back from Windows - not what a script
            # could do if someone ran it.
            try:
                from agni_ops.firewall import expected_programs, read_rules
                rules = read_rules()
            except Exception:
                rules = []
            if rules:
                covered = {(r.get("Program") or "").lower() for r in rules}
                missing = [Path(p).name for p in expected_programs() if p.lower() not in covered]
                (Say.ok if not missing else Say.warn)(
                    f"OS firewall: {len(rules)} rule(s) applied"
                    + (f"; NOT covered: {', '.join(missing)}" if missing else ""))
                lines.append(f"- OS firewall: {len(rules)} rule(s) applied"
                             + (f"; not covered: {', '.join(missing)}" if missing else ""))
            else:
                Say.warn("OS firewall NOT applied - REQUIRES ADMINISTRATOR")
                lines.append("- OS firewall: NOT applied (requires administrator)")
        except Exception as error:
            Say.warn(f"firewall scripts: {error}")
            lines.append(f"- OS firewall scripts: not written - {error}")

        if run([sys.executable, str(ROOT / "scripts" / "install_knowledge_pack.py")],
               timeout=3600, stream=False):
            Say.ok("knowledge pack indexed into the local RAG store")
            lines.append("- Knowledge pack: indexed")
        else:
            Say.warn("knowledge pack was not indexed (see logs/install.log)")
            lines.append("- Knowledge pack: NOT indexed")

    report = ROOT / "logs" / "offline_test.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    Say.note("running the offline self-test (no network; a few minutes)")
    run([sys.executable, str(ROOT / "scripts" / "offline_test.py"), "--quiet",
         "--json", str(report)], timeout=1800)
    ready = False
    try:
        data = json.loads(report.read_text(encoding="utf-8"))
        checks = data.get("results") or []
        lines.append("")
        lines.append("## Offline self-test")
        for check in checks:
            lines.append(f"- {check.get('check')}: **{check.get('status')}** - {check.get('detail', '')}")
        ready = bool(data.get("offline_ready"))
        if not checks:
            ready = False
    except Exception as error:
        lines.append(f"- Offline self-test: no result ({error})")

    try:
        from core.hardware import model_plan
        plan = model_plan()
        lines += ["", "## Model plan for this machine", f"- Tier: {plan['tier']}"]
        lines += [f"- {slot}: {name}" for slot, name in plan["slots"].items()]
    except Exception:
        pass
    lines += ["", "## Not claimed",
              "- The OS firewall is only active after firewall rules are applied as administrator.",
              "- 'Sovereign' covers ZEUS's own process; other programs on this PC are not controlled by ZEUS."]
    READINESS_PATH.parent.mkdir(parents=True, exist_ok=True)
    READINESS_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    Say.ok(f"readiness report: {READINESS_PATH}")
    state.update(ok=ready, detail="offline self-test passed" if ready else
                 "self-test reported problems - see AGNI_READINESS.md")
    return state


# ============================================================== reporting ==

def final_report(results: list, skipped: list) -> bool:
    Say.banner("ZEUS - INSTALLATION SUMMARY")

    for entry in results:
        mark = f"{Say.GREEN}READY{Say.OFF}" if entry["ok"] else \
               f"{Say.AMBER}NOT READY{Say.OFF}"
        print(f"  {entry['name']:<26} {mark:<22} {entry['detail']}")
    for name in skipped:
        print(f"  {name:<26} {Say.DIM}skipped{Say.OFF}")

    essential = next((e for e in results if e["name"] in ("ZEUS packages", "ZEUS packages")), None)
    runnable = essential is None or essential["ok"]
    attempted_ok = all(entry["ok"] for entry in results)
    everything = attempted_ok and not skipped

    print()
    if everything:
        print(f"  {Say.GREEN}{Say.BOLD}EVERYTHING IS INSTALLED (ONLINE & OFFLINE){Say.OFF}")
        print("\n  Start ZEUS:       STEP 2 : START")
    elif attempted_ok and skipped:
        print(f"  {Say.GREEN}{Say.BOLD}EVERYTHING YOU ASKED FOR IS INSTALLED{Say.OFF}")
        print(f"\n  Skipped on purpose: {', '.join(skipped)}.")
        print("\n  Start ZEUS:       STEP 2 : START")
    elif runnable:
        print(f"  {Say.AMBER}{Say.BOLD}ZEUS IS READY - some extras are not{Say.OFF}")
        print("\n  It runs now. What is missing above only affects that feature;")
        print("  everything else works, and text mode is fully supported.")
        print("\n  Start ZEUS:       STEP 2 : START")
        print("  Repair missing parts:     REPAIR.bat")
    else:
        print(f"  {Say.RED}{Say.BOLD}ZEUS CANNOT START YET{Say.OFF}")
        print("\n  The application's own packages did not install. Read the")
        print("  first section above - it names what failed and why.")

    print(f"\n  Full log: {LOG_PATH}")
    if READINESS_PATH.is_file():
        print(f"  Readiness report: {READINESS_PATH}")
    return everything


# ============================================================== commands ==

def _guarded(stage, name: str, *args, **kwargs) -> dict:
    """Run one stage. An unexpected error in it becomes a FAIL line in the
    summary - it must never end the whole setup with a traceback."""
    try:
        return stage(*args, **kwargs)
    except KeyboardInterrupt:
        raise
    except Exception as error:
        Say.fail(f"{name} stopped with an error: {type(error).__name__}: {error}",
                 "the other stages continue; the full error is in logs/install.log")
        log(traceback.format_exc())
        return {"name": name, "ok": False, "detail": f"error: {type(error).__name__}: {error}"}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Install everything ZEUS needs, in one run.")
    parser.add_argument("--check", action="store_true",
                        help="report what is present; change nothing")
    parser.add_argument("--repair", action="store_true",
                        help="reinstall whatever is missing or broken")
    parser.add_argument("--skip-voice", action="store_true",
                        help="everything except the 3.2 GB Chatterbox voice")
    parser.add_argument("--voice-only", action="store_true",
                        help="only the Chatterbox voice")
    parser.add_argument("--skip-models", action="store_true",
                        help="check the Ollama models but do not pull any")
    arguments = parser.parse_args()

    log(f"install_everything {' '.join(sys.argv[1:]) or '(full install)'}")
    log(f"python {sys.version.split()[0]} at {sys.executable}")

    Say.banner("ZEUS - AUTONOMOUS AI COMMAND & INTELLIGENCE SYSTEM - FULL SETUP")
    if arguments.check:
        print("\n  Reading what is installed. Nothing is changed.")
    else:
        print("\n  This sets up everything ZEUS needs on THIS machine (Online & Offline):")
        print("    1. the application's Python packages")
        print("    2. a look at this machine's CPU, RAM and GPU -> the model plan")
        print("    3. the Ollama models that plan needs (only the missing ones)")
        print("    4. Whisper, so ZEUS can listen")
        print("    5. the face login models (optional second factor)")
        if not arguments.skip_voice:
            print("    6. Chatterbox, so ZEUS can speak (~3.2 GB)")
        print("    7. offline & online databases, knowledge pack, and readiness self-test")
        print("\n  Internet is used to download missing models/packages; afterwards ZEUS runs offline.")
        print("  Stop it any time with Ctrl+C - running it again continues")
        print("  from where it stopped.")

    results, skipped = [], []
    started = time.time()

    try:
        if arguments.voice_only:
            results.append(_guarded(stage_voice, "Speaking (Chatterbox)",
                                    arguments.repair, arguments.check))
        else:
            results.append(_guarded(stage_dependencies, "ZEUS packages",
                                    arguments.repair, arguments.check))
            results.append(_guarded(stage_hardware, "Hardware & model plan"))
            results.append(_guarded(stage_ollama, "Ollama",
                                    pull=not (arguments.check or arguments.skip_models),
                                    repair=arguments.repair))
            results.append(_guarded(stage_whisper, "Listening (Whisper)",
                                    arguments.repair, arguments.check))
            results.append(_guarded(stage_face, "Face login (InsightFace)", arguments.check))
            if arguments.skip_voice:
                skipped.append("Speaking (Chatterbox)")
            else:
                results.append(_guarded(stage_voice, "Speaking (Chatterbox)",
                                        arguments.repair, arguments.check))
            results.append(_guarded(stage_readiness, "Readiness self-test", arguments.check))
    except KeyboardInterrupt:
        print("\n\n  Stopped. Nothing was corrupted - run STEP 1 : INSTALL again")
        print("  to continue from where it left off.")
        write_log()
        return 130

    everything = final_report(results, skipped)
    minutes = int((time.time() - started) / 60)
    if minutes >= 1 and not arguments.check:
        print(f"  Took {minutes} minute(s).")
    write_log()
    return 0 if everything else 1


if __name__ == "__main__":
    sys.exit(main())
