"""
The start-up and rendering fixes (2026-09-24), each tied to a measured cause:

* every Ollama call through "localhost" waited ~2 s for a refused IPv6 connect
* model discovery + capability probes held the login screen back ~17 s
* the Whisper preload held the main window back
* a second double-click started a second ZEUS
* the wave animation advanced per frame (4x fast on 240 Hz) and stacked a new
  loop on every hide/show; an infinite CSS animation kept Qt WebEngine
  rendering at full refresh rate forever
* START.bat left a console window for Ollama and guessed a 4 s wait
"""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ============================================================ Ollama address ==

def test_localhost_is_rewritten_to_ipv4_loopback():
    from core.ollama_client import _prefer_ipv4_loopback
    assert _prefer_ipv4_loopback("http://localhost:11434") == "http://127.0.0.1:11434"
    assert _prefer_ipv4_loopback("http://LOCALHOST") == "http://127.0.0.1"
    assert _prefer_ipv4_loopback("http://127.0.0.1:11434") == "http://127.0.0.1:11434"
    assert _prefer_ipv4_loopback("http://192.168.1.5:11434") == "http://192.168.1.5:11434"


def test_the_configured_host_never_goes_through_localhost():
    from core.ollama_client import _LOCAL_DEFAULT, _load_host
    assert "localhost" not in _LOCAL_DEFAULT
    assert "localhost" not in _load_host()
    assert "localhost" not in (ROOT / "config.ini").read_text(encoding="utf-8").split("[ollama]")[1].split("[")[0]


def test_ollama_calls_reuse_one_ssl_context_and_skip_proxy_lookup(monkeypatch):
    import httpx
    from core import ollama_client as oc
    seen = []
    monkeypatch.setattr(httpx, "get", lambda url, **kw: seen.append(kw) or types.SimpleNamespace(status_code=200))
    oc._get("http://127.0.0.1:11434/api/tags", timeout=1)
    oc._get("http://127.0.0.1:11434/api/tags", timeout=1)
    assert seen[0]["trust_env"] is False and seen[0]["timeout"] == 1
    assert seen[0]["verify"] is seen[1]["verify"], "a new SSL context per call"


# ========================================================== single instance ==

@pytest.mark.skipif(sys.platform != "win32", reason="Windows named mutex")
def test_a_second_agni_from_the_same_folder_is_refused():
    code = ("import sys, time; sys.path.insert(0, r'%s'); import main; "
            "print(main.claim_single_instance(), flush=True); time.sleep(8)") % ROOT
    first = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True, cwd=ROOT)
    try:
        assert first.stdout.readline().strip() == "True"
        second = subprocess.run([sys.executable, "-c", code.replace("time.sleep(8)", "")],
                                capture_output=True, text=True, cwd=ROOT, timeout=60)
        assert second.stdout.strip().splitlines()[-1] == "False"
    finally:
        first.kill()


def test_start_bat_treats_already_running_as_success():
    source = (ROOT / "START.bat").read_text(encoding="utf-8")
    assert 'if "%RESULT%"=="3" exit /b 0' in source
    assert "sys.exit(3)" in (ROOT / "main.py").read_text(encoding="utf-8")


# ======================================================== start-up ordering ==

def test_the_login_screen_does_not_wait_for_the_model_probes(monkeypatch):
    import main
    monkeypatch.setattr(main, "discover_local_models", lambda: time.sleep(4))
    monkeypatch.setattr(main, "check_capabilities", lambda: time.sleep(4))
    monkeypatch.setattr(main, "authenticate", lambda use_ui: True)
    started = time.time()
    assert main.start_up(use_ui=True) is True
    assert time.time() - started < 2, "start_up blocked on discovery/readiness"


def test_the_terminal_start_keeps_the_probes_in_line(monkeypatch):
    import main
    order = []
    monkeypatch.setattr(main, "discover_local_models", lambda: order.append("discover"))
    monkeypatch.setattr(main, "check_capabilities", lambda: order.append("capabilities"))
    monkeypatch.setattr(main, "authenticate", lambda use_ui: order.append("login") or True)
    main.start_up(use_ui=False)
    assert order == ["discover", "capabilities", "login"]


def test_plugins_register_once_however_often_discovery_runs():
    from plugins import plugin_loader
    from core.tool_registry import registry
    first = plugin_loader.discover_plugins()
    count = len(registry.list_tools())
    again = plugin_loader.discover_plugins()
    assert again == first and len(registry.list_tools()) == count


# ===================================================== background STT load ==

def test_the_speech_model_loads_once_even_when_two_callers_race():
    from voice.speech_to_text import SpeechToText
    loads = []

    class SlowModel:
        def __init__(self, *a, **k):
            loads.append(1)
            time.sleep(0.3)

    fake = types.ModuleType("faster_whisper")
    fake.WhisperModel = SlowModel
    saved = sys.modules.get("faster_whisper")
    sys.modules["faster_whisper"] = fake
    try:
        stt = SpeechToText.__new__(SpeechToText)
        stt._model, stt.is_available, stt.last_error = None, True, None
        stt._load_lock = threading.Lock()
        stt._device, stt._compute_type, stt._model_size = "cpu", "int8", "base"
        stt.model_dir = ROOT
        workers = [threading.Thread(target=stt._model_or_none) for _ in range(3)]
        for w in workers:
            w.start()
        for w in workers:
            w.join()
        assert len(loads) == 1 and isinstance(stt._model, SlowModel)
    finally:
        if saved is not None:
            sys.modules["faster_whisper"] = saved
        else:
            sys.modules.pop("faster_whisper", None)


def test_the_window_path_asks_for_a_background_preload():
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "speech_to_text.BACKGROUND_PRELOAD = True" in source
    stt = (ROOT / "voice" / "speech_to_text.py").read_text(encoding="utf-8")
    assert 'name="agni-stt-preload"' in stt


# ================================================================ rendering ==

# The rendering rules (elapsed-time wave, one loop, nothing animating forever,
# polling that rests while hidden) are pinned against the ZEUS interface in
# tests/test_no_previous_ui.py.


# ================================================================ START.bat ==

def test_start_bat_is_rebuilt():
    raw = (ROOT / "START.bat").read_bytes()
    source = raw.decode("ascii")
    assert b"\r\n" in raw and raw.count(b"\n") == raw.count(b"\r\n"), "START.bat must be CRLF"
    assert "EnableDelayedExpansion" not in source, "breaks paths that contain '!'"
    assert 'start "Ollama" /min' not in source, "leaves a console window to close by accident"
    assert "timeout /t 4" not in source, "a guessed wait instead of a readiness check"
    assert "chcp 65001" in source and 'set "PYTHONUTF8=1"' in source
    assert 'set "AGNI_CONSOLE_LEVEL=WARNING"' in source
    assert '"%PY%" "scripts\\start_check.py"' in source
    assert "Get-Content -Path 'logs\\medha.log' -Tail 25" in source


def _start_check():
    spec = importlib.util.spec_from_file_location("start_check", ROOT / "scripts" / "start_check.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_start_check_leaves_a_running_ollama_alone(monkeypatch, capsys):
    check = _start_check()
    monkeypatch.setattr(check, "ollama_up", lambda timeout=1.5: True)
    monkeypatch.setattr(check, "start_hidden", lambda exe: pytest.fail("started a second Ollama"))
    assert check.main() == 0
    assert "running" in capsys.readouterr().out


def test_start_check_starts_ollama_hidden_and_waits_until_it_answers(monkeypatch, capsys):
    check = _start_check()
    answers = iter([False, False, False, True])
    started = []
    monkeypatch.setattr(check, "ollama_up", lambda timeout=1.5: next(answers))
    monkeypatch.setattr(check, "find_ollama", lambda: "C:/ollama.exe")
    monkeypatch.setattr(check, "start_hidden", lambda exe: started.append(exe) or True)
    monkeypatch.setattr(check.time, "sleep", lambda s: None)
    assert check.main() == 0
    assert started == ["C:/ollama.exe"]
    assert "running" in capsys.readouterr().out


def test_start_check_never_blocks_the_start_without_ollama(monkeypatch, capsys):
    check = _start_check()
    monkeypatch.setattr(check, "ollama_up", lambda timeout=1.5: False)
    monkeypatch.setattr(check, "find_ollama", lambda: "")
    assert check.main() == 0
    assert "NOT INSTALLED" in capsys.readouterr().out


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process flags")
def test_ollama_is_started_without_a_window():
    source = (ROOT / "scripts" / "start_check.py").read_text(encoding="utf-8")
    assert "CREATE_NO_WINDOW" in source and "0x00000008" in source


def test_the_console_can_be_quiet_while_the_file_keeps_everything(tmp_path):
    code = ("import sys, time; sys.path.insert(0, r'%s'); "
            "from core.logger import setup_logger, get_logger; setup_logger(); "
            "log = get_logger('t'); log.info('INFO-LINE'); log.warning('WARN-LINE'); time.sleep(0.5)") % ROOT
    env = dict(os.environ, AGNI_CONSOLE_LEVEL="WARNING", AGNI_LOG_DIR=str(tmp_path))
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=60)
    assert "WARN-LINE" in result.stderr and "INFO-LINE" not in result.stderr
    assert "INFO-LINE" in (tmp_path / "medha.log").read_text(encoding="utf-8")
