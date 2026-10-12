"""
ZEUS Application Control
Deterministic application/window control. Prefers OS APIs over blind
mouse coordinates, per the security-conscious automation design.
"""

import os
import subprocess
import sys

from core.logger import get_logger

logger = get_logger("app_control")


def open_application(name: str) -> bool:
    try:
        if sys.platform == "win32":
            os.startfile(name)  # noqa
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-a", name])
        else:
            subprocess.Popen([name])
        return True
    except Exception as e:
        logger.error(f"Failed to open '{name}': {e}")
        return False


def close_application(name: str) -> bool:
    try:
        import psutil
        closed = False
        for proc in psutil.process_iter(["name"]):
            if proc.info["name"] and name.lower() in proc.info["name"].lower():
                proc.terminate()
                closed = True
        return closed
    except ImportError:
        logger.warning("psutil not installed - cannot close applications")
        return False


def list_windows():
    """Return visible window titles (Windows only, best-effort elsewhere)."""
    try:
        import pygetwindow as gw
        return [w.title for w in gw.getAllWindows() if w.title.strip()]
    except Exception as e:
        logger.warning(f"Could not list windows: {e}")
        return []


def focus_window(title_substring: str) -> bool:
    try:
        import pygetwindow as gw
        for w in gw.getAllWindows():
            if title_substring.lower() in w.title.lower():
                w.activate()
                return True
        return False
    except Exception as e:
        logger.warning(f"Could not focus window '{title_substring}': {e}")
        return False
