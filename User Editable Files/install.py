#!/usr/bin/env python3
"""
ZEUS - User Editable Installation Script
================================================
You can edit the installation options below (models to pull, shortcut name,
initial admin account setup, etc.) and run this script directly.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# ============================================================================
# USER EDITABLE INSTALLATION SETTINGS
# ============================================================================

# Recommended Ollama models to pull during setup:
MODELS_TO_PULL = [
    "qwen3:4b",
    "qwen3:8b",
    "bge-m3",
]

# Desktop shortcut name (set to "" to skip creating a desktop shortcut):
DESKTOP_SHORTCUT_NAME = "ZEUS.lnk"

# Optional: Automatically create or update an Admin account during install.
# Leave INITIAL_ADMIN_USERNAME = "" to set up your account on first launch instead.
INITIAL_ADMIN_USERNAME = ""
INITIAL_ADMIN_PASSWORD = ""

# ============================================================================

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent if (HERE.parent / "core").is_dir() else HERE
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def run(cmd, check=True):
    print(f"  -> {cmd}")
    return subprocess.run(cmd, shell=True, check=check, cwd=str(ROOT))


def check_python():
    print("[1/6] Checking Python...")
    if sys.version_info < (3, 10):
        print("ERROR: Python 3.10 or higher is required.")
        sys.exit(1)
    print(f"  Python {sys.version.split()[0]} OK")


def create_dirs():
    print("[2/6] Creating directories...")
    for d in [
        "logs", "logs/screenshots", "database", "database/chroma",
        "models/llm", "models/speech", "models/vision",
        "config", "plugins/custom_plugins",
    ]:
        (ROOT / d).mkdir(parents=True, exist_ok=True)
    print("  Directories ready")


def install_deps():
    print("[3/6] Installing Python packages...")
    req = ROOT / "config" / "requirements.txt"
    if not req.is_file():
        req = ROOT / "requirements.txt"
    run(f'"{sys.executable}" -m pip install --upgrade pip')
    if req.is_file():
        run(f'"{sys.executable}" -m pip install -r "{req}"')
    print("  Dependencies installed")


def setup_ollama():
    print("[4/6] Checking Ollama...")
    try:
        r = subprocess.run("ollama --version", shell=True, capture_output=True, text=True)
        if r.returncode == 0:
            print(f"  Ollama found: {r.stdout.strip()}")
            for model in MODELS_TO_PULL:
                print(f"  Pulling model: {model} ...")
                run(f"ollama pull {model}", check=False)
        else:
            print("  Ollama not found. Install from https://ollama.com")
    except Exception:
        print("  Ollama not in PATH.")


def create_shortcut():
    print("[5/6] Creating desktop shortcut (Windows)...")
    if sys.platform != "win32" or not DESKTOP_SHORTCUT_NAME:
        print("  Skipped")
        return
    try:
        from win32com.client import Dispatch
        desktop = os.path.join(os.path.expanduser("~"), "Desktop")
        shortcut_path = os.path.join(desktop, DESKTOP_SHORTCUT_NAME)
        main_py = HERE / "main.py" if (HERE / "main.py").is_file() else (ROOT / "main.py")
        shell = Dispatch("WScript.Shell")
        shortcut = shell.CreateShortCut(shortcut_path)
        shortcut.Targetpath = sys.executable
        shortcut.Arguments = f'"{main_py}" --voice'
        shortcut.WorkingDirectory = str(ROOT)
        shortcut.save()
        print(f"  Shortcut created: {shortcut_path}")
    except Exception as e:
        print(f"  Could not create shortcut: {e}")


def finish():
    print("[6/6] Finalizing database & admin settings...")
    try:
        from memory.database import init_db
        init_db()
        if INITIAL_ADMIN_USERNAME.strip() and INITIAL_ADMIN_PASSWORD:
            import SET_PASSWORD as sp
            sp.TARGET_USERNAME = INITIAL_ADMIN_USERNAME.strip()
            sp.NEW_PASSWORD = INITIAL_ADMIN_PASSWORD
            sp.MAKE_ADMIN = True
            sp.BYPASS_COMPLEXITY_CHECK = True
            sp.main()
    except Exception as e:
        print(f"  DB init note: {e}")
    print()
    print("=" * 56)
    print("  ZEUS installation complete!")
    print("=" * 56)
    print()
    print("Next steps:")
    print('  - Double-click "STEP 2 : START" in the main folder')
    print('  - Or run: python "User Editable Files\\main.py" --voice')
    print()


def main():
    print("ZEUS - User Editable Installer")
    print("-" * 44)
    check_python()
    create_dirs()
    install_deps()
    setup_ollama()
    create_shortcut()
    finish()


if __name__ == "__main__":
    main()
