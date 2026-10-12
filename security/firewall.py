"""
Operating-system firewall layer (Windows Defender Firewall).

ZEUS does not change Windows security settings by itself. This module
GENERATES a PowerShell script that an administrator runs once, and READS the
resulting rules back so the Security Centre can say whether they are present.

The rules block outbound traffic to every non-private address for:
    the Python interpreter ZEUS actually runs on. On Windows a virtual
    environment's .venv\\Scripts\\python.exe is only a small launcher that
    starts the BASE interpreter recorded in .venv\\pyvenv.cfg, and it is that
    base python.exe/pythonw.exe that opens network connections - so that is
    what is blocked. Every program run by the same Python installation is
    affected too (pip included: remove the rules to update packages).
    the Qt WebEngine helper process (QtWebEngineProcess.exe)
    ollama.exe / "ollama app.exe" (optional - Ollama needs the internet only to
    pull models, so installs that are finished can lock it too)

Loopback is never filtered by Windows Firewall, and private LAN ranges are
left open so paired LAN nodes keep working. Removing the rules is one command
(the generated remove script).

    python -m security.firewall --write     writes scripts\\firewall\\*.ps1
    python -m security.firewall --status    shows which ZEUS rules exist
"""

from __future__ import annotations

import ipaddress
import json
import subprocess
import sys
from pathlib import Path
from typing import Dict, List

RULE_PREFIX = "ZEUS Sovereign Block"          # sovereign: public internet blocked, LAN open
LOCKDOWN_PREFIX = "ZEUS Lockdown Block"       # lockdown: every remote address blocked
ALL_PREFIXES = (RULE_PREFIX, LOCKDOWN_PREFIX)
MODE_PREFIX = {"sovereign": RULE_PREFIX, "lockdown": LOCKDOWN_PREFIX}


def include_ollama_default(root: Path) -> bool:
    """config.ini [security] firewall_include_ollama (default true).

    Ollama is ZEUS's model runtime, and it reaches out on its own: on this
    project's reference machine its log shows calls to ollama.com (model
    recommendations, cloud model cache, account checks). Sovereign mode covers
    it by default; `ollama pull` then needs the rules removed for the
    download, like pip does."""
    import configparser
    parser = configparser.ConfigParser()
    try:
        parser.read(root / "config.ini", encoding="utf-8")
        return parser.getboolean("security", "firewall_include_ollama", fallback=True)
    except Exception:
        return True

_PRIVATE_V4 = ["0.0.0.0/8", "10.0.0.0/8", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12",
               "192.168.0.0/16", "224.0.0.0/4", "255.255.255.255/32"]
_PRIVATE_V6 = ["::1/128", "fe80::/10", "fc00::/7", "ff00::/8"]


def _complement(space: str, exclude: List[str]) -> List[str]:
    remaining = [ipaddress.ip_network(space)]
    for text in exclude:
        net = ipaddress.ip_network(text)
        next_round = []
        for block in remaining:
            if block.version != net.version:
                next_round.append(block)
            elif net.subnet_of(block):
                next_round.extend(block.address_exclude(net))
            elif block.subnet_of(net):
                continue
            else:
                next_round.append(block)
        remaining = next_round
    return [str(n) for n in ipaddress.collapse_addresses(remaining)]


def public_ranges() -> List[str]:
    return _complement("0.0.0.0/0", _PRIVATE_V4) + _complement("::/0", _PRIVATE_V6)


def base_interpreter_dir(root: Path) -> Path:
    """The folder of the interpreter .venv was created from (pyvenv.cfg 'home')."""
    cfg = root / ".venv" / "pyvenv.cfg"
    try:
        for line in cfg.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            if key.strip().lower() == "home" and value.strip():
                return Path(value.strip())
    except OSError:
        pass
    return Path(getattr(sys, "_base_executable", sys.executable)).parent


def agni_programs(root: Path, include_ollama: bool = False) -> List[str]:
    """Every executable ZEUS's traffic can come from, for firewall rules."""
    venv = root / ".venv" / "Scripts"
    base = base_interpreter_dir(root)
    programs = [str(base / "python.exe"), str(base / "pythonw.exe"),
                str(venv / "python.exe"), str(venv / "pythonw.exe")]
    webengine = root / ".venv" / "Lib" / "site-packages" / "PySide6" / "QtWebEngineProcess.exe"
    programs.append(str(webengine))
    if include_ollama:
        local = Path.home() / "AppData" / "Local" / "Programs" / "Ollama"
        programs += [str(local / "ollama.exe"), str(local / "ollama app.exe")]
    return list(dict.fromkeys(programs))


_programs = agni_programs          # older name, kept for callers


def apply_script(root: Path, include_ollama: bool = False, mode: str = "sovereign") -> str:
    """The rules for one mode. Applying a mode first removes EVERY ZEUS rule
    (both prefixes), so switching sovereign <-> lockdown replaces the set
    instead of stacking two. Rules that are not ZEUS's are never touched."""
    prefix = MODE_PREFIX.get(mode, RULE_PREFIX)
    if mode == "lockdown":
        remote = "Any"
        purpose = "ZEUS lockdown: no network at all for this program (loopback is never filtered)"
    else:
        remote = "$ranges"
        purpose = "ZEUS sovereign mode: no internet for this program"
    lines = [
        "#Requires -RunAsAdministrator",
        f"# ZEUS - {mode} mode: block egress for ZEUS's own processes"
        + (" (loopback and LAN stay open)." if mode != "lockdown" else " (loopback stays open)."),
        "# Generated by security/firewall.py. Remove with remove_agni_firewall.ps1.",
        "$ErrorActionPreference = 'Stop'",
    ]
    if mode != "lockdown":
        lines.append("$ranges = @(" + ",".join(f"'{r}'" for r in public_ranges()) + ")")
    for existing in ALL_PREFIXES:
        lines.append(f"Get-NetFirewallRule -DisplayName '{existing}*' -ErrorAction SilentlyContinue "
                     f"| Remove-NetFirewallRule")
    for index, program in enumerate(_programs(root, include_ollama), start=1):
        name = Path(program).name
        lines.append(
            f"if (Test-Path '{program}') {{ New-NetFirewallRule -DisplayName '{prefix} {index} {name}' "
            f"-Direction Outbound -Action Block -Program '{program}' -RemoteAddress {remote} "
            f"-Profile Any -Description '{purpose}' | Out-Null; "
            f"Write-Host 'blocked: {program}' }} else {{ Write-Host 'not found (skipped): {program}' }}")
    lines.append(f"Get-NetFirewallRule -DisplayName '{prefix}*' | Select-Object DisplayName, Enabled, Action")
    return "\r\n".join(lines) + "\r\n"


def remove_script() -> str:
    removals = "\r\n".join(f"Get-NetFirewallRule -DisplayName '{prefix}*' -ErrorAction SilentlyContinue | "
                           f"Remove-NetFirewallRule" for prefix in ALL_PREFIXES)
    return (f"#Requires -RunAsAdministrator\r\n{removals}\r\n"
            "Write-Host 'ZEUS firewall rules removed.'\r\n")


def write_scripts(root: Path, include_ollama: bool = False, mode: str = "sovereign") -> Dict[str, str]:
    folder = root / "scripts" / "firewall"
    folder.mkdir(parents=True, exist_ok=True)
    apply_path = folder / "apply_agni_firewall.ps1"
    remove_path = folder / "remove_agni_firewall.ps1"
    apply_path.write_text(apply_script(root, include_ollama, mode), encoding="utf-8")
    remove_path.write_text(remove_script(), encoding="utf-8")
    return {"apply": str(apply_path), "remove": str(remove_path)}


def status() -> Dict:
    """Read the rules back. Reading firewall rules does not need admin rights."""
    if sys.platform != "win32":
        return {"layer": "os firewall", "supported": False, "rules": [],
                "active": False, "detail": "Windows Firewall is only available on Windows"}
    command = ("Get-NetFirewallRule -DisplayName '* Sovereign Block*','* Lockdown Block*' -ErrorAction SilentlyContinue | "
               "Select-Object DisplayName, Enabled, Action | ConvertTo-Json -Compress")
    try:
        output = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
                                capture_output=True, text=True, timeout=20,
                                creationflags=0x08000000).stdout.strip()
        rules = json.loads(output) if output else []
        if isinstance(rules, dict):
            rules = [rules]
    except Exception as error:
        return {"layer": "os firewall", "supported": True, "rules": [], "active": False,
                "detail": f"could not read firewall rules: {error}"}
    enabled = [r for r in rules if str(r.get("Enabled")) in ("1", "True", "true")]
    return {"layer": "os firewall", "supported": True,
            "rules": [r.get("DisplayName") for r in rules],
            "active": bool(enabled),
            "detail": (f"{len(enabled)} ZEUS outbound block rule(s) enabled" if enabled else
                       "no ZEUS firewall rules - run scripts\\firewall\\apply_agni_firewall.ps1 "
                       "as administrator to add the OS layer")}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--include-ollama", action="store_true")
    parser.add_argument("--mode", choices=["sovereign", "lockdown"], default="sovereign")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()
    project = Path(__file__).resolve().parent.parent
    if args.write:
        print(json.dumps(write_scripts(project, args.include_ollama or include_ollama_default(project),
                                       args.mode), indent=2))
    if args.status or not args.write:
        print(json.dumps(status(), indent=2))
