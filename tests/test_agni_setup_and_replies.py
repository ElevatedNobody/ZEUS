"""
ZEUS additions: readable tool replies, the read-only diagnostic identity, the
status routes, and the hardware-aware setup decisions. No network, no models.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ------------------------------------------------------------ replies ----

def test_sovereignty_report_reads_as_pass_fail_lines():
    from core.agni_present import present
    text = present("sovereignty_report", {
        "sovereign_mode": "INCOMPLETE", "duration_ms": 12,
        "lines": [{"label": "Cloud AI", "state": "BLOCKED", "ok": True, "detail": ""},
                  {"label": "OS firewall (Windows)", "state": "NOT APPLIED", "ok": False,
                   "detail": "run the script"}]})
    assert "Sovereign mode: INCOMPLETE" in text
    assert "[PASS] Cloud AI: BLOCKED" in text
    assert "[FAIL] OS firewall (Windows): NOT APPLIED - run the script" in text


def test_a_unit_conversion_reads_as_one_line():
    from core.agni_present import present
    assert present("unit_convert", {"value": 10.0, "from": "bar", "to": "psi",
                                    "result": 145.0377}) == "10 bar = 145.038 psi"


def test_errors_and_unknown_tools_fall_back_to_the_generic_formatter():
    from core.agni_present import present
    assert present("unit_convert", {"error": "incompatible units"}) is None
    assert present("some_other_tool", {"a": 1}) is None


def test_a_security_tool_reply_is_never_empty():
    """'prove it' used to come back as 'Couldn't do that: empty_response'
    because the text validator looked for a 'response' key in the tool data."""
    from core.decision_engine import DecisionEngine
    text = DecisionEngine._format_tool_output("verify_audit_chain",
                                              {"ok": True, "records": 3, "detail": "fine"})
    assert text.startswith("Audit chain intact")


def test_security_and_engineering_tools_are_validated_as_data_not_prose():
    from core.orchestration.agents import agent_for_tool, validator_for
    for tool in ("sovereignty_report", "security_status", "verify_audit_chain",
                 "unit_convert", "engineering_formula", "pipeline_status", "model_routing"):
        agent = agent_for_tool(tool)
        assert agent and validator_for(agent, tool) == "data", (tool, agent)


# --------------------------------------------------------------- routes --

@pytest.mark.parametrize("phrase, tool", [
    ("prove it", "sovereignty_report"),
    ("security status", "security_status"),
    ("check security", "security_status"),
    ("verify audit", "verify_audit_chain"),
    ("network status", "network_status"),
    ("model integrity", "model_integrity_report"),
    ("hardware status", "hardware_profile"),
    ("active agent", "pipeline_status"),
    ("show artifacts", "list_artifacts"),
    ("convert 10 bar to psi", "unit_convert"),
    ('import readings from "c:\\data\\p-101.csv"', "import_readings_csv"),
])
def test_status_phrases_reach_their_tool(phrase, tool):
    from core.decision_engine import _ROUTES
    matched = next((name for pattern, name, _ in _ROUTES if pattern.match(phrase)), None)
    assert matched == tool


def test_the_readings_route_supplies_every_required_argument():
    from core.decision_engine import _ROUTES
    for pattern, name, arg_fn in _ROUTES:
        if name == "import_readings_csv":
            args = arg_fn(pattern.match("import readings from c:\\x\\r.csv"))
            assert args == {"path": "c:\\x\\r.csv", "source": "operator CSV import"}
            return
    pytest.fail("no import_readings_csv route")


# ---------------------------------------------------- diagnostic identity --

def test_the_diagnostic_identity_is_read_only(monkeypatch):
    monkeypatch.delenv("AGNI_TEST_MODE", raising=False)
    from security import policy
    with policy.diagnostic_scope():
        identity = policy.current_identity()
        assert identity.username == "diagnostics" and identity.role == "viewer"
        denied = policy.authorize("delete_file", 3, {"path": "x.txt"}, origin="system")
        assert not denied.allowed and "role" in denied.reason.lower()
    assert policy.current_identity().username != "diagnostics"


# ------------------------------------------------------------- the setup --

def test_an_rtx_50_series_gpu_gets_the_cpu_torch_for_chatterbox():
    """torch 2.6 (pinned by chatterbox-tts) has no Blackwell kernels: a CUDA
    build would import and then fail at the first kernel launch."""
    engine = _load("setup_voice_engine", "scripts/setup_voice_engine.py")
    index, why = engine.torch_index({"gpu": "NVIDIA GeForce RTX 5060 Laptop GPU",
                                     "driver": "576.02"})
    assert index.endswith("/cpu") and "Blackwell" in why
    index, _ = engine.torch_index({"gpu": "NVIDIA GeForce RTX 4060 Laptop GPU",
                                   "driver": "576.02"})
    assert "/cu" in index


def test_the_installer_recognises_already_pulled_models():
    installer = _load("install_everything", "scripts/install_everything.py")
    pulled = ["bge-m3:latest", "qwen3:8b", "qwen2.5-coder:7b"]
    assert installer._model_present("bge-m3", pulled)
    assert installer._model_present("qwen3:8b", pulled)
    assert not installer._model_present("qwen3:4b", pulled)
    assert not installer._model_present("qwen3-vl:8b", pulled)


def test_the_rtx_5060_plan_fits_eight_gigabytes():
    from core.hardware import GPU, HardwareProfile, MODEL_SIZES_GB, model_plan
    hw = HardwareProfile(cpu="Intel Core Ultra 9", cores=24, threads=24,
                         ram_total_gb=16.0, ram_available_gb=10.0,
                         gpus=[GPU("NVIDIA GeForce RTX 5060 Laptop GPU", 8.0, 7.5)],
                         disk_free_gb=400)
    plan = model_plan(hw)
    assert plan["tier"] == "gpu_8gb"
    assert all(MODEL_SIZES_GB[m] <= 7.0 for m in plan["required"]), plan["required"]
    assert plan["whisper"]["device"] == "cuda"


def test_the_whisper_turbo_model_has_a_download_source():
    setup = _load("setup_models", "scripts/setup_models.py")
    assert "large-v3-turbo" in setup.WHISPER_REPOS


# sha256 of the password an earlier copy printed in LOGIN.txt - compared by
# hash so this test does not ship the password itself.
_OLD_PASSWORD_SHA256 = "8a993163787d536bce69db2c4c1070313e6d83af4e909bca0d20c9e86d39c368"


def test_no_plaintext_password_is_shipped():
    import hashlib
    import re
    for name in ("LOGIN.txt", "HOW_TO_RUN.txt", "INSTALL_ALL.bat", "INSTALL.bat",
                 "SET_PASSWORD.py", "RESET_PASSWORD.py", "START_HERE.md", "README.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        for token in set(re.findall(r"[^\s'\"`]+", text)):
            assert hashlib.sha256(token.encode()).hexdigest() != _OLD_PASSWORD_SHA256, name
        assert "or press Enter for" not in text, f"{name} offers a default password"
