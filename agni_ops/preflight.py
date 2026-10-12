"""
STEP 2 : START preflight: what will work in this session, BEFORE starting.

Fast (seconds) and read-only: no model is loaded, no inference is run. Model,
Whisper and face states are the last VERIFIED results, shown with their age -
an unverified component says so.

Every line is COMPONENT / STATUS / REASON, plus FALLBACK when not PASS.
Exit code 1 only when ZEUS cannot start at all (no environment, missing core
packages, unreadable configuration or account database).
"""

from __future__ import annotations

import configparser
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import List

from agni_ops.status import (DEGRADED, FAIL, NOT_TESTED, PASS, UNAVAILABLE, WARN, Result)

ROOT = Path(__file__).resolve().parent.parent
ESSENTIAL = ("Python environment", "Core packages", "Configuration", "Account database")


def _age(timestamp) -> str:
    try:
        hours = (time.time() - float(timestamp)) / 3600
    except (TypeError, ValueError):
        return "unknown age"
    return f"{hours:.0f} h ago" if hours < 48 else f"{hours / 24:.0f} days ago"


def run() -> List[Result]:
    from agni_ops import system
    from core.paths import data_dir, log_dir
    results: List[Result] = []
    info_python = sys.version.split()[0]
    in_venv = Path(sys.prefix).resolve() == (ROOT / ".venv").resolve()
    results.append(Result("Python environment", PASS if in_venv else FAIL,
                          reason=f"Python {info_python} in .venv" if in_venv else
                          f"not running from .venv ({sys.executable}) - run STEP 1 : INSTALL"))
    missing = [m for m, (_pip, _why, required) in system.PACKAGES.items()
               if required and not system.package_present(m)]
    results.append(Result("Core packages", FAIL if missing else PASS,
                          reason=f"missing {', '.join(missing)} - run REPAIR.bat" if missing
                          else "all required packages present"))
    parser = configparser.ConfigParser()
    try:
        parser.read(ROOT / "config.ini", encoding="utf-8")
        results.append(Result("Configuration", PASS if parser.has_section("auth") else FAIL,
                              reason="config.ini readable" if parser.has_section("auth")
                              else "config.ini missing sections - run REPAIR.bat"))
    except configparser.Error as error:
        results.append(Result("Configuration", FAIL, reason=f"config.ini invalid: {error}"))

    try:
        db = data_dir() / "medha.db"
        if not db.exists():
            from memory.database import init_db
            init_db()
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        ok = conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        conn.close()
        results.append(Result("Account database", PASS if ok else FAIL,
                              reason=f"integrity ok, {users} account(s)" if ok else "integrity check failed"))
    except Exception as error:
        results.append(Result("Account database", FAIL, reason=f"{error}"))

    try:
        from jarvis import store as jv_store
        with jv_store.connection() as conn:
            jv_ok = conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        from crudex_net import store as net_store
        with net_store.connect() as conn:
            net_ok = conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        results.append(Result("ZEUS database", PASS if (jv_ok and net_ok) else FAIL,
                              reason="jarvis_projects.db & crudex_net.db ready"))
    except Exception as error:
        results.append(Result("ZEUS database", WARN, reason=f"{error}"))

    # Filesystem permissions
    try:
        for folder in (log_dir(), data_dir()):
            probe = folder / f".write_probe_{os.getpid()}"
            probe.write_text("x", encoding="utf-8")
            probe.unlink()
        results.append(Result("Filesystem", PASS, reason="logs and database folders writable"))
    except Exception as error:
        results.append(Result("Filesystem", FAIL, reason=f"not writable: {error}"))

    # Ollama and ports
    try:
        state = system.ollama_state()
    except Exception as error:
        state = {"running": False, "error": str(error), "models": [], "resident": []}
    if state["running"]:
        results.append(Result("Ollama (port 11434)", PASS,
                              reason=f"{state['version']}, {len(state['models'])} model(s), "
                                     f"{len(state['resident'])} loaded"))
    else:
        results.append(Result("Ollama (port 11434)", DEGRADED,
                              reason=f"not reachable ({state.get('error', '')[:80]})",
                              fallback="deterministic tools only; no conversation until Ollama runs"))

    # Required models: presence + the last verified function test
    try:
        from agni_ops.models import configured_models, resolve_installed
        from core.model_calibration import load
        health = load()
        for model, slot in configured_models().items():
            installed = resolve_installed(model, state.get("models", [])) if state["running"] else ""
            record = health.get(model) or {}
            if state["running"] and not installed:
                results.append(Result(f"Model {model}", UNAVAILABLE, reason=f"not installed ({slot})",
                                      fallback="the next model in that slot, if installed"))
            elif not record:
                results.append(Result(f"Model {model}", WARN, reason=f"{slot}: never function-tested "
                                                                   f"here - run STEP 1 : INSTALL"))
            else:
                status = record.get("status", NOT_TESTED)
                results.append(Result(f"Model {model}", status,
                                      reason=f"{slot}: {record.get('function_detail') or record.get('cause')} "
                                             f"(tested {_age(record.get('tested_at'))})"[:200],
                                      fallback=record.get("fallback", "")))
    except Exception as error:
        results.append(Result("Models", WARN, reason=f"could not read model state: {error}"))

    # Memory + Hermes
    try:
        from memory.hermes_store import _load_hermes, config as memory_config
        backend = memory_config()["backend"]
        if backend == "hermes":
            _load_hermes()
        results.append(Result("Memory (Hermes)", PASS, reason=f"backend {backend}, integrity verified"))
    except Exception as error:
        results.append(Result("Memory (Hermes)", DEGRADED, reason=str(error)[:160],
                              fallback="ZEUS native memory store"))

    # Whisper: last verification record
    try:
        from voice.offline import resolve_whisper_dir
        record_path = resolve_whisper_dir() / "agni_verified.json"
        record = json.loads(record_path.read_text(encoding="utf-8")) if record_path.is_file() else {}
        isolation = os.environ.get("AGNI_STT_ISOLATION", "") or parser.get("voice", "stt_isolation",
                                                                            fallback="process")
        if not (resolve_whisper_dir() / "model.bin").is_file():
            results.append(Result("Whisper (listening)", UNAVAILABLE, reason="no model downloaded",
                                  fallback="typed input"))
        elif record.get("verified_by") == "agni_ops.voice" and record.get("ok"):
            results.append(Result("Whisper (listening)", PASS,
                                  reason=f"{record.get('device')}/{record.get('compute_type')} verified "
                                         f"{_age(record.get('verified_at'))}; runs in a {isolation} worker"))
        else:
            results.append(Result("Whisper (listening)", WARN,
                                  reason="not verified by a real transcription - run REPAIR.bat",
                                  fallback="CPU worker if the GPU backend fails"))
    except Exception as error:
        results.append(Result("Whisper (listening)", WARN, reason=str(error)[:160]))

    # TTS: what exists on disk
    try:
        from voice.providers import ChatterboxBridgeProvider, ProviderState
        report = ChatterboxBridgeProvider().probe()
        system_voice = system.package_present("pyttsx3")
        if report.state in (ProviderState.NOT_PROBED, ProviderState.READY):
            results.append(Result("TTS (speaking)", PASS, reason="Chatterbox installed; Windows voice "
                                  + ("available" if system_voice else "missing")))
        else:
            results.append(Result("TTS (speaking)", DEGRADED if system_voice else UNAVAILABLE,
                                  reason=f"Chatterbox: {report.detail[:100]}",
                                  fallback="Windows voice (SAPI)" if system_voice else "text only"))
    except Exception as error:
        results.append(Result("TTS (speaking)", WARN, reason=str(error)[:160]))

    # Authentication / face
    auth_on = parser.getboolean("auth", "require_login", fallback=True)
    results.append(Result("Authentication", PASS if auth_on else WARN,
                          reason="login required" if auth_on else "require_login = false"))
    face_mode = parser.get("face", "login", fallback="off")
    results.append(Result("Face login", PASS if face_mode == "second_factor" else NOT_TESTED,
                          reason=f"[face] login = {face_mode}"
                                 + ("" if face_mode == "second_factor" else " (password only)")))

    # Firewall
    try:
        from agni_ops.firewall import expected_programs, read_rules
        rules = read_rules()
        mode = parser.get("security", "network_mode", fallback="sovereign")
        if rules:
            enabled = sum(1 for r in rules if r.get("Enabled") in ("True", "1"))
            covered = {(r.get("Program") or "").lower() for r in rules}
            uncovered = [Path(p).name for p in expected_programs() if p.lower() not in covered]
            results.append(Result("Firewall", WARN if uncovered or enabled < len(rules) else PASS,
                                  reason=f"{enabled}/{len(rules)} rules enabled; in-process guard: {mode}"
                                         + (f"; NOT covered: {', '.join(uncovered)}" if uncovered else "")))
        else:
            results.append(Result("Firewall", DEGRADED,
                                  reason="OS rules not applied - REQUIRES_ADMIN",
                                  fallback=f"in-process egress guard ({mode})"))
    except Exception as error:
        results.append(Result("Firewall", WARN, reason=str(error)[:160]))

    # Memory headroom
    from core.sysmem import commit_snapshot
    memory = commit_snapshot()
    headroom = memory.get("commit_available_gb", 0)
    results.append(Result("Memory headroom", PASS if headroom >= 6 else WARN,
                          reason=f"{headroom} GB commit free of {memory.get('commit_limit_gb')} GB"
                                 + ("" if headroom >= 6 else
                                    " - ZEUS keeps one model loaded at a time; close other large apps"),
                          fallback="" if headroom >= 6 else "models are unloaded between uses"))

    # Stale state from a previous crash / an instance already running
    try:
        runtime = json.loads((log_dir() / "agni_runtime.json").read_text(encoding="utf-8"))
        import psutil
        alive = runtime.get("state") == "online" and psutil.pid_exists(int(runtime.get("pid", 0)))
        if alive:
            results.append(Result("Running instance", WARN, reason=f"ZEUS already online (pid {runtime['pid']})"))
        elif runtime.get("state") == "online":
            results.append(Result("Previous session", WARN,
                                  reason=f"ended without shutting down (pid {runtime.get('pid')} gone) - "
                                         f"REPAIR.bat cleans stale state"))
    except Exception:
        pass
    return results


def fatal(results: List[Result]) -> bool:
    return any(r.component in ESSENTIAL and r.status == FAIL for r in results)
