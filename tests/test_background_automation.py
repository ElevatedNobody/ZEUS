import sys
import time
import shutil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import core.builtin_tools as bt
from core.background_events import pop_all_events, push_event
from core.security_manager import security_manager

security_manager.set_confirm_callback(lambda msg: True)

WATCH_DIR = Path("/tmp/medha_test_watch_dir")


def _cleanup():
    shutil.rmtree(WATCH_DIR, ignore_errors=True)
    pop_all_events()  # drain queue between tests


def test_watch_folder_detects_real_new_file():
    _cleanup()
    WATCH_DIR.mkdir(parents=True, exist_ok=True)
    r = bt.watch_folder(str(WATCH_DIR))
    assert "watching" in r
    task_id = r["task_id"]

    time.sleep(0.4)
    (WATCH_DIR / "created_file.txt").write_text("hello")
    time.sleep(0.8)

    events = pop_all_events()
    assert any("created_file.txt" in msg for _ts, msg in events), f"No event captured: {events}"

    stop_r = bt.stop_watching_folder(str(WATCH_DIR))
    assert "stopped" in stop_r
    _cleanup()


def test_watch_folder_rejects_nonexistent_directory():
    r = bt.watch_folder("/tmp/definitely_does_not_exist_xyz_123")
    assert "error" in r


def test_stop_watching_with_no_active_watch_reports_clearly():
    r = bt.stop_watching_folder("/some/folder/nobody/is/watching")
    assert "error" in r


def test_remind_me_fires_after_real_delay():
    pop_all_events()
    r = bt.remind_me("test reminder", minutes=0.01)  # ~0.6s
    assert "reminder_set" in r
    assert pop_all_events() == []  # nothing yet
    time.sleep(1.2)
    events = pop_all_events()
    assert any("test reminder" in msg for _ts, msg in events), f"Reminder never fired: {events}"


def test_cancel_reminder_prevents_firing():
    pop_all_events()
    r = bt.remind_me("should be cancelled", minutes=0.02)  # ~1.2s
    task_id = r["task_id"]
    cancel_r = bt.cancel_reminder(task_id)
    assert "cancelled" in cancel_r
    time.sleep(1.5)
    events = pop_all_events()
    assert not any("should be cancelled" in msg for _ts, msg in events), \
        "Cancelled reminder fired anyway!"


def test_cancel_nonexistent_reminder_reports_clearly():
    r = bt.cancel_reminder(999999)
    assert "error" in r


def test_background_events_queue_drains_correctly():
    pop_all_events()  # start clean
    push_event("event one")
    push_event("event two")
    events = pop_all_events()
    assert [msg for _ts, msg in events] == ["event one", "event two"]
    # Queue should be empty after draining
    assert pop_all_events() == []


# --- routing ---

def test_watch_folder_command_routes_correctly():
    from core.decision_engine import DecisionEngine
    engine = DecisionEngine()
    direct = engine._match_direct_command("watch folder /tmp/downloads")
    assert direct == ("watch_folder", {"path": "/tmp/downloads"})


def test_remind_me_command_routes_correctly_both_phrasings():
    from core.decision_engine import DecisionEngine
    engine = DecisionEngine()
    d1 = engine._match_direct_command("remind me to call mom in 5 minutes")
    assert d1 == ("remind_me", {"message": "call mom", "minutes": 5.0})
    d2 = engine._match_direct_command("remind me in 10 minutes to check the oven")
    assert d2 == ("remind_me", {"message": "check the oven", "minutes": 10.0})


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
    _cleanup()
    sys.exit(1 if fails else 0)
