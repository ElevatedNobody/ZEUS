"""
Windows Filtering Platform (WFP) layer, through bin/agni-wfp.exe.

agni-wfp.exe is built from tools/agni-wfp (Go) on top of the operator-supplied
github.com/tailscale/wf library (third_party/wf). It keeps ONE persistent set
of WFP filters, owned by the "ZEUS Sovereign Firewall" provider, that confines
ZEUS's own programs to loopback (and, optionally, the LAN).

ZEUS never changes these on its own. Applying or removing them needs an
administrator: from ZEUS the request goes through the approval gateway and
then Windows' own UAC prompt; from a terminal, APPLY_FIREWALL.bat /
REMOVE_FIREWALL.bat do the same. Reading them usually needs elevation too, so
status() reports "unknown - needs administrator" rather than guessing.

    python -m security.wfp status | apply [--allow-lan] [--include-ollama] | remove | drops
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).resolve().parent.parent
HELPER = ROOT / "bin" / "agni-wfp.exe"


def helper_path() -> Path:
    return HELPER


def available() -> bool:
    return sys.platform == "win32" and HELPER.is_file()


def is_elevated() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _parse(text: str) -> Dict:
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return {"ok": False, "error": f"unexpected helper output: {(text or '').strip()[:200]}"}


def _run_direct(args: List[str], timeout: float = 60) -> Dict:
    result = subprocess.run([str(HELPER), *args], capture_output=True, text=True,
                            timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return _parse(result.stdout)


def _run_elevated(args: List[str], timeout: float = 180) -> Dict:
    """Start the helper through UAC and read its JSON back from a file."""
    handle, out = tempfile.mkstemp(prefix="agni-wfp-", suffix=".json")
    os.close(handle)
    os.remove(out)
    argv = [f"--out={out}", *args]
    quoted = ",".join("'" + a.replace("'", "''") + "'" for a in argv)
    command = (f"$p = Start-Process -FilePath '{HELPER}' -ArgumentList {quoted} "
               f"-Verb RunAs -WindowStyle Hidden -Wait -PassThru; exit $p.ExitCode")
    try:
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
                       capture_output=True, text=True, timeout=timeout,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if not os.path.exists(out):
            return {"ok": False, "error": "not run - the administrator prompt was declined "
                                          "or did not appear"}
        with open(out, encoding="utf-8") as stream:
            return _parse(stream.read())
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "the administrator prompt was not answered in time"}
    finally:
        try:
            os.remove(out)
        except OSError:
            pass


def run(args: List[str], elevate: bool = False) -> Dict:
    if not available():
        return {"ok": False, "error": f"the WFP helper is not installed ({HELPER})"}
    if is_elevated() or not elevate:
        return _run_direct(args)
    return _run_elevated(args)


def status() -> Dict:
    """Read-only. Never raises a UAC prompt."""
    if not available():
        return {"layer": "WFP (tailscale/wf)", "available": False,
                "installed": False, "detail": f"helper missing: {HELPER}"}
    data = run(["status"], elevate=False)
    if not data.get("ok"):
        needs_admin = "administrator" in str(data.get("error", ""))
        return {"layer": "WFP (tailscale/wf)", "available": True,
                "installed": None if needs_admin else False,
                "detail": ("unknown - reading WFP filters needs an administrator; run "
                           "APPLY_FIREWALL.bat or ask ZEUS to show the WFP firewall as admin")
                if needs_admin else data.get("error", "")}
    return {"layer": "WFP (tailscale/wf)", "available": True, "installed": bool(data.get("installed")),
            "filters": data.get("filters", 0), "rules": data.get("rules", []),
            "detail": f"{data.get('filters', 0)} ZEUS filter(s) active" if data.get("installed")
                      else "no ZEUS filters installed"}


def apply(allow_lan: bool = True, include_ollama: bool = False) -> Dict:
    from security.firewall import agni_programs
    args = ["apply"]
    for program in agni_programs(ROOT, include_ollama):
        args += ["-app", program]
    if allow_lan:
        args.append("-allow-lan")
    return run(args, elevate=True)


def remove() -> Dict:
    return run(["remove"], elevate=True)


def drops(minutes: int = 60) -> Dict:
    return run(["drops", "-minutes", str(int(minutes))], elevate=True)


def _main(argv: List[str]) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="ZEUS WFP firewall (tailscale/wf)")
    parser.add_argument("command", choices=["status", "apply", "remove", "drops"])
    parser.add_argument("--no-lan", action="store_true", help="do not permit private LAN ranges")
    parser.add_argument("--include-ollama", action="store_true",
                        help="also confine Ollama (it then cannot pull new models)")
    parser.add_argument("--minutes", type=int, default=60)
    args = parser.parse_args(argv)
    if args.command == "status":
        result = status()
    elif args.command == "apply":
        result = apply(allow_lan=not args.no_lan, include_ollama=args.include_ollama)
    elif args.command == "remove":
        result = remove()
    else:
        result = drops(args.minutes)
    print(json.dumps(result, indent=2))
    if args.command in ("apply", "remove") and result.get("ok"):
        try:
            from memory.database import log_auth_event
            log_auth_event(f"wfp_firewall_{args.command}", "", json.dumps(
                {k: result.get(k) for k in ("filters_added", "removed_filters", "confined", "allow_lan")}))
        except Exception:
            pass
    return 0 if result.get("ok", result.get("available", False)) else 1


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
