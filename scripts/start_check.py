"""
START.bat's pre-flight: make sure the local model runtime (Ollama) is up.

Standard library only, so it runs even when packages are broken, and fast:

* Ollama answering on 127.0.0.1:11434 -> nothing to do.
* not answering, but installed -> start `ollama serve` HIDDEN (no extra
  console window to close by accident), with the VRAM settings START.bat put
  in the environment, and wait until it answers - up to 40 s, not a fixed
  guess. Its output goes to logs/ollama_serve.log.
* not installed -> say so; ZEUS still starts and says the model is unreachable.

Exit code is always 0: ZEUS runs without Ollama (reduced), so this never
blocks the start.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
URL = "http://127.0.0.1:11434/api/tags"


def ollama_up(timeout: float = 1.5) -> bool:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(URL, timeout=timeout) as response:
            return response.status == 200
    except Exception:
        return False


def find_ollama() -> str:
    found = shutil.which("ollama")
    if found:
        return found
    local = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"
    return str(local) if local.is_file() else ""


def start_hidden(exe: str) -> bool:
    log_dir = ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    flags = 0
    if os.name == "nt":
        flags = (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
                 | 0x00000008)                     # DETACHED_PROCESS: outlives this window
    try:
        with open(log_dir / "ollama_serve.log", "ab") as log:
            subprocess.Popen([exe, "serve"], stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                             creationflags=flags, close_fds=True)
        return True
    except OSError as error:
        print(f"  [WARN] could not start Ollama: {error}")
        return False


def main() -> int:
    if ollama_up():
        print("  Local AI runtime (Ollama) ........ running")
        return 0
    exe = find_ollama()
    if not exe:
        print("  Local AI runtime (Ollama) ........ NOT INSTALLED")
        print("         ZEUS starts, but answers that need a model will say so.")
        print("         Install Ollama from ollama.com, then run STEP 1 : INSTALL.")
        return 0
    print("  Local AI runtime (Ollama) ........ starting", end="", flush=True)
    if start_hidden(exe):
        deadline = time.time() + 40
        while time.time() < deadline:
            if ollama_up(timeout=1.0):
                print(" - running")
                return 0
            print(".", end="", flush=True)
            time.sleep(1)
    print(" - NOT ANSWERING")
    print("         ZEUS starts anyway; see logs\\ollama_serve.log for why Ollama did not start.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
