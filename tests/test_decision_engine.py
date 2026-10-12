import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.decision_engine import DecisionEngine
from core.security_manager import security_manager

security_manager.set_confirm_callback(lambda msg: True)
engine = DecisionEngine()


def test_calculate_executes_directly():
    r = engine.process("calculate 12*8", {"history": []})
    assert "96" in r["response"]


def test_what_is_arithmetic_executes_directly():
    r = engine.process("what is 5+5", {"history": []})
    assert "10" in r["response"]


def test_system_info_executes_directly():
    r = engine.process("system info", {"history": []})
    assert "os:" in r["response"]


def test_time_executes_directly():
    r = engine.process("what is the time", {"history": []})
    assert "time:" in r["response"]


def test_create_and_delete_folder_roundtrip():
    r1 = engine.process("create folder /tmp/medha_engine_test", {"history": []})
    assert "created" in r1["response"]
    import shutil
    shutil.rmtree("/tmp/medha_engine_test", ignore_errors=True)


def test_open_application_routes_to_tool_not_chat():
    r = engine.process("open notepad", {"history": []})
    # Should be a COMMAND (tool attempt), never silently become a QUESTION/chat call.
    assert r["intent"] == "COMMAND"


def test_unmatched_arithmetic_like_text_falls_back_to_chat():
    r = engine.process("what is the meaning of life", {"history": []})
    assert r["intent"] == "QUESTION"


def test_help_lists_tool_catalogue():
    r = engine.process("help", {"history": []})
    assert "get_system_info" in r["response"]


def test_write_document_generates_and_saves_docx():
    from unittest.mock import patch
    realistic_content = (
        "This is an introductory paragraph with enough words to pass content validation "
        "thresholds and set up the topic clearly for the reader who is following along.\n\n"
        "This is the body paragraph developing the main points in detail with supporting "
        "examples that elaborate on the core ideas presented in the introduction above.\n\n"
        "This is the concluding paragraph that wraps up the discussion and summarizes the "
        "key takeaways for the reader before the document ends here."
    )
    with patch("core.ollama_client.OllamaClient.is_available", return_value=True), \
         patch("core.ollama_client.OllamaClient.chat", return_value=realistic_content):
        r = engine.process("write an essay about gravity", {"history": []})
        assert r["intent"] == "COMMAND"
        assert "saved" in r["response"]
        assert ".docx" in r["response"] or ".txt" in r["response"]


def test_write_document_fails_clearly_without_ollama():
    from unittest.mock import patch
    with patch("core.ollama_client.OllamaClient.is_available", return_value=False):
        r = engine.process("write a report on climate change", {"history": []})
        assert "ollama" in r["response"].lower()


def test_app_name_resolution_maps_friendly_names():
    from core.builtin_tools import _resolve_app_name
    assert _resolve_app_name("chrome") == "chrome.exe"
    assert _resolve_app_name("VS Code") == "code.exe"
    assert _resolve_app_name("totally_unknown_app") == "totally_unknown_app"


def test_fuzzy_match_handles_typos_like_whatsappmka():
    from core.builtin_tools import _fuzzy_best_match
    candidates = ["WhatsApp", "Word", "Zoom Workplace", "Notepad", "Xbox"]
    assert _fuzzy_best_match("whatsappmka", candidates) == "WhatsApp"
    assert _fuzzy_best_match("powerpiont", ["PowerPoint", "Power Automate"]) == "PowerPoint"


def test_open_application_uses_start_menu_shortcut_when_found():
    import sys
    from unittest.mock import patch
    import core.builtin_tools as bt
    with patch.object(sys, "platform", "win32"), \
         patch.object(bt, "_find_start_menu_shortcut", return_value="C:\\Start Menu\\WhatsApp.lnk"), \
         patch("os.startfile", create=True) as mock_startfile:
        result = bt.open_application("whatsappmka")
        assert result["method"] == "start-menu-shortcut"
        mock_startfile.assert_called_once_with("C:\\Start Menu\\WhatsApp.lnk")


def test_open_application_falls_back_to_store_aumid():
    import sys
    from unittest.mock import patch
    import core.builtin_tools as bt
    with patch.object(sys, "platform", "win32"), \
         patch.object(bt, "_find_start_menu_shortcut", return_value=None), \
         patch("subprocess.Popen") as mock_popen:
        result = bt.open_application("calculator")
        assert result["method"] == "store-aumid"
        assert "WindowsCalculator" in mock_popen.call_args[0][0][1]


def test_write_document_matches_natural_phrasings():
    from core.decision_engine import DecisionEngine
    engine2 = DecisionEngine()
    phrasings = [
        "write my assignment on photosynthesis",
        "can you write my assignment on climate change",
        "please write a report for renewable energy",
        "could you write me a paper on quantum computing",
    ]
    for p in phrasings:
        direct = engine2._match_direct_command(p)
        assert direct is not None and direct[0] == "write_document", f"failed on: {p!r}"


def test_open_browser_not_shadowed_by_generic_open_pattern():
    r = engine._match_direct_command("open browser")
    assert r == ("open_browser", {})


def test_open_settings_not_shadowed_by_generic_open_pattern():
    r = engine._match_direct_command("open settings")
    assert r == ("open_settings", {})


def test_list_memories_works_without_prior_db_init():
    # Simulates a fresh DB with no facts table yet - must not crash.
    r = engine.process("what do you remember", {"history": []})
    assert r["intent"] == "COMMAND"
    assert "error" not in r["response"].lower() or "nothing remembered" in r["response"].lower()


if __name__ == "__main__":
    import inspect
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and inspect.isfunction(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                fails += 1
                print(f"FAIL {name}: {e}")
    sys.exit(1 if fails else 0)
