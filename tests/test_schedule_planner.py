import sys
import json
import shutil
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import core.builtin_tools as bt
from core.decision_engine import DecisionEngine
from core.security_manager import security_manager
from memory import database

security_manager.set_confirm_callback(lambda msg: True)
engine = DecisionEngine()

DOCS_DIR = Path.home() / "Documents" / "ZEUS_Documents"

READINESS_OK = json.dumps({"ready": True, "title": "Test Weekly Plan"})


def _day_json(day, items=None, exercise=None, summary="On track."):
    d = {"day": day, "items": items or [], "summary": summary}
    if exercise:
        d["exercise"] = exercise
    return json.dumps(d)


def _full_week_sequence(overrides: dict = None):
    """Build the standard 8-call sequence (readiness + 7 days) that
    create_schedule now makes, with optional per-day overrides."""
    overrides = overrides or {}
    days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    seq = [READINESS_OK]
    for d in days:
        seq.append(overrides.get(d, _day_json(d)))
    return seq


def _cleanup():
    shutil.rmtree(DOCS_DIR, ignore_errors=True)
    database.init_db()
    conn = database.get_connection()
    try:
        conn.execute("DELETE FROM todos")
        conn.execute("DELETE FROM calendar_events")
        conn.execute("DELETE FROM schedules")
        conn.commit()
    finally:
        conn.close()


# --- routing (unaffected by the architecture change) ---

def test_plan_my_week_routes_to_create_schedule():
    text = "plan my week: classes Mon 9-11am, work Tue 9-5, want 2hrs study daily"
    direct = engine._match_direct_command(text)
    assert direct[0] == "create_schedule"
    assert direct[1]["details"] == text


def test_update_my_schedule_routes_to_update_not_create():
    direct = engine._match_direct_command("update my schedule: move gym to 7am on Tuesday")
    assert direct[0] == "update_schedule"


def test_unrelated_commands_not_swallowed_by_schedule_triggers():
    assert engine._match_direct_command("what is the time")[0] == "get_time"
    assert engine._match_direct_command("write an assignment on gravity")[0] == "write_document"


# --- create_schedule: day-by-day generation ---

def test_create_schedule_makes_one_readiness_call_plus_one_per_day():
    _cleanup()
    seq = _full_week_sequence({
        "Monday": _day_json("Monday", items=[
            {"time": "09:00-11:00", "activity": "Class: Calculus", "category": "class", "checkbox": True},
        ], exercise=[{"exercise": "Bench Press", "sets_reps": "4x8"}], summary="Study target met."),
    })
    with patch("core.ollama_client.OllamaClient.is_available", return_value=True), \
         patch("core.ollama_client.OllamaClient.chat", side_effect=seq) as mock_chat:
        r = bt.create_schedule("classes Mon 9-11, rest of week free")
        assert mock_chat.call_count == 8  # 1 readiness + 7 days, NOT one giant call
        assert "saved_pdf" in r
        assert r["days"] == 7
    _cleanup()


def test_create_schedule_produces_valid_pdf_with_all_days():
    _cleanup()
    seq = _full_week_sequence({
        "Monday": _day_json("Monday", items=[
            {"time": "09:00-11:00", "activity": "Class: Calculus", "category": "class", "checkbox": True},
        ], exercise=[{"exercise": "Bench Press", "sets_reps": "4x8"}], summary="Study target met."),
    })
    with patch("core.ollama_client.OllamaClient.is_available", return_value=True), \
         patch("core.ollama_client.OllamaClient.chat", side_effect=seq):
        r = bt.create_schedule("test details")
        from pypdf import PdfReader
        reader = PdfReader(r["saved_pdf"])
        text = reader.pages[0].extract_text()
        assert "Class: Calculus" in text
        assert "Bench Press" in text
        assert "Sunday" in text  # confirms all 7 days actually rendered
    _cleanup()


def test_one_bad_day_does_not_kill_the_whole_plan():
    """This is the actual bug fix: a single malformed day must not fail the
    entire week the way one bad field used to fail the old giant call."""
    _cleanup()
    seq = [READINESS_OK,
           _day_json("Monday"),
           _day_json("Tuesday"),
           "garbage, not json", "still garbage",  # Wednesday fails twice (1 retry)
           _day_json("Thursday"), _day_json("Friday"),
           _day_json("Saturday"), _day_json("Sunday")]
    with patch("core.ollama_client.OllamaClient.is_available", return_value=True), \
         patch("core.ollama_client.OllamaClient.chat", side_effect=seq):
        r = bt.create_schedule("test details")
        assert "saved_pdf" in r  # plan still produced
        assert r["days"] == 6
        assert r["days_failed"] == ["Wednesday"]
    _cleanup()


def test_create_schedule_syncs_real_todos_and_events():
    _cleanup()
    seq = _full_week_sequence({
        "Tuesday": _day_json("Tuesday", items=[
            {"time": "09:00-17:00", "activity": "Work shift", "category": "work", "checkbox": True},
        ]),
    })
    with patch("core.ollama_client.OllamaClient.is_available", return_value=True), \
         patch("core.ollama_client.OllamaClient.chat", side_effect=seq):
        bt.create_schedule("test details")

    events = database.list_events()
    assert any("Work shift" in e["title"] for e in events)
    _cleanup()


def test_create_schedule_asks_clarifying_questions_when_info_missing():
    with patch("core.ollama_client.OllamaClient.is_available", return_value=True), \
         patch("core.ollama_client.OllamaClient.chat",
               return_value=json.dumps({"need_info": ["What time do you wake up?", "Any fixed commitments?"]})):
        r = bt.create_schedule("I want to study more")
        assert "need_info" in r
        assert "saved_pdf" not in r


def test_create_schedule_reports_ollama_offline_clearly():
    with patch("core.ollama_client.OllamaClient.is_available", return_value=False):
        r = bt.create_schedule("test details")
        assert "error" in r
        assert "ollama" in r["error"].lower()


def test_create_schedule_saves_to_database():
    _cleanup()
    seq = _full_week_sequence()
    with patch("core.ollama_client.OllamaClient.is_available", return_value=True), \
         patch("core.ollama_client.OllamaClient.chat", side_effect=seq):
        bt.create_schedule("test details")

    active = database.get_active_schedule()
    assert active is not None
    data = json.loads(active["data"])
    assert data["title"] == "Test Weekly Plan"
    assert len(data["week"]) == 7
    _cleanup()


# --- update_schedule: only the affected day gets regenerated ---

def test_update_schedule_only_calls_model_for_resolve_plus_affected_day():
    """The actual speed fix for updates: NOT a whole-week regeneration -
    one small call to resolve which day, one small call to update it."""
    _cleanup()
    seq = _full_week_sequence({
        "Monday": _day_json("Monday", items=[
            {"time": "18:00-19:00", "activity": "Gym", "category": "exercise", "checkbox": True},
        ]),
    })
    with patch("core.ollama_client.OllamaClient.is_available", return_value=True), \
         patch("core.ollama_client.OllamaClient.chat", side_effect=seq):
        bt.create_schedule("initial plan")

    resolve_response = json.dumps({"days": ["Monday"]})
    updated_monday = _day_json("Monday", items=[
        {"time": "07:00-08:00", "activity": "Gym", "category": "exercise", "checkbox": True},
    ], summary="Moved to morning.")

    with patch("core.ollama_client.OllamaClient.is_available", return_value=True), \
         patch("core.ollama_client.OllamaClient.chat", side_effect=[resolve_response, updated_monday]) as mock_chat:
        r = bt.update_schedule("move Monday gym to 7am")
        assert mock_chat.call_count == 2  # resolve + one day, NOT a whole-week call
        assert r["updated_days"] == ["Monday"]
    _cleanup()


def test_update_schedule_fails_clearly_with_no_active_plan():
    _cleanup()
    r = bt.update_schedule("move gym to 7am")
    assert "error" in r
    assert "no active schedule" in r["error"].lower()


def test_update_schedule_persists_change_and_leaves_other_days_untouched():
    _cleanup()
    seq = _full_week_sequence({
        "Monday": _day_json("Monday", items=[
            {"time": "14:00-16:00", "activity": "Study session", "category": "study", "checkbox": True},
        ]),
        "Tuesday": _day_json("Tuesday", items=[
            {"time": "09:00-17:00", "activity": "Work shift", "category": "work", "checkbox": True},
        ]),
    })
    with patch("core.ollama_client.OllamaClient.is_available", return_value=True), \
         patch("core.ollama_client.OllamaClient.chat", side_effect=seq):
        bt.create_schedule("initial plan")

    resolve_response = json.dumps({"days": ["Monday"]})
    updated_monday = _day_json("Monday", items=[
        {"time": "15:00-17:00", "activity": "Study session", "category": "study", "checkbox": True},
    ], summary="Moved to 3pm.")

    with patch("core.ollama_client.OllamaClient.is_available", return_value=True), \
         patch("core.ollama_client.OllamaClient.chat", side_effect=[resolve_response, updated_monday]):
        bt.update_schedule("move Monday study to 3pm")

    active = database.get_active_schedule()
    data = json.loads(active["data"])
    monday = next(d for d in data["week"] if d["day"] == "Monday")
    tuesday = next(d for d in data["week"] if d["day"] == "Tuesday")
    assert monday["items"][0]["time"] == "15:00-17:00"
    assert tuesday["items"][0]["activity"] == "Work shift"  # untouched
    _cleanup()


def test_resyncing_does_not_touch_users_own_todos():
    _cleanup()
    database.add_todo("My own personal task, not from a plan")
    seq = _full_week_sequence()
    with patch("core.ollama_client.OllamaClient.is_available", return_value=True), \
         patch("core.ollama_client.OllamaClient.chat", side_effect=seq):
        bt.create_schedule("initial plan")

    todos = database.list_todos()
    assert any("My own personal task" in t["text"] for t in todos)
    _cleanup()


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
