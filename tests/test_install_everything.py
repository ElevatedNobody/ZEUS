"""
Tests for the all-in-one installer.

The installer downloads gigabytes and installs packages, so none of that runs
here. What IS tested is every decision it makes around those downloads: what
it skips, what it refuses to do in --check mode, how it reports partial
success, and whether one stage failing takes the others down.

Those are the parts that decide whether somebody ends up with a working
ZEUS or a confusing half-installed one.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture()
def installer():
    spec = importlib.util.spec_from_file_location(
        "install_everything", ROOT / "scripts" / "install_everything.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ================================================= --check changes nothing ==
# A status command with side effects is one nobody can trust enough to run.

def test_check_never_installs_packages(installer, monkeypatch):
    ran = []
    monkeypatch.setattr(installer, "run",
                        lambda *a, **k: ran.append(a) or True)
    monkeypatch.setattr(installer, "importable", lambda module: False)

    state = installer.stage_dependencies(repair=False, check_only=True)

    assert ran == [], f"--check ran commands: {ran}"
    assert state["ok"] is False
    assert "missing" in state["detail"]


def test_check_never_downloads_whisper(installer, monkeypatch):
    ran = []
    monkeypatch.setattr(installer, "run",
                        lambda *a, **k: ran.append(a) or True)
    monkeypatch.setattr(installer, "importable", lambda module: False)

    state = installer.stage_whisper(repair=False, check_only=True)

    assert ran == [], f"--check started a download: {ran}"
    assert state["ok"] is False


def test_check_passes_check_through_to_the_voice_installer(installer,
                                                           monkeypatch):
    """The voice stage delegates. It must delegate the read-only flag too, or
    --check would trigger a 2 GB download."""
    seen = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        return True

    monkeypatch.setattr(installer, "run", fake_run)
    installer.stage_voice(repair=False, check_only=True)
    assert "--check" in seen["command"]


def test_repair_is_passed_through_not_swallowed(installer, monkeypatch):
    seen = {}
    monkeypatch.setattr(installer, "run",
                        lambda command, **k: seen.setdefault("command", command) or True)
    installer.stage_voice(repair=True, check_only=False)
    assert "--repair" in seen["command"]


# ======================================================== skip what is done ==

def test_installed_packages_are_not_reinstalled(installer, monkeypatch):
    ran = []
    monkeypatch.setattr(installer, "run", lambda *a, **k: ran.append(a) or True)
    monkeypatch.setattr(installer, "importable", lambda module: True)

    state = installer.stage_dependencies(repair=False)

    assert ran == [], "reinstalled packages that already import"
    assert state["ok"] is True
    assert "already" in state["detail"]


def test_repair_reinstalls_even_when_present(installer, monkeypatch):
    """--repair exists precisely for the case where something is present but
    broken, so 'already installed' must not short-circuit it."""
    ran = []
    monkeypatch.setattr(installer, "run", lambda *a, **k: ran.append(a) or True)
    monkeypatch.setattr(installer, "importable", lambda module: True)

    installer.stage_dependencies(repair=True)
    assert ran, "--repair skipped the reinstall"


def test_an_existing_whisper_model_is_not_downloaded_again(installer,
                                                           monkeypatch):
    ran = []
    monkeypatch.setattr(installer, "run", lambda *a, **k: ran.append(a) or True)
    from voice import offline
    monkeypatch.setattr(offline, "missing_files", lambda *a, **k: [])
    # Downloaded is not verified (2026-09-25): without a current record the
    # installer proves it with a real transcription - stubbed here.
    monkeypatch.setattr(installer, "_whisper_record_is_current", lambda target: False)
    monkeypatch.setattr(installer, "importable", lambda module: True)
    monkeypatch.setattr(installer, "verify_whisper_device", lambda size: "cuda/float16")

    state = installer.stage_whisper(repair=False)
    assert ran == []
    assert state["ok"] is True
    assert "verified on cuda/float16" in state["detail"]


def test_a_downloaded_whisper_that_does_not_transcribe_is_not_ready(installer, monkeypatch):
    """The 2026-09-24 run showed Whisper 'already downloaded' as READY while
    every device failed; presence alone is never ready."""
    ran = []
    monkeypatch.setattr(installer, "run", lambda *a, **k: ran.append(a) or True)
    from voice import offline
    monkeypatch.setattr(offline, "missing_files", lambda *a, **k: [])
    monkeypatch.setattr(installer, "_whisper_record_is_current", lambda target: False)
    monkeypatch.setattr(installer, "importable", lambda module: True)
    monkeypatch.setattr(installer, "verify_whisper_device", lambda size: "")

    state = installer.stage_whisper(repair=False)
    assert ran == [], "a failed verification must not trigger a re-download"
    assert state["ok"] is False
    assert "NOT verified" in state["detail"]


def test_a_current_whisper_verification_is_not_repeated(installer, monkeypatch):
    from voice import offline
    monkeypatch.setattr(offline, "missing_files", lambda *a, **k: [])
    monkeypatch.setattr(installer, "_whisper_record_is_current", lambda target: True)
    monkeypatch.setattr(installer, "verify_whisper_device",
                        lambda size: pytest.fail("verified again although the record is current"))
    assert installer.stage_whisper(repair=False)["ok"] is True


def _fallback_setting(monkeypatch, size, tmp_path, present):
    from voice import offline
    monkeypatch.setattr(offline, "config_str", lambda key, default="": size if key == "stt_fallback_model" else default)
    monkeypatch.setattr(offline, "resolve_models_dir", lambda: tmp_path)
    monkeypatch.setattr(offline, "missing_files", lambda folder, files: [] if present else list(files))


def test_the_low_memory_fallback_is_installed_once(installer, monkeypatch, tmp_path):
    ran = []
    monkeypatch.setattr(installer, "run", lambda command, **k: ran.append(command) or True)
    _fallback_setting(monkeypatch, "small", tmp_path, present=False)
    installer._whisper_fallback(check_only=False)
    assert len(ran) == 1 and ran[0][-2:] == ["--whisper-fallback", "small"]
    _fallback_setting(monkeypatch, "small", tmp_path, present=True)
    installer._whisper_fallback(check_only=False)
    assert len(ran) == 1, "an installed fallback was downloaded again"


@pytest.mark.parametrize("size, present", [("small", False), ("none", False), ("auto", False)])
def test_check_or_no_fallback_setting_never_downloads_one(installer, monkeypatch, tmp_path, size, present):
    monkeypatch.setattr(installer, "run", lambda *a, **k: pytest.fail("a download was started"))
    _fallback_setting(monkeypatch, size, tmp_path, present)
    installer._whisper_fallback(check_only=True)
    if size != "small":
        installer._whisper_fallback(check_only=False)


# ============================================== imports decide, not pip ==

def test_pip_success_is_not_trusted_over_a_failed_import(installer,
                                                         monkeypatch):
    """pip can exit 0 while the wheel landed in a different interpreter. The
    question is whether the module IMPORTS."""
    monkeypatch.setattr(installer, "run", lambda *a, **k: True)
    monkeypatch.setattr(installer, "importable", lambda module: False)

    state = installer.stage_dependencies(repair=False)
    assert state["ok"] is False, "believed pip instead of checking the import"
    assert "cannot import" in state["detail"]


def test_pip_failure_is_forgiven_when_everything_needed_imports(installer,
                                                                monkeypatch,
                                                                capsys):
    """One optional package failing to build must not be reported as a failed
    installation when the application will run perfectly well."""
    calls = {"n": 0}

    def sometimes_failing_run(command, **kwargs):
        calls["n"] += 1
        return calls["n"] <= 1          # pip upgrade succeeds, install "fails"

    monkeypatch.setattr(installer, "run", sometimes_failing_run)

    # Missing on the first check so the install runs, present afterwards.
    checks = {"n": 0}

    def importable(module):
        checks["n"] += 1
        return checks["n"] > len(installer.CORE_IMPORTS)

    monkeypatch.setattr(installer, "importable", importable)

    state = installer.stage_dependencies(repair=False)
    assert state["ok"] is True
    assert "still run" in capsys.readouterr().out


# ============================================ one failure is not all failure ==

def test_a_failed_stage_does_not_stop_the_others(installer, monkeypatch,
                                                 capsys):
    """The stages are independent. A voice that will not install must not
    prevent ZEUS from being usable in text mode."""
    order = []

    monkeypatch.setattr(installer, "stage_dependencies",
                        lambda *a, **k: order.append("deps") or
                        {"name": "ZEUS packages", "ok": True, "detail": ""})
    monkeypatch.setattr(installer, "stage_hardware",
                        lambda *a, **k: order.append("hardware") or
                        {"name": "Hardware & model plan", "ok": True, "detail": ""})
    monkeypatch.setattr(installer, "stage_ollama",
                        lambda *a, **k: order.append("ollama") or
                        {"name": "Ollama", "ok": False, "detail": "not running"})
    monkeypatch.setattr(installer, "stage_whisper",
                        lambda *a, **k: order.append("whisper") or
                        {"name": "Listening (Whisper)", "ok": False,
                         "detail": "download failed"})
    monkeypatch.setattr(installer, "stage_voice",
                        lambda *a, **k: order.append("voice") or
                        {"name": "Speaking (Chatterbox)", "ok": True, "detail": ""})
    monkeypatch.setattr(installer, "stage_face",
                        lambda *a, **k: order.append("face") or
                        {"name": "Face login (InsightFace)", "ok": True, "detail": ""})
    monkeypatch.setattr(installer, "stage_readiness",
                        lambda *a, **k: order.append("readiness") or
                        {"name": "Readiness self-test", "ok": False, "detail": ""})
    monkeypatch.setattr(sys, "argv", ["install_everything.py"])

    installer.main()
    assert order == ["deps", "hardware", "ollama", "whisper", "face", "voice", "readiness"], (
        f"a failing stage stopped the run: {order}")


def test_the_summary_says_medha_runs_when_only_extras_failed(installer,
                                                              capsys):
    results = [
        {"name": "ZEUS packages", "ok": True, "detail": "installed"},
        {"name": "Ollama", "ok": False, "detail": "not running"},
        {"name": "Listening (Whisper)", "ok": False, "detail": "failed"},
        {"name": "Speaking (Chatterbox)", "ok": False, "detail": "failed"},
    ]
    assert installer.final_report(results, []) is False
    output = capsys.readouterr().out
    assert "ZEUS IS READY" in output
    assert "text mode is fully supported" in output


def test_the_summary_says_it_cannot_start_when_the_core_failed(installer,
                                                               capsys):
    """The one failure that IS fatal has to read differently from the ones
    that are not."""
    results = [
        {"name": "ZEUS packages", "ok": False, "detail": "cannot import PySide6"},
        {"name": "Ollama", "ok": True, "detail": "running"},
    ]
    installer.final_report(results, [])
    output = capsys.readouterr().out
    assert "CANNOT START" in output
    assert "ZEUS IS READY" not in output


def test_everything_installed_only_when_everything_installed(installer,
                                                             capsys):
    results = [
        {"name": "ZEUS packages", "ok": True, "detail": ""},
        {"name": "Ollama", "ok": True, "detail": ""},
        {"name": "Listening (Whisper)", "ok": True, "detail": ""},
        {"name": "Speaking (Chatterbox)", "ok": True, "detail": ""},
    ]
    assert installer.final_report(results, []) is True
    assert "EVERYTHING IS INSTALLED" in capsys.readouterr().out


def test_a_skipped_stage_is_never_reported_as_installed(installer, capsys):
    """--skip-voice must not produce a report claiming the voice is ready...
    but it must not read as a failure either. Nothing went wrong; the user
    chose to leave it out, and telling them to --repair it would be nonsense."""
    results = [{"name": "ZEUS packages", "ok": True, "detail": ""}]
    assert installer.final_report(results, ["Speaking (Chatterbox)"]) is False
    output = capsys.readouterr().out
    assert "skipped" in output
    assert "EVERYTHING IS INSTALLED" not in output
    assert "EVERYTHING YOU ASKED FOR" in output
    assert "--repair" not in output, (
        "a deliberate skip was reported as something to repair")
    assert "--voice-only" in output, "no way offered to add it later"


# ================================================================ Ollama ==

def test_ollama_is_checked_never_installed(installer):
    """Silently installing a system application from a Python script is not
    something this should do."""
    source = (ROOT / "scripts" / "install_everything.py").read_text(encoding="utf-8")
    stage = source[source.index("def stage_ollama"):source.index("def stage_whisper")]
    for forbidden in ("ollama.com/install", "winget install", "curl ",
                      "OllamaSetup"):
        assert forbidden not in stage, f"the Ollama stage tries to install: {forbidden}"
    assert "ollama pull" in stage, "it should at least tell the user the command"


def test_a_missing_embedding_model_is_called_out(installer, monkeypatch,
                                                 capsys):
    """RAG silently produces nothing without an embedding model, and that is
    hard to diagnose from the symptom - so it has to be named here."""
    monkeypatch.setattr(installer, "_ollama_models", lambda *a, **k: ["qwen2.5:7b"])

    state = installer.stage_ollama()
    output = capsys.readouterr().out
    assert "bge-m3" in output
    assert state["ok"] is False


def test_ollama_being_down_is_a_warning_not_a_crash(installer, monkeypatch):
    def refuse(*_args, **_kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(installer, "_ollama_models", refuse)
    state = installer.stage_ollama()
    assert state["ok"] is False
    assert state["detail"] == "not running"


# ============================ setup must survive the packages it installs ==
# On a new Python (3.13/3.14) one requirement without a wheel made pip install
# NOTHING, and stage 3 then died on `import requests` with a traceback.

def test_the_installer_itself_never_imports_a_third_party_http_client():
    import ast
    source = (ROOT / "scripts" / "install_everything.py").read_text(encoding="utf-8")
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not imported & {"requests", "httpx", "aiohttp"}, imported


def test_one_unavailable_package_does_not_block_the_rest(installer, monkeypatch, capsys):
    """pip -r fails as a whole; each requirement is then installed on its own."""
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        if "-r" in command:
            return False                              # the all-at-once install fails
        return not any("rapidocr-onnxruntime" in str(part) for part in command)

    checks = {"n": 0}

    def importable(module):
        checks["n"] += 1
        return checks["n"] > len(installer.CORE_IMPORTS)

    monkeypatch.setattr(installer, "run", fake_run)
    monkeypatch.setattr(installer, "importable", importable)
    state = installer.stage_dependencies(repair=False)

    singles = [c for c in commands if "-r" not in c and "--upgrade" not in c]
    assert len(singles) > 10, "the requirements were not retried one at a time"
    assert any("requests" in str(c[-1]) for c in singles)
    assert state["ok"] is True
    assert any("rapidocr-onnxruntime" in line for line in state.get("failed_packages", []))


def test_a_stage_that_crashes_becomes_a_fail_line(installer, monkeypatch, capsys):
    def explode(*_a, **_k):
        raise ModuleNotFoundError("No module named 'requests'")

    for name in ("stage_dependencies", "stage_hardware", "stage_whisper", "stage_face",
                 "stage_readiness"):
        monkeypatch.setattr(installer, name, lambda *a, **k: {"name": name, "ok": True, "detail": ""})
    monkeypatch.setattr(installer, "stage_ollama", explode)
    monkeypatch.setattr(sys, "argv", ["install_everything.py", "--skip-voice"])
    code = installer.main()
    output = capsys.readouterr().out
    assert code == 1
    assert "Ollama stopped with an error" in output
    assert "Traceback" not in output


def test_requirements_pick_an_ocr_engine_for_every_python():
    lines = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert 'rapidocr-onnxruntime>=1.3.24; python_version < "3.13"' in lines
    assert 'rapidocr>=3.0; python_version >= "3.13"' in lines


# ================================================================ wiring ==

def test_the_bat_file_passes_its_arguments_through():
    """INSTALL.bat --check has to reach the Python script as --check."""
    source = (ROOT / "INSTALL.bat").read_text(encoding="utf-8")
    assert '"scripts\\install_everything.py" %*' in source, (
        "INSTALL.bat drops the arguments it was given")


def test_the_bat_file_finds_python_before_blaming_the_user():
    source = (ROOT / "INSTALL.bat").read_text(encoding="utf-8")
    assert "where python" in source
    assert "where py " in source or "where py>" in source or "where py " in source
    assert "Add python.exe to PATH" in source, (
        "the no-Python message should name the checkbox that causes this")


def test_the_installer_delegates_rather_than_duplicating():
    """Two copies of the download logic means two places to fix a bug and one
    of them always gets missed."""
    source = (ROOT / "scripts" / "install_everything.py").read_text(encoding="utf-8")
    assert "setup_voice_engine.py" in source
    assert "setup_models.py" in source
    # It must not have grown its own copy of the model URLs.
    for duplicated in ("ResembleAI/chatterbox", "Systran/faster-whisper",
                       "download.pytorch.org"):
        assert duplicated not in source, (
            f"{duplicated} is hard-coded here as well as in the script that "
            f"owns it")


# ============================== a missing package is not three catastrophes ==
# The launcher used to try --voice, then plain, then --cli. A missing package
# fails all three identically, so one absent dependency printed the same
# traceback three times and read as a broken application.

def test_the_launcher_checks_imports_before_launching():
    source = (ROOT / "START.bat").read_text(encoding="utf-8")
    assert "import httpx, PySide6, psutil, requests" in source, (
        "the launcher does not check its dependencies before starting")
    check = source.index("import httpx, PySide6")
    launch = source.index('main.py --voice')
    assert check < launch, "the import check runs after the first launch attempt"


def test_a_missing_package_stops_rather_than_cascading():
    source = (ROOT / "START.bat").read_text(encoding="utf-8")
    guard = source[source.index("MISSING PACKAGES"):
                   source.index('main.py --voice')]
    assert "exit /b 1" in guard, (
        "a missing package still falls through to three launch attempts")
    assert "pip install -r requirements.txt" in guard, (
        "the one command that fixes it is not shown")


def test_the_launcher_names_which_packages_are_missing():
    """'Missing packages' without saying which is a riddle."""
    source = (ROOT / "START.bat").read_text(encoding="utf-8")
    assert "for %%M in (httpx PySide6 psutil requests)" in source


def test_httpx_is_actually_declared_in_requirements():
    """It failed at runtime, so the obvious suspicion is that it was never
    declared. It was - the folder simply had no install run in it."""
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "httpx" in requirements


def test_the_missing_package_message_names_the_actual_interpreter():
    """A machine with two Pythons makes 'pip install -r requirements.txt'
    ambiguous: bare pip can belong to the OTHER interpreter, so the packages
    install successfully and the launcher still cannot find them. The command
    shown has to name the Python that will run ZEUS."""
    source = (ROOT / "START.bat").read_text(encoding="utf-8")
    guard = source[source.index("MISSING PACKAGES"):source.index('main.py --voice')]

    assert '"%PY%" -m pip install -r requirements.txt' in guard, (
        "the fix command does not name the interpreter ZEUS runs on")
    assert "bare" in guard.lower(), "the two-Pythons trap is not explained"
    # And the plain form must not be offered as the instruction.
    assert "echo        pip install -r requirements.txt" not in guard
