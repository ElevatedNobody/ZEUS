"""
ZEUS Built-in Tools
Real implementations (no placeholders). Registered against the shared
`registry` object at import time. Each tool degrades gracefully if an
optional dependency is missing.
"""

import fnmatch
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from core.paths import documents_root as _documents_root
from typing import Any, List, Optional

from core.logger import get_logger
from core.security_manager import RiskLevel, security_manager

logger = get_logger("builtin_tools")

# `registry` is imported lazily inside tool_registry to avoid a circular
# import (tool_registry imports this module at the bottom of its file).
from core.tool_registry import registry


# ---------------------------------------------------------------- SYSTEM ---

@registry.register("get_system_info", "Get OS, CPU, and memory summary", RiskLevel.SAFE)
def get_system_info():
    import platform
    info = {
        "os": platform.system(),
        "os_version": platform.version(),
        "machine": platform.machine(),
        "python": platform.python_version(),
    }
    try:
        import psutil
        info["cpu_percent"] = psutil.cpu_percent(interval=0.3)
        vm = psutil.virtual_memory()
        info["memory_used_pct"] = vm.percent
        info["memory_total_gb"] = round(vm.total / (1024 ** 3), 1)
    except ImportError:
        info["note"] = "psutil not installed - limited stats"
    return info


@registry.register("get_cpu_usage", "Get current CPU usage percentage", RiskLevel.SAFE)
def get_cpu_usage():
    try:
        import psutil
        return {"cpu_percent": psutil.cpu_percent(interval=0.3)}
    except ImportError:
        return {"error": "psutil not installed"}


@registry.register("get_memory_usage", "Get current RAM usage", RiskLevel.SAFE)
def get_memory_usage():
    try:
        import psutil
        vm = psutil.virtual_memory()
        return {"used_pct": vm.percent, "available_gb": round(vm.available / (1024 ** 3), 2)}
    except ImportError:
        return {"error": "psutil not installed"}


@registry.register("get_disk_usage", "Get disk usage for the main drive", RiskLevel.SAFE)
def get_disk_usage():
    try:
        import psutil
        root = "C:\\" if sys.platform == "win32" else "/"
        du = psutil.disk_usage(root)
        return {"used_pct": du.percent, "free_gb": round(du.free / (1024 ** 3), 1)}
    except ImportError:
        return {"error": "psutil not installed"}


@registry.register("get_battery", "Get battery status if available", RiskLevel.SAFE)
def get_battery():
    try:
        import psutil
        b = psutil.sensors_battery()
        if b is None:
            return {"available": False}
        return {"available": True, "percent": b.percent, "plugged_in": b.power_plugged}
    except ImportError:
        return {"error": "psutil not installed"}


# ------------------------------------------------------------------ FILES --

def _safe_resolve(path: str) -> Path:
    p = Path(path).expanduser()
    if not security_manager.validate_path(str(p)):
        raise PermissionError(f"Access to '{path}' is blocked by security policy")
    return p


@registry.register(
    "search_files", "Search for files by name pattern under a root directory",
    RiskLevel.SAFE, params={"root": "directory to search", "pattern": "glob pattern e.g. *.pdf"},
)
def search_files(root: str, pattern: str = "*", max_results: int = 50):
    root_path = _safe_resolve(root)
    if not root_path.exists():
        return {"error": f"'{root}' does not exist"}
    matches = []
    for dirpath, _, filenames in os.walk(root_path):
        for fn in filenames:
            if fnmatch.fnmatch(fn.lower(), pattern.lower()):
                matches.append(str(Path(dirpath) / fn))
                if len(matches) >= max_results:
                    return {"count": len(matches), "files": matches}
    return {"count": len(matches), "files": matches}


@registry.register("read_file", "Read the text contents of a file", RiskLevel.LOW_RISK,
                    params={"path": "file path"})
def read_file(path: str, max_chars: int = 20000):
    p = _safe_resolve(path)
    if not p.exists():
        return {"error": f"'{path}' does not exist"}
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
        return {"content": text[:max_chars], "truncated": len(text) > max_chars}
    except Exception as e:
        return {"error": str(e)}


@registry.register("write_file", "Write text content to a file", RiskLevel.MEDIUM_RISK,
                    params={"path": "file path", "content": "text to write"})
def write_file(path: str, content: str):
    p = _safe_resolve(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return {"written": str(p), "bytes": len(content.encode("utf-8"))}


@registry.register("copy_file", "Copy a file to a new location", RiskLevel.LOW_RISK,
                    params={"src": "source path", "dst": "destination path"})
def copy_file(src: str, dst: str):
    import shutil
    s, d = _safe_resolve(src), _safe_resolve(dst)
    d.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(s, d)
    return {"copied": str(s), "to": str(d)}


@registry.register("move_file", "Move/rename a file", RiskLevel.MEDIUM_RISK,
                    params={"src": "source path", "dst": "destination path"})
def move_file(src: str, dst: str):
    import shutil
    s, d = _safe_resolve(src), _safe_resolve(dst)
    d.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(s), str(d))
    return {"moved": str(s), "to": str(d)}


@registry.register("create_folder", "Create a new directory", RiskLevel.LOW_RISK,
                    params={"path": "directory path"})
def create_folder(path: str):
    p = _safe_resolve(path)
    p.mkdir(parents=True, exist_ok=True)
    return {"created": str(p)}


@registry.register("delete_file", "Delete a file (irreversible)", RiskLevel.HIGH_RISK,
                    params={"path": "file path"})
def delete_file(path: str):
    p = _safe_resolve(path)
    if not p.exists():
        return {"error": f"'{path}' does not exist"}
    if p.is_dir():
        return {"error": "Refusing to delete a directory with this tool"}
    p.unlink()
    return {"deleted": str(p)}


@registry.register("get_time", "Get the current date and time", RiskLevel.SAFE)
def get_time():
    import datetime
    now = datetime.datetime.now()
    return {"time": now.strftime("%I:%M %p"), "date": now.strftime("%A, %B %d, %Y")}


# ------------------------------------------------------------ APPLICATIONS --

# Fast path for very common apps - tried first since it's instant (no disk walk).
FRIENDLY_APPS = {
    "chrome": "chrome.exe", "google chrome": "chrome.exe",
    "edge": "msedge.exe", "microsoft edge": "msedge.exe",
    "notepad": "notepad.exe",
    "word": "winword.exe", "microsoft word": "winword.exe",
    "excel": "excel.exe", "microsoft excel": "excel.exe",
    "powerpoint": "powerpnt.exe", "microsoft powerpoint": "powerpnt.exe",
    "outlook": "outlook.exe", "microsoft outlook": "outlook.exe",
    "onenote": "onenote.exe", "onedrive": "onedrive.exe",
    "teams": "ms-teams.exe", "microsoft teams": "ms-teams.exe",
    "vscode": "code.exe", "vs code": "code.exe", "visual studio code": "code.exe",
    "explorer": "explorer.exe", "file explorer": "explorer.exe",
    "spotify": "spotify.exe",
    "paint": "mspaint.exe",
    "cmd": "cmd.exe", "command prompt": "cmd.exe", "terminal": "cmd.exe",
    "powershell": "powershell.exe",
    "firefox": "firefox.exe",
    "zoom": "Zoom.exe",
    "settings": "ms-settings:",
}

# Best-known Application User Model IDs for common Microsoft Store / UWP
# inbox apps (these don't have a plain .exe on PATH - they're launched via
# `explorer.exe shell:appsfolder\<AUMID>`). Publisher ID "8wekyb3d8bbwe" is
# Microsoft's fixed Store publisher ID and is stable across Windows builds.
STORE_APP_AUMIDS = {
    "calculator": "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App",
    "mail": "microsoft.windowscommunicationsapps_8wekyb3d8bbwe!microsoft.windowslive.mail",
    "maps": "Microsoft.WindowsMaps_8wekyb3d8bbwe!App",
    "photos": "Microsoft.Windows.Photos_8wekyb3d8bbwe!App",
    "snipping tool": "Microsoft.ScreenSketch_8wekyb3d8bbwe!App",
    "screen snipping": "Microsoft.ScreenSketch_8wekyb3d8bbwe!App",
    "sound recorder": "Microsoft.WindowsSoundRecorder_8wekyb3d8bbwe!App",
    "solitaire": "Microsoft.MicrosoftSolitaireCollection_8wekyb3d8bbwe!App",
    "microsoft to do": "Microsoft.Todos_8wekyb3d8bbwe!App", "to do": "Microsoft.Todos_8wekyb3d8bbwe!App",
    "microsoft whiteboard": "Microsoft.Whiteboard_8wekyb3d8bbwe!App", "whiteboard": "Microsoft.Whiteboard_8wekyb3d8bbwe!App",
    "clipchamp": "Clipchamp.Clipchamp_yxz26nhyzhsrt!App",
    "phone link": "Microsoft.YourPhone_8wekyb3d8bbwe!App",
    "xbox": "Microsoft.GamingApp_8wekyb3d8bbwe!Microsoft.Xbox.App",
    "quick assist": "MicrosoftCorporationII.QuickAssist_8wekyb3d8bbwe!App",
    "people": "Microsoft.People_8wekyb3d8bbwe!App",
}


def _resolve_app_name(name: str) -> str:
    return FRIENDLY_APPS.get(name.lower().strip(), name.strip())


def _fuzzy_best_match(query: str, candidates: list, cutoff: float = 0.5) -> Optional[str]:
    """Typo-tolerant matching so 'whatsappmka' still resolves to 'WhatsApp'."""
    import difflib
    if not candidates:
        return None
    lower_map = {c.lower(): c for c in candidates}
    matches = difflib.get_close_matches(query.lower(), list(lower_map.keys()), n=1, cutoff=cutoff)
    if matches:
        return lower_map[matches[0]]
    # Fallback: substring containment catches cases like "whatsappmka" where
    # the extra trailing characters push the SequenceMatcher ratio down.
    ql = query.lower()
    for cand_lower, original in lower_map.items():
        if cand_lower in ql or ql in cand_lower:
            return original
    return None


def _find_start_menu_shortcut(query: str) -> Optional[str]:
    """
    Search the user's and system's Start Menu for a .lnk whose name fuzzy-
    matches `query`. This is the most general strategy - it works for any
    installed app (WhatsApp, Zoom, random third-party software) without
    needing to hardcode its executable name, because virtually every
    Windows installer creates a Start Menu shortcut.
    """
    if sys.platform != "win32":
        return None
    search_dirs = [
        os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs"),
        os.path.join(os.environ.get("PROGRAMDATA", "C:\\ProgramData"), "Microsoft", "Windows", "Start Menu", "Programs"),
    ]
    candidates = {}
    for d in search_dirs:
        if not os.path.isdir(d):
            continue
        for root, _dirs, files in os.walk(d):
            for f in files:
                if f.lower().endswith(".lnk"):
                    candidates[f[:-4]] = os.path.join(root, f)
    if not candidates:
        return None
    best = _fuzzy_best_match(query, list(candidates.keys()), cutoff=0.45)
    return candidates.get(best) if best else None


@registry.register("open_application", "Launch an application by name (typo-tolerant)", RiskLevel.LOW_RISK,
                    params={"name": "application name, e.g. notepad, chrome, whatsapp"})
def open_application(name: str):
    errors = []

    if sys.platform == "win32":
        # Strategy 1: known-fast-path exe for very common apps.
        resolved_friendly = FRIENDLY_APPS.get(name.lower().strip())
        if resolved_friendly:
            try:
                os.startfile(resolved_friendly)  # noqa
                return {"launched": name, "resolved": resolved_friendly, "method": "friendly-map"}
            except Exception as e:
                errors.append(f"friendly-map({resolved_friendly}): {e}")

        # Strategy 2: fuzzy Start Menu shortcut search - handles typos and
        # any installed app not in the hardcoded maps (e.g. "whatsappmka").
        shortcut = _find_start_menu_shortcut(name)
        if shortcut:
            try:
                os.startfile(shortcut)  # noqa
                return {"launched": name, "resolved": os.path.basename(shortcut), "method": "start-menu-shortcut"}
            except Exception as e:
                errors.append(f"start-menu-shortcut({shortcut}): {e}")

        # Strategy 3: known Microsoft Store / UWP inbox apps via AUMID.
        store_key = _fuzzy_best_match(name, list(STORE_APP_AUMIDS.keys()), cutoff=0.55)
        if store_key:
            aumid = STORE_APP_AUMIDS[store_key]
            try:
                subprocess.Popen(["explorer.exe", f"shell:appsfolder\\{aumid}"])
                return {"launched": name, "resolved": store_key, "method": "store-aumid"}
            except Exception as e:
                errors.append(f"store-aumid({aumid}): {e}")

        # Strategy 4: last-resort raw attempts (handles exact exe names /
        # things already on PATH that weren't in any map above).
        target = name.strip()
        try:
            os.startfile(target)  # noqa
            return {"launched": name, "resolved": target, "method": "startfile-raw"}
        except Exception as e:
            errors.append(f"startfile-raw({target}): {e}")
        try:
            subprocess.Popen(f'start "" "{target}"', shell=True)
            return {"launched": name, "resolved": target, "method": "shell-start"}
        except Exception as e:
            errors.append(f"shell-start({target}): {e}")

        return {"error": f"Could not find or launch '{name}'. Tried: {'; '.join(errors)}. "
                          f"Make sure it's installed and appears in the Start Menu."}

    elif sys.platform == "darwin":
        try:
            subprocess.Popen(["open", "-a", name.strip()])
            return {"launched": name}
        except Exception as e:
            return {"error": f"Could not launch '{name}': {e}"}
    else:
        try:
            subprocess.Popen([name.strip().lower()])
            return {"launched": name}
        except Exception as e:
            return {"error": f"Could not launch '{name}': {e}"}


@registry.register("open_browser", "Open the default web browser (optionally to a URL)", RiskLevel.SAFE,
                    params={})
def open_browser(url: str = "https://www.google.com"):
    import webbrowser
    try:
        webbrowser.open(url)
        return {"opened": url}
    except Exception as e:
        return {"error": f"Could not open browser: {e}"}


@registry.register("open_file_explorer", "Open the file explorer / finder to a folder", RiskLevel.SAFE,
                    params={})
def open_file_explorer(path: str = ""):
    target = Path(path).expanduser() if path else Path.home()
    try:
        if sys.platform == "win32":
            subprocess.Popen(["explorer", str(target)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(target)])
        return {"opened": str(target)}
    except Exception as e:
        return {"error": f"Could not open file explorer: {e}"}


@registry.register("list_memories", "Show facts ZEUS currently remembers about you", RiskLevel.SAFE)
def list_memories():
    from memory.database import init_db
    from memory.memory_manager import all_memory_texts
    init_db()  # defensive: safe/idempotent even if already initialized
    facts = all_memory_texts()
    if not facts:
        return {"facts": "Nothing remembered yet. Say 'remember that ...' to teach me something."}
    return {"facts": facts}


@registry.register("remember_fact", "Remember a fact or preference the user states",
                    RiskLevel.SAFE, params={"fact": "the thing to remember"})
def remember_fact(fact: str):
    """Deterministic handler for "remember that ...".

    MemoryManager already captures this pattern when the message is
    recorded, so this tool goes through the SAME write path
    (memory.memory_manager.remember_text): an identical memory is not
    stored twice, a changed fact replaces the old one, and secrets are
    refused. Having it as a tool means the instruction works (and is
    confirmed) with no model call at all, which is what makes it reliable
    when spoken offline.
    """
    from memory.database import init_db
    from memory.memory_manager import remember_text
    cleaned = (fact or "").strip().rstrip(".")
    if not cleaned:
        return {"error": "Nothing to remember - tell me what to store."}
    init_db()
    outcome = remember_text(cleaned, source="user", explicit=True)
    if not outcome.get("stored"):
        return {"error": f"Not remembered - {outcome.get('reason')}"}
    result = {"remembered": outcome["text"]}
    if outcome.get("replaced"):
        result["replaced"] = outcome["replaced"]
    return result


@registry.register("forget_fact", "Forget a remembered fact by its text", RiskLevel.LOW_RISK,
                    params={"fact": "the remembered text to remove"})
def forget_fact(fact: str):
    from memory.database import init_db
    from memory.memory_manager import forget_text
    init_db()
    removed = forget_text(fact)
    if not removed:
        return {"error": f"I don't have anything remembered matching '{fact}'"}
    return {"forgotten": removed}


def _hermes_health() -> dict:
    """Backend, integrity, what the store holds by type - measured, not assumed."""
    from memory import hermes_context
    health = {"backend": None, "integrity": None, "memories": 0, "by_type": {}, "working_turns": 0}
    try:
        from memory.hermes_store import _load_hermes, get_service
        try:
            _load_hermes()
            health["integrity"] = "vendored Hermes matches its pinned SHA-256"
        except Exception as error:
            health["integrity"] = f"FAILED - {error} (the native store is used)"
        service = get_service()
        health["backend"] = service.backend_name
        stored = service.list(limit=500)
        health["memories"] = len(stored)
        for m in stored:
            kind = hermes_context.memory_type(m)
            health["by_type"][kind] = health["by_type"].get(kind, 0) + 1
    except Exception as error:
        health["error"] = str(error)[:200]
    health["working_turns"] = len(hermes_context.working_memory.recent(hermes_context.active_user_key()))
    health["authority"] = hermes_context.AUTHORITY
    return health


@registry.register("hermes_context", "Hermes context console: the last context packet sent with a request "
                   "(needs, relevant memory by type, this session's turns, the exact model block) and health",
                   RiskLevel.SAFE)
def hermes_context_tool():
    from memory import hermes_context
    return {"ok": True, "packet": hermes_context.last_packet(hermes_context.active_user_key()),
            "health": _hermes_health(), "types": list(hermes_context.TYPES), "tiers": list(hermes_context.TIERS)}


@registry.register("hermes_context_preview", "Build the Hermes context packet for a question WITHOUT asking a "
                   "model - shows what would be sent", RiskLevel.SAFE, params={"question": "the request to preview"})
def hermes_context_preview(question: str):
    from memory import recall
    built = recall.build(question or "")
    return {"ok": built.status != "unavailable", "status": built.status, "reason": built.reason,
            "packet": built.packet, "model_block": built.block, "summary": built.summary()}


_AUTOMATION_TOOLS = ("watch_folder", "stop_watching_folder", "remind_me", "cancel_reminder", "cancel_task",
                     "create_schedule", "update_schedule")


@registry.register("automation_overview", "Every background automation (folder watches, reminders, other background "
                   "work) with its id, status, runs and errors, plus the audit trail of automation actions",
                   RiskLevel.SAFE)
def automation_overview(limit: int = 60):
    import json as _json
    from core.security_manager import AUDIT_LOG
    from core.task_manager import task_manager
    tasks = []
    for task in task_manager.list_all(int(limit)):
        row = task.to_dict()
        text = row["description"].lower()
        row["kind"] = ("folder watch" if text.startswith("watching folder") else
                       "reminder" if text.startswith("reminder") else "background work")
        row["runs"] = len(row.get("steps") or [])
        row["last_run"] = (row.get("steps") or [""])[-1][:160]
        row["cancellable"] = row["status"] in ("QUEUED", "PLANNING", "RUNNING", "WAITING_FOR_USER")
        tasks.append(row)
    audit = []
    try:
        for line in (AUDIT_LOG.read_text(encoding="utf-8").strip().splitlines()[-600:] if AUDIT_LOG.exists() else []):
            try:
                entry = _json.loads(line)
            except ValueError:
                continue
            if any(name in str(entry.get("description", "")) for name in _AUTOMATION_TOOLS):
                audit.append({"at": entry.get("ts"), "action": entry.get("description"), "risk": entry.get("risk"),
                              "approved": entry.get("approved")})
    except OSError as error:
        audit = [{"action": f"audit log unreadable: {error}"}]
    return {"ok": True, "tasks": tasks, "active": sum(1 for t in tasks if t["cancellable"]),
            "audit": audit[-30:][::-1]}


@registry.register("show_audit_log", "Show recent security-audited actions", RiskLevel.SAFE)
def show_audit_log(lines: int = 10):
    import json as _json
    from core.security_manager import AUDIT_LOG as log_path
    if not log_path.exists():
        return {"entries": "No audit log yet."}
    try:
        raw_lines = log_path.read_text(encoding="utf-8").strip().splitlines()[-lines:]
        entries = []
        for line in raw_lines:
            try:
                e = _json.loads(line)
                entries.append(f"[{e['risk']}] {'✓' if e['approved'] else '✗'} {e['description']}")
            except (ValueError, KeyError):
                continue
        return {"entries": entries or "No audit log yet."}
    except Exception as e:
        return {"error": f"Could not read audit log: {e}"}


@registry.register("open_settings", "Open ZEUS's config.ini for editing", RiskLevel.LOW_RISK)
def open_settings():
    config_path = Path(__file__).resolve().parent.parent / "config.ini"
    try:
        if sys.platform == "win32":
            subprocess.Popen(["notepad.exe", str(config_path)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-t", str(config_path)])
        else:
            subprocess.Popen(["xdg-open", str(config_path)])
        return {"opened": str(config_path)}
    except Exception as e:
        return {"error": f"Could not open settings: {e}"}


@registry.register("close_application", "Close a running application by name", RiskLevel.MEDIUM_RISK,
                    params={"name": "application/process name, e.g. notepad, chrome"})
def close_application(name: str):
    from automation.application_control import close_application as _close
    closed = _close(name)
    if not closed:
        # Retry with a fuzzy match against actually-running process names,
        # so a typo'd or partial name ("whatsap") still finds "WhatsApp.exe".
        try:
            import psutil
            running = sorted({p.info["name"] for p in psutil.process_iter(["name"]) if p.info["name"]})
            best = _fuzzy_best_match(name, running, cutoff=0.5)
            if best:
                closed = _close(best)
                return {"closed": closed, "name": name, "matched_process": best}
        except ImportError:
            pass
    return {"closed": closed, "name": name}


@registry.register("list_windows", "List titles of currently open windows", RiskLevel.SAFE)
def list_windows_tool():
    from automation.application_control import list_windows as _list
    return {"windows": _list()}


@registry.register("focus_window", "Bring a window to the foreground by (partial) title", RiskLevel.LOW_RISK,
                    params={"title": "substring of the window title to focus"})
def focus_window_tool(title: str):
    from automation.application_control import focus_window as _focus
    ok = _focus(title)
    return {"focused": ok, "title": title}


@registry.register("list_running_apps", "List currently running processes", RiskLevel.SAFE)
def list_running_apps(limit: int = 30):
    try:
        import psutil
        names = sorted({p.info["name"] for p in psutil.process_iter(["name"]) if p.info["name"]})
        return {"count": len(names), "processes": names[:limit]}
    except ImportError:
        return {"error": "psutil not installed"}


# --------------------------------------------------------- SYSTEM ACTIONS --

@registry.register("lock_screen", "Lock the computer screen", RiskLevel.MEDIUM_RISK)
def lock_screen_tool():
    from automation.system_control import lock_screen as _lock
    ok = _lock()
    return {"locked": ok} if ok else {"error": "Lock screen not supported on this OS"}


@registry.register("shutdown_pc", "Shut down the computer (irreversible)", RiskLevel.CRITICAL,
                    timeout=10.0)
def shutdown_pc_tool(delay_seconds: int = 30):
    from automation.system_control import shutdown as _shutdown
    ok = _shutdown(delay_seconds)
    return {"shutdown_scheduled": ok, "delay_seconds": delay_seconds}


@registry.register("restart_pc", "Restart the computer (irreversible)", RiskLevel.CRITICAL,
                    timeout=10.0)
def restart_pc_tool(delay_seconds: int = 30):
    from automation.system_control import restart as _restart
    ok = _restart(delay_seconds)
    return {"restart_scheduled": ok, "delay_seconds": delay_seconds}


@registry.register("cancel_shutdown", "Cancel a pending shutdown/restart", RiskLevel.LOW_RISK)
def cancel_shutdown_tool():
    from automation.system_control import cancel_shutdown as _cancel
    ok = _cancel()
    return {"cancelled": ok}


@registry.register("media_control", "Control media playback / volume via system media keys",
                    RiskLevel.SAFE, params={"action": "play|pause|next|previous|volume_up|volume_down|mute"})
def media_control(action: str):
    try:
        import pyautogui
        key_map = {
            "play": "playpause", "pause": "playpause", "next": "nexttrack",
            "previous": "prevtrack", "volume_up": "volumeup",
            "volume_down": "volumedown", "mute": "volumemute",
        }
        key = key_map.get(action)
        if not key:
            return {"error": f"Unknown media action '{action}'"}
        pyautogui.press(key)
        return {"action": action, "sent": True}
    except ImportError:
        return {"error": "pyautogui not installed - cannot send media keys"}
    except Exception as e:
        return {"error": f"Media control failed: {e}"}


# ------------------------------------------------------------------- MISC --

@registry.register("take_screenshot", "Capture a screenshot of the screen", RiskLevel.LOW_RISK)
def take_screenshot():
    try:
        from PIL import ImageGrab
        # Screenshots are the operator's content, so they go with the other
        # generated files - not into logs/, which tools may not read.
        out_dir = _documents_root() / "Screenshots"
        out_dir.mkdir(parents=True, exist_ok=True)
        import time as _t
        path = out_dir / f"screenshot_{int(_t.time())}.png"
        img = ImageGrab.grab()
        img.save(path)
        return {"saved": str(path), "size": img.size}
    except Exception as e:
        return {"error": f"Screenshot failed: {e}"}


# --------------------------------------------------------------- WRITING ---

def _clean_generated_content(content: str) -> str:
    """
    LLMs frequently wrap output in markdown code fences or add a leading
    'Here is your essay:' preamble even when explicitly told not to. Strip
    that so the saved document contains only real content.
    """
    text = content.strip()
    text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
    text = re.sub(r"\n?```$", "", text)
    text = re.sub(r"^(here'?s?|sure,?|okay,?|certainly,?)\b[^\n]{0,80}:\s*\n+", "", text, flags=re.IGNORECASE)
    return text.strip()


@registry.register(
    "write_document",
    "Write a high-quality assignment/essay/report on a topic and save it as a professional Word document",
    RiskLevel.MEDIUM_RISK,
    params={"topic": "subject to write about"},
    timeout=300.0,
)
def write_document(topic: str, filename: str = "", word_count: int = 600):
    """Produce a polished .docx. Prefer REAL knowledge-pack data when the
    topic matches Nexus / ZEUS project content; otherwise use the model
    or a structured template. Always writes a file.
    """
    # 1) Knowledge pack first — real company / project facts
    try:
        from core.knowledge_docs import build_word_from_knowledge, pack_available
        if pack_available():
            grounded = build_word_from_knowledge(topic, filename=filename)
            if grounded and grounded.get("saved"):
                return grounded
    except Exception as e:
        logger.warning(f"knowledge-pack word build skipped: {e}")

    from core.ollama_client import ollama_client
    from core.model_router import model_router
    import datetime

    model = None
    content = None
    if ollama_client.is_available():
        try:
            model = model_router.route("reasoning")
        except Exception:
            model = None

    system_msg = (
        "You are a senior editor and subject-matter writer for professional reports. "
        "Write like a strong human analyst: concrete, specific, and useful. "
        "Rules you never break: "
        "(1) No filler ('it is important to note', 'in today's world', 'as we all know'). "
        "(2) No AI self-reference. "
        "(3) Every paragraph must add a fact, definition, example, comparison, or actionable point. "
        "(4) Prefer precise nouns and active verbs. "
        "(5) Short paragraphs of 3-5 sentences."
    )
    prompt = (
        f"Write a high-quality document of about {word_count} words on: \"{topic}\".\n\n"
        "Structure (use these ## headings, adapt titles to the topic):\n"
        "## Introduction\n"
        "## Background and Context\n"
        "## Key Concepts\n"
        "## Analysis and Evidence\n"
        "## Practical Implications\n"
        "## Conclusion\n\n"
        "Quality bar:\n"
        "- Include at least one concrete example or case in the Analysis section.\n"
        "- In Practical Implications, give 3-5 numbered action points a reader can use.\n"
        "- Conclusion must synthesise, not repeat the introduction.\n"
        "- Formal, readable tone for college or workplace.\n"
        "- Output ONLY the document body with ## headings. No preamble.\n"
    )


    # Prefer local knowledge pack when the topic touches Nexus / ZEUS project data
    _kb_context = ""
    try:
        _topic_l = (topic or "").lower()
        if any(k in _topic_l for k in (
            "nexus", "company history", "precision", "jarvis project", "sih",
            "deal", "fy19", "fy20", "fy25", "p&l", "machinery", "staff cost",
            "kpi", "inventory", "task tracker", "model inventory"
        )):
            sk = search_knowledge(topic, top_k=5)
            if isinstance(sk, dict) and sk.get("matches"):
                parts = []
                for i, m in enumerate(sk["matches"][:5], 1):
                    parts.append(f"[{i}] {m.get('source')}: {m.get('text')}")
                _kb_context = "\n".join(parts)
    except Exception:
        _kb_context = ""

    if model is not None:
        _user = prompt
        if _kb_context:
            _user = (
                prompt
                + "\n\nUse the following LOCAL KNOWLEDGE PACK evidence when relevant. "
                "Prefer these facts over inventing numbers:\n"
                + _kb_context
            )
        content = ollama_client.chat(model, [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": _user},
        ], retries=0, timeout=generation_timeout(220.0))

    # No model, no document. A generic template dressed up as a generated
    # report is exactly the kind of fake output ZEUS must never produce - the
    # operator is told what went wrong and how to fix it instead.
    if model is None:
        return {"error": "Ollama is not running or no writing model is installed, so no "
                         "document was generated. Start it with 'ollama serve' and pull a "
                         "model (run STEP 1 : INSTALL, or: ollama pull qwen3:8b)."}
    if content and "could not reach the local AI model" in content:
        return {"error": f"Ollama did not answer (not running, or the model {model} took too "
                         f"long), so no document was generated. Start ZEUS with START.bat, "
                         f"or check the model with VERIFY_MODELS.bat."}
    if content and content.strip().startswith("[Model '") and "not found" in content:
        return {"error": f"Ollama does not have the model {model}, so no document was "
                         f"generated. Run STEP 1 : INSTALL, or: ollama pull {model}"}
    if not content or content.strip().startswith("["):
        return {"error": f"The local model ({model}) did not return any usable text, so no "
                         f"document was generated. {(content or '').strip()[:160]}".strip()}
    if len(content.split()) < 40:
        return {"error": f"The local model ({model}) returned too little content "
                         f"({len(content.split())} words) to be a real document, so nothing "
                         f"was saved."}

    content = _clean_generated_content(content)

    docs_dir = _documents_root()
    docs_dir.mkdir(parents=True, exist_ok=True)
    safe_name = filename.strip() or re.sub(r"[^\w\s-]", "", topic).strip().replace(" ", "_")[:50] or "document"

    def _unique_path(base_dir: Path, base_name: str, ext: str) -> Path:
        candidate = base_dir / f"{base_name}.{ext}"
        if not candidate.exists():
            return candidate
        import time as _t
        return base_dir / f"{base_name}_{int(_t.time())}.{ext}"

    def _parse_sections(text: str):
        parts = re.split(r"^##\s+(.+)$", text, flags=re.MULTILINE)
        if len(parts) < 3:
            return [(None, text)]
        sections = []
        for i in range(1, len(parts), 2):
            heading = parts[i].strip()
            body = parts[i + 1].strip() if i + 1 < len(parts) else ""
            sections.append((heading, body))
        return sections

    try:
        from core.rich_docs import polish_word_document
        from core import office

        docs_dir = _documents_root()
        docs_dir.mkdir(parents=True, exist_ok=True)
        safe_name = filename.strip() or re.sub(r"[^\w\s-]", "", topic).strip().replace(" ", "_")[:50] or "document"
        out_path = docs_dir / f"{safe_name}.docx"
        if out_path.exists():
            import time as _t
            out_path = docs_dir / f"{safe_name}_{int(_t.time())}.docx"

        sections = _parse_sections(content)
        callouts = []
        for heading, body in sections[:3]:
            if heading:
                callouts.append(str(heading)[:40])
        polish_word_document(topic, sections, out_path, callouts=callouts or None)
        return office.finish(out_path, "docx",
                             extra={"words": len(content.split()), "topic": topic, "quality": "rich"})
    except Exception as e:
        # Last-resort plain text so the user still gets a file
        docs_dir = _documents_root()
        docs_dir.mkdir(parents=True, exist_ok=True)
        safe_name = filename.strip() or re.sub(r"[^\w\s-]", "", topic).strip().replace(" ", "_")[:50] or "document"
        out_path = docs_dir / f"{safe_name}.txt"
        out_path.write_text(content, encoding="utf-8")
        return {"saved": str(out_path), "words": len(content.split()), "format": "txt",
                "note": f"Rich Word build failed ({e}); saved text instead. pip install python-docx"}





# ------------------------------------------------------------ KEYBOARD/MOUSE -
# Real Windows UI automation. Prefers deterministic APIs (typing into the
# currently-focused control, named hotkeys) over blind coordinate clicking,
# per the security design - coordinate clicks are gated at MEDIUM_RISK and
# require the caller to have already confirmed the target on-screen.

@registry.register("type_text", "Type text into whatever window currently has focus", RiskLevel.MEDIUM_RISK,
                    params={"text": "the text to type"}, timeout=30.0)
def type_text(text: str, interval: float = 0.02):
    try:
        import pyautogui
        pyautogui.write(text, interval=interval)
        return {"typed_chars": len(text)}
    except ImportError:
        return {"error": "pyautogui not installed - run: pip install pyautogui"}
    except Exception as e:
        return {"error": f"Typing failed: {e}"}


@registry.register("press_key", "Press a single keyboard key (e.g. enter, tab, esc, f5)", RiskLevel.LOW_RISK,
                    params={"key": "key name, e.g. enter, tab, escape, backspace"})
def press_key(key: str):
    try:
        import pyautogui
        pyautogui.press(key.lower())
        return {"pressed": key}
    except ImportError:
        return {"error": "pyautogui not installed - run: pip install pyautogui"}
    except Exception as e:
        return {"error": f"Key press failed: {e}"}


@registry.register("hotkey", "Press a keyboard shortcut combo (e.g. ctrl+s, ctrl+shift+esc)", RiskLevel.LOW_RISK,
                    params={"combo": "keys joined by '+', e.g. 'ctrl+s'"})
def hotkey(combo: str):
    try:
        import pyautogui
        keys = [k.strip().lower() for k in combo.split("+") if k.strip()]
        if not keys:
            return {"error": "No keys given"}
        pyautogui.hotkey(*keys)
        return {"pressed": combo}
    except ImportError:
        return {"error": "pyautogui not installed - run: pip install pyautogui"}
    except Exception as e:
        return {"error": f"Hotkey failed: {e}"}


@registry.register("mouse_move", "Move the mouse cursor to screen coordinates", RiskLevel.LOW_RISK,
                    params={"x": "x coordinate", "y": "y coordinate"})
def mouse_move(x: int, y: int, duration: float = 0.2):
    try:
        import pyautogui
        pyautogui.moveTo(x, y, duration=duration)
        return {"moved_to": [x, y]}
    except ImportError:
        return {"error": "pyautogui not installed - run: pip install pyautogui"}
    except Exception as e:
        return {"error": f"Mouse move failed: {e}"}


@registry.register("mouse_click", "Click the mouse at screen coordinates (or current position)",
                    RiskLevel.MEDIUM_RISK, params={})
def mouse_click(x: Optional[int] = None, y: Optional[int] = None, button: str = "left", clicks: int = 1):
    try:
        import pyautogui
        if x is not None and y is not None:
            pyautogui.click(x=x, y=y, button=button, clicks=clicks)
        else:
            pyautogui.click(button=button, clicks=clicks)
        return {"clicked": [x, y] if x is not None else "current position", "button": button}
    except ImportError:
        return {"error": "pyautogui not installed - run: pip install pyautogui"}
    except Exception as e:
        return {"error": f"Mouse click failed: {e}"}


@registry.register("get_clipboard", "Read the current clipboard text", RiskLevel.SAFE)
def get_clipboard():
    try:
        import pyperclip
        return {"clipboard": pyperclip.paste()}
    except ImportError:
        return {"error": "pyperclip not installed - run: pip install pyperclip"}
    except Exception as e:
        return {"error": f"Clipboard read failed: {e}"}


@registry.register("set_clipboard", "Copy text to the clipboard", RiskLevel.LOW_RISK,
                    params={"text": "text to copy"})
def set_clipboard(text: str):
    try:
        import pyperclip
        pyperclip.copy(text)
        return {"copied_chars": len(text)}
    except ImportError:
        return {"error": "pyperclip not installed - run: pip install pyperclip"}
    except Exception as e:
        return {"error": f"Clipboard write failed: {e}"}


# ----------------------------------------------------------- WINDOW CONTROL -

@registry.register("minimize_window", "Minimize a window by (partial) title", RiskLevel.LOW_RISK,
                    params={"title": "substring of the window title"})
def minimize_window(title: str):
    try:
        import pygetwindow as gw
        for w in gw.getAllWindows():
            if title.lower() in w.title.lower():
                w.minimize()
                return {"minimized": w.title}
        return {"error": f"No window matching '{title}' found"}
    except ImportError:
        return {"error": "pygetwindow not installed - run: pip install pygetwindow"}
    except Exception as e:
        return {"error": f"Minimize failed: {e}"}


@registry.register("maximize_window", "Maximize a window by (partial) title", RiskLevel.LOW_RISK,
                    params={"title": "substring of the window title"})
def maximize_window(title: str):
    try:
        import pygetwindow as gw
        for w in gw.getAllWindows():
            if title.lower() in w.title.lower():
                w.maximize()
                return {"maximized": w.title}
        return {"error": f"No window matching '{title}' found"}
    except ImportError:
        return {"error": "pygetwindow not installed - run: pip install pygetwindow"}
    except Exception as e:
        return {"error": f"Maximize failed: {e}"}


@registry.register("close_window", "Close a window by (partial) title", RiskLevel.MEDIUM_RISK,
                    params={"title": "substring of the window title"})
def close_window(title: str):
    try:
        import pygetwindow as gw
        for w in gw.getAllWindows():
            if title.lower() in w.title.lower():
                w.close()
                return {"closed": w.title}
        return {"error": f"No window matching '{title}' found"}
    except ImportError:
        return {"error": "pygetwindow not installed - run: pip install pygetwindow"}
    except Exception as e:
        return {"error": f"Close failed: {e}"}


# ------------------------------------------------------------------ VISION -

@registry.register("read_screen", "Take a screenshot and extract any visible text via OCR", RiskLevel.SAFE,
                    timeout=45.0)
def read_screen():
    try:
        from PIL import ImageGrab
        img = ImageGrab.grab()
    except Exception as e:
        return {"error": f"Screenshot failed: {e}"}

    # The same layered local OCR the document pipeline uses (RapidOCR first,
    # then PaddleOCR / Tesseract / EasyOCR) - one implementation, not two.
    try:
        from industrial.ocr_engines import run_ocr
        result = run_ocr(img, min_chars=1)
    except Exception as e:
        return {"error": f"OCR failed: {e}"}
    if not result.ok and result.error:
        return {"error": f"OCR failed: {result.error}"}
    lines = [line.text for line in result.lines if line.confidence >= 0.4 and line.text]
    if not lines:
        return {"text": "(no readable text detected on screen)", "engine": result.engine}
    return {"text": "\n".join(lines), "engine": result.engine,
            "confidence": round(result.confidence, 3)}


_ocr_reader_cache = {}


def _get_ocr_reader():
    # EasyOCR model loading is expensive (~seconds) - cache the reader
    # instance across calls instead of reloading it every time.
    if "reader" not in _ocr_reader_cache:
        import easyocr
        # download_enabled=False: EasyOCR otherwise fetches its weights from the
        # internet the first time it runs. A missing model is an error, never a
        # download.
        _ocr_reader_cache["reader"] = easyocr.Reader(["en"], gpu=False, verbose=False,
                                                     download_enabled=False)
    return _ocr_reader_cache["reader"]


# ---------------------------------------------------------- COMPOUND TASKS -

@registry.register(
    "open_and_type",
    "Open an application and type text into it once it launches - a real two-step "
    "automation, not just a single command",
    RiskLevel.MEDIUM_RISK,
    params={"app": "application to open", "text": "text to type once it's open"},
    timeout=20.0,
)
def open_and_type(app: str, text: str, wait_seconds: float = 1.5):
    open_result = open_application(app)
    if "error" in open_result:
        return {"error": f"Could not open '{app}': {open_result['error']}"}

    import time as _t
    _t.sleep(wait_seconds)  # give the app time to actually gain focus

    try:
        import pyautogui
        pyautogui.write(text, interval=0.02)
        return {"opened": app, "typed_chars": len(text)}
    except ImportError:
        return {"opened": app, "error": "Opened successfully, but pyautogui isn't installed "
                                          "so text could not be typed. Run: pip install pyautogui"}
    except Exception as e:
        return {"opened": app, "error": f"Opened successfully, but typing failed: {e}"}


# --------------------------------------------------------------- BACKGROUND -
# Real background automation: folder watching and scheduled reminders.
# Every background task is tracked in task_manager (visible, cancellable)
# and surfaces its results through background_events so the dashboard can
# show it proactively - this is what makes ZEUS feel like it's actually
# working alongside you instead of only responding when spoken to.

_active_watchers = {}   # task_id -> watchdog Observer
_active_reminders = {}  # task_id -> threading.Timer


@registry.register(
    "watch_folder",
    "Watch a folder in the background and notify when new files appear",
    RiskLevel.LOW_RISK,
    params={"path": "folder to watch"},
    timeout=10.0,  # only the *setup* is timed - the watch itself runs indefinitely in the background
)
def watch_folder(path: str, pattern: str = "*"):
    from core.task_manager import task_manager, TaskStatus
    from core.background_events import push_event

    target = Path(path).expanduser()
    if not security_manager.validate_path(str(target)):
        return {"error": f"Access to '{path}' is blocked by security policy"}
    if not target.is_dir():
        return {"error": f"'{path}' is not a valid directory"}

    try:
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler
    except ImportError:
        return {"error": "watchdog not installed - run: pip install watchdog"}

    task = task_manager.create(f"Watching folder: {target}")
    task_manager.update(task.id, status=TaskStatus.RUNNING)

    class Handler(FileSystemEventHandler):
        def on_created(self, event):
            if event.is_directory:
                return
            name = Path(event.src_path).name
            if pattern != "*" and not fnmatch.fnmatch(name.lower(), pattern.lower()):
                return
            msg = f"New file detected in {target.name}: {name}"
            push_event(msg)
            task_manager.add_step(task.id, msg)

    observer = Observer()
    observer.schedule(Handler(), str(target), recursive=False)
    observer.start()
    _active_watchers[task.id] = observer

    return {"watching": str(target), "task_id": task.id,
            "note": f"Say 'stop watching {target.name}' or use task #{task.id} to cancel."}


@registry.register("stop_watching_folder", "Stop a background folder watch", RiskLevel.SAFE,
                    params={"path": "folder path or name that was being watched"})
def stop_watching_folder(path: str):
    from core.task_manager import task_manager, TaskStatus

    match_name = path.strip().lower()
    for task_id, observer in list(_active_watchers.items()):
        task = task_manager.get(task_id)
        if task and match_name in task.description.lower():
            observer.stop()
            observer.join(timeout=2)
            del _active_watchers[task_id]
            task_manager.update(task_id, status=TaskStatus.CANCELLED)
            return {"stopped": task.description}
    return {"error": f"No active folder watch matching '{path}'"}


@registry.register(
    "remind_me",
    "Set a reminder that fires after a delay, in minutes",
    RiskLevel.SAFE,
    params={"message": "what to be reminded about", "minutes": "delay in minutes"},
)
def remind_me(message: str, minutes: float):
    from core.task_manager import task_manager, TaskStatus
    from core.background_events import push_event
    import threading as _threading

    task = task_manager.create(f"Reminder in {minutes}min: {message}")
    task_manager.update(task.id, status=TaskStatus.WAITING_FOR_USER)

    def fire():
        push_event(f"⏰ Reminder: {message}")
        task_manager.update(task.id, status=TaskStatus.COMPLETED, progress=1.0)
        _active_reminders.pop(task.id, None)

    timer = _threading.Timer(max(0.0, minutes) * 60, fire)
    timer.daemon = True
    timer.start()
    _active_reminders[task.id] = timer

    return {"reminder_set": message, "in_minutes": minutes, "task_id": task.id}


@registry.register("cancel_reminder", "Cancel a pending reminder by its task ID", RiskLevel.SAFE,
                    params={"task_id": "the reminder's task ID"})
def cancel_reminder(task_id: int):
    from core.task_manager import task_manager
    timer = _active_reminders.pop(int(task_id), None)
    if timer:
        timer.cancel()
        task_manager.cancel(int(task_id))
        return {"cancelled": task_id}
    return {"error": f"No active reminder with task ID {task_id}"}


@registry.register("cancel_task", "Cancel any active task by its ID (works for reminders, "
                                   "folder watches, and in-progress multi-step tasks)",
                    RiskLevel.SAFE, params={"task_id": "the task's numeric ID"})
def cancel_task(task_id: int):
    """Unified cancellation - correctly tears down whatever background
    resource the task actually holds (a watchdog Observer, a Timer) rather
    than just flipping its status and leaving the real thread/timer running."""
    from core.task_manager import task_manager
    tid = int(task_id)

    if tid in _active_watchers:
        observer = _active_watchers.pop(tid)
        observer.stop()
        observer.join(timeout=2)
        task_manager.cancel(tid)
        return {"cancelled": tid, "type": "folder_watch"}

    if tid in _active_reminders:
        timer = _active_reminders.pop(tid)
        timer.cancel()
        task_manager.cancel(tid)
        return {"cancelled": tid, "type": "reminder"}

    task = task_manager.get(tid)
    if task is None:
        return {"error": f"No task with ID {tid}"}
    task_manager.cancel(tid)
    return {"cancelled": tid, "type": "task"}


# ---------------------------------------------------------------- OFFICE ---
# Real PPTX/XLSX generation, driven by the local model, following the same
# pattern proven with write_document: single attempt, generous bounded
# timeout, content cleaning, auto-open on success.

def _generate_structured_json(prompt: str, system: str) -> Optional[list]:
    """Ask the model for a JSON array and parse it robustly - LLMs frequently
    wrap JSON in markdown fences or add commentary despite instructions not to."""
    result = _generate_structured_json_any(prompt, system, shape="array", timeout=180.0)
    return result if isinstance(result, list) else None


def generation_timeout(requested: float) -> float:
    """How long a generation call may take on THIS machine.

    A hard-coded 50 seconds is a hidden hardware assumption. On a CPU-only
    laptop the local model runs at roughly 5 tokens/second, so a 250-token
    answer needs 50s and the request is cancelled at the exact moment it was
    about to succeed - the Ollama log shows it as `500 | 50.0059s POST
    /api/chat`. The request is then retried and fails identically.

    So the floor comes from config (`[ollama] generation_timeout`), and the
    per-call figure can only raise it, never lower it below that floor.
    """
    floor = 240.0
    try:
        import configparser
        parser = configparser.ConfigParser()
        parser.read(Path(__file__).resolve().parent.parent / "config.ini")
        if parser.has_option("ollama", "generation_timeout"):
            floor = float(parser.get("ollama", "generation_timeout"))
    except Exception:
        pass
    return max(float(requested or 0), floor)


def _generate_structured_json_any(prompt: str, system: str, shape: str = "object",
                                   timeout: float = 180.0, slot: str = "reasoning"):
    """Like _generate_structured_json but for a JSON object ({...}) response,
    or either shape when shape='any'. Returns None on failure/unavailability.

    `slot` picks the model. Structured output (slide content, spreadsheet
    headers, document outlines) should ask for 'json_structured' - a 3B model
    that respects a schema, rather than the 7B reasoning model. It is roughly
    twice as fast on CPU and better at returning clean JSON, which is the
    whole job here.
    """
    from core.ollama_client import ollama_client
    from core.model_router import model_router

    if not ollama_client.is_available():
        return None
    model = model_router.route(slot)
    raw = ollama_client.chat(model, [
        {"role": "system", "content": system},
        {"role": "user", "content": prompt},
    ], retries=0, timeout=generation_timeout(timeout),
        task_type=slot, purpose="Structured content generation")

    if not raw:
        return None
    cleaned = _clean_generated_content(raw)

    if shape == "array":
        match = re.search(r"\[.*\]", cleaned, re.DOTALL)
    elif shape == "object":
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    else:
        match = re.search(r"(\{.*\}|\[.*\])", cleaned, re.DOTALL)
    if match:
        cleaned = match.group(0)

    try:
        import json
        return json.loads(cleaned)
    except (ValueError, TypeError):
        logger.warning(f"Could not parse structured JSON from model output: {cleaned[:200]}")
        return None


# NOTE: this helper used to carry a `@registry.register("create_presentation")`
# decorator, which registered the tool name against a PRIVATE function with a
# different signature (no `filename`, returns a list of titles rather than a
# saved deck). The real tool is registered further down under the same name,
# and registration is last-write-wins, so behaviour was correct only because
# of the order the two appear in this file. Any reordering would have silently
# swapped the tool for its own helper. The decorator is removed; the helper is
# just a helper.
def _generate_slide_outline(topic: str, slide_count: int) -> Optional[List[str]]:
    result = _generate_structured_json_any(
        f"Design a {slide_count}-slide professional presentation on \"{topic}\".\n"
        "The first string is the deck title (short, confident).\n"
        "Remaining titles should follow a logical arc: overview → concepts → analysis → "
        "applications → challenges → summary. Use specific titles, not generic ones like "
        "'Slide 2' or 'Content'.\n"
        "Return ONLY a JSON array of strings.",
        "You are a senior presentation designer. Output only a JSON array of title strings.",
        shape="array", timeout=50.0, slot="json_structured",
    )
    return result if isinstance(result, list) and result else None


# Text that is an unfilled template rather than content. A small model asked
# for JSON will sometimes echo the example back verbatim - a real deck was
# shipped with "<point 1> <point 2> <point 3>" on a slide because nothing
# checked. Anything matching this is dropped, never written to a file.
_PLACEHOLDER_TEXT = re.compile(
    r"^\s*[<\[][^>\]]{0,60}[>\]]\s*$"                      # <point 1>, [your text]
    r"|^\s*(?:point|bullet|item|topic|title|heading|text|content|detail)"
    r"\s*[-#]?\s*\d*\s*$"                                  # "point 1", "bullet"
    r"|^\s*(?:tbd|todo|n/?a|xxx+|\.\.\.|lorem ipsum.*)\s*$"
    r"|your\s+(?:text|content|point|bullet)\s+here"
    r"|insert\s+(?:text|content|your)",
    re.IGNORECASE)


def _is_placeholder(text: Any) -> bool:
    """True when this is template scaffolding, not something a person wrote."""
    if text is None:
        return True
    value = str(text).strip()
    # Two characters is the defensible floor: "5%" and "pH" are real content,
    # a single stray letter on a slide is not.
    if len(value) < 2:
        return True
    return bool(_PLACEHOLDER_TEXT.search(value))


def _clean_points(points: Any) -> List[str]:
    """Drop placeholder entries and duplicates, keeping order."""
    cleaned: List[str] = []
    for point in points or []:
        text = str(point).strip()
        if _is_placeholder(text):
            continue
        if text.lower() not in {existing.lower() for existing in cleaned}:
            cleaned.append(text[:400])
    return cleaned


def _generate_single_slide_content(title: str, topic: str) -> Optional[dict]:
    """One slide's content per call - small, fast, and a failure here only
    loses one slide instead of the whole deck (same pattern as the schedule
    planner's per-day generation).

    The prompt describes the shape in words rather than showing an example
    object full of angle-bracket placeholders: a 3B model asked to follow
    `{"bullets": ["<point 1>", ...]}` will sometimes return exactly that.
    Whatever comes back is filtered anyway - a slide whose every point is
    scaffolding counts as a failed slide, not as a slide.
    """
    for attempt in range(2):
        result = _generate_structured_json_any(
            f"Write content for ONE professional presentation slide.\n"
            f"Deck topic: \"{topic}\"\n"
            f"Slide title: \"{title}\"\n\n"
            f"Return a JSON object with exactly three keys:\n"
            f"  key_point - one sharp sentence: the single message of this slide\n"
            f"  bullets   - array of 3 to 5 strings. Each bullet must be specific "
            f"(include a fact, number, named step, or concrete example). "
            f"No vague lines like 'important points' or 'key benefits'.\n"
            f"  notes     - 1-2 sentences of speaker notes with a talking tip\n"
            f"Respond with ONLY the JSON object.",
            "You are a professional presentation writer. Output only valid JSON.",
            shape="object", timeout=50.0, slot="json_structured",
        )
        if not result or "bullets" not in result:
            continue
        bullets = _clean_points(result.get("bullets"))
        if not bullets:
            logger.warning(f"slide '{title}': the model returned only placeholder "
                           f"text (attempt {attempt + 1})")
            continue
        key_point = result.get("key_point") or ""
        return {
            "bullets": bullets,
            "key_point": "" if _is_placeholder(key_point) else str(key_point)[:300],
            "notes": "" if _is_placeholder(result.get("notes")) else str(result.get("notes"))[:500],
        }
    return None


def _plant_deliverable(topic: str, filename: str, kind: str):
    """Build a deck or workbook from the plant database, or return None.

    None means "this request is not about plant data", and the caller carries
    on with the ordinary model-generated path. That fallback is the important
    part: this must never intercept a request it cannot ground, because a
    document of invented plant figures is worse than a generic one.
    """
    try:
        from industrial import plant_analytics
    except ImportError:
        return None
    try:
        analysis = plant_analytics.analysis_for(topic)
    except Exception as error:
        logger.warning(f"plant analysis failed for {topic!r}: {error}")
        return None
    if analysis is None:
        return None

    docs_dir = _documents_root()
    docs_dir.mkdir(parents=True, exist_ok=True)
    stem = (filename.strip()
            or re.sub(r"[^\w\s-]", "", analysis.title).strip().replace(" ", "_")[:50]
            or "report")
    out_path = docs_dir / f"{stem}.{kind}"
    if out_path.exists():
        out_path = docs_dir / f"{stem}_{int(time.time())}.{kind}"

    try:
        if kind == "pptx":
            from industrial.deck_builder import build_deck
            result = build_deck(analysis, out_path)
        else:
            from industrial.workbook_builder import build_workbook
            result = build_workbook(analysis, out_path)
    except Exception as error:
        logger.warning(f"grounded {kind} build failed: {error}")
        return None

    result["topic"] = topic
    result["grounded_in"] = analysis.source_data
    result["demo_data"] = analysis.contains_demo_data
    result["source"] = "plant database"
    return result



@registry.register(
    "create_presentation",
    "Generate a PowerPoint presentation on a topic using the local AI model",
    RiskLevel.MEDIUM_RISK,
    params={"topic": "subject of the presentation"},
    timeout=400.0,  # slide_count small calls, generously bounded
)
def create_presentation(topic: str, slide_count: int = 6, filename: str = ""):
    """Build a deck on a topic, rendered through the SAME design system as
    every other ZEUS presentation.

    This used to be a second, separate generator: python-pptx's default 4:3
    template, title-and-bullets placeholders, no charts, no cards, no layout
    of its own. That is why a plant deck looked professional and a deck about
    any other subject looked like a 2003 school project. There is now one
    renderer (`industrial.deck_builder`) and one visual language.

    The per-slide call pattern is kept exactly as it was, because it is the
    right design: one small outline call, then one small call per slide, so a
    single bad slide costs one slide rather than the whole deck.

    Plant topics take a different path entirely. If the request is about
    something in the plant database - a tagged asset, maintenance, safety,
    production, stock - the deck is built from THOSE ROWS rather than from the
    model's idea of what a refinery is like. That path needs no model at all,
    finishes in under a second, and every figure on the slides can be traced
    to a record.
    """
    grounded = _plant_deliverable(topic, filename, "pptx")
    if grounded is not None:
        return grounded

    from core.ollama_client import ollama_client

    try:
        from industrial.deck_builder import build_topic_deck
    except ImportError as e:
        return {"error": f"presentation renderer unavailable: {e}"}

    titles = _generate_slide_outline(topic, slide_count)
    if not titles:
        running = ollama_client.is_available()
        return {"error": ("The local model did not produce a slide outline, so no presentation "
                          "was generated." if running else
                          "Ollama is not running, so no presentation was generated. Start it with "
                          "'ollama serve' (and run STEP 1 : INSTALL if no model is installed).")}

    deck_title = str(titles[0])[:120]
    sections = []
    failed_slides = []
    for title in titles[1:]:
        content = _generate_single_slide_content(title, topic)
        if not content:
            # One bad slide costs one slide - it is left out and reported,
            # never replaced with generic bullets.
            failed_slides.append(title)
            continue
        sections.append({
            "title": str(title)[:120],
            "section": "Topic",
            "key_point": content.get("key_point", ""),
            "bullets": content.get("bullets", []),
            "notes": content.get("notes", ""),
        })

    docs_dir = _documents_root()
    docs_dir.mkdir(parents=True, exist_ok=True)
    safe_name = filename.strip() or re.sub(r"[^\w\s-]", "", topic).strip().replace(" ", "_")[:50] or "presentation"
    out_path = docs_dir / f"{safe_name}.pptx"
    if out_path.exists():
        out_path = docs_dir / f"{safe_name}_{int(time.time())}.pptx"

    if not sections:
        return {"error": "The local model did not produce content for any slide, so no "
                         "presentation was generated.", "slides_failed": failed_slides}
    result = build_topic_deck(deck_title, sections, out_path, subtitle=topic)
    if "error" in result:
        return result
    result["topic"] = topic
    result["slides_built"] = 1 + len(sections)
    if failed_slides:
        result["slides_failed"] = failed_slides
    return result


def _generate_spreadsheet_headers(topic: str) -> Optional[List[str]]:
    result = _generate_structured_json_any(
        f"Design column headers for a professional spreadsheet about \"{topic}\".\n"
        "Include 4-7 useful columns (mix of labels and measurable values where relevant).\n"
        "Use clear business/academic names (e.g. Rank, Name, Value, Category, Year).\n"
        "Return ONLY a JSON array of short header strings.",
        "Output only a JSON array of strings.", shape="array", timeout=40.0, slot="json_structured",
    )
    return result if isinstance(result, list) and result else None


def _generate_spreadsheet_rows(topic: str, headers: List[str], count: int) -> Optional[List[dict]]:
    """One small batch of rows per call - a 20-row single call was the
    unreliable old design; batching keeps each call's JSON small."""
    for attempt in range(2):
        result = _generate_structured_json_any(
            f"Generate {count} realistic data rows for a spreadsheet about \"{topic}\".\n"
            f"Use EXACTLY these column headers as JSON keys: {headers}\n"
            f"Rules: use real-looking values; plain numbers only (no $ or commas); "
            f"vary the data; no placeholder text like 'N/A' or 'example'.\n"
            f"Return ONLY a JSON array of objects.",
            "Output only a JSON array of objects with the given keys.", shape="array",
            timeout=60.0, slot="json_structured",
        )
        if result and isinstance(result, list) and all(isinstance(r, dict) for r in result):
            return result
    return None


@registry.register(
    "create_spreadsheet",
    "Generate an Excel spreadsheet with structured data on a topic using the local AI model",
    RiskLevel.MEDIUM_RISK,
    params={"topic": "what data to generate, e.g. 'top 10 planets with size and distance'"},
    timeout=300.0,
)
def create_spreadsheet(topic: str, filename: str = ""):
    # A plant topic is built from the database, with formulas, totals and
    # charts over real rows, rather than from model-invented data.
    grounded = _plant_deliverable(topic, filename, "xlsx")
    if grounded is not None:
        return grounded

    # ZEUS knowledge pack (Nexus Precision, project trackers, etc.)
    try:
        from core.knowledge_docs import build_excel_from_knowledge, pack_available
        if pack_available():
            kb = build_excel_from_knowledge(topic, filename=filename)
            if kb and kb.get("saved"):
                return kb
    except Exception as e:
        logger.warning(f"knowledge-pack excel build skipped: {e}")

    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment
        from openpyxl.utils import get_column_letter
        from openpyxl.chart import BarChart, Reference
    except ImportError:
        return {"error": "openpyxl not installed - run: pip install openpyxl"}

    from core.ollama_client import ollama_client

    headers = _generate_spreadsheet_headers(topic) if ollama_client.is_available() else None
    rows: List[dict] = []
    if headers:
        batch_size = 5
        target_rows = 10
        max_batch_attempts = 4
        for _ in range(max_batch_attempts):
            if len(rows) >= target_rows:
                break
            remaining = target_rows - len(rows)
            batch = _generate_spreadsheet_rows(topic, headers, min(batch_size, remaining))
            if not batch:
                break
            rows.extend(batch)

    # No invented "Sample row" data: without real rows there is no workbook.
    if not headers:
        running = ollama_client.is_available()
        return {"error": ("The local model did not produce column headers, so no spreadsheet "
                          "was generated." if running else
                          "Ollama is not running, so no spreadsheet was generated. Start it with "
                          "'ollama serve' (and run STEP 1 : INSTALL if no model is installed).")}
    if not rows:
        return {"error": "The local model produced headers but no data rows, so no spreadsheet "
                         "was generated.", "columns": headers}

    # Rich multi-sheet workbook: Cover + Data (charts) + Summary
    from core.rich_docs import polish_spreadsheet
    from core import office

    docs_dir = _documents_root()
    docs_dir.mkdir(parents=True, exist_ok=True)
    safe_name = (filename.strip() or re.sub(r"[^\w\s-]", "", topic).strip().replace(" ", "_")[:50]
                 or "spreadsheet")
    out_path = docs_dir / f"{safe_name}.xlsx"
    if out_path.exists():
        out_path = docs_dir / f"{safe_name}_{int(time.time())}.xlsx"

    polish_spreadsheet(topic, headers, rows, out_path)
    return office.finish(out_path, "xlsx",
                         extra={"rows": len(rows), "columns": headers, "topic": topic,
                                "quality": "rich", "sheets": ["Cover", "Data", "Summary"]})


# ------------------------------------------------------------ CONVERSION ---
# Real format conversion for the pairs we can actually support well. "Any
# format to any format" isn't honest for a local tool - image formats,
# docx/txt/pdf, and xlsx/csv are the pairs with reliable libraries. Anything
# outside that reports clearly rather than pretending to succeed.

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tiff", ".ico"}


def _convert_image(src: Path, dst: Path) -> dict:
    from PIL import Image
    img = Image.open(src)
    if dst.suffix.lower() in (".jpg", ".jpeg") and img.mode in ("RGBA", "P"):
        img = img.convert("RGB")  # JPEG doesn't support alpha/palette
    img.save(dst)
    return {"converted": str(src), "to": str(dst)}


def _convert_docx_to_txt(src: Path, dst: Path) -> dict:
    from docx import Document
    doc = Document(str(src))
    text = "\n".join(p.text for p in doc.paragraphs)
    dst.write_text(text, encoding="utf-8")
    return {"converted": str(src), "to": str(dst)}


def _convert_txt_to_docx(src: Path, dst: Path) -> dict:
    from docx import Document
    doc = Document()
    for line in src.read_text(encoding="utf-8", errors="replace").split("\n\n"):
        if line.strip():
            doc.add_paragraph(line.strip())
    doc.save(str(dst))
    return {"converted": str(src), "to": str(dst)}


def _convert_docx_to_pdf(src: Path, dst: Path) -> dict:
    if sys.platform != "win32":
        return {"error": "docx-to-pdf conversion requires Microsoft Word and only works on Windows"}
    try:
        from docx2pdf import convert
        convert(str(src), str(dst))
        return {"converted": str(src), "to": str(dst)}
    except ImportError:
        return {"error": "docx2pdf not installed - run: pip install docx2pdf (requires MS Word installed)"}
    except Exception as e:
        return {"error": f"docx-to-pdf conversion failed: {e}. Make sure Microsoft Word is installed."}


def _convert_xlsx_csv(src: Path, dst: Path) -> dict:
    import pandas as pd
    if src.suffix.lower() == ".csv":
        df = pd.read_csv(src)
    else:
        df = pd.read_excel(src)

    if dst.suffix.lower() == ".csv":
        df.to_csv(dst, index=False)
    else:
        df.to_excel(dst, index=False)
    return {"converted": str(src), "to": str(dst)}


_CONVERTERS = {
    # (src_ext, dst_ext): converter function
}


def _build_converters():
    for a in _IMAGE_EXTS:
        for b in _IMAGE_EXTS:
            if a != b:
                _CONVERTERS[(a, b)] = _convert_image
    _CONVERTERS[(".docx", ".txt")] = _convert_docx_to_txt
    _CONVERTERS[(".txt", ".docx")] = _convert_txt_to_docx
    _CONVERTERS[(".docx", ".pdf")] = _convert_docx_to_pdf
    _CONVERTERS[(".xlsx", ".csv")] = _convert_xlsx_csv
    _CONVERTERS[(".csv", ".xlsx")] = _convert_xlsx_csv
    _CONVERTERS[(".xls", ".csv")] = _convert_xlsx_csv


_build_converters()


@registry.register(
    "convert_file",
    "Convert a file from one format to another (images, docx/txt/pdf, xlsx/csv)",
    RiskLevel.LOW_RISK,
    params={"src": "source file path", "dst": "destination file path (extension determines target format)"},
    timeout=60.0,
)
def convert_file(src: str, dst: str):
    src_path = Path(src).expanduser()
    dst_path = Path(dst).expanduser()

    if not security_manager.validate_path(str(src_path)) or not security_manager.validate_path(str(dst_path)):
        return {"error": "Access to one of those paths is blocked by security policy"}
    if not src_path.exists():
        return {"error": f"'{src}' does not exist"}

    src_ext = src_path.suffix.lower()
    dst_ext = dst_path.suffix.lower()
    converter = _CONVERTERS.get((src_ext, dst_ext))
    if not converter:
        supported = sorted({f"{a}→{b}" for a, b in _CONVERTERS.keys()})
        return {"error": f"No converter for {src_ext} → {dst_ext}. Supported: {', '.join(supported[:12])}"
                          f"{'...' if len(supported) > 12 else ''}"}

    dst_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = converter(src_path, dst_path)
        if "error" not in result:
            # A converter that returned without raising is not proof of a good
            # file. Verify the output exists, is non-empty and reopens before
            # reporting the conversion as done.
            from core import office
            check = office.verify_office_file(dst_path, dst_ext.lstrip("."))
            if not check["ok"]:
                return {"error": f"Conversion produced an unusable file: "
                                 f"{check.get('error')}",
                        "path": str(dst_path), "verification": check}
            result.setdefault("saved", str(dst_path))
            result["verified"] = True
            result["bytes"] = check.get("bytes")
            result["opened"] = office.open_when_done(dst_path)
        return result
    except ImportError as e:
        return {"error": f"Missing dependency for this conversion: {e}"}
    except Exception as e:
        return {"error": f"Conversion failed: {e}"}


# ----------------------------------------------------------------- TO-DO ---

@registry.register("add_todo", "Add a task, optionally with a due date, "
                   "priority and who it is for", RiskLevel.SAFE,
                   params={"text": "the task text"})
def add_todo_tool(text: str, due_date: str = "", priority: str = "",
                  assignee: str = ""):
    """The extra arguments all default to empty, so `add_todo(text)` - which is
    what every existing route and caller sends - behaves exactly as before."""
    from memory.database import init_db, add_todo
    init_db()
    resolved_due = _parse_relative_date(due_date) if due_date else None
    priority = (priority or "").strip().lower() or None
    if priority and priority not in ("low", "normal", "high"):
        return {"error": f"Priority must be low, normal or high - not '{priority}'."}
    todo_id = add_todo(text.strip(), due_date=resolved_due, priority=priority,
                       assignee=(assignee or "").strip() or None, source="user")
    added = {"added": text.strip(), "id": todo_id}
    if resolved_due:
        added["due"] = resolved_due
    if priority:
        added["priority"] = priority
    if assignee:
        added["assignee"] = assignee.strip()
    return added


def _format_task(task: dict) -> str:
    """One task, as a line. Deadline and owner belong on the line - a task
    list that hides when things are due is not usable as a task list."""
    box = "[x]" if task["completed"] else "[ ]"
    line = f"#{task['id']} {box} {task['text']}"
    if task.get("due_date"):
        line += f"  (due {task['due_date']})"
    if task.get("priority") and task["priority"] != "normal":
        line += f"  [{task['priority']}]"
    if task.get("assignee"):
        line += f"  -> {task['assignee']}"
    return line


@registry.register("list_todos", "List tasks, with their due dates and owners",
                   RiskLevel.SAFE)
def list_todos_tool(include_completed: bool = True, due_before: str = ""):
    from memory.database import init_db, list_todos
    init_db()
    resolved = _parse_relative_date(due_before) if due_before else None
    todos = list_todos(include_completed=include_completed, due_before=resolved)
    if not todos:
        return {"todos": "Nothing on your list."}
    return {"todos": [_format_task(t) for t in todos]}


@registry.register("update_todo", "Change a task - its text, due date, "
                   "priority or who it is for", RiskLevel.SAFE,
                   params={"todo_id": "the task's numeric ID"})
def update_todo_tool(todo_id: int, text: str = "", due_date: str = "",
                     priority: str = "", assignee: str = ""):
    from memory.database import init_db, update_todo
    init_db()
    fields = {}
    if text.strip():
        fields["text"] = text.strip()
    if due_date.strip():
        fields["due_date"] = _parse_relative_date(due_date)
    if priority.strip():
        value = priority.strip().lower()
        if value not in ("low", "normal", "high"):
            return {"error": f"Priority must be low, normal or high - not '{value}'."}
        fields["priority"] = value
    if assignee.strip():
        fields["assignee"] = assignee.strip()
    if not fields:
        return {"error": "Nothing to change - say what the task should become."}
    ok = update_todo(int(todo_id), **fields)
    return ({"updated": todo_id, "changed": fields} if ok
            else {"error": f"No task with ID {todo_id}"})


@registry.register("overdue_tasks", "Show tasks whose due date has passed, and "
                   "what is due today", RiskLevel.SAFE)
def overdue_tasks_tool():
    from datetime import date
    from memory.database import init_db, list_todos
    init_db()
    today = date.today().isoformat()
    open_tasks = list_todos(include_completed=False)
    overdue = [t for t in open_tasks if t.get("due_date") and t["due_date"] < today]
    due_today = [t for t in open_tasks if t.get("due_date") == today]
    if not overdue and not due_today:
        return {"tasks": "Nothing overdue and nothing due today."}
    return {"overdue": [_format_task(t) for t in overdue] or "none",
            "due_today": [_format_task(t) for t in due_today] or "none",
            "counts": {"overdue": len(overdue), "due_today": len(due_today)}}


@registry.register("complete_todo", "Mark a to-do item as done", RiskLevel.SAFE,
                    params={"todo_id": "the to-do's numeric ID"})
def complete_todo_tool(todo_id: int):
    from memory.database import init_db, set_todo_completed
    init_db()
    ok = set_todo_completed(int(todo_id), True)
    return {"completed": todo_id} if ok else {"error": f"No to-do with ID {todo_id}"}


@registry.register("uncomplete_todo", "Mark a to-do item as not done", RiskLevel.SAFE,
                    params={"todo_id": "the to-do's numeric ID"})
def uncomplete_todo_tool(todo_id: int):
    from memory.database import init_db, set_todo_completed
    init_db()
    ok = set_todo_completed(int(todo_id), False)
    return {"uncompleted": todo_id} if ok else {"error": f"No to-do with ID {todo_id}"}


@registry.register("delete_todo", "Delete a to-do item", RiskLevel.SAFE,
                    params={"todo_id": "the to-do's numeric ID"})
def delete_todo_tool(todo_id: int):
    from memory.database import init_db, delete_todo
    init_db()
    ok = delete_todo(int(todo_id))
    return {"deleted": todo_id} if ok else {"error": f"No to-do with ID {todo_id}"}


# --------------------------------------------------------------- CALENDAR --

def _parse_relative_date(text: str) -> str:
    """Resolve 'today'/'tomorrow'/an explicit YYYY-MM-DD into a date string."""
    import datetime
    t = text.strip().lower()
    today = datetime.date.today()
    if t in ("today",):
        return today.isoformat()
    if t in ("tomorrow",):
        return (today + datetime.timedelta(days=1)).isoformat()
    try:
        datetime.date.fromisoformat(t)
        return t
    except ValueError:
        return today.isoformat()  # best-effort fallback, never crash on odd input


@registry.register("add_event", "Add a calendar event", RiskLevel.SAFE,
                    params={"title": "event title", "date": "YYYY-MM-DD, 'today', or 'tomorrow'"})
def add_event_tool(title: str, date: str = "today", event_time: str = "", notes: str = ""):
    from memory.database import init_db, add_event
    init_db()
    resolved_date = _parse_relative_date(date)
    event_id = add_event(title.strip(), resolved_date, event_time.strip() or None, notes.strip() or None)
    return {"added": title.strip(), "date": resolved_date, "time": event_time or None, "id": event_id}


def _format_event(event: dict) -> str:
    when = f"{event['event_date']} {event['event_time'] or ''}".strip()
    line = f"#{event['id']} {when} - {event['title']}"
    if event.get("kind") == "meeting":
        line += "  (meeting"
        if event.get("duration_minutes"):
            line += f", {event['duration_minutes']} min"
        line += ")"
    if event.get("location"):
        line += f"  @ {event['location']}"
    if event.get("attendees"):
        line += f"  with {event['attendees']}"
    return line


@registry.register("list_events", "Show calendar events and meetings",
                   RiskLevel.SAFE)
def list_events_tool(date: str = "", kind: str = ""):
    from memory.database import init_db, list_events
    init_db()
    resolved = _parse_relative_date(date) if date else None
    events = list_events(start_date=resolved, kind=(kind or "").strip() or None)
    if not events:
        return {"events": "No events found."}
    return {"events": [_format_event(e) for e in events]}


@registry.register("schedule_meeting", "Schedule a meeting with attendees, a "
                   "location and an agenda", RiskLevel.SAFE,
                   params={"title": "what the meeting is about",
                           "date": "YYYY-MM-DD, 'today', or 'tomorrow'"})
def schedule_meeting_tool(title: str, date: str = "today", time: str = "",
                          attendees: str = "", location: str = "",
                          agenda: str = "", duration_minutes: int = 30):
    """A meeting is a calendar event with kind='meeting'.

    Deliberately not a separate table: one timeline means "what is on Friday"
    answers with appointments and meetings together, in the right order,
    instead of the caller having to query two subsystems and merge them.
    """
    from memory.database import init_db, add_event
    init_db()
    if not title.strip():
        return {"error": "A meeting needs a subject."}
    resolved_date = _parse_relative_date(date)
    try:
        duration = max(5, int(duration_minutes))
    except (TypeError, ValueError):
        duration = 30
    event_id = add_event(
        title.strip(), resolved_date, (time or "").strip() or None,
        notes=None, kind="meeting",
        attendees=(attendees or "").strip() or None,
        location=(location or "").strip() or None,
        agenda=(agenda or "").strip() or None,
        duration_minutes=duration)
    return {"scheduled": title.strip(), "date": resolved_date,
            "time": (time or "").strip() or "all day", "id": event_id,
            "attendees": (attendees or "").strip() or "none recorded",
            "location": (location or "").strip() or "not set",
            "duration_minutes": duration}


@registry.register("list_meetings", "Show scheduled meetings", RiskLevel.SAFE)
def list_meetings_tool(date: str = ""):
    from memory.database import init_db, list_events
    init_db()
    resolved = _parse_relative_date(date) if date else None
    meetings = list_events(start_date=resolved, kind="meeting")
    if not meetings:
        return {"meetings": "No meetings scheduled."}
    return {"meetings": [_format_event(m) for m in meetings]}


@registry.register("agenda_for", "Everything happening on a day - meetings, "
                   "events and the tasks due", RiskLevel.SAFE)
def agenda_for_tool(date: str = "today"):
    """One question, one answer. Previously this needed three separate calls
    and the caller had to merge them."""
    from memory.database import init_db, list_events, list_todos
    init_db()
    resolved = _parse_relative_date(date)
    events = list_events(start_date=resolved)
    due = [t for t in list_todos(include_completed=False)
           if t.get("due_date") == resolved]
    if not events and not due:
        return {"day": resolved, "agenda": "Nothing scheduled and nothing due."}
    return {"day": resolved,
            "events": [_format_event(e) for e in events] or "none",
            "tasks_due": [_format_task(t) for t in due] or "none"}


@registry.register("delete_event", "Delete a calendar event", RiskLevel.SAFE,
                    params={"event_id": "the event's numeric ID"})
def delete_event_tool(event_id: int):
    from memory.database import init_db, delete_event
    init_db()
    ok = delete_event(int(event_id))
    return {"deleted": event_id} if ok else {"error": f"No event with ID {event_id}"}


# ----------------------------------------------------------------- NOTES ---

@registry.register("add_note", "Save a note", RiskLevel.SAFE,
                    params={"content": "the note text"})
def add_note_tool(content: str, title: str = ""):
    from memory.database import init_db, add_note
    init_db()
    note_title = title.strip() or (content.strip()[:40] + ("..." if len(content.strip()) > 40 else ""))
    note_id = add_note(note_title, content.strip())
    return {"added": note_title, "id": note_id}


@registry.register("update_note", "Update the content of an existing note", RiskLevel.SAFE,
                    params={"note_id": "the note's numeric ID", "content": "new content"})
def update_note_tool(note_id: int, content: str):
    from memory.database import init_db, update_note
    init_db()
    ok = update_note(int(note_id), content.strip())
    return {"updated": note_id} if ok else {"error": f"No note with ID {note_id}"}


@registry.register("list_notes", "Show saved notes", RiskLevel.SAFE)
def list_notes_tool():
    from memory.database import init_db, list_notes
    init_db()
    notes = list_notes()
    if not notes:
        return {"notes": "No notes yet."}
    lines = [f"#{n['id']} {n['title']}" for n in notes]
    return {"notes": lines}


@registry.register("delete_note", "Delete a note", RiskLevel.SAFE,
                    params={"note_id": "the note's numeric ID"})
def delete_note_tool(note_id: int):
    from memory.database import init_db, delete_note
    init_db()
    ok = delete_note(int(note_id))
    return {"deleted": note_id} if ok else {"error": f"No note with ID {note_id}"}


# ------------------------------------------------------------ SCHEDULE ----
# Weekly schedule planner. Real PDF output (reportlab), real sync into the
# existing To-Do/Calendar panels for a genuinely tappable interactive
# checklist (not a fake mockup), and incremental updates that only touch
# the affected day(s) instead of forcing a full re-describe.

_SCHEDULE_CATEGORY_COLORS = {
    "work": "#2563EB",       # blue
    "class": "#2563EB",
    "study": "#7C3AED",      # purple
    "exercise": "#DC2626",   # red
    "meal": "#D97706",       # amber
    "personal": "#059669",   # green
    "hobby": "#059669",
    "sleep": "#475569",      # slate
    "free time": "#94A3B8",  # light gray
}

_SCHEDULE_DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

# Split into three SMALL, fast prompts instead of one giant "generate the
# entire week in one shot" call. That original design was the actual bug:
# a 7B local model asked for a full week's nested JSON in one response is
# slow (huge output) AND unreliable (one malformed field anywhere in that
# huge blob fails the whole plan). Generating day-by-day makes each call
# fast and lets one bad day fail without losing the rest of the week.

_SCHEDULE_READINESS_PROMPT = (
    "You are a personal schedule planning assistant. Given what the user has "
    "told you, decide if there's enough to build a weekly schedule. Respond "
    "with ONLY JSON, no prose, no markdown fences.\n"
    "If critical info is missing (no fixed commitments AND no goals at all, "
    "or exercise requested with no time-of-day preference), respond: "
    '{"need_info": ["<question 1>", "<question 2>"]} - at most 4 short '
    "specific questions.\n"
    "Otherwise respond: {\"ready\": true, \"title\": \"<short plan title>\"}"
)

_SCHEDULE_DAY_GENERATE_PROMPT = (
    "You are a personal schedule planning assistant. Build ONLY {day}'s "
    "schedule based on these weekly commitments/goals: \"{details}\".\n"
    "Respond with ONLY this JSON object, no prose, no markdown fences:\n"
    '{{"day": "{day}", "items": [ {{"time": "07:00", "activity": "Wake up", '
    '"category": "personal", "checkbox": false}}, {{"time": "09:00-17:00", '
    '"activity": "Work shift", "category": "work", "checkbox": true}} ], '
    '"exercise": [ {{"exercise": "Bench press", "sets_reps": "4x8"}} ], '
    '"summary": "Study: 2/3 hrs target met - work ran late."}}\n'
    "Rules: place fixed commitments exactly as given; fit meals into stated "
    "windows near natural breaks; distribute goal activities into remaining "
    "free time, getting as close to the target as possible for THIS day - if "
    "it can't be fully met, say so plainly in 'summary' and why; if this is "
    "a rest day, use category 'free time' instead of goal activities; every "
    "schedulable item gets \"checkbox\": true except passive items like "
    "sleep/wake which get \"checkbox\": false; only include 'exercise' if a "
    "workout happens this day, with real sets/reps, and cardio if the goal "
    "involves weight change. Category must be one of: work, class, study, "
    "exercise, meal, personal, hobby, sleep, free time."
)

_SCHEDULE_DAY_UPDATE_PROMPT = (
    "You are a personal schedule planning assistant. Here is {day}'s current "
    "schedule as JSON:\n{existing}\n\n"
    "Apply ONLY this change: \"{change}\". Keep everything else on this day "
    "exactly as it was. Respond with the FULL updated day in the same JSON "
    "object shape (day/items/exercise/summary), no prose, no markdown fences."
)

_SCHEDULE_RESOLVE_DAYS_PROMPT = (
    "Which day(s) of the week does this schedule change refer to: \"{change}\"? "
    "Respond with ONLY JSON: {{\"days\": [\"Monday\"]}} - one or more day names "
    "from Monday/Tuesday/Wednesday/Thursday/Friday/Saturday/Sunday, exactly "
    "as spelled and capitalized. If genuinely unclear, guess the single most "
    "likely day rather than listing all seven."
)


def _generate_single_day(day: str, details: str = "", existing_day: Optional[dict] = None,
                          change: Optional[str] = None) -> Optional[dict]:
    """Generate or update exactly one day's schedule. Small prompt, small
    expected output -> fast and far less likely to fail than a whole-week
    call. Retries once on a parse failure before giving up on this day."""
    import json
    if existing_day is not None and change:
        prompt = _SCHEDULE_DAY_UPDATE_PROMPT.format(day=day, existing=json.dumps(existing_day), change=change)
    else:
        prompt = _SCHEDULE_DAY_GENERATE_PROMPT.format(day=day, details=details)

    for attempt in range(2):  # one retry - cheap since each call is small
        result = _generate_structured_json_any(
            prompt, "You are a scheduling assistant. Output only valid JSON, nothing else.",
            shape="object", timeout=70.0,
        )
        if result and "items" in result:
            result.setdefault("day", day)
            return result
    return None


def _resolve_affected_days(change: str) -> List[str]:
    result = _generate_structured_json_any(
        _SCHEDULE_RESOLVE_DAYS_PROMPT.format(change=change),
        "Output only valid JSON, nothing else.", shape="object", timeout=40.0,
    )
    days = (result or {}).get("days") or []
    valid = [d for d in days if d in _SCHEDULE_DAY_NAMES]
    return valid or [_SCHEDULE_DAY_NAMES[0]]  # best-effort fallback rather than failing outright


def _render_schedule_pdf(schedule: dict, out_path: Path):
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib import colors
    from reportlab.lib.units import inch
    from reportlab.platypus import (SimpleDocTemplate, Table, TableStyle, Paragraph,
                                     Spacer, PageBreak)
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("PlanTitle", parent=styles["Title"], fontSize=18)
    day_style = ParagraphStyle("DayHeading", parent=styles["Heading2"], spaceBefore=14)
    summary_style = ParagraphStyle("Summary", parent=styles["Normal"], fontSize=9,
                                    textColor=colors.HexColor("#475569"), spaceBefore=4)

    doc = SimpleDocTemplate(str(out_path), pagesize=LETTER,
                             topMargin=0.6 * inch, bottomMargin=0.6 * inch,
                             leftMargin=0.6 * inch, rightMargin=0.6 * inch)
    story = [Paragraph(schedule.get("title", "Weekly Schedule"), title_style), Spacer(1, 10)]

    for day in schedule.get("week", []):
        story.append(Paragraph(day.get("day", ""), day_style))

        rows = [["", "Time", "Activity", "Category"]]
        row_colors = [None]
        for item in day.get("items", []):
            box = "\u2610" if item.get("checkbox", True) else ""
            rows.append([box, item.get("time", ""), item.get("activity", ""), item.get("category", "")])
            row_colors.append(_SCHEDULE_CATEGORY_COLORS.get(str(item.get("category", "")).lower()))

        if len(rows) > 1:
            table = Table(rows, colWidths=[0.3 * inch, 1.3 * inch, 3.6 * inch, 1.1 * inch])
            style_cmds = [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F2937")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ALIGN", (0, 0), (0, -1), "CENTER"),
            ]
            for i, color_hex in enumerate(row_colors):
                if i == 0 or not color_hex:
                    continue
                style_cmds.append(("TEXTCOLOR", (3, i), (3, i), colors.HexColor(color_hex)))
                style_cmds.append(("FONTNAME", (3, i), (3, i), "Helvetica-Bold"))
            table.setStyle(TableStyle(style_cmds))
            story.append(table)

        exercise = day.get("exercise") or []
        if exercise:
            story.append(Spacer(1, 6))
            ex_rows = [["", "Exercise", "Sets/Reps"]]
            for ex in exercise:
                ex_rows.append(["\u2610", ex.get("exercise", ""), ex.get("sets_reps", "")])
            ex_table = Table(ex_rows, colWidths=[0.3 * inch, 3.5 * inch, 1.5 * inch])
            ex_table.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#DC2626")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
                ("ALIGN", (0, 0), (0, -1), "CENTER"),
            ]))
            story.append(ex_table)

        if day.get("summary"):
            story.append(Paragraph(f"Summary: {day['summary']}", summary_style))

    doc.build(story)


def _render_schedule_html(schedule: dict, out_path: Path):
    """A self-contained, tappable checklist page mirroring the PDF. Opens
    in any browser - genuinely usable on a phone if you copy the file over,
    but this does NOT auto-sync to a phone the way the on-device To-Do
    panel does; it's a standalone shareable artifact."""
    parts = [
        "<!DOCTYPE html><html><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width, initial-scale=1'>",
        f"<title>{schedule.get('title', 'Weekly Schedule')}</title>",
        "<style>",
        "body{font-family:-apple-system,Segoe UI,sans-serif;background:#0a0f1a;"
        "color:#e8ecf5;margin:0;padding:16px;max-width:640px;margin:0 auto;}",
        "h1{font-size:20px;} h2{font-size:16px;margin-top:28px;"
        "border-bottom:1px solid #2a3547;padding-bottom:6px;}",
        ".item{display:flex;align-items:center;gap:10px;padding:8px 4px;"
        "border-bottom:1px solid #16263a;}",
        ".item input{width:18px;height:18px;}",
        ".time{color:#7a8aa3;font-size:12px;width:90px;flex-shrink:0;}",
        ".cat{font-size:11px;padding:2px 8px;border-radius:10px;color:#fff;}",
        ".summary{color:#7a8aa3;font-size:12px;margin-top:8px;font-style:italic;}",
        ".done{text-decoration:line-through;opacity:0.5;}",
        "</style></head><body>",
        f"<h1>{schedule.get('title', 'Weekly Schedule')}</h1>",
    ]
    for day in schedule.get("week", []):
        parts.append(f"<h2>{day.get('day', '')}</h2>")
        for item in day.get("items", []):
            color = _SCHEDULE_CATEGORY_COLORS.get(str(item.get("category", "")).lower(), "#475569")
            checked = "" if item.get("checkbox", True) else "disabled"
            parts.append(
                "<div class='item'>"
                f"<input type='checkbox' {checked} onclick=\"this.parentElement.classList.toggle('done')\">"
                f"<span class='time'>{item.get('time', '')}</span>"
                f"<span style='flex:1'>{item.get('activity', '')}</span>"
                f"<span class='cat' style='background:{color}'>{item.get('category', '')}</span>"
                "</div>"
            )
        for ex in day.get("exercise") or []:
            parts.append(
                "<div class='item'>"
                "<input type='checkbox' onclick=\"this.parentElement.classList.toggle('done')\">"
                f"<span style='flex:1'>{ex.get('exercise', '')}</span>"
                f"<span class='time'>{ex.get('sets_reps', '')}</span>"
                "</div>"
            )
        if day.get("summary"):
            parts.append(f"<div class='summary'>{day['summary']}</div>")
    parts.append("</body></html>")
    out_path.write_text("\n".join(parts), encoding="utf-8")


def _sync_schedule_to_dashboard(schedule: dict):
    """Push the plan into the real, already-functional To-Do and Calendar
    panels so it's genuinely tappable/checkable in the app - not a separate
    fake system. Schedule-linked todos/events are tagged with a '[Plan]'
    prefix so re-syncing on update can find and replace only those, without
    touching the user's own manually-added items."""
    from memory.database import (init_db, list_todos, delete_todo, add_todo,
                                  list_events, delete_event, add_event)
    import datetime

    init_db()

    for t in list_todos():
        if t["text"].startswith("[Plan]"):
            delete_todo(t["id"])
    for e in list_events():
        if e["title"].startswith("[Plan]"):
            delete_event(e["id"])

    day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    today = datetime.date.today()
    monday_this_week = today - datetime.timedelta(days=today.weekday())

    for day in schedule.get("week", []):
        day_name = day.get("day", "")
        try:
            offset = day_names.index(day_name)
            event_date = (monday_this_week + datetime.timedelta(days=offset)).isoformat()
        except ValueError:
            event_date = today.isoformat()

        for item in day.get("items", []):
            label = f"[Plan] [{day_name}] {item.get('activity', '')}"
            if item.get("time"):
                label += f" ({item['time']})"
            if item.get("checkbox", True):
                add_todo(label)
            if item.get("category") in ("work", "class"):
                add_event(f"[Plan] {item.get('activity', '')}", event_date,
                          str(item.get("time", "")).split("-")[0].strip() or None)

        for ex in day.get("exercise") or []:
            add_todo(f"[Plan] [{day_name}] Exercise: {ex.get('exercise', '')} ({ex.get('sets_reps', '')})")


@registry.register(
    "create_schedule",
    "Build a personalized weekly schedule (fixed commitments, goals, meals, exercise) "
    "with a printable PDF, a shareable checklist page, and sync to the To-Do/Calendar panels",
    RiskLevel.MEDIUM_RISK,
    params={"details": "everything the user said about commitments, goals, meals, sleep, exercise, etc."},
    timeout=600.0,  # 7 small day-calls, generously bounded - see _generate_single_day
)
def create_schedule(details: str):
    readiness = _generate_structured_json_any(
        f"Here's what the user told me: \"{details}\"",
        _SCHEDULE_READINESS_PROMPT, shape="object", timeout=50.0,
    )
    if not readiness:
        return {"error": "Ollama is not running, or generation failed. Start it with 'ollama serve' "
                          "and make sure qwen2.5:7b is pulled, then try again."}

    if readiness.get("need_info"):
        questions = readiness["need_info"]
        return {"need_info": questions,
                "message": "I need a bit more detail before I can build this: " +
                           " ".join(f"({i+1}) {q}" for i, q in enumerate(questions))}

    title = readiness.get("title", "Weekly Schedule")
    week = []
    failed_days = []
    for day in _SCHEDULE_DAY_NAMES:
        day_result = _generate_single_day(day, details=details)
        if day_result:
            week.append(day_result)
        else:
            failed_days.append(day)

    if not week:
        return {"error": "Could not generate a valid schedule from that. Try giving me your "
                          "fixed commitments and goals more specifically."}

    result = {"title": title, "week": week}

    import json
    from memory.database import init_db, save_schedule
    init_db()
    save_schedule(title, json.dumps(result))

    docs_dir = _documents_root()
    docs_dir.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^\w\s-]", "", title).strip().replace(" ", "_")[:50] or "weekly_schedule"

    pdf_path = docs_dir / f"{safe_name}.pdf"
    if pdf_path.exists():
        pdf_path = docs_dir / f"{safe_name}_{int(time.time())}.pdf"
    _render_schedule_pdf(result, pdf_path)

    html_path = pdf_path.with_suffix(".html")
    _render_schedule_html(result, html_path)

    _sync_schedule_to_dashboard(result)

    try:
        if sys.platform == "win32":
            os.startfile(str(pdf_path))  # noqa
    except Exception:
        pass

    response = {"saved_pdf": str(pdf_path), "saved_html": str(html_path),
                "days": len(week),
                "note": "Also synced to your To-Do list and Calendar - check them off right in the app."}
    if failed_days:
        response["days_failed"] = failed_days
        response["note"] += (f" Could not generate: {', '.join(failed_days)} - "
                              f"try 'update my schedule for {failed_days[0]}: ...' to fill it in.")
    return response


@registry.register(
    "update_schedule",
    "Update the current weekly schedule (move/add/remove an activity, change a day) "
    "without regenerating the whole plan from scratch",
    RiskLevel.MEDIUM_RISK,
    params={"change_description": "what to change, e.g. 'move gym to 7am on Tuesday'"},
    timeout=180.0,  # only the affected day(s) get regenerated, so this stays small
)
def update_schedule(change_description: str):
    from memory.database import init_db, get_active_schedule, update_active_schedule
    init_db()
    current = get_active_schedule()
    if not current:
        return {"error": "There's no active schedule yet. Say something like "
                          "'plan my week' first, then I can update it."}

    import json
    current_data = json.loads(current["data"])
    days_by_name = {d.get("day"): d for d in current_data.get("week", [])}

    affected_days = _resolve_affected_days(change_description)
    changed_days = []
    for day_name in affected_days:
        existing_day = days_by_name.get(day_name, {"day": day_name, "items": []})
        updated_day = _generate_single_day(day_name, existing_day=existing_day, change=change_description)
        if updated_day and updated_day != existing_day:
            days_by_name[day_name] = updated_day
            changed_days.append(day_name)

    if not changed_days:
        return {"error": "Could not apply that change. Try being more specific about the day and activity."}

    current_data["week"] = [days_by_name[d] for d in _SCHEDULE_DAY_NAMES if d in days_by_name]
    update_active_schedule(json.dumps(current_data))

    docs_dir = _documents_root()
    docs_dir.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^\w\s-]", "", current_data.get("title", "weekly_schedule")).strip().replace(" ", "_")[:50] \
        or "weekly_schedule"
    pdf_path = docs_dir / f"{safe_name}.pdf"
    _render_schedule_pdf(current_data, pdf_path)
    _render_schedule_html(current_data, pdf_path.with_suffix(".html"))
    _sync_schedule_to_dashboard(current_data)

    try:
        if sys.platform == "win32":
            os.startfile(str(pdf_path))  # noqa
    except Exception:
        pass

    return {"updated_days": changed_days, "saved_pdf": str(pdf_path)}


# ------------------------------------------------------------------- RAG ---
# Local retrieval-augmented generation: index your own documents/notes, then
# ask questions grounded in them with source citations. Embeddings via
# Ollama's nomic-embed-text (small, CPU-friendly). Vector search is plain
# cosine similarity over chunks stored in SQLite - no separate vector DB
# dependency, which keeps this simple and appropriate for the hardware.

_RAG_SUPPORTED_EXTENSIONS = {".txt", ".md", ".py", ".js", ".ts", ".java", ".c", ".cpp",
                             ".json", ".csv", ".pdf", ".docx"}
_RAG_MAX_FILES_PER_INDEX = 200
_RAG_MAX_CHUNKS_PER_INDEX = 1500


def _extract_text_from_file(path: Path) -> Optional[str]:
    ext = path.suffix.lower()
    try:
        if ext == ".pdf":
            from pypdf import PdfReader
            reader = PdfReader(str(path))
            return "\n".join(page.extract_text() or "" for page in reader.pages)
        elif ext == ".docx":
            from docx import Document
            doc = Document(str(path))
            return "\n".join(p.text for p in doc.paragraphs)
        elif ext in _RAG_SUPPORTED_EXTENSIONS:
            return path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        logger.warning(f"Could not extract text from {path}: {e}")
    return None


def _chunk_text(text: str, chunk_size: int = 900, overlap: int = 180) -> List[str]:
    """Paragraph-aware chunking with word-boundary fallback and overlap.

    Prefer splitting on blank lines so each chunk stays coherent for RAG
    retrieval; fall back to fixed word windows for dense text.
    """
    text = (text or "").strip()
    if not text:
        return []
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: List[str] = []
    current = ""
    for para in paragraphs:
        candidate = (current + "\n\n" + para).strip() if current else para
        if len(candidate) <= chunk_size:
            current = candidate
            continue
        if current:
            chunks.append(current)
        if len(para) <= chunk_size:
            current = para
        else:
            # long paragraph: word windows
            words = para.split()
            approx = max(50, chunk_size // 6)
            ov = max(0, overlap // 6)
            start = 0
            while start < len(words):
                end = min(len(words), start + approx)
                piece = " ".join(words[start:end]).strip()
                if piece:
                    chunks.append(piece)
                if end >= len(words):
                    break
                start = end - ov
            current = ""
    if current:
        chunks.append(current)
    return chunks


def _cosine_similarity(a: list, b: list) -> float:
    import numpy as np
    va, vb = np.array(a), np.array(b)
    denom = (np.linalg.norm(va) * np.linalg.norm(vb))
    if denom == 0:
        return 0.0
    return float(np.dot(va, vb) / denom)


@registry.register(
    "index_folder",
    "Index documents in a folder (txt/md/pdf/docx/code) into the local knowledge base for RAG search",
    RiskLevel.LOW_RISK,
    params={"path": "folder to index"},
    timeout=300.0,
)
def index_folder(path: str, extensions: str = ""):
    from core.ollama_client import ollama_client
    from core.model_router import model_router
    from memory.database import init_db, add_chunk, delete_chunks_by_source

    folder = Path(path).expanduser()
    if not security_manager.validate_path(str(folder)):
        return {"error": f"Access to '{path}' is blocked by security policy"}
    if not folder.is_dir():
        return {"error": f"'{path}' is not a valid directory"}

    from core.capabilities import embedding_provider

    embed_ok = False
    if ollama_client.is_available():
        embed_model = embedding_provider.resolve()
        embed_ok = bool(embed_model)
    # Soft mode: still index text for keyword search when embeddings are offline

    allowed_exts = ({f".{e.strip().lstrip('.')}" for e in extensions.split(",") if e.strip()}
                     if extensions else _RAG_SUPPORTED_EXTENSIONS)

    init_db()

    files_indexed = 0
    chunks_indexed = 0
    files_skipped = []
    total_files_seen = 0

    for file_path in sorted(folder.rglob("*")):
        if total_files_seen >= _RAG_MAX_FILES_PER_INDEX or chunks_indexed >= _RAG_MAX_CHUNKS_PER_INDEX:
            break
        if not file_path.is_file() or file_path.suffix.lower() not in allowed_exts:
            continue
        total_files_seen += 1

        text = _extract_text_from_file(file_path)
        if not text or not text.strip():
            files_skipped.append(str(file_path))
            continue

        delete_chunks_by_source(str(file_path))  # re-indexing replaces, doesn't duplicate
        chunks = _chunk_text(text)
        embedded_any = False
        for i, chunk in enumerate(chunks):
            if chunks_indexed >= _RAG_MAX_CHUNKS_PER_INDEX:
                break
            embedding = embedding_provider.embed(chunk) if embed_ok else None
            add_chunk(str(file_path), i, chunk, embedding)
            chunks_indexed += 1
            embedded_any = True

        if embedded_any or chunks:  # keyword mode still counts
            files_indexed += 1
        else:
            files_skipped.append(str(file_path))

    result = {"files_indexed": files_indexed, "chunks_indexed": chunks_indexed}
    if not embed_ok:
        result["embeddings"] = ("unavailable - Ollama is not running or no embedding model "
                                "(bge-m3) is pulled; indexed for KEYWORD search only. "
                                "Re-index after starting Ollama for semantic search.")
    if files_skipped:
        result["files_skipped"] = files_skipped[:10]
    if total_files_seen >= _RAG_MAX_FILES_PER_INDEX:
        result["note"] = f"Stopped after {_RAG_MAX_FILES_PER_INDEX} files - index a subfolder for more."
    return result


@registry.register(
    "ask_knowledge_base",
    "Answer a question using your indexed documents, with source citations",
    RiskLevel.SAFE,
    params={"query": "the question to answer from indexed documents"},
    timeout=90.0,
)
def ask_knowledge_base(query: str, top_k: int = 5):
    """Grounded answering over the general knowledge base.

    This runs the same pipeline as the industrial knowledge base, because
    having two RAG paths of different quality means the answer a user gets
    depends on which door they came in through:

        query embedding -> dense retrieval -> top-K candidates -> reranking
        -> top-N selection -> evidence extraction -> citation contract
        -> generation -> citation validation

    Retrieval widens to 4x top_k before reranking: the reranker can only
    reorder what it is given, so handing it exactly the number of passages
    that will be kept makes it decorative.
    """
    from core.ollama_client import ollama_client
    from core.model_router import model_router
    from core.capabilities import embedding_provider, rerank_provider
    from industrial import provenance
    from memory.database import init_db, get_all_chunks

    init_db()

    all_chunks = get_all_chunks()
    if not all_chunks:
        return {"error": "Nothing indexed yet. Say 'index folder <path>' first."}

    embed_model = embedding_provider.resolve() or ""
    query_embedding = embedding_provider.embed(query)
    if query_embedding is None:
        if not ollama_client.is_available():
            return {"error": "Ollama is not running. Start it with 'ollama serve' and try again."}
        return {"error": f"Could not embed the query - is an embedding model pulled? "
                          f"Run: ollama pull {embed_model or 'bge-m3'}"}

    # 7-8. dense retrieval -> top-K candidates
    scored = [(_cosine_similarity(query_embedding, c["embedding"]), c) for c in all_chunks]
    scored.sort(key=lambda pair: pair[0], reverse=True)
    candidates = []
    for score, chunk in scored[:max(top_k * 4, 12)]:
        if score <= 0.3:                      # below this, it is not about the question
            continue
        row = dict(chunk)
        row["retrieval_score"] = score
        row["document_title"] = Path(chunk["source_path"]).name
        candidates.append(row)

    if not candidates:
        return {"answer": "Nothing in your indexed documents looks relevant to that question.",
                "sources": [], "grounded": False}

    # 9-10. reranking -> top-N
    ordered, rerank_info = rerank_provider.rerank(
        query, candidates,
        dense_scores=[row["retrieval_score"] for row in candidates],
        top_n=top_k)

    # 11-12. evidence extraction and provenance
    evidences = []
    for position, row in enumerate(ordered[:top_k], start=1):
        row.setdefault("filename", Path(row["source_path"]).name)
        row.setdefault("document_id", row["source_path"])
        evidences.append(provenance.Evidence.from_row(
            row, evidence_id=f"E{position}",
            score=float(row.get("rerank_score", row.get("retrieval_score", 0.0))),
            retrieval_score=float(row.get("retrieval_score", 0.0)),
            rerank_score=row.get("rerank_score")))

    retrieval = {"candidates": len(candidates), "returned": len(evidences),
                 "embedding_model": embed_model, "reranker": rerank_info}

    if not ollama_client.is_available():
        # The evidence is real and worth returning even when generation cannot run.
        return {"answer": None,
                "error": "Ollama is not running - start it with 'ollama serve'",
                "evidence": provenance.evidence_table(evidences),
                "sources": provenance.answer_sources(evidences),
                "retrieval": retrieval, "grounded": False}

    # 13. generation, under the same citation contract as the industrial path
    model = model_router.route("reasoning")
    system = ("You are a research assistant for an on-premise workbench. You answer "
              "strictly from the supplied evidence and never invent a source.\n"
              + provenance.GROUNDING_CONTRACT)
    user = (f"EVIDENCE:\n{provenance.build_evidence_prompt(evidences)}\n\n"
            f"QUESTION: {query}\n\n"
            "Answer using only the evidence above, citing evidence ids in square "
            "brackets. If the evidence is insufficient, say what is missing.")
    raw_answer = ollama_client.chat(
        model, [{"role": "system", "content": system},
                {"role": "user", "content": user}],
        retries=0, timeout=generation_timeout(90.0),
        task_type="rag", purpose="Grounded answer from indexed documents")

    # 14. citation validation, then delivery
    report = provenance.CitationValidator(evidences).validate(raw_answer)
    return {"answer": report.cleaned_answer,
            "model": model,
            "grounded": report.grounded,
            "citation_report": report.to_dict(),
            "sources": provenance.answer_sources(evidences, report.valid_citations or None),
            "evidence": provenance.evidence_table(evidences),
            "retrieval": retrieval}


@registry.register("list_knowledge_sources", "List documents indexed in the local knowledge base", RiskLevel.SAFE)
def list_knowledge_sources():
    from memory.database import init_db, list_indexed_sources
    init_db()
    sources = list_indexed_sources()
    if not sources:
        return {"sources": "Nothing indexed yet."}
    return {"sources": [f"{s['source_path']} ({s['chunk_count']} chunks)" for s in sources]}


@registry.register("forget_document", "Remove a document from the knowledge base", RiskLevel.LOW_RISK,
                    params={"path": "file path to remove from the index"})
def forget_document(path: str):
    from memory.database import init_db, delete_chunks_by_source
    init_db()
    removed = delete_chunks_by_source(str(Path(path).expanduser()))
    return {"removed_chunks": removed} if removed else {"error": f"'{path}' wasn't indexed"}


@registry.register("clear_knowledge_base", "Clear the entire local knowledge base index", RiskLevel.MEDIUM_RISK)
def clear_knowledge_base():
    from memory.database import init_db, clear_all_chunks
    init_db()
    removed = clear_all_chunks()
    return {"cleared_chunks": removed}



@registry.register(
    "index_file",
    "Index a single document into the local RAG knowledge base",
    RiskLevel.LOW_RISK,
    params={"path": "file path to index (pdf/docx/txt/md/code)"},
    timeout=120.0,
)
def index_file(path: str):
    from memory.database import init_db, add_chunk, delete_chunks_by_source
    from core.capabilities import embedding_provider
    from core.ollama_client import ollama_client

    file_path = Path(path).expanduser()
    if not security_manager.validate_path(str(file_path)):
        return {"error": f"Access to '{path}' is blocked by security policy"}
    if not file_path.is_file():
        return {"error": f"'{path}' is not a file"}
    if file_path.suffix.lower() not in _RAG_SUPPORTED_EXTENSIONS:
        return {"error": f"Unsupported type {file_path.suffix}. Supported: {sorted(_RAG_SUPPORTED_EXTENSIONS)}"}

    text = _extract_text_from_file(file_path)
    if not text or not text.strip():
        return {"error": "Could not extract text from file"}

    init_db()
    delete_chunks_by_source(str(file_path))
    chunks = _chunk_text(text)
    if not chunks:
        return {"error": "No chunks produced"}

    chunks_indexed = 0
    embed_ok = ollama_client.is_available() and embedding_provider.resolve()
    for i, chunk in enumerate(chunks):
        embedding = embedding_provider.embed(chunk) if embed_ok else None
        # Store even without embedding so keyword search still works
        add_chunk(str(file_path), i, chunk, embedding)
        chunks_indexed += 1
    return {
        "file": str(file_path),
        "chunks_indexed": chunks_indexed,
        "embeddings": bool(embed_ok),
        "note": None if embed_ok else "Stored for keyword search; pull an embedding model for semantic RAG",
    }


@registry.register(
    "index_my_documents",
    "Index the ZEUS_Documents folder (and optional extra paths) into the knowledge base",
    RiskLevel.LOW_RISK,
    timeout=300.0,
)
def index_my_documents(extra_path: str = ""):
    docs = _documents_root()
    results = []
    if docs.is_dir():
        results.append(index_folder(str(docs)))
    else:
        results.append({"note": f"{docs} does not exist yet - create documents first"})
    # Bundled ZEUS knowledge pack (Nexus Precision + project deliverables)
    pack = Path(__file__).resolve().parent.parent / "knowledge" / "JARVIS_PACK" / "markdown"
    if pack.is_dir():
        results.append(index_folder(str(pack)))
    if extra_path.strip():
        results.append(index_folder(extra_path.strip()))
    return {"indexed": results}


@registry.register(
    "search_knowledge",
    "Search the local knowledge base (semantic if embeddings available, else keyword)",
    RiskLevel.SAFE,
    params={"query": "what to search for"},
    timeout=60.0,
)
def search_knowledge(query: str, top_k: int = 8):
    """Always returns something if documents are indexed - keyword fallback when
    embeddings / Ollama are offline so RAG is usable on low-end machines."""
    from memory.database import init_db, get_all_chunks
    from core.capabilities import embedding_provider
    from core.ollama_client import ollama_client

    init_db()
    all_chunks = get_all_chunks()
    if not all_chunks:
        return {"error": "Nothing indexed yet. Try: index folder <path>  or  index my documents"}

    q = (query or "").strip().lower()
    results = []

    # Semantic path
    if ollama_client.is_available() and embedding_provider.resolve():
        try:
            q_emb = embedding_provider.embed(query)
            if q_emb is not None:
                scored = []
                for ch in all_chunks:
                    emb = ch.get("embedding")
                    if not emb:
                        continue
                    score = _cosine_similarity(q_emb, emb)
                    scored.append((score, ch))
                scored.sort(key=lambda x: x[0], reverse=True)
                for score, ch in scored[:top_k]:
                    results.append({
                        "score": round(float(score), 4),
                        "source": ch.get("source_path") or ch.get("source") or "",
                        "text": (ch.get("chunk_text") or ch.get("text") or ch.get("content") or "")[:500],
                        "mode": "semantic",
                    })
        except Exception as e:
            logger.warning(f"semantic search failed: {e}")

    # Keyword fallback / supplement
    if len(results) < top_k and q:
        words = [w for w in q.split() if len(w) > 2]
        scored = []
        for ch in all_chunks:
            text = (ch.get("chunk_text") or ch.get("text") or ch.get("content") or "").lower()
            if not text:
                continue
            hits = sum(1 for w in words if w in text)
            if hits:
                scored.append((hits, ch))
        scored.sort(key=lambda x: x[0], reverse=True)
        seen = {r.get("text", "")[:80] for r in results}
        for hits, ch in scored:
            snippet = (ch.get("chunk_text") or ch.get("text") or ch.get("content") or "")[:500]
            if snippet[:80] in seen:
                continue
            results.append({
                "score": hits,
                "source": ch.get("source_path") or ch.get("source") or "",
                "text": snippet,
                "mode": "keyword",
            })
            if len(results) >= top_k:
                break

    if not results:
        return {"matches": [], "note": "No matches. Try different words or index more files."}
    return {"matches": results, "count": len(results)}


@registry.register(
    "rag_write_document",
    "Write a Word document grounded in your indexed knowledge base (RAG-enhanced)",
    RiskLevel.MEDIUM_RISK,
    params={"topic": "subject to write about using indexed sources"},
    timeout=300.0,
)
def rag_write_document(topic: str, filename: str = ""):
    """Retrieve from local index, then write a document that cites sources."""
    search = search_knowledge(topic, top_k=6)
    evidence = []
    if isinstance(search, dict) and search.get("matches"):
        evidence = search["matches"]

    evidence_block = ""
    if evidence:
        parts = []
        for i, m in enumerate(evidence, 1):
            parts.append(f"[{i}] Source: {m.get('source')}\n{m.get('text')}")
        evidence_block = "\n\n".join(parts)

    from core.ollama_client import ollama_client
    from core.model_router import model_router

    content = None
    if ollama_client.is_available() and evidence_block:
        model = model_router.route("reasoning")
        prompt = (
            "Write a structured document about %r using ONLY the evidence below. "
            "Cite sources as [1], [2], etc. Use ## headings.\n\nEVIDENCE:\n%s"
        ) % (topic, evidence_block)
        content = ollama_client.chat(model, [
            {"role": "system", "content": "You write grounded reports. Use only provided evidence. Cite [n]."},
            {"role": "user", "content": prompt},
        ], retries=0, timeout=generation_timeout(200.0))

    if not content or len(str(content).split()) < 30:
        lines = [f"## Overview\n\nReport on {topic} based on local indexed documents.\n"]
        if evidence:
            lines.append("## Evidence Summary\n")
            for i, m in enumerate(evidence, 1):
                lines.append(f"### Source [{i}]: {m.get('source')}\n\n{m.get('text')}\n")
            lines.append("## Conclusion\n\nSee cited sources above for details grounded in your files.")
        else:
            lines.append(
                "## Note\n\nNo indexed documents matched this topic. "
                "Run: index folder <path>  then try again."
            )
        content = "\n".join(lines)

    return write_document(topic if not evidence else f"{topic} (grounded)", filename=filename or "")


@registry.register(
    "knowledge_stats",
    "Show how many documents and chunks are in the local RAG index",
    RiskLevel.SAFE,
)
def knowledge_stats():
    from memory.database import init_db, get_all_chunks, list_indexed_sources
    init_db()
    chunks = get_all_chunks()
    sources = list_indexed_sources()
    return {
        "documents": len(sources) if sources else 0,
        "chunks": len(chunks) if chunks else 0,
        "sources": [s.get("source_path") or s for s in (sources or [])][:30],
    }


# --------------------------------------------------------------- VISION ---
# Real multimodal understanding (not just OCR text extraction) via a
# vision-capable Ollama model. Complements read_screen (which only pulls
# text out) with actual visual reasoning about layout, UI, diagrams, photos.

@registry.register(
    "analyze_image",
    "Analyze an image or the current screen using a vision-capable AI model - describes "
    "layout, objects, diagrams, not just text (use read_screen for pure OCR text extraction)",
    RiskLevel.SAFE,
    timeout=100.0,
)
def analyze_image(image_path: str = "", prompt: str = ""):
    from core.ollama_client import ollama_client
    from core.model_router import model_router
    import base64
    import io

    if not ollama_client.is_available():
        return {"error": "Ollama is not running. Start it with 'ollama serve' and try again."}

    try:
        from PIL import Image
        if image_path:
            img_path = Path(image_path).expanduser()
            if not security_manager.validate_path(str(img_path)):
                return {"error": f"Access to '{image_path}' is blocked by security policy"}
            if not img_path.exists():
                return {"error": f"'{image_path}' does not exist"}
            img = Image.open(img_path)
        else:
            from PIL import ImageGrab
            img = ImageGrab.grab()
    except Exception as e:
        return {"error": f"Could not load image: {e}"}

    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=85)
    image_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")

    vision_model = model_router.route("vision")
    description = ollama_client.chat_vision(
        vision_model, prompt or "Describe what you see in detail, including any UI elements, "
                                 "text, diagrams, or notable objects.", image_b64,
    )
    if description.strip().startswith("["):
        return {"error": description}
    return {"description": description, "source": image_path or "screenshot"}


# ------------------------------------------------------------ DIAGNOSTICS --

def _check_python_package(module_name: str) -> bool:
    import importlib.util
    return importlib.util.find_spec(module_name) is not None


@registry.register("run_diagnostics", "Run a self-check of ZEUS's dependencies, models, "
                                       "and connections, and report exactly what's broken",
                    RiskLevel.SAFE, timeout=20.0)
def run_diagnostics():
    from core.ollama_client import ollama_client
    results = []

    ollama_up = ollama_client.is_available(force=True)
    results.append({"check": "Ollama server", "status": "PASS" if ollama_up else "FAIL",
                     "detail": "reachable" if ollama_up else "not reachable - run 'ollama serve'"})

    if ollama_up:
        installed = set(ollama_client.list_models())
        for label, model in [("reasoning model", "mistral:7b-instruct-q4_K_M"),
                              ("json/structured model", "qwen2.5:3b-instruct-q4_K_M"),
                              ("vision model", "qwen2.5vl:3b-q4_K_M"),
                              ("embedding model", "bge-m3")]:
            found = any(m == model or m.startswith(model.split(":")[0]) for m in installed)
            results.append({"check": label, "status": "PASS" if found else "WARN",
                             "detail": "pulled" if found else f"not pulled - run 'ollama pull {model}'"})
    else:
        results.append({"check": "model availability", "status": "SKIP",
                         "detail": "cannot check - Ollama unreachable"})

    package_checks = [
        ("python-docx", "docx", "Word generation"), ("python-pptx", "pptx", "PowerPoint generation"),
        ("openpyxl", "openpyxl", "Excel generation"), ("pandas", "pandas", "Excel/CSV conversion"),
        ("reportlab", "reportlab", "Schedule PDF export"), ("pypdf", "pypdf", "PDF reading for RAG"),
        ("Pillow", "PIL", "Screenshots/vision"), ("pyautogui", "pyautogui", "Keyboard/mouse automation"),
        ("pyperclip", "pyperclip", "Clipboard access"), ("pygetwindow", "pygetwindow", "Window management"),
        ("watchdog", "watchdog", "Folder watching"), ("easyocr", "easyocr", "Screen OCR"),
        ("numpy", "numpy", "RAG similarity search"),
    ]
    for display_name, import_name, used_for in package_checks:
        found = _check_python_package(import_name)
        results.append({"check": f"{display_name} ({used_for})", "status": "PASS" if found else "WARN",
                         "detail": "installed" if found else f"not installed - pip install {display_name}"})

    try:
        docs_dir = _documents_root()
        docs_dir.mkdir(parents=True, exist_ok=True)
        test_file = docs_dir / ".medha_write_test"
        test_file.write_text("ok")
        test_file.unlink()
        results.append({"check": "Output folder writable", "status": "PASS", "detail": str(docs_dir)})
    except Exception as e:
        results.append({"check": "Output folder writable", "status": "FAIL", "detail": str(e)})

    try:
        from memory.database import init_db, get_connection
        init_db()
        conn = get_connection()
        conn.execute("SELECT 1")
        conn.close()
        results.append({"check": "Local database", "status": "PASS", "detail": "accessible"})
    except Exception as e:
        results.append({"check": "Local database", "status": "FAIL", "detail": str(e)})

    fails = [r for r in results if r["status"] == "FAIL"]
    warns = [r for r in results if r["status"] == "WARN"]
    summary = f"{len(results) - len(fails) - len(warns)} PASS, {len(warns)} WARN, {len(fails)} FAIL"
    lines = [f"[{r['status']}] {r['check']}: {r['detail']}" for r in results]
    return {"summary": summary, "results": lines}


# ------------------------------------------------------------------ AGENTS --

@registry.register("list_agents", "List ZEUS's specialized agents and what each one owns", RiskLevel.SAFE)
def list_agents_tool():
    from core.agents import list_agents
    agents = list_agents()
    lines = [f"{a['name']}: {a['purpose']} (backend: {a['backend']}, "
             f"{a['tool_count']} tools, offline: {a['offline']})" for a in agents]
    return {"agent_count": len(agents), "agents": lines}


@registry.register("describe_agent", "Show which agent owns a specific capability/tool",
                    RiskLevel.SAFE, params={"agent_or_tool": "an agent name or a tool/capability name"})
def describe_agent_tool(agent_or_tool: str):
    from core.agents import describe_agent, get_agent_for_tool, AGENT_REGISTRY
    query = agent_or_tool.strip().lower().replace(" ", "_")
    for agent_id in AGENT_REGISTRY:
        if agent_id == query or AGENT_REGISTRY[agent_id].name.lower() == agent_or_tool.strip().lower():
            return describe_agent(agent_id)
    owner = get_agent_for_tool(agent_or_tool.strip())
    if owner:
        return describe_agent(owner)
    return {"error": f"No agent or tool matching '{agent_or_tool}'. Say 'list agents' to see all of them."}


# --------------------------------------------------------------- TERMINAL --

@registry.register("run_command", "Execute a shell/terminal command (requires confirmation)",
                    RiskLevel.HIGH_RISK, params={"command": "the shell command to run"}, timeout=60.0)
def run_command(command: str, working_dir: str = ""):
    if security_manager.is_blocked_command(command):
        return {"error": "That command matches a blocked pattern and was refused."}

    cwd = None
    if working_dir:
        cwd_path = Path(working_dir).expanduser()
        if not security_manager.validate_path(str(cwd_path)):
            return {"error": f"Access to '{working_dir}' is blocked by security policy"}
        cwd = str(cwd_path)

    try:
        proc = subprocess.run(command, shell=True, cwd=cwd, capture_output=True, text=True, timeout=45)
        return {"command": command, "exit_code": proc.returncode,
                "stdout": proc.stdout[-4000:], "stderr": proc.stderr[-2000:]}
    except subprocess.TimeoutExpired:
        return {"error": f"Command timed out after 45 seconds: {command}"}
    except Exception as e:
        return {"error": f"Command execution failed: {e}"}
