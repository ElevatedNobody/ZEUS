"""
Model lifecycle against the local Ollama daemon: LOAD -> USE -> UNLOAD.

START.bat sets OLLAMA_MAX_LOADED_MODELS=1, but that only reaches an Ollama
that START.bat itself launches. On a machine where the Ollama tray app already
runs the server (the usual case), nothing limited residency: the setup run of
2026-09-24 verified seven models back to back, each stayed loaded for Ollama's
default five minutes, and the machine ran out of commit memory (see
core/sysmem.py). This module enforces the policy from ZEUS's side, whoever
started the daemon:

    resident()                 what /api/ps says is loaded right now
    unload(model)              keep_alive=0, then CONFIRM it left /api/ps
    ensure_room(model)         before a load: evict other models until the
                               commit headroom for this one exists
    keep_alive()               the residency ZEUS requests on every call

Policy (config.ini [models]):
    keep_alive   = 5m     how long a model stays after its last use
    max_resident = 1      models allowed resident at once (0 = no limit)

Standard library only - the installer and the diagnostics use this before
the application's own packages are guaranteed to import.
"""

from __future__ import annotations

import configparser
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_HOST = "http://127.0.0.1:11434"

# Host memory an Ollama model commits relative to its file size on Windows +
# CUDA (mmap disabled). Measured on the RTX 5060 laptop: qwen3-vl:8b, 5.8 GB
# on disk, took 4.4 GB of commit while 5.15 GB of it sat in VRAM.
_COMMIT_FACTOR = 0.85
_COMMIT_OVERHEAD_GB = 0.5


def active_in_this_process() -> bool:
    """Runtime callers (chat, STT, TTS) evict models only in a real ZEUS
    process. Under pytest the host in config.ini is the operator's REAL
    Ollama, and a test must never unload their models; tests of this module
    opt in with AGNI_LIFECYCLE_IN_TESTS=1 against a fake server."""
    import os
    import sys
    if os.environ.get("AGNI_TEST_MODE") == "1" and "pytest" in sys.modules:
        return os.environ.get("AGNI_LIFECYCLE_IN_TESTS") == "1"
    return True


def _config() -> configparser.ConfigParser:
    parser = configparser.ConfigParser()
    try:
        parser.read(ROOT / "config.ini", encoding="utf-8")
    except configparser.Error:
        pass
    return parser


def host() -> str:
    try:
        from core.ollama_client import _load_host
        return _load_host().rstrip("/")
    except Exception:
        return DEFAULT_HOST


def keep_alive() -> str:
    value = _config().get("models", "keep_alive", fallback="5m").strip()
    return value or "5m"


def max_resident() -> int:
    raw = _config().get("models", "max_resident", fallback="1").strip()
    try:
        return max(0, int(raw))
    except ValueError:
        return 1


def request(path: str, body: Optional[dict] = None, timeout: float = 10.0,
            base: Optional[str] = None) -> Tuple[int, Optional[dict], str]:
    """(HTTP status, parsed JSON or None, raw text). Never uses a proxy.
    A connection failure is status 0 with the error as the text."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        f"{(base or host()).rstrip('/')}{path}", data=data, method="POST" if data is not None else "GET",
        headers={"Content-Type": "application/json"} if data is not None else {})
    try:
        with opener.open(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
            status = response.status
    except urllib.error.HTTPError as error:
        try:
            raw = error.read().decode("utf-8", "replace")
        except Exception:
            raw = ""
        status = error.code
    except Exception as error:
        return 0, None, f"{type(error).__name__}: {error}"
    try:
        parsed = json.loads(raw) if raw.strip() else None
    except ValueError:
        parsed = None
    return status, parsed if isinstance(parsed, dict) else None, raw


def resident(base: Optional[str] = None) -> List[Dict]:
    """Models the daemon holds right now. Empty when it is not reachable."""
    status, data, _raw = request("/api/ps", timeout=5.0, base=base)
    if status != 200 or not data:
        return []
    return [{"name": m.get("name", ""), "size_gb": round((m.get("size") or 0) / 1024 ** 3, 2),
             "vram_gb": round((m.get("size_vram") or 0) / 1024 ** 3, 2),
             "expires_at": m.get("expires_at", "")} for m in data.get("models", [])]


_sizes: Dict[str, float] = {}
_sizes_at = 0.0


def model_size_gb(name: str, base: Optional[str] = None) -> float:
    """On-disk size of an installed model, from /api/tags (cached 60 s)."""
    global _sizes_at
    if time.time() - _sizes_at > 60 or name not in _sizes:
        status, data, _raw = request("/api/tags", timeout=5.0, base=base)
        if status == 200 and data:
            _sizes.clear()
            for model in data.get("models", []):
                _sizes[model.get("name", "")] = (model.get("size") or 0) / 1024 ** 3
            _sizes_at = time.time()
    if name in _sizes:
        return _sizes[name]
    if f"{name}:latest" in _sizes:
        return _sizes[f"{name}:latest"]
    return 0.0


def estimate_commit_gb(name: str, base: Optional[str] = None) -> float:
    size = model_size_gb(name, base)
    return round(size * _COMMIT_FACTOR + _COMMIT_OVERHEAD_GB, 2) if size else 1.5


def _is_resident(name: str, models: List[Dict]) -> bool:
    return any(m["name"] == name or m["name"] == f"{name}:latest" for m in models)


def unload(name: str, wait_seconds: float = 15.0, base: Optional[str] = None) -> bool:
    """Ask the daemon to drop a model and confirm it is gone from /api/ps.

    Ollama 0.34 accepts keep_alive=0 on /api/generate for embedding models
    too (done_reason "unload"); /api/embed is the fallback for a runtime that
    refuses it."""
    status, _data, _raw = request("/api/generate", {"model": name, "keep_alive": 0},
                                  timeout=30, base=base)
    if status != 200:
        request("/api/embed", {"model": name, "input": "", "keep_alive": 0}, timeout=30, base=base)
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        if not _is_resident(name, resident(base)):
            return True
        time.sleep(0.5)
    return not _is_resident(name, resident(base))


def unload_all(except_model: str = "", base: Optional[str] = None) -> List[str]:
    dropped = []
    for model in resident(base):
        if model["name"] in (except_model, f"{except_model}:latest"):
            continue
        if unload(model["name"], base=base):
            dropped.append(model["name"])
    return dropped


# Heavy things ZEUS holds IN ITS OWN processes that can be let go when a
# language model needs the memory - today the Whisper worker. Each callback
# returns True if it released something (and False if it was busy).
_releasers: Dict[str, List[Callable[[], bool]]] = {}


def register_releaser(name: str, release: Callable[[], bool]) -> None:
    _releasers.setdefault(name, [])
    if release not in _releasers[name]:
        _releasers[name].append(release)


def unregister_releaser(name: str, release: Callable[[], bool]) -> None:
    if release in _releasers.get(name, []):
        _releasers[name].remove(release)


def release_idle_components() -> List[str]:
    released = []
    for name, callbacks in list(_releasers.items()):
        for release in list(callbacks):
            try:
                if release():
                    released.append(name)
            except Exception:
                continue
    return released


def ensure_room(name: str, margin_gb: Optional[float] = None,
                base: Optional[str] = None) -> Tuple[bool, str, List[str]]:
    """Before loading `name`: make sure it can load without exhausting memory.

    Returns (ok, reason, models_unloaded). A model that is already resident
    needs nothing. Otherwise the residency limit is applied first, then the
    commit headroom is checked and other models are evicted until it fits.
    `ok=False` means the load would very likely fail - the caller should
    report DEGRADED with the reason rather than try and crash the runtime."""
    from core.sysmem import DEFAULT_MARGIN_GB, has_headroom
    margin = DEFAULT_MARGIN_GB if margin_gb is None else margin_gb
    loaded = resident(base)
    if _is_resident(name, loaded):
        return True, f"{name} is already resident", []
    unloaded: List[str] = []
    limit = max_resident()
    if limit and len(loaded) >= limit:
        # Oldest expiry first = least recently used.
        for model in sorted(loaded, key=lambda m: m.get("expires_at", ""))[:len(loaded) - limit + 1]:
            if unload(model["name"], base=base):
                unloaded.append(model["name"])
    need = estimate_commit_gb(name, base)
    ok, reason = has_headroom(need, margin)
    if not ok:
        for model in resident(base):
            if unload(model["name"], base=base):
                unloaded.append(model["name"])
        ok, reason = has_headroom(need, margin)
    if not ok:
        unloaded += release_idle_components()
        ok, reason = has_headroom(need, margin)
    return ok, reason, unloaded
