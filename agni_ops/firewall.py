"""
ZEUS's operating-system firewall: apply, verify, test, detect tampering, undo.

    status     the ZEUS rules that exist right now                (no admin)
    audit      each expected rule checked field by field          (no admin)
    test       a real egress attempt from ZEUS's Python + the in-process
               guard's sovereign/lockdown/monitor behaviour         (no admin)
    tamper     current rules compared with the applied manifest    (no admin)
    apply      the configured mode ([security] network_mode)       (ADMIN)
    sovereign  public internet blocked, LAN and loopback open      (ADMIN)
    lockdown   every remote address blocked, loopback open         (ADMIN)
    remove     every ZEUS rule removed (after a backup)            (ADMIN)
    restore    the last applied mode re-applied from the manifest  (ADMIN)
    all        status + audit + tamper + test

"Applied" is never inferred from a command having run: after every change
the rules are read back and each one is checked - it EXISTS, is ENABLED, is
OUTBOUND, is BLOCK, and names the right PROGRAM - and then a real connection
is attempted from ZEUS's own interpreter. Only ZEUS-prefixed rules are ever
created, changed or removed; a JSON backup of them is written before any
change, and `remove` undoes everything.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional

from agni_ops.status import (FAIL, NOT_TESTED, PASS, UNAVAILABLE, WARN, Result)

ROOT = Path(__file__).resolve().parent.parent

# Exit codes FIREWALL_AGNI.bat returns.
EXIT_OK, EXIT_FAIL, EXIT_NEEDS_ADMIN, EXIT_INCONCLUSIVE, EXIT_TAMPERED = 0, 1, 2, 3, 4

_READ_RULES = r"""
$out = @()
foreach ($r in Get-NetFirewallRule -DisplayName '* Sovereign Block*','* Lockdown Block*' -ErrorAction SilentlyContinue) {
  $app = ($r | Get-NetFirewallApplicationFilter).Program
  $addr = @(($r | Get-NetFirewallAddressFilter).RemoteAddress)
  $out += [pscustomobject]@{Name=$r.DisplayName; Enabled=[string]$r.Enabled;
    Direction=[string]$r.Direction; Action=[string]$r.Action; Profile=[string]$r.Profile;
    Program=[string]$app; RemoteCount=$addr.Count; RemoteFirst=[string]$addr[0]}
}
ConvertTo-Json -InputObject @($out) -Compress -Depth 3
"""


def _manifest_path() -> Path:
    return ROOT / "config" / "firewall_manifest.json"


def _backup_dir() -> Path:
    try:
        from core.paths import log_dir
        folder = log_dir() / "firewall_backups"
    except Exception:
        folder = ROOT / "logs" / "firewall_backups"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _powershell(script: str, timeout: float = 120) -> subprocess.CompletedProcess:
    return subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
                           "Bypass", "-Command", script], capture_output=True, text=True,
                          timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def read_rules() -> List[Dict]:
    """Every ZEUS rule with the fields verification needs. Reading needs no admin."""
    if sys.platform != "win32":
        return []
    result = _powershell(_READ_RULES)
    text = (result.stdout or "").strip()
    if not text:
        return []
    data = json.loads(text)
    return data if isinstance(data, list) else [data]


def configured_mode() -> str:
    import configparser
    parser = configparser.ConfigParser()
    try:
        parser.read(ROOT / "config.ini", encoding="utf-8")
    except Exception:
        pass
    mode = parser.get("security", "network_mode", fallback="sovereign").strip().lower()
    return mode if mode in ("sovereign", "lockdown") else "sovereign"


def expected_programs() -> List[str]:
    from security.firewall import agni_programs, include_ollama_default
    return [p for p in agni_programs(ROOT, include_ollama_default(ROOT)) if Path(p).is_file()]


def load_manifest() -> Optional[Dict]:
    try:
        return json.loads(_manifest_path().read_text(encoding="utf-8"))
    except Exception:
        return None


# ---------------------------------------------------------------- verify ----

def audit(mode: Optional[str] = None) -> List[Result]:
    """Field-by-field verification of every rule ZEUS expects."""
    from security.firewall import LOCKDOWN_PREFIX, RULE_PREFIX
    rules = read_rules()
    if mode is None:
        present = {r["Name"].split(" ")[1].lower() for r in rules if r.get("Name")}
        mode = "lockdown" if "lockdown" in present else ("sovereign" if rules else configured_mode())
    prefix = LOCKDOWN_PREFIX if mode == "lockdown" else RULE_PREFIX
    results: List[Result] = []
    if not rules:
        return [Result("OS firewall rules", UNAVAILABLE, group="firewall",
                       reason="no ZEUS rules exist - run FIREWALL_AGNI.bat apply (needs administrator)",
                       current_state="not applied", fallback="in-process egress guard only")]
    by_program = {}
    for rule in rules:
        by_program.setdefault((rule.get("Program") or "").lower(), []).append(rule)
    for program in expected_programs():
        matches = [r for r in by_program.get(program.lower(), []) if r["Name"].startswith(prefix)]
        label = f"rule for {Path(program).name} ({Path(program).parent.name})"
        if not matches:
            results.append(Result(label, FAIL, reason=f"RULE MISSING for {program} ({mode} mode)",
                                  where="Windows Defender Firewall", group="firewall"))
            continue
        rule = matches[0]
        problems = []
        if rule.get("Enabled") not in ("True", "1", "true"):
            problems.append("not enabled")
        if rule.get("Direction") != "Outbound":
            problems.append(f"direction {rule.get('Direction')}")
        if rule.get("Action") != "Block":
            problems.append(f"action {rule.get('Action')}")
        if (rule.get("Program") or "").lower() != program.lower():
            problems.append("wrong program")
        remote_ok = (rule.get("RemoteFirst") == "Any") if mode == "lockdown" else (rule.get("RemoteCount", 0) > 100)
        if not remote_ok:
            problems.append(f"remote addresses {rule.get('RemoteFirst')} x{rule.get('RemoteCount')}")
        results.append(Result(
            label, FAIL if problems else PASS, group="firewall",
            reason=("; ".join(problems) if problems else
                    f"EXISTS, ENABLED, OUTBOUND, BLOCK, PROGRAM ok, {rule.get('RemoteCount')} "
                    f"remote range(s) - {rule['Name']}"),
            where=rule["Name"], details=rule))
    foreign = [r for r in rules if not r["Name"].startswith(prefix)]
    if foreign:
        results.append(Result("mixed rule sets", WARN, group="firewall",
                              reason=f"{len(foreign)} rule(s) from another ZEUS mode also present: "
                                     + ", ".join(r["Name"] for r in foreign[:4])))
    return results


def egress_test() -> List[Result]:
    """A real connection attempt from ZEUS's own interpreter, in a clean
    process (so ZEUS's in-process guard is not what answers), plus a check
    that loopback - the local model runtime - still works."""
    probe = (
        "import socket, json\n"
        "out = {}\n"
        "for host, port in (('1.1.1.1', 443), ('9.9.9.9', 443)):\n"
        "    s = socket.socket(); s.settimeout(5)\n"
        "    try:\n        s.connect((host, port)); out[host] = 'OPEN'\n"
        "    except OSError as e:\n        out[host] = 'ERR %s %s' % (getattr(e, 'winerror', None) or e.errno, e)\n"
        "    finally:\n        s.close()\n"
        "s = socket.socket(); s.settimeout(3)\n"
        "try:\n    s.connect(('127.0.0.1', 11434)); out['loopback'] = 'OPEN'\n"
        "except OSError as e:\n    out['loopback'] = 'ERR %s' % e\n"
        "finally:\n    s.close()\n"
        "print(json.dumps(out))\n")
    venv_python = ROOT / ".venv" / "Scripts" / "python.exe"
    python = str(venv_python if venv_python.is_file() else sys.executable)
    try:
        run = subprocess.run([python, "-c", probe], capture_output=True, text=True, timeout=40,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        observed = json.loads((run.stdout or "{}").strip().splitlines()[-1])
    except Exception as error:
        return [Result("egress test", FAIL, reason=f"the probe did not run: {error}", group="firewall")]
    results = []
    public = [observed.get(h, "") for h in ("1.1.1.1", "9.9.9.9")]
    evidence = json.dumps(observed)
    rules_present = bool(read_rules())
    if all("10013" in p for p in public):
        results.append(Result("egress from ZEUS's Python", PASS, group="firewall", evidence=evidence,
                              reason="BLOCKED by Windows Firewall (WinError 10013) - enforcement proven"))
    elif any(p == "OPEN" for p in public):
        results.append(Result("egress from ZEUS's Python", FAIL if rules_present else WARN,
                              group="firewall", evidence=evidence,
                              reason=("internet reachable DESPITE ZEUS rules - enforcement NOT working"
                                      if rules_present else
                                      "internet reachable - no ZEUS firewall rules applied"),
                              fallback="in-process egress guard still blocks ZEUS's own code"))
    else:
        results.append(Result("egress from ZEUS's Python", WARN, group="firewall", evidence=evidence,
                              reason="inconclusive - connections failed without the firewall's "
                                     "signature (offline or filtered elsewhere)"))
    loop = observed.get("loopback", "")
    results.append(Result("loopback to Ollama", PASS if loop == "OPEN" else WARN, group="firewall",
                          reason="127.0.0.1:11434 reachable - the local runtime is not cut off"
                          if loop == "OPEN" else f"loopback not reachable ({loop}) - is Ollama running?",
                          evidence=evidence))
    results.append(guard_test())
    return results


def guard_test() -> Result:
    """ZEUS's in-process egress guard changes behaviour by mode: monitor
    allows, sovereign and lockdown refuse a public address - before any
    packet is sent. Run in a clean process."""
    probe = (
        "import sys, json; sys.path.insert(0, %r)\n"
        "from security.egress import egress_guard, EgressBlocked\n"
        "import socket\n"
        "egress_guard.install(); out = {}\n"
        "for mode in ('monitor', 'sovereign', 'lockdown'):\n"
        "    egress_guard.configure(mode=mode)\n"
        "    try:\n"
        "        egress_guard._check(('1.1.1.1', 443)); out[mode] = 'allowed'\n"
        "    except EgressBlocked as e:\n"
        "        out[mode] = 'refused'\n"
        "    try:\n"
        "        egress_guard._check(('192.168.1.10', 80)); out[mode + '_lan'] = 'allowed'\n"
        "    except EgressBlocked:\n"
        "        out[mode + '_lan'] = 'refused'\n"
        "print(json.dumps(out))\n") % str(ROOT)
    try:
        run = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=60,
                             cwd=str(ROOT), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        observed = json.loads((run.stdout or "{}").strip().splitlines()[-1])
    except Exception as error:
        return Result("in-process egress guard", FAIL, reason=f"probe failed: {error}", group="firewall")
    expected = {"monitor": "allowed", "sovereign": "refused", "lockdown": "refused",
                "monitor_lan": "allowed", "sovereign_lan": "allowed", "lockdown_lan": "refused"}
    wrong = {k: v for k, v in observed.items() if expected.get(k) != v}
    return Result("in-process egress guard (sovereign mode changes policy)", FAIL if wrong else PASS,
                  group="firewall", evidence=json.dumps(observed),
                  reason=("monitor allows, sovereign refuses public/allows LAN, lockdown refuses both"
                          if not wrong else f"unexpected: {wrong}"))


def tamper_check() -> List[Result]:
    manifest = load_manifest()
    if not manifest:
        return [Result("tamper check", NOT_TESTED, group="firewall",
                       reason="no manifest - ZEUS has not applied rules through FIREWALL_AGNI.bat yet")]
    current = {r["Name"]: r for r in read_rules()}
    expected = {r["Name"]: r for r in manifest.get("rules", [])}
    findings = []
    for name, rule in expected.items():
        now = current.get(name)
        if now is None:
            findings.append(f"DELETED: {name}")
            continue
        for field in ("Enabled", "Direction", "Action", "Program", "RemoteCount"):
            if str(now.get(field)) != str(rule.get(field)):
                findings.append(f"CHANGED {name}: {field} {rule.get(field)} -> {now.get(field)}")
    for name in current:
        if name not in expected:
            findings.append(f"ADDED outside FIREWALL_AGNI.bat: {name}")
    if findings:
        try:
            from security import events
            events.record("firewall_tamper", "; ".join(findings)[:500], severity="critical")
        except Exception:
            pass
        return [Result("tamper check", FAIL, group="firewall", reason=f"{len(findings)} change(s) since "
                       f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(manifest.get('applied_at', 0)))}",
                       evidence="\n".join(findings), fix_attempted="none - run FIREWALL_AGNI.bat restore")]
    return [Result("tamper check", PASS, group="firewall",
                   reason=f"{len(expected)} rule(s) exactly as applied ({manifest.get('mode')} mode)")]


# ---------------------------------------------------------------- change ----

def backup(label: str) -> Path:
    path = _backup_dir() / f"firewall_{label}_{time.strftime('%Y%m%d_%H%M%S')}.json"
    path.write_text(json.dumps({"taken_at": time.time(), "rules": read_rules()}, indent=2),
                    encoding="utf-8")
    return path


def _run_script(text: str) -> subprocess.CompletedProcess:
    handle = tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False, encoding="utf-8")
    handle.write(text)
    handle.close()
    try:
        return subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
                               "Bypass", "-File", handle.name], capture_output=True, text=True,
                              timeout=300)
    finally:
        Path(handle.name).unlink(missing_ok=True)


def apply(mode: str) -> List[Result]:
    from agni_ops.system import is_admin
    from security.firewall import apply_script, include_ollama_default, write_scripts
    if not is_admin():
        return [Result(f"apply {mode}", UNAVAILABLE, group="firewall",
                       reason="REQUIRES_ADMIN - run FIREWALL_AGNI.bat, which asks Windows for "
                              "administrator rights", current_state="unchanged")]
    saved = backup(f"before_{mode}")
    include_ollama = include_ollama_default(ROOT)
    write_scripts(ROOT, include_ollama, mode)
    run = _run_script(apply_script(ROOT, include_ollama, mode))
    results = [Result(f"apply {mode}", PASS if run.returncode == 0 else FAIL, group="firewall",
                      reason=("rules written" if run.returncode == 0 else
                              f"PowerShell failed: {(run.stderr or run.stdout)[-400:]}"),
                      evidence=(run.stdout or "")[-1500:], fix_attempted=f"backup: {saved}")]
    rules = read_rules()
    _manifest_path().write_text(json.dumps({"mode": mode, "applied_at": time.time(),
                                            "include_ollama": include_ollama, "rules": rules},
                                           indent=2), encoding="utf-8")
    results += audit(mode)
    try:
        from memory.database import log_auth_event
        log_auth_event(f"os_firewall_{mode}", "", f"{len(rules)} rule(s); backup {saved.name}")
    except Exception:
        pass
    return results


def remove() -> List[Result]:
    from agni_ops.system import is_admin
    from security.firewall import remove_script
    if not is_admin():
        return [Result("remove", UNAVAILABLE, group="firewall", reason="REQUIRES_ADMIN")]
    saved = backup("before_remove")
    run = _run_script(remove_script())
    left = read_rules()
    try:
        _manifest_path().unlink(missing_ok=True)
    except OSError:
        pass
    return [Result("remove", PASS if not left and run.returncode == 0 else FAIL, group="firewall",
                   reason=("every ZEUS rule removed; nothing else touched" if not left
                           else f"{len(left)} ZEUS rule(s) still present"),
                   fix_attempted=f"backup: {saved}")]


def restore() -> List[Result]:
    manifest = load_manifest()
    mode = (manifest or {}).get("mode") or configured_mode()
    return apply(mode)


def wfp_status() -> Result:
    """The second, independent layer (WFP filters via bin/agni-wfp.exe,
    applied by APPLY_FIREWALL.bat). Reading it usually needs an
    administrator, so 'unknown' is reported as such - never as present."""
    try:
        from security import wfp
        state = wfp.status()
    except Exception as error:
        return Result("WFP layer (tailscale/wf)", NOT_TESTED, group="firewall", reason=str(error)[:160])
    installed = state.get("installed")
    return Result("WFP layer (tailscale/wf)",
                  PASS if installed else (NOT_TESTED if installed is None else UNAVAILABLE),
                  group="firewall", reason=state.get("detail", ""))


def status() -> List[Result]:
    rules = read_rules()
    if not rules:
        return [Result("OS firewall", UNAVAILABLE, group="firewall",
                       reason="no ZEUS rules applied (FIREWALL_AGNI.bat apply needs administrator)",
                       fallback="in-process egress guard"), wfp_status()]
    enabled = [r for r in rules if r.get("Enabled") in ("True", "1")]
    modes = sorted({r["Name"].split(" ")[1] for r in rules})
    return [Result("OS firewall", PASS if enabled else FAIL, group="firewall",
                   reason=f"{len(enabled)}/{len(rules)} ZEUS rule(s) enabled, mode(s): {', '.join(modes)}",
                   details={"rules": [r["Name"] for r in rules]}), wfp_status()]


def exit_code(results: List[Result]) -> int:
    statuses = [r.status for r in results]
    if any(r.component == "tamper check" and r.status == FAIL for r in results):
        return EXIT_TAMPERED
    if any(r.status == UNAVAILABLE and "REQUIRES_ADMIN" in r.reason for r in results):
        return EXIT_NEEDS_ADMIN
    if FAIL in statuses:
        return EXIT_FAIL
    if any(r.status == WARN and "inconclusive" in r.reason for r in results):
        return EXIT_INCONCLUSIVE
    return EXIT_OK


def run(command: str) -> List[Result]:
    if command == "status":
        return status()
    if command == "audit":
        return audit()
    if command == "test":
        return egress_test()
    if command == "tamper":
        return tamper_check()
    if command in ("sovereign", "lockdown"):
        return apply(command) + egress_test()
    if command == "apply":
        return apply(configured_mode()) + egress_test()
    if command == "remove":
        return remove()
    if command == "restore":
        return restore() + egress_test()
    if command == "all":
        return status() + audit() + tamper_check() + egress_test()
    raise ValueError(f"unknown firewall command: {command}")


COMMANDS = ("status", "audit", "apply", "remove", "sovereign", "lockdown", "restore", "test",
            "tamper", "all")
ADMIN_COMMANDS = ("apply", "remove", "sovereign", "lockdown", "restore")
