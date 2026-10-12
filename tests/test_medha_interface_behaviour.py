"""
The behaviours the previous interface's tests pinned, checked against ZEUS.

The old window tests (test_command_center*, test_dashboard_*, test_access_window)
went with the old window. What they protected did not: one request at a time
with a clear refusal, nothing slow on the GUI thread, file results summarised
instead of dumped, no text under ten pixels, a sign-in screen that states only
what it checked and never shows secrets. Live-page behaviour (screens, pipeline,
approvals, lock, idle frames) is covered by tests/e2e/test_medha_e2e.py.
"""

import re
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
ZEUS = ROOT / "ui" / "medha"


def _js(name):
    return (ZEUS / "js" / name).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def Bridge():
    pytest.importorskip("PySide6.QtWebEngineCore")
    from ui import agni_shell
    return agni_shell._build_classes()[3]


# ------------------------------------------------------ one request at a time --

def test_a_second_request_while_one_runs_is_refused_out_loud(Bridge):
    started, release = threading.Event(), threading.Event()

    def on_message(text):
        started.set()
        release.wait(5)
        return {"response": f"did {text}"}

    bridge = Bridge(SimpleNamespace(on_message=on_message, on_listen=lambda: {"transcript": "x"}))
    first = {}
    worker = threading.Thread(target=lambda: first.update(bridge._m_submit("first")))
    worker.start()
    assert started.wait(5)
    second = bridge._m_submit("second")
    assert second["busy"] is True and "not started" in second["response"]
    assert bridge._m_listen()["busy"] is True, "voice must not start an engine run alongside a typed one"
    release.set()
    worker.join(5)
    assert first["response"] == "did first"
    assert bridge._m_submit("third")["response"] == "did third"
    assert [h["request"] for h in bridge._history] == ["first", "third"]


def test_the_busy_reply_is_never_shared_state(Bridge):
    from ui.agni_shell import BUSY_REPLY
    bridge = Bridge(SimpleNamespace(on_message=lambda t: {"response": t}, on_listen=None))
    bridge._engine.acquire()
    try:
        reply = bridge._m_submit("x")
        reply["response"] = "changed"
        assert BUSY_REPLY["response"] != "changed"
    finally:
        bridge._engine.release()


def test_empty_input_does_nothing(Bridge):
    calls = []
    bridge = Bridge(SimpleNamespace(on_message=lambda t: calls.append(t) or {"response": t}, on_listen=None))
    assert bridge._m_submit("   ") == {"response": ""}
    assert calls == [] and bridge._history == []
    shell = _js("shell.js")
    assert "if (!text) return;" in shell


def test_the_page_refuses_a_second_request_and_keeps_the_text():
    shell = _js("shell.js")
    submit = shell[shell.index("async function submit(text)"):shell.index("M.submit = submit;")]
    guard = submit.index("if (busy())")
    cleared = submit.rindex('M.$("#cmd").value = "";', 0, submit.index("const turn = {"))
    assert guard < cleared, "the text is cleared before the refusal"
    assert "was not sent" in submit
    assert "turn.pending" in shell and "const busy = () => conversation.some" in shell


def test_nothing_slow_runs_on_the_gui_thread():
    """Password hashing, the engine and the pipeline run on worker threads -
    the window keeps painting while they work."""
    from ui.bridge_contract import METHODS
    for method in ("submit", "listen", "ask_company", "auth_login", "auth_setup", "auth_unlock",
                   "account_password", "account_create", "vault_open"):
        assert METHODS[method][2] == "worker", method


# ------------------------------------------------------------- file results --

def test_producing_a_file_is_summarised_not_dumped(tmp_path):
    from core.decision_engine import DecisionEngine
    report = tmp_path / "maintenance_report_P-204.docx"
    report.write_bytes(b"docx")
    payload = {"saved": str(report), "format": "docx", "bytes": 37879, "verified": True, "records": 3,
               "narrative_included": False, "demo_data": True, "analysis": {"mtbf": 12.0},
               "paragraphs": 40, "opened": False, "equipment": "P-204"}
    text = DecisionEngine._format_tool_output("maintenance_report", payload)
    assert "bytes:" not in text and "analysis:" not in text and " | " not in text
    assert "Produced maintenance_report_P-204.docx (DOCX)" in text
    assert "reopened and parsed" in text and "3 records" in text and "DEMO" in text
    assert f"File saved to {report}" in text


def test_a_file_that_failed_its_check_or_is_gone_is_said_so(tmp_path):
    from core.agni_present import present
    missing = tmp_path / "deck.pptx"
    text = present("create_presentation", {"saved": str(missing), "format": "pptx", "verified": False,
                                           "slides": 5, "slides_failed": ["Bad Slide"]})
    assert "did NOT pass" in text and "no file is there now" in text
    assert "1 slide(s) could not be generated: Bad Slide" in text
    assert "saved to" not in text.lower()


def test_a_tool_that_reads_a_file_is_not_described_as_producing_one(tmp_path):
    from core.agni_present import present
    source = tmp_path / "input.pdf"
    source.write_bytes(b"%PDF")
    text = present("summarize_document", {"path": str(source), "summary": "About pumps."}) or ""
    assert "Produced" not in text


# ------------------------------------------------------------------ legibility --

def test_no_text_is_drawn_below_ten_pixels():
    sizes = []
    for css in (ZEUS / "css").glob("*.css"):
        sizes += [(css.name, float(v)) for v in re.findall(r"font-size:\s*([0-9.]+)px", css.read_text(encoding="utf-8"))]
        sizes += [(css.name, float(v)) for v in re.findall(r"--fs-\d+:\s*([0-9.]+)px", css.read_text(encoding="utf-8"))]
    for js in (ZEUS / "js").rglob("*.js"):
        sizes += [(js.name, float(v)) for v in re.findall(r'font-size="([0-9.]+)"', js.read_text(encoding="utf-8"))]
    small = [(name, size) for name, size in sizes if size < 10]
    assert sizes and not small, small


# -------------------------------------------------------------------- sign-in --

def test_a_mismatched_confirmation_is_caught_before_any_account_exists():
    from auth import web_login
    created = []
    accounts = SimpleNamespace(first_run_required=lambda: True,
                               create_account=lambda *a, **k: created.append(a) or {"id": 1})
    ctl = web_login.AuthController(session_manager=SimpleNamespace(session=None), auth_manager=accounts)
    result = ctl.setup("first.admin", "correct horse battery", "correct horse batterY")
    assert result["ok"] is False and "do not match" in result["message"]
    assert created == []


def test_the_sign_in_state_carries_no_secret():
    source = (ROOT / "auth" / "web_login.py").read_text(encoding="utf-8")
    state = source[source.index("    def state(self)"):source.index("    def setup(")]
    for secret in ("password_hash", "salt", "template", "embedding", "totp_secret", "token"):
        assert secret not in state, secret


def test_passwords_are_never_echoed_or_remembered_by_the_page():
    auth = _js("auth.js")
    assert 'autocomplete: "off"' in auth
    assert re.search(r'field\("password", "[^"]+", "password"\)', auth)
    assert "localStorage" not in auth and "sessionStorage" not in auth
    assert "console.log" not in auth


def test_escape_does_not_dismiss_the_sign_in_screen():
    auth = _js("auth.js")
    assert "Escape" not in auth
    core = _js("core.js")
    escape_lines = [line for line in core.splitlines() if "Escape" in line]
    assert escape_lines and all("scrim" in line for line in escape_lines), "Escape only closes dialogs"


def test_the_sign_in_screen_claims_nothing_the_backend_did_not_say():
    auth = _js("auth.js")
    for claim in ("SECURE", "PROTECTED"):
        assert f'"{claim}"' not in auth
    # ACCESS GRANTED is drawn only on a backend "ok", or when the backend
    # reports the workspace ready for a signed-in session.
    for line in auth.splitlines():
        if 'setState("ACCESS GRANTED")' in line:
            assert "if (r.ok)" in line or line.strip() == 'setState("ACCESS GRANTED");', line
    calls = [line.strip() for line in auth.splitlines() if "finish()" in line and "function finish" not in line]
    assert calls and all('state === "ready"' in line for line in calls), calls
    assert 'if (state.phase !== "signed_in") return refresh();' in auth


def test_closing_the_window_releases_the_camera():
    shell = (ROOT / "ui" / "agni_shell.py").read_text(encoding="utf-8")
    close = shell[shell.index("def closeEvent"):]
    assert "self.auth.face_cancel()" in close[:600]
    assert "self._events_timer.stop()" in close[:900]


# ------------------------------------------------------ the Voice Core design --

def test_the_design_mock_numbers_never_reach_the_interface():
    """The supplied design showed fixed readings (82,450 BPD, 4,120 PSI, a 43/22/18/9
    product split, '48 kHz'). The interface draws those panels from plant_telemetry
    and says '—' where a value is missing - no number is typed into the page."""
    everything = "\n".join(p.read_text(encoding="utf-8") for p in (ZEUS / "js").rglob("*.js"))
    for invented in ("82,450", "82450", "4,120", "34.2", "12,340", "142 PSI", "48 kHz", "+4.8%",
                     "ACOUSTIC STREAM NOMINAL", "SYNCHRONIZED", "Subsea Trunk Line", "ZEUS", "PetroVision"):
        assert invented not in everything, invented
    home = _js("screens/command.js")
    assert 'M.call("telemetry")' in home and "change_pct_vs_previous" in home
    assert "M.chip(\"DEMO DATA\", \"sample\")" in home, "demo readings are labelled on the home screen"


def test_mic_and_audio_report_what_the_voice_stack_can_do():
    shell = _js("shell.js")
    for label in ("MIC: UNAVAILABLE", "MIC: READY", "AUDIO: UNAVAILABLE", "AUDIO: MUTED", "AUDIO: ON"):
        assert label in shell, label
    assert "mic.disabled = !v.can_listen" in shell and "sp.disabled = !v.can_speak" in shell


def test_the_mic_shows_the_recognisers_real_stage_never_its_own_guess():
    """LISTENING used to be shown the moment the button was pressed - while Whisper
    was still loading, so the first words were lost. The stages now come from the
    backend (voice/speech_to_text.py publish_stage); the page only says STARTING."""
    shell = _js("shell.js")
    assert 'M.on("stt", onStt)' in shell and "`MIC: ${stage}`" in shell
    assert "MIC: LISTENING" not in shell
    from voice.speech_to_text import STT_STAGES
    assert STT_STAGES == ("MIC READY", "PREPARING RECOGNISER", "LISTENING", "SPEECH DETECTED", "TRANSCRIBING",
                          "TRANSCRIBED", "NO SPEECH", "MIC ERROR", "STT ERROR")
    source = (ZEUS.parent.parent / "ui" / "agni_shell.py").read_text(encoding="utf-8")
    assert "subscribe_stage(self._on_stt_stage)" in source and 'self.bridge.push("stt"' in source


def test_the_voice_line_only_says_listening_while_listening():
    shell = _js("shell.js")
    table = shell[shell.index("const VOICE_LINE"):shell.index("};", shell.index("const VOICE_LINE"))]
    assert 'LISTENING: "I\'m listening"' in table
    assert table.count("I'm listening") == 1, "no other state may claim to be listening"


def test_cinematic_view_keeps_escape_for_dialogs_first():
    shell = _js("shell.js")
    handler = shell[shell.index('if (e.key !== "Escape"'):shell.index("toggleCinema();\n    });")]
    assert 'M.$(".scrim")' in handler and "closeSheet()" in handler


def test_a_panel_refresh_is_not_the_assistant_working():
    """The voice line and the wave show the assistant working on a request. A
    SAFE read the interface makes to fill a panel must not set the state - it
    kept the wave moving on an idle home screen (e2e idle test, 2026-09-27)."""
    source = (ROOT / "core" / "tool_registry.py").read_text(encoding="utf-8")
    assert 'panel_read = decision.origin == "ui" and tool.risk == RiskLevel.SAFE' in source
    assert "activity = None if panel_read else assistant_state.activity(" in source


def test_column_names_keep_their_underscores_in_answers():
    """'Monthly_Rent_INR' rendered as 'MonthlyRentINR' with 'Rent' in italics:
    emphasis must start and end at a word boundary."""
    core = _js("core.js")
    assert "(^|[\s(])_([^_]{3,}?)_(?=[\s.,;:)!?]|$)" in core
    assert '.replace(/_([^_]{3,})_/g' not in core
