"""
Sandboxed code execution for the Coding and Calculation agents.

Code is written to a fresh temporary directory and run by a SEPARATE Python
process that:

  * starts in isolated mode (-I: no user site-packages, no PYTHONPATH, no
    current directory on sys.path);
  * loads a guard first that disables sockets (no network at all - not even
    loopback, so it cannot reach Ollama or a LAN node), subprocess creation,
    os.system, and writes outside its own working directory, and refuses to
    read ZEUS's protected data, logs, configuration and code;
  * runs inside a Windows Job Object that caps memory, allows exactly one
    process and kills the process tree when the job closes;
  * is killed on a wall-clock timeout;
  * gets a minimal environment (no proxies, no tokens, offline flags set).

Honest limits: this is process isolation plus in-process guards, not a virtual
machine or an AppContainer. A determined attacker with native-code tricks
(ctypes) could get around the Python-level guards; the Job Object limits and
the OS firewall rules still apply to them. It is appropriate for running code
ZEUS generated and the operator asked for - not for running hostile code.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, Optional

from core.logger import get_logger

logger = get_logger("security.sandbox")

MAX_OUTPUT = 12000

_GUARD = r'''
import builtins, os, sys, socket, io

_ALLOWED_ROOT = os.path.realpath(os.getcwd())
_DENIED = [os.path.normcase(os.path.realpath(p)) for p in __DENIED__]

def _blocked(*args, **kwargs):
    raise PermissionError("ZEUS sandbox: this operation is not permitted")

class _NoSocket(socket.socket):
    def __init__(self, *args, **kwargs):
        raise PermissionError("ZEUS sandbox: network access is disabled")
socket.socket = _NoSocket
socket.create_connection = _blocked
socket.getaddrinfo = _blocked
try:
    import _socket
    _socket.socket = _NoSocket
except Exception:
    pass

import subprocess
subprocess.Popen = _blocked
subprocess.run = _blocked
subprocess.call = _blocked
subprocess.check_call = _blocked
subprocess.check_output = _blocked
os.system = _blocked
os.popen = _blocked
for _name in ("execv", "execve", "execl", "execle", "execlp", "execvp", "spawnl",
              "spawnv", "spawnve", "startfile", "kill"):
    if hasattr(os, _name):
        setattr(os, _name, _blocked)

_real_open = builtins.open
def _guarded_open(file, mode="r", *args, **kwargs):
    if isinstance(file, int):
        return _real_open(file, mode, *args, **kwargs)
    path = os.path.normcase(os.path.realpath(os.fspath(file)))
    for denied in _DENIED:
        if path == denied or path.startswith(denied.rstrip("\\/") + os.sep):
            raise PermissionError("ZEUS sandbox: that location is protected")
    if any(flag in mode for flag in ("w", "a", "x", "+")):
        if not (path == os.path.normcase(_ALLOWED_ROOT) or
                path.startswith(os.path.normcase(_ALLOWED_ROOT) + os.sep)):
            raise PermissionError("ZEUS sandbox: writing outside the sandbox folder is not permitted")
    return _real_open(file, mode, *args, **kwargs)
builtins.open = _guarded_open
io.open = _guarded_open
for _name in ("remove", "unlink", "rmdir", "rename", "replace", "removedirs", "chmod"):
    _orig = getattr(os, _name, None)
    if _orig is None:
        continue
    def _make(orig):
        def _checked(path, *args, **kwargs):
            p = os.path.normcase(os.path.realpath(os.fspath(path)))
            if not p.startswith(os.path.normcase(_ALLOWED_ROOT) + os.sep):
                raise PermissionError("ZEUS sandbox: changing files outside the sandbox folder is not permitted")
            return orig(path, *args, **kwargs)
        return _checked
    setattr(os, _name, _make(_orig))
'''

_RUNNER = r'''
import runpy, sys, os
sys.path[0:0] = [os.getcwd()] + __SITE__
exec(open("_agni_guard.py", encoding="utf-8").read(), {"__name__": "_agni_guard"})
sys.argv = ["main.py"]
runpy.run_path("main.py", run_name="__main__")
'''


def _job_limits(process: subprocess.Popen, memory_mb: int) -> Optional[object]:
    """Put the child in a Job Object. Returns the job handle (keep it alive)."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                        ("PerJobUserTimeLimit", ctypes.c_longlong),
                        ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class EXTENDED(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO_COUNTERS),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = EXTENDED()
        LIMIT_PROCESS_MEMORY, LIMIT_ACTIVE_PROCESS, LIMIT_KILL_ON_CLOSE = 0x100, 0x8, 0x2000
        info.BasicLimitInformation.LimitFlags = (LIMIT_PROCESS_MEMORY | LIMIT_ACTIVE_PROCESS
                                                 | LIMIT_KILL_ON_CLOSE)
        info.BasicLimitInformation.ActiveProcessLimit = 1
        info.ProcessMemoryLimit = int(memory_mb) * 1024 * 1024
        ok = kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))
        if not ok:
            kernel32.CloseHandle(job)
            return None
        kernel32.OpenProcess.restype = wintypes.HANDLE
        handle = kernel32.OpenProcess(0x1F0FFF, False, process.pid)
        if not handle or not kernel32.AssignProcessToJobObject(job, handle):
            kernel32.CloseHandle(job)
            return None
        kernel32.CloseHandle(handle)
        return job
    except Exception as error:
        logger.debug(f"job object unavailable: {error}")
        return None


def _close_job(job) -> None:
    if job:
        try:
            import ctypes
            ctypes.WinDLL("kernel32").CloseHandle(job)
        except Exception:
            pass


def run_python(code: str, timeout: float = 20.0, memory_mb: int = 768,
               files: Optional[Dict[str, str]] = None, stdin: str = "") -> Dict:
    """Run `code` in the sandbox. Returns a structured result, never raises."""
    from security.pathguard import protected_roots
    started = time.time()
    workdir = Path(tempfile.mkdtemp(prefix="agni_sandbox_"))
    denied = [str(p) for p in protected_roots()]
    (workdir / "_agni_guard.py").write_text(_GUARD.replace("__DENIED__", json.dumps(denied)),
                                            encoding="utf-8")
    (workdir / "main.py").write_text(code, encoding="utf-8")
    # A venv's python.exe on Windows is a launcher that starts the real
    # interpreter as a SECOND process - which the one-process Job Object limit
    # (correctly) refuses. So the base interpreter runs the code directly and
    # is given the venv's site-packages explicitly.
    import site
    site_dirs = [p for p in site.getsitepackages() if Path(p).is_dir()]
    (workdir / "_agni_run.py").write_text(_RUNNER.replace("__SITE__", json.dumps(site_dirs)),
                                          encoding="utf-8")
    for name, content in (files or {}).items():
        safe = Path(name).name
        (workdir / safe).write_text(content, encoding="utf-8")

    env = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP",
                                            "PATHEXT", "NUMBER_OF_PROCESSORS")
           if key in os.environ}
    env.update({"PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1",
                "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "NO_PROXY": "*",
                "PATH": os.path.dirname(getattr(sys, "_base_executable", "") or sys.executable)})
    creationflags = 0x08000000 if sys.platform == "win32" else 0   # CREATE_NO_WINDOW
    try:
        interpreter = getattr(sys, "_base_executable", "") or sys.executable
        process = subprocess.Popen([interpreter, "-I", "-X", "utf8", "_agni_run.py"],
                                   cwd=str(workdir), env=env, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   creationflags=creationflags)
    except Exception as error:
        return {"ok": False, "error": f"sandbox could not start: {error}",
                "exit_code": None, "stdout": "", "stderr": ""}
    job = _job_limits(process, memory_mb)
    timed_out = False
    try:
        stdout, stderr = process.communicate(input=stdin.encode("utf-8"), timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
        stdout, stderr = process.communicate()
    finally:
        _close_job(job)
    out = stdout.decode("utf-8", "replace")
    err = stderr.decode("utf-8", "replace")
    produced = sorted(p.name for p in workdir.iterdir()
                      if p.is_file() and p.name not in ("_agni_guard.py", "main.py", "_agni_run.py"))
    result = {
        "ok": process.returncode == 0 and not timed_out,
        "exit_code": process.returncode,
        "timed_out": timed_out,
        "stdout": out[-MAX_OUTPUT:],
        "stderr": err[-MAX_OUTPUT:],
        "duration_ms": int((time.time() - started) * 1000),
        "workdir": str(workdir),
        "files_produced": produced,
        "isolation": {"job_object": bool(job), "network": "disabled",
                      "memory_limit_mb": memory_mb, "timeout_s": timeout},
    }
    if "ZEUS sandbox:" in err:
        try:
            from security import events
            events.record("sandbox_violation", err.strip().splitlines()[-1][:300],
                          severity="warning")
        except Exception:
            pass
    return result
