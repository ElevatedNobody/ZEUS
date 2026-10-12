"""
The operator tooling (agni_ops) and the security fixes that came with it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_the_test_mode_variable_is_not_a_login_bypass_outside_pytest():
    """AGNI_TEST_MODE=1 used to give an unauthenticated process an ADMIN
    identity with no approvals."""
    code = ("import sys, json; sys.path.insert(0, '.'); from security import policy; "
            "i = policy.current_identity(); print(json.dumps([policy.test_mode(), i.role, i.authenticated]))")
    env = dict(os.environ, AGNI_TEST_MODE="1")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env,
                         cwd=str(ROOT), timeout=120).stdout.strip().splitlines()[-1]
    test_mode, role, authenticated = json.loads(out)
    assert test_mode is False
    assert authenticated is False and role == "viewer"


def test_main_removes_the_test_mode_variable_before_anything_reads_it():
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    body = source[source.index("def main():"):]
    assert body.index("_refuse_test_mode_bypass()") < body.index("argparse.ArgumentParser")


def test_lockdown_blocks_every_address_and_modes_replace_each_other(tmp_path):
    from security.firewall import ALL_PREFIXES, apply_script, remove_script
    lockdown = apply_script(tmp_path, mode="lockdown")
    sovereign = apply_script(tmp_path, mode="sovereign")
    assert "-RemoteAddress Any" in lockdown and "$ranges" not in lockdown
    assert "-RemoteAddress $ranges" in sovereign
    for script in (lockdown, sovereign, remove_script()):
        for prefix in ALL_PREFIXES:
            assert f"Get-NetFirewallRule -DisplayName '{prefix}*'" in script
    # Never a rule that is not ZEUS's.
    for script in (lockdown, sovereign, remove_script()):
        for line in script.splitlines():
            if "Remove-NetFirewallRule" in line:
                assert "ZEUS " in line


def test_firewall_exit_codes():
    from agni_ops.firewall import (EXIT_FAIL, EXIT_NEEDS_ADMIN, EXIT_OK, EXIT_TAMPERED, exit_code)
    from agni_ops.status import FAIL, PASS, UNAVAILABLE, Result
    assert exit_code([Result("x", PASS)]) == EXIT_OK
    assert exit_code([Result("x", FAIL)]) == EXIT_FAIL
    assert exit_code([Result("apply", UNAVAILABLE, reason="REQUIRES_ADMIN - ...")]) == EXIT_NEEDS_ADMIN
    assert exit_code([Result("tamper check", FAIL)]) == EXIT_TAMPERED


def test_every_admin_command_is_elevated_by_the_bat_file():
    from agni_ops.firewall import ADMIN_COMMANDS, COMMANDS
    bat = (ROOT / "FIREWALL_AGNI.bat").read_text(encoding="utf-8")
    for command in ADMIN_COMMANDS:
        assert command in bat.split("for %%A in (")[1].split(")")[0]
    for command in COMMANDS:
        assert command in bat
    assert "-Verb RunAs" in bat


def test_the_bat_files_are_crlf_and_call_the_shared_backend():
    for name, needle in (("STEP 1 : INSTALL", "agni_ops install-verify"),
                         ("STEP 2 : START", "agni_ops preflight"),
                         ("REPAIR.bat", "agni_ops repair"),
                         ("FIREWALL_AGNI.bat", "agni_ops firewall"),
                         ("TEST_AGNI.bat", "agni_ops test")):
        data = (ROOT / name).read_bytes()
        assert b"\r\n" in data and b"\n" not in data.replace(b"\r\n", b""), f"{name} is not CRLF"
        assert needle.encode() in data, name


def test_start_never_claims_online_itself():
    bat = (ROOT / "STEP 2 : START").read_text(encoding="utf-8").upper()
    assert "ECHO   ZEUS ONLINE" not in bat
    assert "announce_state(\"online\"" in (ROOT / "main.py").read_text(encoding="utf-8")


def test_room_is_made_by_unloading_resident_models(monkeypatch):
    from core import model_lifecycle, sysmem
    resident = [{"name": "qwen3:8b", "size_gb": 5, "vram_gb": 5, "expires_at": "1"}]
    unloaded = []
    monkeypatch.setattr(model_lifecycle, "resident", lambda base=None: list(resident))
    monkeypatch.setattr(model_lifecycle, "estimate_commit_gb", lambda name, base=None: 5.0)
    free = {"gb": 2.0}
    monkeypatch.setattr(sysmem, "has_headroom",
                        lambda need, margin=1.0: (free["gb"] >= need + margin, f"{free['gb']} GB"))
    monkeypatch.setattr(model_lifecycle, "max_resident", lambda: 1)

    def fake_unload(name, wait_seconds=15.0, base=None):
        unloaded.append(name)
        resident.clear()
        free["gb"] = 9.0
        return True
    monkeypatch.setattr(model_lifecycle, "unload", fake_unload)
    ok, reason, dropped = model_lifecycle.ensure_room("qwen3-vl:8b")
    assert ok and dropped == ["qwen3:8b"], (ok, reason, dropped)


def test_run_logs_are_never_overwritten(tmp_path, monkeypatch):
    monkeypatch.setenv("AGNI_LOG_DIR", str(tmp_path))
    from agni_ops.runlog import RunLog
    first, second = RunLog("test", quiet=True), RunLog("test", quiet=True)
    assert first.path != second.path
    first.close()
    second.close()


def test_a_restore_carries_everything_a_fresh_folder_would_lose():
    """RESTORE_MY_DATA.py copied medha.db but not the encrypted vault files,
    the audit key, the audit history or the memory stores - a restored
    install listed vault files it could not open and failed its audit."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("restore_items", ROOT / "RESTORE_MY_DATA.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    targets = {target for target, _, _ in module.ITEMS}
    for needed in ("database/medha.db", "database/vault", "database/.audit_key", "database/memory",
                   "logs/audit_chain.jsonl", "config/trusted_models.json", "models/insightface"):
        assert needed in targets, needed


def test_the_release_zip_never_contains_private_data():
    import importlib.util
    spec = importlib.util.spec_from_file_location("make_release", ROOT / "scripts" / "make_release.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for private in ("database/medha.db", "database/.audit_key", "database/vault/abc.bin",
                    "database/memory/hermes_u1.sqlite", "logs/audit_chain.jsonl",
                    "config/trusted_models.json", ".venv/Scripts/python.exe",
                    "models/whisper/model.bin", "scripts/firewall/apply_agni_firewall.ps1",
                    "backups/repair_1/medha.db"):
        assert not module.included(Path(private)), private
    for public in ("main.py", "agni_ops/cli.py", "STEP 2 : START", "models/README.txt",
                   "third_party/hermes/hermes/memory.py"):
        assert module.included(Path(public)), public


def test_the_diagnostic_report_names_every_failure_field(tmp_path, monkeypatch):
    monkeypatch.setenv("AGNI_LOG_DIR", str(tmp_path))
    from agni_ops import report
    from agni_ops.status import FAIL, PASS, Result
    monkeypatch.setattr(report, "ROOT", tmp_path)
    results = [Result("model bge-m3", PASS, group="models"),
               Result("Whisper 6: GPU float16", FAIL, group="whisper", reason="cuda_runtime",
                      where="large-v3-turbo cuda/float16", evidence="cudnn missing",
                      fix_attempted="none", current_state="CPU in use", fallback="CPU int8")]
    paths = report.write(results, {"memory": {}, "gpus": [], "packages": {}})
    text = paths["txt"].read_text(encoding="utf-8")
    for field in ("WHERE", "EVIDENCE", "FIX ATTEMPTED", "CURRENT STATE", "FALLBACK", "WHISPER", "MODELS"):
        assert field in text
    data = json.loads(paths["json"].read_text(encoding="utf-8"))
    assert data["overall"] == FAIL and data["counts"]["PASS"] == 1
