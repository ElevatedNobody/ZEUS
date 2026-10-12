"""
How much memory a new model can REALLY have right now - measured as Windows
commit headroom, not "free RAM".

Why this module exists (2026-09-24 incident on the RTX 5060 / 15.4 GB laptop):
every failure in that setup run - bge-m3 returning HTTP 500 ("0-dim vector"),
Whisper's `mkl_malloc: failed to allocate memory` on CUDA *and* CPU, and the
Chatterbox worker dying in c10.dll - was one condition: the Windows COMMIT
LIMIT (RAM + page file) was exhausted. Ollama's log recorded it exactly:
`system_free="1.9 GiB" free_swap="175.3 MiB"`.

Free physical RAM is the wrong number to look at on Windows. An allocation
fails when the commit charge would pass the commit limit, even with RAM free,
and Ollama on Windows+CUDA disables mmap - so a model that sits in VRAM still
commits roughly its own size in host memory. One loaded qwen3-vl:8b (5.8 GB)
took this machine from 4.8 GB of commit headroom to 0.4 GB.

    commit_snapshot()          -> dict of GB figures (never raises)
    commit_available_gb()      -> float
    has_headroom(need_gb)      -> (bool, reason)

Standard library only (ctypes on Windows, psutil elsewhere when present).
"""

from __future__ import annotations

import sys
from typing import Dict, Tuple

_GB = 1024 ** 3

# What is kept back for the application itself, the UI and Windows when a
# model load is planned. Loading "right up to the limit" is what crashed.
DEFAULT_MARGIN_GB = 1.0


def _windows_status() -> Dict[str, float]:
    import ctypes
    from ctypes import wintypes

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

    status = MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        raise OSError("GlobalMemoryStatusEx failed")
    # On Windows the "PageFile" fields are the COMMIT limit and the commit
    # still available - RAM plus page file - which is what allocations need.
    return {"ram_total_gb": status.ullTotalPhys / _GB,
            "ram_available_gb": status.ullAvailPhys / _GB,
            "commit_limit_gb": status.ullTotalPageFile / _GB,
            "commit_available_gb": status.ullAvailPageFile / _GB,
            "memory_load_percent": float(status.dwMemoryLoad)}


def _portable_status() -> Dict[str, float]:
    try:
        import psutil
        memory, swap = psutil.virtual_memory(), psutil.swap_memory()
        return {"ram_total_gb": memory.total / _GB,
                "ram_available_gb": memory.available / _GB,
                "commit_limit_gb": (memory.total + swap.total) / _GB,
                "commit_available_gb": (memory.available + swap.free) / _GB,
                "memory_load_percent": float(memory.percent)}
    except Exception:
        return {}


def commit_snapshot() -> Dict[str, float]:
    """RAM and commit figures in GB, rounded. Empty dict if nothing can be read."""
    try:
        raw = _windows_status() if sys.platform == "win32" else _portable_status()
    except Exception:
        raw = _portable_status()
    return {key: round(value, 2) for key, value in raw.items()}


def commit_available_gb() -> float:
    """Commit headroom in GB; a large number when it cannot be measured, so an
    unmeasurable machine is never refused on a guess (the caller still sees
    the real failure if a load does run out)."""
    snapshot = commit_snapshot()
    return float(snapshot.get("commit_available_gb", 1e6))


def has_headroom(need_gb: float, margin_gb: float = DEFAULT_MARGIN_GB) -> Tuple[bool, str]:
    """Can something needing `need_gb` of commit be loaded without pushing the
    machine to the edge? Returns (ok, human-readable reason)."""
    snapshot = commit_snapshot()
    if "commit_available_gb" not in snapshot:
        return True, "commit headroom could not be measured"
    available = snapshot["commit_available_gb"]
    required = need_gb + margin_gb
    if available >= required:
        return True, (f"{available:.1f} GB commit available, {required:.1f} GB needed "
                      f"({need_gb:.1f} + {margin_gb:.1f} margin)")
    return False, (f"insufficient runtime memory: {available:.1f} GB commit available "
                   f"(of {snapshot.get('commit_limit_gb', 0):.1f} GB RAM+page file), "
                   f"{required:.1f} GB needed")
