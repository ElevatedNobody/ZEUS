"""
ZEUS System Control
Low-level system actions. Anything destructive (shutdown, restart) must
be routed through core.security_manager by the caller before invocation -
these functions perform the action, they do not gate it.
"""

import platform
import subprocess

from core.logger import get_logger

logger = get_logger("sys_control")


def get_platform_summary() -> dict:
    return {
        "system": platform.system(),
        "release": platform.release(),
        "version": platform.version(),
        "machine": platform.machine(),
    }


def lock_screen() -> bool:
    try:
        if platform.system() == "Windows":
            subprocess.run(["rundll32.exe", "user32.dll,LockWorkStation"], check=True)
            return True
        elif platform.system() == "Darwin":
            subprocess.run(["pmset", "displaysleepnow"], check=True)
            return True
        return False
    except Exception as e:
        logger.error(f"Lock screen failed: {e}")
        return False


def shutdown(delay_seconds: int = 30) -> bool:
    """Caller MUST confirm via security_manager before invoking this."""
    try:
        if platform.system() == "Windows":
            subprocess.run(["shutdown", "/s", "/t", str(delay_seconds)], check=True)
        else:
            subprocess.run(["shutdown", "-h", f"+{max(1, delay_seconds // 60)}"], check=True)
        return True
    except Exception as e:
        logger.error(f"Shutdown command failed: {e}")
        return False


def restart(delay_seconds: int = 30) -> bool:
    """Caller MUST confirm via security_manager before invoking this."""
    try:
        if platform.system() == "Windows":
            subprocess.run(["shutdown", "/r", "/t", str(delay_seconds)], check=True)
        else:
            subprocess.run(["shutdown", "-r", f"+{max(1, delay_seconds // 60)}"], check=True)
        return True
    except Exception as e:
        logger.error(f"Restart command failed: {e}")
        return False


def cancel_shutdown() -> bool:
    try:
        if platform.system() == "Windows":
            subprocess.run(["shutdown", "/a"], check=True)
        return True
    except Exception as e:
        logger.error(f"Cancel shutdown failed: {e}")
        return False
