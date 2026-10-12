#!/usr/bin/env python3
"""
ZEUS - User Editable Launcher
=====================================
Lightweight bootstrap that checks the Python environment, applies any custom
startup arguments or Admin/account overrides configured below, and starts
main.py.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# ============================================================================
# USER EDITABLE LAUNCHER SETTINGS
# ============================================================================

# Default arguments passed to main.py when launched (e.g. ["--voice"], ["--cli"], or [])
DEFAULT_LAUNCH_ARGS = ["--voice"]

# Minimum required Python version
MIN_PYTHON_VERSION = (3, 10)

# ============================================================================

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent if (HERE.parent / "core").is_dir() else HERE


def check_python():
    if sys.version_info < MIN_PYTHON_VERSION:
        print(f"Python {MIN_PYTHON_VERSION[0]}.{MIN_PYTHON_VERSION[1]}+ is required.")
        sys.exit(1)


def main():
    check_python()
    main_py = HERE / "main.py"
    if not main_py.is_file():
        main_py = ROOT / "main.py"
    user_args = sys.argv[1:] if len(sys.argv) > 1 else list(DEFAULT_LAUNCH_ARGS)
    subprocess.run([sys.executable, str(main_py)] + user_args, cwd=str(ROOT))


if __name__ == "__main__":
    main()
