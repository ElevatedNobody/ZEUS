"""
What this machine is - OS, Python, CPU, RAM, commit headroom, GPU/VRAM, disk,
administrator rights, Ollama, the virtual environment and the packages.

Read-only. Nothing here installs or changes anything.
"""

from __future__ import annotations

import importlib.util
import os
import platform
import shutil
import struct
import subprocess
import sys
from pathlib import Path
from typing import Dict, List

from agni_ops.status import FAIL, PASS, UNAVAILABLE, WARN, Result

ROOT = Path(__file__).resolve().parent.parent
VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"

# import name -> (pip name, why ZEUS needs it, required?)
PACKAGES = {
    "PySide6": ("PySide6", "the application window", True),
    "httpx": ("httpx", "talking to Ollama", True),
    "requests": ("requests", "installer readiness checks", True),
    "psutil": ("psutil", "system monitoring", True),
    "loguru": ("loguru", "logging", True),
    "numpy": ("numpy", "numerics", True),
    "argon2": ("argon2-cffi", "password hashing", True),
    "cryptography": ("cryptography", "vault encryption", True),
    "docx": ("python-docx", "Word documents", True),
    "openpyxl": ("openpyxl", "spreadsheets", True),
    "pptx": ("python-pptx", "presentations", True),
    "fitz": ("PyMuPDF", "PDF reading", True),
    "faster_whisper": ("faster-whisper", "speech recognition", False),
    "ctranslate2": ("ctranslate2", "Whisper runtime", False),
    "sounddevice": ("sounddevice", "microphone capture", False),
    "onnxruntime": ("onnxruntime", "face login / OCR runtime", False),
    "cv2": ("opencv-python-headless", "camera and images", False),
    "skimage": ("scikit-image", "face alignment", False),
    "pyttsx3": ("pyttsx3", "Windows voice fallback", False),
    "pytest": ("pytest", "the regression suite (requirements-dev.txt)", False),
}


def is_admin() -> bool:
    if os.name != "nt":
        try:
            return os.geteuid() == 0
        except AttributeError:
            return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def package_present(module: str) -> bool:
    """find_spec, not import: importing ctranslate2/onnxruntime loads native
    DLLs, and a native crash here would take the checker down with it."""
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def package_version(pip_name: str) -> str:
    try:
        from importlib.metadata import version
        return version(pip_name)
    except Exception:
        return ""


def ollama_exe() -> str:
    found = shutil.which("ollama")
    if found:
        return found
    candidate = Path.home() / "AppData" / "Local" / "Programs" / "Ollama" / "ollama.exe"
    return str(candidate) if candidate.is_file() else ""


def ollama_state() -> Dict:
    from core import model_lifecycle
    status, data, raw = model_lifecycle.request("/api/version", timeout=4)
    running = status == 200
    models: List[str] = []
    if running:
        _s, tags, _r = model_lifecycle.request("/api/tags", timeout=8)
        models = [m.get("name", "") for m in (tags or {}).get("models", [])]
    return {"installed": bool(ollama_exe()), "exe": ollama_exe(), "running": running,
            "version": (data or {}).get("version", "") if running else "",
            "models": models, "error": "" if running else raw[:160],
            "resident": model_lifecycle.resident() if running else []}


def network_egress_blocked_for_python() -> Dict:
    """Is THIS Python blocked from the internet by the OS firewall?

    Tries one TCP connection to a public resolver in a clean subprocess (so
    ZEUS's in-process egress guard is not what answers). WinError 10013 is
    Windows Firewall refusing the socket - which is what ZEUS's sovereign
    rules do, and why pip cannot install packages while they are on."""
    probe = ("import socket,sys\n"
             "s=socket.socket(); s.settimeout(4)\n"
             "try:\n s.connect(('1.1.1.1',443)); print('OPEN')\n"
             "except OSError as e:\n print('ERR', getattr(e,'winerror',None) or e.errno, e)\n"
             "finally:\n s.close()\n")
    try:
        out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                             timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
                             ).stdout.strip()
    except Exception as error:
        return {"state": "unknown", "evidence": str(error)}
    if out.startswith("OPEN"):
        return {"state": "open", "evidence": "TCP connect to 1.1.1.1:443 succeeded"}
    if "10013" in out:
        return {"state": "blocked_by_firewall", "evidence": out}
    return {"state": "offline_or_filtered", "evidence": out}


def snapshot() -> Dict:
    from core.sysmem import commit_snapshot
    info: Dict = {"os": platform.platform(), "windows": os.name == "nt",
                  "python": sys.version.split()[0], "python_exe": sys.executable,
                  "arch": f"{platform.machine()} / {struct.calcsize('P') * 8}-bit",
                  "admin": is_admin(), "root": str(ROOT),
                  "venv": VENV_PY.is_file(),
                  "in_venv": Path(sys.prefix).resolve() == (ROOT / ".venv").resolve()}
    try:
        from core.hardware import profile
        hw = profile(force=True)
        info.update(cpu=hw.cpu, cores=hw.cores, threads=hw.threads,
                    ram_total_gb=hw.ram_total_gb, disk_free_gb=hw.disk_free_gb,
                    gpus=[{"name": g.name, "vram_total_gb": g.vram_total_gb,
                           "vram_free_gb": g.vram_free_gb, "driver": g.driver,
                           "compute_capability": g.compute_capability} for g in hw.gpus],
                    tier=hw.tier)
    except Exception as error:
        info["hardware_error"] = str(error)
    info["memory"] = commit_snapshot()
    info["packages"] = {module: {"present": package_present(module),
                                 "version": package_version(pip), "required": required,
                                 "why": why}
                        for module, (pip, why, required) in PACKAGES.items()}
    return info


def check(info: Dict = None) -> List[Result]:
    """The system section of the report, as results."""
    info = info or snapshot()
    results: List[Result] = []
    version = tuple(int(p) for p in info["python"].split(".")[:2])
    results.append(Result("Python", PASS if version in ((3, 11), (3, 12)) else WARN,
                          reason=f"{info['python']} {info['arch']}"
                                 + ("" if version in ((3, 11), (3, 12))
                                    else " - ZEUS is tested on 3.11/3.12"),
                          where=info["python_exe"], group="system"))
    results.append(Result("Virtual environment", PASS if info["venv"] else FAIL,
                          reason=".venv present" if info["venv"] else ".venv missing - run STEP 1 : INSTALL",
                          where=str(VENV_PY), group="system"))
    memory = info.get("memory", {})
    headroom = memory.get("commit_available_gb", 0)
    results.append(Result(
        "Memory (commit headroom)",
        PASS if headroom >= 6 else WARN,
        reason=(f"{memory.get('ram_total_gb', '?')} GB RAM, commit {headroom} GB free of "
                f"{memory.get('commit_limit_gb', '?')} GB")
               + ("" if headroom >= 6 else
                  " - room for about one large model; ZEUS unloads models between uses"),
        details=memory, group="system"))
    gpus = info.get("gpus") or []
    results.append(Result("GPU", PASS if gpus else WARN,
                          reason=", ".join(f"{g['name']} {g['vram_total_gb']} GB "
                                           f"(driver {g['driver']}, cc {g['compute_capability']})"
                                           for g in gpus) or "no NVIDIA GPU - CPU inference",
                          group="system"))
    disk = info.get("disk_free_gb", 0) or 0
    results.append(Result("Disk", PASS if disk > 20 else WARN, reason=f"{disk} GB free",
                          group="system"))
    missing = [m for m, p in info["packages"].items() if p["required"] and not p["present"]]
    results.append(Result("Required packages", FAIL if missing else PASS,
                          reason=("missing: " + ", ".join(missing)) if missing
                          else f"{sum(1 for p in info['packages'].values() if p['required'])} present",
                          group="system"))
    optional_missing = [m for m, p in info["packages"].items() if not p["required"] and not p["present"]]
    if optional_missing:
        results.append(Result("Optional packages", WARN,
                              reason="not installed: " + ", ".join(optional_missing),
                              group="system"))
    results.append(Result("Administrator", PASS if info["admin"] else UNAVAILABLE,
                          reason="running elevated" if info["admin"] else
                          "not elevated - only firewall apply/remove needs it",
                          group="system"))
    return results
