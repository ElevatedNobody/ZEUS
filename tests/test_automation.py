import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Mock optional automation backends before importing builtin_tools, since
# they aren't installed in this test environment (no display available).
sys.modules.setdefault("pyautogui", MagicMock())
sys.modules.setdefault("pyperclip", MagicMock())

import core.builtin_tools as bt
from core.decision_engine import DecisionEngine
from core.security_manager import security_manager

security_manager.set_confirm_callback(lambda msg: True)
engine = DecisionEngine()


def test_type_text_calls_pyautogui():
    with patch.object(bt, "open_application"):
        pass  # no-op, just ensuring module state is clean
    import pyautogui as mocked
    mocked.write.reset_mock()
    r = bt.type_text("hello")
    assert r["typed_chars"] == 5
    mocked.write.assert_called_once()


def test_hotkey_splits_combo_correctly():
    import pyautogui as mocked
    mocked.hotkey.reset_mock()
    bt.hotkey("ctrl+shift+esc")
    mocked.hotkey.assert_called_once_with("ctrl", "shift", "esc")


def test_clipboard_round_trip_calls_pyperclip():
    import pyperclip as mocked
    bt.set_clipboard("test value")
    mocked.copy.assert_called_with("test value")


def test_window_management_matches_partial_title():
    class FakeWindow:
        def __init__(self, title):
            self.title = title
            self.minimized = False

        def minimize(self):
            self.minimized = True

    fake = FakeWindow("Untitled - Notepad")
    mock_gw = MagicMock()
    mock_gw.getAllWindows.return_value = [fake]
    with patch.dict(sys.modules, {"pygetwindow": mock_gw}):
        r = bt.minimize_window("notepad")
        assert r["minimized"] == "Untitled - Notepad"
        assert fake.minimized is True


def test_window_management_reports_no_match_clearly():
    mock_gw = MagicMock()
    mock_gw.getAllWindows.return_value = []
    with patch.dict(sys.modules, {"pygetwindow": mock_gw}):
        r = bt.minimize_window("nonexistent_window_xyz")
        assert "error" in r


def test_open_and_type_propagates_open_failure():
    with patch.object(bt, "open_application", return_value={"error": "app not found"}):
        r = bt.open_and_type("bad_app", "text", wait_seconds=0.01)
        assert "error" in r
        assert "bad_app" in r["error"]


def test_open_and_type_types_after_successful_open():
    import pyautogui as mocked
    mocked.write.reset_mock()
    with patch.object(bt, "open_application", return_value={"launched": "notepad"}):
        r = bt.open_and_type("notepad", "hello from test", wait_seconds=0.01)
        assert r["typed_chars"] == len("hello from test")
        mocked.write.assert_called_once()


def test_read_screen_fails_gracefully_without_crash():
    r = bt.read_screen()
    assert isinstance(r, dict)
    assert "error" in r or "text" in r


# --- routing precedence: these previously had real ordering bugs ---

def test_compound_open_and_type_not_swallowed_by_generic_open():
    direct = engine._match_direct_command("open notepad and type hello world")
    assert direct == ("open_and_type", {"app": "notepad", "text": "hello world"})


def test_plain_open_still_works_after_compound_route_added():
    direct = engine._match_direct_command("open chrome")
    assert direct == ("open_application", {"name": "chrome"})


def test_clipboard_copy_not_swallowed_by_generic_copy_file():
    direct = engine._match_direct_command("copy hello to clipboard")
    assert direct == ("set_clipboard", {"text": "hello"})


def test_file_copy_still_works_after_clipboard_route_added():
    direct = engine._match_direct_command("copy report.txt to backup.txt")
    assert direct == ("copy_file", {"src": "report.txt", "dst": "backup.txt"})


def test_read_screen_not_swallowed_by_generic_read_file():
    direct = engine._match_direct_command("read my screen")
    assert direct == ("read_screen", {})


def test_read_file_still_works_after_screen_route_added():
    direct = engine._match_direct_command("read file notes.txt")
    assert direct == ("read_file", {"path": "notes.txt"})


def test_type_text_routes_correctly():
    direct = engine._match_direct_command("type hello world")
    assert direct == ("type_text", {"text": "hello world"})


def test_hotkey_routes_correctly():
    direct = engine._match_direct_command("hotkey ctrl+s")
    assert direct == ("hotkey", {"combo": "ctrl+s"})


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
            except Exception as e:
                fails += 1
                print(f"ERROR {name}: {e}")
    sys.exit(1 if fails else 0)
