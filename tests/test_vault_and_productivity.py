"""
The secure locker, the employee register, and the extended task/calendar work.

Two halves. The first proves the NEW features do what they claim - encryption
that is real, a lock that locks, an audit trail that records refusals. The
second proves the EXISTING features were extended and not replaced: the same
tables, the same tool names, the same call signatures, no second to-do list.

That second half matters more. Tasks, the calendar and notes all already
worked; the danger in this change was never that the vault would fail, it was
that a parallel "tasks" system would appear beside the real one and split the
user's data across both.
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from memory import database  # noqa: E402

DB_PATH = Path(tempfile.gettempdir()) / "medha_test_vault.db"


@pytest.fixture(autouse=True)
def fresh_db():
    database.DB_PATH = DB_PATH
    database.set_active_user(None)
    if DB_PATH.exists():
        DB_PATH.unlink()
    database.init_db()
    yield
    database.set_active_user(None)


@pytest.fixture
def confirms():
    """Approve HIGH_RISK confirmations for the duration of a test.

    Without a callback the security manager denies HIGH_RISK tools outright -
    which is the correct default and is asserted separately below. A test that
    wants to exercise what the tool DOES has to stand in for the person who
    would have clicked yes.
    """
    from core.security_manager import security_manager
    previous = security_manager._confirm_callback
    security_manager.set_confirm_callback(lambda message: True)
    yield
    security_manager._confirm_callback = previous


@pytest.fixture
def vault(tmp_path):
    """A vault rooted in a temporary directory, so tests never touch the real
    one and never leave an encrypted file behind."""
    import core.vault as module
    original_dir, original_config = module.VAULT_DIR, module.CONFIG_PATH
    module.VAULT_DIR = tmp_path / "vault"
    module.CONFIG_PATH = module.VAULT_DIR / "vault.json"
    instance = module.Vault()
    yield instance
    module.VAULT_DIR, module.CONFIG_PATH = original_dir, original_config


@pytest.fixture
def unlocked(vault, monkeypatch):
    """The vault, created and UNLOCKED, installed as the one the tools and the
    policy see. Restricted data (the employee register, vault files) is only
    reachable while the vault is unlocked - ZEUS policy, security/policy.py."""
    import core.vault as module
    monkeypatch.setattr(module, "vault", vault)
    vault.create("a-long-enough-passphrase")
    yield vault
    vault.lock()


def _sample(tmp_path: Path, text: str = "CONFIDENTIAL settlement terms") -> Path:
    path = tmp_path / "secret.txt"
    path.write_text(text, encoding="utf-8")
    return path


# ================================================== the vault is real ======

def test_a_stored_file_is_not_readable_on_disk(vault, tmp_path):
    """The whole point. If the plaintext survives in the vault directory then
    nothing here is doing anything."""
    import core.vault as module

    vault.create("a-long-enough-passphrase")
    source = _sample(tmp_path, "CONFIDENTIAL settlement terms, do not disclose")
    record = vault.store(source)

    blob = (module.VAULT_DIR / f"{record['file_id']}.bin").read_bytes()
    assert b"CONFIDENTIAL" not in blob
    assert b"settlement" not in blob
    assert blob != source.read_bytes()


def test_the_stored_file_comes_back_byte_for_byte(vault, tmp_path):
    vault.create("a-long-enough-passphrase")
    original = b"\x00\x01binary content \xff\xfe and text"
    source = tmp_path / "mixed.bin"
    source.write_bytes(original)
    record = vault.store(source)
    assert vault.read(record["file_id"]) == original


def test_the_original_file_is_left_alone(vault, tmp_path):
    """Storing is a copy, not a move. Deleting the operator's file because
    they put a copy in the vault would be an unpleasant surprise."""
    vault.create("a-long-enough-passphrase")
    source = _sample(tmp_path)
    vault.store(source)
    assert source.is_file()
    assert "CONFIDENTIAL" in source.read_text()


def test_locking_actually_locks(vault, tmp_path):
    from core.vault import VaultLocked

    vault.create("a-long-enough-passphrase")
    record = vault.store(_sample(tmp_path))
    assert vault.read(record["file_id"])

    vault.lock()
    assert not vault.is_unlocked
    with pytest.raises(VaultLocked):
        vault.read(record["file_id"])


def test_the_key_is_wiped_on_lock(vault):
    """Not just dereferenced. A bytearray is mutable, so the buffer is cleared
    rather than left for the garbage collector to release whenever it likes."""
    vault.create("a-long-enough-passphrase")
    key_buffer = vault._key
    assert any(key_buffer), "the key was never populated"
    vault.lock()
    assert vault._key is None
    assert not any(key_buffer), "the key bytes were left in memory"


def test_a_wrong_passphrase_is_refused(vault):
    vault.create("the-real-passphrase")
    vault.lock()
    assert vault.unlock("not-the-passphrase") is False
    assert not vault.is_unlocked
    assert vault.unlock("the-real-passphrase") is True


def test_a_tampered_file_is_detected(vault, tmp_path):
    """Both ciphers are authenticated, so an altered blob must fail to decrypt
    rather than returning wrong bytes."""
    import core.vault as module
    from core.vault import VaultError

    vault.create("a-long-enough-passphrase")
    record = vault.store(_sample(tmp_path))
    path = module.VAULT_DIR / f"{record['file_id']}.bin"
    blob = bytearray(path.read_bytes())
    blob[-1] ^= 0xFF
    path.write_bytes(bytes(blob))

    with pytest.raises(VaultError) as caught:
        vault.read(record["file_id"])
    assert "altered" in str(caught.value) or "decrypted" in str(caught.value)


def test_a_short_passphrase_is_refused(vault):
    from core.vault import VaultError
    with pytest.raises(VaultError, match="at least"):
        vault.create("short")


def test_creating_twice_is_refused(vault):
    """Re-creating would generate a new salt and orphan every stored file,
    which looks exactly like data loss."""
    from core.vault import VaultError
    vault.create("a-long-enough-passphrase")
    with pytest.raises(VaultError, match="already has a passphrase"):
        vault.create("another-long-passphrase")


def test_changing_the_passphrase_rekeys_every_file(vault, tmp_path):
    vault.create("the-first-passphrase")
    ids = []
    for index in range(3):
        source = tmp_path / f"file{index}.txt"
        source.write_text(f"contents of file {index}")
        ids.append(vault.store(source)["file_id"])

    assert vault.change_passphrase("the-first-passphrase", "the-second-passphrase") == 3

    vault.lock()
    assert vault.unlock("the-first-passphrase") is False
    assert vault.unlock("the-second-passphrase") is True
    for index, file_id in enumerate(ids):
        assert vault.read(file_id).decode() == f"contents of file {index}"


def test_a_wrong_current_passphrase_changes_nothing(vault, tmp_path):
    from core.vault import VaultError
    vault.create("the-first-passphrase")
    record = vault.store(_sample(tmp_path))
    with pytest.raises(VaultError):
        vault.change_passphrase("wrong", "a-new-long-passphrase")
    assert vault.unlock("the-first-passphrase")
    assert vault.read(record["file_id"])


def test_status_never_claims_more_than_it_checked(vault):
    status = vault.status()
    assert status.configured is False
    assert status.unlocked is False
    assert "not set up" in status.detail

    vault.create("a-long-enough-passphrase")
    status = vault.status()
    assert status.configured and status.unlocked
    assert status.backend in ("aes-256-gcm", "fernet")

    vault.lock()
    assert vault.status().unlocked is False


def test_the_vault_refuses_to_store_anything_without_a_cipher(vault, monkeypatch):
    """A locker that only looks locked is worse than an obviously absent one."""
    import core.vault as module
    from core.vault import VaultError

    monkeypatch.setattr(module, "_backend", lambda: "")
    with pytest.raises(VaultError, match="cryptography"):
        vault.create("a-long-enough-passphrase")


# ============================================== the vault through tools ====

def _tool(_tool_name, /, **kwargs):
    """Positional-only on purpose: several tools take an argument called
    `name`, which would otherwise collide with this helper's own parameter."""
    from core.tool_registry import registry
    return registry.execute(_tool_name, **kwargs)


def test_the_vault_tools_are_registered():
    from core.tool_registry import registry
    names = {t.name for t in registry.list_tools()}
    for expected in ("vault_status", "lock_vault", "list_vault_files",
                     "add_file_to_vault", "open_vault_file", "export_vault_file",
                     "remove_vault_file", "vault_audit_log"):
        assert expected in names, f"{expected} is not registered"


def test_no_tool_takes_a_passphrase_as_an_argument():
    """`registry.execute` stringifies every argument into the security audit
    log. A passphrase passed as a tool argument would be written to disk in
    plaintext, so unlocking is done through a dialog instead."""
    from core.tool_registry import registry
    for tool in registry.list_tools():
        for parameter in tool.params:
            assert "pass" not in parameter.lower(), \
                f"{tool.name} takes '{parameter}' as an argument"
        import inspect
        try:
            signature = inspect.signature(tool.func)
        except (TypeError, ValueError):
            continue
        for parameter in signature.parameters:
            assert "passphrase" not in parameter.lower() and \
                   "password" not in parameter.lower(), \
                f"{tool.name} takes '{parameter}' as an argument"


def test_a_high_risk_vault_tool_is_denied_when_nobody_can_confirm(unlocked):
    """The security manager denies HIGH_RISK by default when no confirmation
    callback is registered. That is what stops a headless or scripted run from
    quietly decrypting files out of the vault - even with the vault unlocked."""
    from core.security_manager import security_manager
    previous = security_manager._confirm_callback
    security_manager._confirm_callback = None
    try:
        outcome = _tool("open_vault_file", name="anything")
        assert outcome.success is False
        assert "not approved" in (outcome.error or "").lower()
    finally:
        security_manager._confirm_callback = previous


def test_the_high_risk_vault_tools_are_marked_high_risk():
    """Exporting and opening put decrypted content back on the ordinary
    filesystem. That deserves a confirmation prompt."""
    from core.security_manager import RiskLevel
    from core.tool_registry import registry
    for name in ("open_vault_file", "export_vault_file", "remove_vault_file"):
        tool = registry.get(name)
        assert tool.risk >= RiskLevel.HIGH_RISK, f"{name} is only {tool.risk.name}"


def test_a_locked_vault_refuses_through_the_tool_layer(vault, tmp_path, monkeypatch, confirms):
    import core.vault as module
    monkeypatch.setattr(module, "vault", vault)
    vault.create("a-long-enough-passphrase")
    vault.store(_sample(tmp_path), display_name="terms.txt")
    vault.lock()

    outcome = _tool("open_vault_file", name="terms.txt")
    # ZEUS: the policy refuses before the tool runs - a locked vault is not
    # reachable by any tool at all.
    assert outcome.success is False
    assert "locked" in (outcome.error or "").lower()


def test_listing_is_refused_while_locked(vault, tmp_path, monkeypatch):
    """ZEUS changed this deliberately: file NAMES in a confidential locker are
    themselves confidential ("Q3 layoffs.xlsx"), so a locked vault lists
    nothing. The refusal says why, and names nothing."""
    import core.vault as module
    monkeypatch.setattr(module, "vault", vault)
    vault.create("a-long-enough-passphrase")
    vault.store(_sample(tmp_path), display_name="terms.txt")
    vault.lock()

    outcome = _tool("list_vault_files")
    assert outcome.success is False
    assert "locked" in (outcome.error or "").lower()
    assert "terms.txt" not in (outcome.error or "")

    vault.unlock("a-long-enough-passphrase")
    output = _tool("list_vault_files").output
    assert any("terms.txt" in line for line in output["files"])


def test_every_vault_action_is_audited_including_refusals(vault, tmp_path,
                                                          monkeypatch, confirms):
    import core.vault as module
    monkeypatch.setattr(module, "vault", vault)
    vault.create("a-long-enough-passphrase")
    vault.lock()

    _tool("open_vault_file", name="nothing-here.txt")
    events = database.recent_vault_events()
    assert events, "nothing was audited"
    refusals = [e for e in events if not e["succeeded"]]
    assert refusals, "a refused action was not recorded"
    # recorded by the policy layer (the tool name) or by the tool itself
    assert refusals[0]["action"] in ("open", "open_vault_file")


# ============================================================ employees ====

def test_employee_records_round_trip(unlocked):
    outcome = _tool("add_employee", employee_id="E-1042", name="Priya Sharma",
                    role="Rotating Equipment Engineer", department="Maintenance",
                    email="priya@example.com")
    assert outcome.success and "added" in outcome.output

    found = _tool("find_employee", query="E-1042").output
    assert found["name"] == "Priya Sharma"
    assert found["department"] == "Maintenance"

    by_name = _tool("find_employee", query="Priya").output
    assert by_name["employee_id"] == "E-1042"


def test_a_duplicate_works_number_is_refused(unlocked):
    _tool("add_employee", employee_id="E-1042", name="Priya Sharma")
    again = _tool("add_employee", employee_id="E-1042", name="Someone Else")
    assert "already" in again.output["error"]
    assert len(database.list_employees()) == 1


def test_the_department_roster_counts_real_records(unlocked):
    for number, department in (("E-1", "Maintenance"), ("E-2", "Maintenance"),
                               ("E-3", "Operations")):
        _tool("add_employee", employee_id=number, name=f"Person {number}",
              department=department)
    roster = _tool("department_roster").output
    assert roster["total"] == 3
    assert roster["departments"] == 2
    assert "Maintenance: 2" in roster["roster"]


def test_employee_lookups_are_audited(unlocked):
    _tool("add_employee", employee_id="E-9", name="Someone")
    _tool("find_employee", query="E-9")
    actions = {e["action"] for e in database.recent_vault_events()}
    assert "employee_add" in actions
    assert "employee_lookup" in actions


# ======================= tasks and calendar: EXTENDED, not replaced ========

def test_there_is_still_exactly_one_task_table():
    """The regression this whole design exists to prevent. A second table
    would split the user's list in half without telling them."""
    connection = database.get_connection()
    try:
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        connection.close()
    task_tables = {t for t in tables
                   if "todo" in t or ("task" in t and t != "task_history")}
    assert task_tables == {"todos"}, f"more than one task table: {task_tables}"


def test_the_old_add_todo_signature_still_works():
    """Every existing caller and route sends only `text`."""
    from memory.database import add_todo, list_todos
    add_todo("buy milk")
    tasks = list_todos()
    assert len(tasks) == 1
    assert tasks[0]["text"] == "buy milk"
    assert tasks[0]["due_date"] is None
    assert tasks[0]["completed"] == 0


def test_the_old_add_event_signature_still_works():
    from memory.database import add_event, list_events
    add_event("Turnaround briefing", "2026-09-30")
    events = list_events()
    assert len(events) == 1
    assert events[0]["title"] == "Turnaround briefing"
    assert events[0]["kind"] is None      # an ordinary event, not a meeting


def test_a_task_can_carry_a_deadline_priority_and_owner():
    from memory.database import add_todo, list_todos
    add_todo("Inspect P-101", due_date="2026-09-11", priority="high",
             assignee="Priya")
    task = list_todos()[0]
    assert task["due_date"] == "2026-09-11"
    assert task["priority"] == "high"
    assert task["assignee"] == "Priya"


def test_dated_tasks_sort_ahead_of_undated_ones():
    """A list where an overdue item sits below a dateless idea is not usable."""
    from memory.database import add_todo, list_todos
    add_todo("someday: tidy the store room")
    add_todo("urgent: isolate the pump", due_date="2026-09-10")
    add_todo("later: order parts", due_date="2026-12-01")
    order = [t["text"] for t in list_todos()]
    assert order[0].startswith("urgent")
    assert order[1].startswith("later")
    assert order[2].startswith("someday")


def test_overdue_tasks_are_separated_from_what_is_due_today():
    from datetime import date, timedelta
    from memory.database import add_todo
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    add_todo("overdue item", due_date=yesterday)
    add_todo("today item", due_date=date.today().isoformat())
    add_todo("no deadline")

    output = _tool("overdue_tasks").output
    assert output["counts"]["overdue"] == 1
    assert output["counts"]["due_today"] == 1


def test_a_meeting_is_a_calendar_event_not_a_separate_system():
    """One timeline. 'What is on Friday' must answer with both."""
    from memory.database import list_events
    _tool("add_event", title="Dentist", date="2026-09-11")
    _tool("schedule_meeting", title="Turnaround review", date="2026-09-11",
          time="14:30", attendees="Priya, Arun", location="Room 2",
          duration_minutes=45)

    day = list_events(start_date="2026-09-11")
    assert len(day) == 2, "the meeting and the event are not on one timeline"
    meetings = list_events(start_date="2026-09-11", kind="meeting")
    assert len(meetings) == 1
    assert meetings[0]["attendees"] == "Priya, Arun"
    assert meetings[0]["duration_minutes"] == 45


def test_the_agenda_answers_with_events_and_tasks_together():
    from datetime import date
    from memory.database import add_todo
    today = date.today().isoformat()
    _tool("schedule_meeting", title="Safety stand-down", date="today", time="09:00")
    add_todo("submit the permit", due_date=today)

    agenda = _tool("agenda_for", date="today").output
    assert agenda["day"] == today
    assert any("Safety stand-down" in line for line in agenda["events"])
    assert any("permit" in line for line in agenda["tasks_due"])


def test_an_invalid_priority_is_refused_rather_than_stored():
    outcome = _tool("add_todo", text="something", priority="URGENT!!")
    assert "error" in outcome.output
    assert database.list_todos() == []


# ============================================== nothing was taken away ====

def test_every_pre_existing_productivity_tool_still_exists():
    from core.tool_registry import registry
    names = {t.name for t in registry.list_tools()}
    for tool in ("add_todo", "list_todos", "complete_todo", "uncomplete_todo",
                 "delete_todo", "add_event", "list_events", "delete_event",
                 "add_note", "list_notes", "update_note", "delete_note",
                 "create_schedule", "update_schedule"):
        assert tool in names, f"{tool} disappeared"


def test_the_productivity_agent_still_owns_its_original_tools():
    from core.orchestration import agents
    owned = set(agents.SPECIALISTS["productivity"].tools)
    for tool in ("add_todo", "list_todos", "complete_todo", "uncomplete_todo",
                 "delete_todo", "add_event", "list_events", "delete_event",
                 "add_note", "list_notes", "update_note", "delete_note",
                 "create_schedule", "update_schedule"):
        assert tool in owned, f"the productivity agent lost {tool}"


def test_no_tool_is_owned_by_two_agents():
    from core.orchestration import agents
    owners: dict = {}
    for agent_id, specialist in agents.SPECIALISTS.items():
        for tool in specialist.tools:
            owners.setdefault(tool, []).append(agent_id)
    contested = {tool: who for tool, who in owners.items() if len(who) > 1}
    assert not contested, f"tools owned by more than one agent: {contested}"


def test_the_new_agents_exist_and_own_their_tools():
    from core.orchestration import agents
    from core.tool_registry import registry
    names = {t.name for t in registry.list_tools()}
    for agent_id in ("vault", "people"):
        assert agent_id in agents.SPECIALISTS
        for tool in agents.SPECIALISTS[agent_id].tools:
            assert tool in names, f"{agent_id} claims a tool that does not exist: {tool}"


def test_vault_and_employee_data_are_user_scoped(unlocked):
    """These are personal, so they belong in the scoped set - which also means
    they are migrated, counted and adopted like every other personal table."""
    assert "vault_files" in database._USER_SCOPED_TABLES
    assert "employees" in database._USER_SCOPED_TABLES

    database.set_active_user(1)
    _tool("add_employee", employee_id="E-1", name="First User Person")
    database.set_active_user(2)
    assert database.list_employees() == [], "one user can see another's records"
    database.set_active_user(1)
    assert len(database.list_employees()) == 1


def test_vault_contents_never_enter_the_rag_index():
    """A confidential file that can be recalled by asking a question is not
    confidential. The vault is a separate table for exactly this reason."""
    from memory.database import get_all_chunks, get_industrial_chunks
    connection = database.get_connection()
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(vault_files)")}
    finally:
        connection.close()
    assert "chunk_text" not in columns
    assert "embedding" not in columns
    assert get_all_chunks() == []
    assert get_industrial_chunks() == []


# ============================================ the parity contract =========

def test_the_baseline_capability_counts_are_intact():
    """Counted from the live registries, not from a document.

    These are the numbers from the inventory taken before this work started.
    They may only go UP - a drop means something was removed while adding
    something else, which is the failure mode this whole exercise exists to
    prevent.
    """
    from plugins.plugin_loader import discover_plugins
    discover_plugins()

    from core.decision_engine import _ROUTES
    from core.model_router import DEFAULT_PREFERENCES
    from core.orchestration import agents, autofix, validators
    from core.tool_registry import registry

    baseline = {"tools": 147, "specialists": 19, "validators": 12,
                "autofix": 14, "model_slots": 8, "routes": 164}
    now = {"tools": len(registry.list_tools()),
           "specialists": len(agents.SPECIALISTS),
           "validators": len(validators.registered_kinds()),
           "autofix": len(autofix.known_strategies()),
           "model_slots": len(DEFAULT_PREFERENCES),
           "routes": len(_ROUTES)}
    shrunk = {k: (baseline[k], now[k]) for k in baseline if now[k] < baseline[k]}
    assert not shrunk, f"capabilities were lost (was, now): {shrunk}"


def test_no_pre_existing_tool_disappeared():
    """A sample across every tool family that existed before this work."""
    from plugins.plugin_loader import discover_plugins
    discover_plugins()
    from core.tool_registry import registry

    names = {t.name for t in registry.list_tools()}
    expected = [
        # files, documents, conversion
        "search_files", "read_file", "write_file", "copy_file", "move_file",
        "delete_file", "create_folder", "convert_file", "watch_folder",
        "write_document", "create_presentation", "create_spreadsheet",
        # productivity (the ones this work extended)
        "add_todo", "list_todos", "complete_todo", "uncomplete_todo",
        "delete_todo", "add_event", "list_events", "delete_event",
        "add_note", "list_notes", "update_note", "delete_note",
        "create_schedule", "update_schedule",
        # memory, RAG, vision
        "remember_fact", "list_memories", "forget_fact", "index_folder",
        "ask_knowledge_base", "list_knowledge_sources", "analyze_image",
        # industrial and plant
        "list_equipment", "plant_status", "safety_status", "maintenance_due",
        "approve_permit", "executive_report", "executive_workbook",
        "executive_presentation", "analyze_spreadsheet",
        "list_generated_documents", "open_document",
        # system and introspection
        "run_command", "show_audit_log", "calculate",
    ]
    missing = [name for name in expected if name not in names]
    assert not missing, f"tools that existed before have gone: {missing}"


def test_create_presentation_is_bound_to_the_real_function():
    """It was registered twice - once on a private helper with a different
    signature - and only import order made it correct."""
    import inspect
    from core.tool_registry import registry

    tool = registry.get("create_presentation")
    assert tool is not None
    signature = inspect.signature(tool.func)
    assert "filename" in signature.parameters, \
        "create_presentation is bound to the outline helper, not the real tool"


def test_embedding_and_reranking_still_refuse_the_chat_router():
    """The architectural fix from the previous work must survive this one."""
    from core.model_router import model_router
    for capability in ("embedding", "reranker"):
        with pytest.raises(ValueError, match="not a chat capability"):
            model_router.route(capability)


def test_new_routes_did_not_shadow_any_existing_command():
    """Routes are first-match-wins and anchored. A new pattern placed above an
    old one silently steals it."""
    from core.decision_engine import DecisionEngine
    matcher = DecisionEngine()._match_direct_command
    for text, expected in [
            ("open notepad", "open_application"),
            ("open browser", "open_browser"),
            ("open file explorer", "open_file_explorer"),
            ("add todo buy milk", "add_todo"),
            ("list todos", "list_todos"),
            ("what is 2+2", "calculate"),
            ("show the plant status", "plant_status"),
            ("show the equipment list", "list_equipment"),
            ("show my recent documents", "list_generated_documents"),
            ("run diagnostics", "run_diagnostics")]:
        matched = matcher(text)
        assert matched is not None, f"'{text}' stopped routing entirely"
        assert matched[0] == expected, \
            f"'{text}' now routes to {matched[0]}, was {expected}"


def test_general_conversation_is_not_captured_by_the_new_routes():
    """An employee-lookup route broad enough to catch "who is the plant
    manager" would break ordinary chat. An earlier draft did exactly that."""
    from core.decision_engine import DecisionEngine
    matcher = DecisionEngine()._match_direct_command
    for text in ("who is the prime minister of india",
                 "find the best approach for this",
                 "what is the weather like",
                 "show me the report you wrote"):
        assert matcher(text) is None, \
            f"'{text}' was captured by a route instead of going to chat"
