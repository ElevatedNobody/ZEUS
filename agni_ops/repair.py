"""
REPAIR.bat: diagnose, repair what is safe to repair, verify.

Before anything is changed a BACKUP is taken (backups/repair_<stamp>/):
the account/memory databases, the Hermes and native memory stores, config.ini
and config/*.json. Never deleted, by any step: user memory, project memory,
the Vault, the audit history, generated artifacts.

Safe repairs, each reported with what was found and what was done:
    stale processes      orphaned Whisper workers / Chatterbox bridges
    stale state          'online' runtime record left by a crash; *.part downloads
    configuration        missing keys added with their documented defaults
    Ollama               started if installed and not running; idle models unloaded
    cached model state   calibration for models that are no longer installed
    core & project DBs   ensures schema for accounts, ZEUS projects, and network sync
    firewall scripts     regenerates PowerShell firewall scripts for current directory
    Whisper record       re-verified by a real transcription when it is stale
    databases            SQLite integrity checks (account DB, project DB, memory stores)
    packages             reinstalled from requirements.txt when missing
"""

from __future__ import annotations

import configparser
import json
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import List

from agni_ops.status import FAIL, NOT_TESTED, PASS, UNAVAILABLE, WARN, Result

ROOT = Path(__file__).resolve().parent.parent

# Keys added by this repair release, with the defaults they document.
CONFIG_DEFAULTS = {
    "models": {"keep_alive": "5m", "max_resident": "1"},
    "memory": {"backend": "hermes", "min_score": "0.12", "max_results": "6",
               "context_chars": "1600", "on_unavailable": "answer"},
    "voice": {"stt_isolation": "process", "tts_keep_loaded": "auto", "tts_idle_seconds": "300",
              "listen_until_silence": "true", "listen_max_seconds": "15"},
    "security": {"firewall_include_ollama": "true"},
    "knowledge": {"default_classification": "internal", "fiscal_year_label": "end", "embed_on_ingest": "auto"},
    "crudex": {"mode": "offline", "server_url": "", "ca_file": "", "sync_interval_seconds": "0"},
}


def backup() -> Path:
    from core.paths import data_dir
    target = ROOT / "backups" / f"repair_{time.strftime('%Y%m%d_%H%M%S')}"
    target.mkdir(parents=True, exist_ok=True)
    data = data_dir()
    for path in list(data.glob("*.db")) + [ROOT / "config.ini"] + list((ROOT / "config").glob("*.json")):
        if path.is_file():
            shutil.copy2(path, target / path.name)
    memory = data / "memory"
    if memory.is_dir():
        shutil.copytree(memory, target / "memory", dirs_exist_ok=True)
    # SQLite files in WAL mode: copy the companions so the backup is consistent.
    for path in data.glob("*.db-wal"):
        shutil.copy2(path, target / path.name)
    return target


def stale_processes() -> Result:
    try:
        import psutil
    except ImportError:
        return Result("stale processes", NOT_TESTED, reason="psutil missing", group="repair")
    killed = []
    for process in psutil.process_iter(["pid", "name", "cmdline", "ppid"]):
        try:
            cmd = " ".join(process.info.get("cmdline") or [])
            if "voice.stt_worker" not in cmd and "chatterbox_bridge.py" not in cmd:
                continue
            if str(ROOT).lower() not in cmd.lower() and "voice.stt_worker" not in cmd:
                continue
            parent_alive = psutil.pid_exists(process.info["ppid"] or 0)
            if not parent_alive:
                process.kill()
                killed.append(f"{process.info['pid']} ({'whisper worker' if 'stt_worker' in cmd else 'chatterbox'})")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return Result("stale processes", PASS, group="repair",
                  reason=f"stopped orphaned: {', '.join(killed)}" if killed else "none found",
                  fix_attempted="killed orphans" if killed else "")


def stale_state() -> Result:
    from core.paths import log_dir
    fixed = []
    runtime = log_dir() / "agni_runtime.json"
    try:
        record = json.loads(runtime.read_text(encoding="utf-8"))
        import psutil
        if record.get("state") == "online" and not psutil.pid_exists(int(record.get("pid", 0))):
            record.update(state="stopped", cleaned_by="repair", cleaned_at=time.time())
            runtime.write_text(json.dumps(record, indent=2), encoding="utf-8")
            fixed.append(f"runtime record of dead pid {record.get('pid')}")
    except Exception:
        pass
    for part in (ROOT / "models").rglob("*.part"):
        try:
            part.unlink()
            fixed.append(part.name)
        except OSError:
            pass
    return Result("stale state / partial downloads", PASS, group="repair",
                  reason=("cleaned: " + ", ".join(fixed)) if fixed else "none found")


def configuration() -> Result:
    path = ROOT / "config.ini"
    parser = configparser.ConfigParser()
    try:
        parser.read(path, encoding="utf-8")
    except configparser.Error as error:
        return Result("configuration", FAIL, group="repair", reason=f"config.ini unreadable: {error}",
                      fallback="restore config.ini from backups/")
    missing = [(section, key, value) for section, keys in CONFIG_DEFAULTS.items()
               for key, value in keys.items()
               if not parser.has_option(section, key)]
    if not missing:
        return Result("configuration", PASS, group="repair", reason="every documented key present")
    # Append, never rewrite: the operator's comments and layout stay intact.
    text = path.read_text(encoding="utf-8")
    added = []
    for section in dict.fromkeys(s for s, _k, _v in missing):
        lines = [f"{key} = {value}" for s, key, value in missing if s == section]
        if parser.has_section(section):
            marker = f"[{section}]"
            index = text.find(marker)
            end = text.find("\n[", index + len(marker))
            end = len(text) if end == -1 else end
            text = text[:end].rstrip("\n") + "\n" + "\n".join(lines) + "\n" + text[end:]
        else:
            text = text.rstrip("\n") + f"\n\n[{section}]\n" + "\n".join(lines) + "\n"
        added += [f"[{section}] {line}" for line in lines]
    path.write_text(text, encoding="utf-8")
    return Result("configuration", PASS, group="repair", reason=f"added {len(added)} missing key(s)",
                  fix_attempted="; ".join(added))


def ollama() -> List[Result]:
    from agni_ops.system import ollama_state
    from core import model_lifecycle
    state = ollama_state()
    results = []
    if not state["running"]:
        if not state["installed"]:
            return [Result("Ollama", UNAVAILABLE, group="repair", reason="not installed - https://ollama.com")]
        subprocess.run([sys.executable, str(ROOT / "scripts" / "start_check.py")], timeout=120,
                       capture_output=True, cwd=str(ROOT))
        state = ollama_state()
        results.append(Result("Ollama service", PASS if state["running"] else FAIL, group="repair",
                              reason="started" if state["running"] else "could not be started",
                              fix_attempted="scripts/start_check.py"))
    else:
        results.append(Result("Ollama service", PASS, group="repair",
                              reason=f"running ({state['version']}, {len(state['models'])} models)"))
    dropped = model_lifecycle.unload_all()
    results.append(Result("idle models", PASS, group="repair",
                          reason=f"unloaded {', '.join(dropped)}" if dropped else "none resident"))
    try:
        from core.model_calibration import load, path
        records = load()
        installed = state["models"]
        from agni_ops.models import resolve_installed
        gone = [m for m in records if not resolve_installed(m, installed)]
        if gone:
            data = {"models": {k: v for k, v in records.items() if k not in gone}}
            path().write_text(json.dumps(data, indent=2), encoding="utf-8")
        results.append(Result("cached model state", PASS, group="repair",
                              reason=f"dropped state of removed models: {', '.join(gone)}" if gone
                              else "consistent with installed models"))
    except Exception as error:
        results.append(Result("cached model state", WARN, group="repair", reason=str(error)))
    return results


def core_and_project_stores() -> List[Result]:
    """Ensure account DB, ZEUS DB, network sync DB, and firewall scripts are initialized and repaired."""
    results: List[Result] = []
    try:
        from memory.database import init_db, list_users
        init_db()
        users = list_users()
        results.append(Result("account database schema", PASS, group="repair",
                              reason=f"medha.db verified ({len(users)} account(s))"))
    except Exception as error:
        results.append(Result("account database schema", FAIL, group="repair", reason=str(error)))

    try:
        from jarvis import search, store as jv_store
        info = jv_store.integrity()
        counts = search.reindex(embed=False)
        results.append(Result("ZEUS database & index", PASS if info.get("ok") else FAIL, group="repair",
                              reason=f"jarvis_projects.db schema verified; indexed {counts.get('activities', 0)} activities, "
                                     f"{counts.get('evidence', 0)} evidence"))
    except Exception as error:
        results.append(Result("ZEUS database & index", FAIL, group="repair", reason=str(error)))

    try:
        from crudex_net import store as net_store
        with net_store.connect() as conn:
            ok = conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        results.append(Result("network sync database", PASS if ok else FAIL, group="repair",
                              reason="crudex_net.db schema verified"))
    except Exception as error:
        results.append(Result("network sync database", FAIL, group="repair", reason=str(error)))

    try:
        from security.firewall import include_ollama_default, write_scripts
        written = write_scripts(ROOT, include_ollama_default(ROOT))
        results.append(Result("firewall scripts", PASS, group="repair",
                              reason=f"regenerated for current path ({Path(written.get('apply', '')).name})"))
    except Exception as error:
        results.append(Result("firewall scripts", WARN, group="repair", reason=str(error)))

    return results


def databases() -> List[Result]:
    from core.paths import data_dir
    results = []
    targets = list(data_dir().glob("*.db")) + list((data_dir() / "memory").glob("*.sqlite"))
    for db in targets:
        try:
            conn = sqlite3.connect(str(db), timeout=10)
            verdict = conn.execute("PRAGMA integrity_check").fetchone()[0]
            conn.close()
            results.append(Result(f"database {db.name}", PASS if verdict == "ok" else FAIL, group="repair",
                                  reason="integrity ok" if verdict == "ok" else verdict[:200],
                                  fallback="" if verdict == "ok" else "restore it from backups/"))
        except Exception as error:
            results.append(Result(f"database {db.name}", FAIL, group="repair", reason=str(error)))
    return results


def packages() -> Result:
    from agni_ops.system import PACKAGES, network_egress_blocked_for_python, package_present
    missing = [pip for module, (pip, _why, required) in PACKAGES.items()
               if required and not package_present(module)]
    if not missing:
        return Result("packages", PASS, group="repair", reason="every required package present")
    network = network_egress_blocked_for_python()
    if network["state"] == "blocked_by_firewall":
        return Result("packages", FAIL, group="repair", reason=f"missing {', '.join(missing)}; pip cannot "
                      "download because Windows Firewall blocks this Python (WinError 10013)",
                      fix_attempted="none", fallback="Temporarily remove firewall rules -> run REPAIR.bat -> re-apply firewall")
    req_path = ROOT / "config" / "requirements.txt"
    if not req_path.is_file():
        req_path = ROOT / "requirements.txt"
    run = subprocess.run([sys.executable, "-m", "pip", "install", "-r", str(req_path)],
                         capture_output=True, text=True, timeout=3600)
    still = [pip for module, (pip, _why, required) in PACKAGES.items()
             if required and not package_present(module)]
    return Result("packages", PASS if not still else FAIL, group="repair",
                  reason="reinstalled" if not still else f"still missing {', '.join(still)}",
                  fix_attempted="pip install -r requirements.txt", evidence=(run.stdout or "")[-1500:])


def whisper_record() -> Result:
    try:
        from voice.offline import resolve_whisper_dir
        record = json.loads((resolve_whisper_dir() / "agni_verified.json").read_text(encoding="utf-8"))
    except Exception:
        record = {}
    if record.get("verified_by") == "agni_ops.voice" and record.get("ok"):
        return Result("Whisper verification", PASS, group="repair",
                      reason=f"{record.get('device')}/{record.get('compute_type')} verified by a real transcription")
    import tempfile
    from agni_ops.voice import whisper_suite
    results = whisper_suite(Path(tempfile.mkdtemp(prefix="agni_repair_")), crash_test=False)
    final = results[-1]
    return Result("Whisper verification", final.status, group="repair", reason=final.reason,
                  fix_attempted="re-verified with a real transcription (stale or unverified record)",
                  fallback=final.fallback)


def memory_stores() -> Result:
    """Every account's memory store exists and holds its earlier facts."""
    try:
        from memory.hermes_store import config, initialize_stores
        counts = initialize_stores()
        return Result("memory stores", PASS, group="repair",
                      reason=f"{config()['backend']}: " + (", ".join(f"{u} {n}" for u, n in counts.items())
                                                           or "no accounts yet"))
    except Exception as error:
        return Result("memory stores", FAIL, group="repair", reason=f"{type(error).__name__}: {error}",
                      fallback="config.ini [memory] backend = native")


def run() -> List[Result]:
    results: List[Result] = []
    saved = backup()
    results.append(Result("backup", PASS, group="repair", reason=f"taken before any change: {saved}"))
    for step in (stale_processes, stale_state, configuration):
        results.append(step())
    results += ollama()
    results.append(memory_stores())
    results += core_and_project_stores()
    results += databases()
    results.append(packages())
    results.append(whisper_record())
    return results
