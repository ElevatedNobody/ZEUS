import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import core.builtin_tools as bt
from core.decision_engine import DecisionEngine
from core.security_manager import security_manager
from memory import database

security_manager.set_confirm_callback(lambda msg: True)
engine = DecisionEngine()


def _clean_tables():
    database.init_db()
    conn = database.get_connection()
    try:
        conn.execute("DELETE FROM todos")
        conn.execute("DELETE FROM calendar_events")
        conn.execute("DELETE FROM notes")
        conn.commit()
    finally:
        conn.close()


# --- todos ---

def test_add_and_complete_todo_round_trip():
    _clean_tables()
    r1 = bt.add_todo_tool("buy milk")
    assert "added" in r1
    todo_id = r1["id"]

    r2 = bt.list_todos_tool()
    assert any("buy milk" in line for line in r2["todos"])

    r3 = bt.complete_todo_tool(todo_id)
    assert r3["completed"] == todo_id

    r4 = bt.list_todos_tool()
    assert any("[x]" in line and "buy milk" in line for line in r4["todos"])
    _clean_tables()


def test_delete_todo_removes_it():
    _clean_tables()
    r1 = bt.add_todo_tool("temporary task")
    bt.delete_todo_tool(r1["id"])
    r2 = bt.list_todos_tool()
    assert r2["todos"] == "Nothing on your list." or not any(
        "temporary task" in line for line in r2.get("todos", [])
    )
    _clean_tables()


def test_complete_nonexistent_todo_reports_clearly():
    r = bt.complete_todo_tool(999999)
    assert "error" in r


# --- calendar ---

def test_add_event_resolves_relative_dates():
    _clean_tables()
    import datetime
    r = bt.add_event_tool("Team sync", date="tomorrow", event_time="09:00")
    expected = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
    assert r["date"] == expected
    _clean_tables()


def test_list_events_filters_by_date():
    _clean_tables()
    bt.add_event_tool("Today's event", date="today")
    bt.add_event_tool("Tomorrow's event", date="tomorrow")

    today_events = bt.list_events_tool(date="today")
    assert any("Today's event" in line for line in today_events["events"])
    assert not any("Tomorrow's event" in line for line in today_events["events"])
    _clean_tables()


def test_delete_event_removes_it():
    _clean_tables()
    r1 = bt.add_event_tool("Removable event", date="today")
    bt.delete_event_tool(r1["id"])
    r2 = bt.list_events_tool()
    assert r2["events"] == "No events found." or not any(
        "Removable event" in line for line in r2.get("events", [])
    )
    _clean_tables()


# --- notes ---

def test_add_and_list_notes():
    _clean_tables()
    r1 = bt.add_note_tool("Remember to water the plants")
    assert "added" in r1
    r2 = bt.list_notes_tool()
    assert any(r1["added"] in line for line in r2["notes"])
    _clean_tables()


def test_delete_note_removes_it():
    _clean_tables()
    r1 = bt.add_note_tool("temp note")
    bt.delete_note_tool(r1["id"])
    r2 = bt.list_notes_tool()
    assert r2["notes"] == "No notes yet." or not any(
        "temp note" in line for line in r2.get("notes", [])
    )
    _clean_tables()


# --- routing precedence (these had real ordering bugs) ---

def test_delete_todo_not_swallowed_by_generic_delete_file():
    direct = engine._match_direct_command("delete todo 2")
    assert direct == ("delete_todo", {"todo_id": 2})


def test_delete_event_not_swallowed_by_generic_delete_file():
    direct = engine._match_direct_command("delete event 3")
    assert direct == ("delete_event", {"event_id": 3})


def test_delete_note_not_swallowed_by_generic_delete_file():
    direct = engine._match_direct_command("delete note 4")
    assert direct == ("delete_note", {"note_id": 4})


def test_delete_file_still_works_after_crud_routes_added():
    direct = engine._match_direct_command("delete report.txt")
    assert direct == ("delete_file", {"path": "report.txt"})


def test_add_todo_routes_correctly():
    direct = engine._match_direct_command("add todo: call the dentist")
    assert direct == ("add_todo", {"text": "call the dentist"})


def test_add_event_routes_correctly():
    # Route arguments now keep the case the user typed/spoke. Previously the
    # whole command was lowercased before matching, so a calendar entry was
    # saved as "doctor visit" and a path as "d:/reports" - see
    # DecisionEngine._restore_original_case.
    direct = engine._match_direct_command("add event Doctor visit on 2026-09-01 at 14:00")
    assert direct == ("add_event", {"title": "Doctor visit", "date": "2026-09-01", "event_time": "14:00"})


def test_route_arguments_preserve_case():
    """Regression guard for the lowercasing defect: titles, notes and paths
    must reach the tool exactly as the user gave them."""
    assert engine._match_direct_command("add note Call Dr Menon about P-101A")[1] == {
        "content": "Call Dr Menon about P-101A"}
    assert engine._match_direct_command("index folder D:/College Notes")[1] == {
        "path": "D:/College Notes"}


def test_add_note_routes_correctly():
    direct = engine._match_direct_command("add note pick up the dry cleaning")
    assert direct == ("add_note", {"content": "pick up the dry cleaning"})


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
    _clean_tables()
    sys.exit(1 if fails else 0)
