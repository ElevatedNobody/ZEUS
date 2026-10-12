"""
Mandatory local login: account creation, hashing, lockout, sessions,
per-user memory isolation and access control.

Everything runs against a real (temporary) SQLite database and the real
hashing backend installed on this machine - nothing about authentication
is stubbed.
"""

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memory import database  # noqa: E402

DB_PATH = Path("/tmp/medha_test_auth.db")
database.DB_PATH = DB_PATH
if DB_PATH.exists():
    DB_PATH.unlink()
database.init_db()

import auth.password_manager as pm  # noqa: E402
from auth.auth_manager import AuthManager  # noqa: E402
from auth.session_manager import SessionManager  # noqa: E402

PASSWORD = "IndustrialAI2026"
OTHER_PASSWORD = "SecondOperator99"


def _fresh():
    """A clean database plus a manager pair, for tests that need isolation."""
    # Pin the database for THIS module on every call: pytest imports all test
    # modules before running any of them, and several of them point
    # database.DB_PATH at their own temporary file at import time.
    database.DB_PATH = DB_PATH
    database.set_active_user(None)
    if DB_PATH.exists():
        DB_PATH.unlink()
    database.init_db()
    manager = AuthManager()
    return manager, SessionManager(manager)


def setup_function(function):
    database.DB_PATH = DB_PATH
    database.set_active_user(None)


# ------------------------------------------------------------- hashing ----

def test_password_is_never_stored_in_plaintext():
    auth, sessions = _fresh()
    auth.create_account("dhruv", PASSWORD)
    user = database.get_user("dhruv")
    stored = str(dict(user))
    assert PASSWORD not in stored
    assert user["password_hash"] != PASSWORD
    assert len(user["password_hash"]) > 40


def test_hash_uses_an_established_algorithm():
    auth, _ = _fresh()
    auth.create_account("dhruv", PASSWORD)
    algorithm = database.get_user("dhruv")["password_algorithm"]
    assert algorithm in ("argon2id", "bcrypt", "scrypt")
    assert pm.preferred_backend() in ("argon2id", "bcrypt", "scrypt")


def test_hashing_never_degrades_to_a_bare_digest():
    """The weakest permitted fallback is scrypt, which is memory-hard and
    always present in the standard library."""
    assert "scrypt" in pm.available_backends()
    hashed = pm.hash_password(PASSWORD)
    assert not hashed.startswith("sha")
    assert pm.algorithm_of(hashed) in ("argon2id", "bcrypt", "scrypt")


def test_same_password_hashes_differently_each_time():
    """Salting: two accounts with the same password must not share a hash."""
    auth, _ = _fresh()
    auth.create_account("a", PASSWORD)
    auth.create_account("b", PASSWORD)
    assert database.get_user("a")["password_hash"] != database.get_user("b")["password_hash"]


def test_verification_accepts_the_right_password_only():
    hashed = pm.hash_password(PASSWORD)
    assert pm.verify_password(PASSWORD, hashed) is True
    assert pm.verify_password(PASSWORD + "x", hashed) is False
    assert pm.verify_password("", hashed) is False
    assert pm.verify_password(PASSWORD, "") is False
    assert pm.verify_password(PASSWORD, "not-a-hash") is False


def test_short_passwords_are_rejected():
    auth, _ = _fresh()
    for bad in ("", "short", "1234567"):
        try:
            auth.create_account("weak", bad)
        except ValueError:
            continue
        raise AssertionError(f"password '{bad}' should have been rejected")


# ------------------------------------------------------------ first run ----

def test_first_run_requires_setup_then_does_not():
    auth, _ = _fresh()
    assert auth.first_run_required() is True
    auth.create_account("dhruv", PASSWORD)
    assert auth.first_run_required() is False


def test_no_default_account_exists():
    """Nothing is pre-seeded: a fresh install has zero accounts, so there is
    no hidden default username/password to find."""
    database.DB_PATH = DB_PATH
    database.set_active_user(None)
    if DB_PATH.exists():
        DB_PATH.unlink()
    database.init_db()
    assert database.count_users() == 0
    assert database.list_users() == []


def test_first_account_is_the_owner():
    auth, _ = _fresh()
    first = auth.create_account("dhruv", PASSWORD)
    second = auth.create_account("operator", OTHER_PASSWORD)
    assert bool(first["is_admin"]) is True
    assert bool(second["is_admin"]) is False


def test_duplicate_username_is_refused():
    auth, _ = _fresh()
    auth.create_account("dhruv", PASSWORD)
    try:
        auth.create_account("DHRUV", OTHER_PASSWORD)   # case-insensitive
    except ValueError as e:
        assert "already exists" in str(e)
    else:
        raise AssertionError("duplicate username was accepted")


# --------------------------------------------------------------- login ----

def test_correct_credentials_authenticate():
    auth, sessions = _fresh()
    auth.create_account("dhruv", PASSWORD)
    result = sessions.login("dhruv", PASSWORD)
    assert result.ok is True
    assert sessions.is_authenticated is True
    assert sessions.username == "dhruv"


def test_wrong_password_fails():
    auth, sessions = _fresh()
    auth.create_account("dhruv", PASSWORD)
    assert sessions.login("dhruv", "wrong-password").ok is False
    assert sessions.is_authenticated is False


def test_unknown_username_fails_without_revealing_that():
    auth, sessions = _fresh()
    auth.create_account("dhruv", PASSWORD)
    unknown = sessions.login("nobody", PASSWORD)
    wrong = sessions.login("dhruv", "wrong-password")
    assert unknown.ok is False and wrong.ok is False
    # The same generic wording either way - no account enumeration.
    assert "Invalid credentials" in unknown.message


def test_empty_fields_fail_safely():
    auth, sessions = _fresh()
    auth.create_account("dhruv", PASSWORD)
    for username, password in (("", ""), ("dhruv", ""), ("", PASSWORD)):
        result = sessions.login(username, password)
        assert result.ok is False
        assert sessions.is_authenticated is False


def test_login_is_recorded_without_secrets():
    auth, sessions = _fresh()
    auth.create_account("dhruv", PASSWORD)
    sessions.login("dhruv", PASSWORD)
    sessions.login("dhruv", "wrong-password")
    events = database.recent_auth_events(10)
    assert any(e["event"] == "login_success" for e in events)
    assert any(e["event"] == "login_failed" for e in events)
    blob = str(events)
    assert PASSWORD not in blob
    assert "wrong-password" not in blob


# ------------------------------------------------------------- lockout ----

def test_lockout_after_repeated_failures_and_it_expires():
    auth, sessions = _fresh()
    auth.create_account("dhruv", PASSWORD)
    limit = auth.max_failed_attempts

    for attempt in range(limit - 1):
        result = sessions.login("dhruv", "wrong-password")
        assert result.ok is False
        assert result.locked_until is None, f"locked too early at attempt {attempt + 1}"

    locked = sessions.login("dhruv", "wrong-password")
    assert locked.locked_until is not None
    # Even the CORRECT password is refused while the lock stands.
    assert sessions.login("dhruv", PASSWORD).ok is False

    # The lock is temporary by design - never a permanent brick.
    user = database.get_user("dhruv")
    assert user["locked_until"] - time.time() <= auth.lockout_seconds + 1

    database.set_user_lock(user["id"], time.time() - 1)   # simulate expiry
    assert sessions.login("dhruv", PASSWORD).ok is True


def test_successful_login_clears_the_failure_counter():
    auth, sessions = _fresh()
    auth.create_account("dhruv", PASSWORD)
    sessions.login("dhruv", "wrong-password")
    sessions.login("dhruv", "wrong-password")
    assert database.get_user("dhruv")["failed_attempts"] == 2
    sessions.login("dhruv", PASSWORD)
    assert database.get_user("dhruv")["failed_attempts"] == 0


def test_lockout_policy_is_configurable():
    auth, _ = _fresh()
    assert auth.max_failed_attempts >= 1
    assert auth.lockout_seconds >= 1
    auth.policy["max_failed_attempts"] = "2"
    auth.policy["lockout_seconds"] = "5"
    assert auth.max_failed_attempts == 2
    assert auth.lockout_seconds == 5


# ------------------------------------------------------- lock / logout ----

def test_lock_withdraws_access_without_ending_the_session():
    auth, sessions = _fresh()
    auth.create_account("dhruv", PASSWORD)
    sessions.login("dhruv", PASSWORD)
    assert sessions.lock() is True
    assert sessions.is_authenticated is False
    assert sessions.session is not None          # still the same session
    assert sessions.unlock("wrong-password").ok is False
    assert sessions.is_authenticated is False
    assert sessions.unlock(PASSWORD).ok is True
    assert sessions.is_authenticated is True


def test_logout_ends_the_session_and_detaches_memory():
    auth, sessions = _fresh()
    auth.create_account("dhruv", PASSWORD)
    sessions.login("dhruv", PASSWORD)
    assert database.get_active_user() is not None
    assert sessions.logout() is True
    assert sessions.is_authenticated is False
    assert sessions.session is None
    assert database.get_active_user() is None


def test_restart_requires_login_again():
    """A new process starts with no session - the DB row does not grant access."""
    auth, sessions = _fresh()
    auth.create_account("dhruv", PASSWORD)
    sessions.login("dhruv", PASSWORD)
    restarted = SessionManager(auth)              # stands in for a new process
    assert restarted.is_authenticated is False
    assert restarted.current_user is None


def test_require_authenticated_raises_when_locked():
    auth, sessions = _fresh()
    auth.create_account("dhruv", PASSWORD)
    sessions.login("dhruv", PASSWORD)
    sessions.require_authenticated()              # no exception
    sessions.lock()
    try:
        sessions.require_authenticated()
    except PermissionError:
        return
    raise AssertionError("a locked session must not pass the guard")


def test_idle_timeout_locks_the_session():
    auth, sessions = _fresh()
    auth.policy["session_idle_minutes"] = "1"
    auth.create_account("dhruv", PASSWORD)
    sessions.login("dhruv", PASSWORD)
    assert sessions.is_authenticated is True
    sessions.session.last_activity = time.time() - 120     # two minutes ago
    assert sessions.is_authenticated is False
    assert sessions.session.locked is True


# --------------------------------------------------- per-user memory ------

def test_users_do_not_see_each_others_memory():
    auth, sessions = _fresh()
    auth.create_account("alice", PASSWORD)
    auth.create_account("bob", OTHER_PASSWORD)

    sessions.login("alice", PASSWORD)
    database.add_message("user", "alice private note")
    database.set_fact("pref", "alice prefers long reports")
    database.add_todo("alice task")
    database.add_note("A", "alice note body")
    sessions.logout()

    sessions.login("bob", OTHER_PASSWORD)
    assert database.get_all_facts() == {}
    assert database.get_recent_messages() == []
    assert database.list_todos() == []
    assert database.list_notes() == []

    # Bob may use the SAME fact key without touching alice's value.
    database.set_fact("pref", "bob prefers short reports")
    assert database.get_all_facts()["pref"] == "bob prefers short reports"
    sessions.logout()

    sessions.login("alice", PASSWORD)
    assert database.get_all_facts()["pref"] == "alice prefers long reports"
    assert any("alice private note" in c for _r, c in database.get_recent_messages())
    sessions.logout()


def test_one_user_cannot_delete_another_users_todo():
    auth, sessions = _fresh()
    auth.create_account("alice", PASSWORD)
    auth.create_account("bob", OTHER_PASSWORD)

    sessions.login("alice", PASSWORD)
    todo_id = database.add_todo("alice only")
    sessions.logout()

    sessions.login("bob", OTHER_PASSWORD)
    assert database.delete_todo(todo_id) is False
    assert database.set_todo_completed(todo_id, True) is False
    sessions.logout()

    sessions.login("alice", PASSWORD)
    assert any(t["id"] == todo_id for t in database.list_todos())
    sessions.logout()


def test_pre_login_data_is_adopted_by_the_first_account():
    """Upgrading an existing single-user install must not look like data loss."""
    auth, sessions = _fresh()
    database.set_active_user(None)
    database.add_message("user", "history from before login existed")
    database.set_fact("legacy", "old preference")
    database.add_todo("old task")

    auth.create_account("dhruv", PASSWORD)
    sessions.login("dhruv", PASSWORD)

    assert any("before login" in c for _r, c in database.get_recent_messages())
    assert database.get_all_facts().get("legacy") == "old preference"
    assert any(t["text"] == "old task" for t in database.list_todos())
    sessions.logout()


def test_adoption_does_not_run_with_several_accounts():
    auth, sessions = _fresh()
    database.set_active_user(None)
    database.add_message("user", "unowned row")
    auth.create_account("alice", PASSWORD)
    auth.create_account("bob", OTHER_PASSWORD)
    sessions.login("alice", PASSWORD)
    # Ownership would be a guess with two accounts, so nothing is adopted.
    assert database.get_recent_messages() == []
    sessions.logout()


def test_single_user_behaviour_is_unchanged_without_a_session():
    """No session -> the database behaves exactly as it did before login
    existed. This is what keeps every pre-existing test and script working."""
    auth, sessions = _fresh()
    database.set_active_user(None)
    database.add_message("user", "no session message")
    database.set_fact("k", "v")
    assert database.get_all_facts()["k"] == "v"
    assert any("no session message" in c for _r, c in database.get_recent_messages())


# --------------------------------------------------- shared industrial ----

def test_industrial_knowledge_stays_shared_between_users():
    """Plant documents are workbench data, not personal memory - both
    operators must see the same knowledge base."""
    auth, sessions = _fresh()
    auth.create_account("alice", PASSWORD)
    auth.create_account("bob", OTHER_PASSWORD)

    sessions.login("alice", PASSWORD)
    database.upsert_industrial_document({
        "document_id": "doc1", "filename": "INSP-1.pdf", "source_path": "/tmp/INSP-1.pdf",
        "document_type": "inspection_report", "document_class": "user_document"})
    alice_view = database.list_industrial_documents()
    sessions.logout()

    sessions.login("bob", OTHER_PASSWORD)
    assert len(database.list_industrial_documents()) == len(alice_view) == 1
    sessions.logout()


# ------------------------------------------------------------- accounts ----

def test_password_change_requires_the_current_password():
    auth, sessions = _fresh()
    auth.create_account("dhruv", PASSWORD)
    assert auth.change_password("dhruv", "wrong", "NewPassword123") is False
    assert auth.change_password("dhruv", PASSWORD, "NewPassword123") is True
    assert sessions.login("dhruv", "NewPassword123").ok is True
    assert sessions.login("dhruv", PASSWORD).ok is False


def test_disabled_account_cannot_log_in():
    auth, sessions = _fresh()
    user = auth.create_account("dhruv", PASSWORD)
    database.set_user_active(user["id"], False)
    result = sessions.login("dhruv", PASSWORD)
    assert result.ok is False
    assert "disabled" in result.message.lower()


def test_status_never_exposes_hashes():
    auth, _ = _fresh()
    auth.create_account("dhruv", PASSWORD)
    status = auth.status()
    blob = str(status)
    assert PASSWORD not in blob
    assert "$argon2" not in blob and "scrypt$" not in blob
    assert status["external_identity_provider"] == "NONE"
    assert "sqlite" in status["storage"]


def test_list_users_does_not_return_password_material():
    auth, _ = _fresh()
    auth.create_account("dhruv", PASSWORD)
    for user in database.list_users():
        assert "password_hash" not in user
        assert "password_algorithm" not in user


# ------------------------------------------------------- offline / gate ----

def test_authentication_uses_no_network_library():
    forbidden = {"firebase_admin", "auth0", "supabase", "clerk", "msal",
                 "google.auth", "requests_oauthlib", "flask", "fastapi"}
    assert not (forbidden & set(sys.modules))


def test_auth_sources_contain_no_remote_endpoints():
    auth_dir = Path(__file__).resolve().parent.parent / "auth"
    for path in auth_dir.glob("*.py"):
        text = path.read_text(encoding="utf-8").lower()
        for needle in ("http://", "https://", "firebase", "auth0", "supabase",
                       "oauth", "identitytoolkit"):
            assert needle not in text, f"{path.name} references {needle}"


def test_main_gates_the_application_behind_authenticate():
    """The entry point must not construct MedhaAssistant before the gate.

    Checked structurally rather than by string order: main() must call
    start_up() and refuse to continue on a falsey result, and start_up() must
    itself refuse to continue when authenticate() is falsey.
    """
    import ast
    source = (Path(__file__).resolve().parent.parent / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    functions = {node.name: node for node in ast.walk(tree)
                 if isinstance(node, ast.FunctionDef)}
    assert {"authenticate", "start_up", "main"} <= set(functions)

    def guarded_by(container: ast.FunctionDef, callee: str) -> bool:
        """Is `callee` called inside `if not ...:` with a return in the body?"""
        for node in ast.walk(container):
            if not (isinstance(node, ast.If) and isinstance(node.test, ast.UnaryOp)
                    and isinstance(node.test.op, ast.Not)):
                continue
            called = {sub.func.id for sub in ast.walk(node.test)
                      if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)}
            if callee in called and any(isinstance(stmt, ast.Return)
                                        for stmt in node.body):
                return True
        return False

    assert guarded_by(functions["main"], "start_up"), \
        "main() does not stop when startup fails"
    assert guarded_by(functions["start_up"], "authenticate"), \
        "start_up() continues past a failed authentication"

    # And the construction happens in main(), after that guard.
    main_body = ast.get_source_segment(source, functions["main"])
    assert "assistant = MedhaAssistant(" in main_body
    assert main_body.index("start_up(") < main_body.index("assistant = MedhaAssistant(")


def test_the_startup_sequence_runs_in_the_documented_order():
    """PART 4: one authoritative path, and it is the one in the log."""
    import ast
    source = (Path(__file__).resolve().parent.parent / "main.py").read_text(encoding="utf-8")
    start_up = next(node for node in ast.walk(ast.parse(source))
                    if isinstance(node, ast.FunctionDef) and node.name == "start_up")
    body = ast.get_source_segment(source, start_up)
    stages = ["ZEUS START", "SYSTEM INITIALIZATION", "LOCAL MODEL DISCOVERY",
              "CAPABILITY READINESS", "AUTHENTICATION", "COMMAND CENTER"]
    positions = [body.index(stage) for stage in stages]
    assert positions == sorted(positions), \
        f"the startup stages are out of order: {stages}"


def test_login_screen_exists_and_offers_no_bypass():
    """The sign-in is the ZEUS page (ui/medha/js/auth.js) driven by
    auth/web_login.py - the previous Qt screen (ui/access.py) was removed on
    2026-09-27. Neither may offer a way around the password or the face."""
    root = Path(__file__).resolve().parent.parent
    page, controller = root / "ui" / "medha" / "js" / "auth.js", root / "auth" / "web_login.py"
    assert page.exists() and controller.exists()
    for path in (page, controller):
        text = path.read_text(encoding="utf-8").lower()
        for banned in ("skip login", "bypass", "guest", "default password", "admin/admin"):
            assert banned not in text, f"{path.name}: {banned}"


def test_there_is_exactly_one_authentication_screen():
    """Two login screens means one of them can be shown by mistake, and means
    a security fix has to be made twice. The page has one sign-in script; no
    Python window draws a login of its own any more."""
    root = Path(__file__).resolve().parent.parent
    qt_screens = [path.name for path in (root / "ui").glob("*.py")
                  if "prompt_login" in path.read_text(encoding="utf-8")]
    assert qt_screens == [], f"a Qt login screen came back: {qt_screens}"
    pages = sorted(path.name for path in (root / "ui" / "medha" / "js").rglob("*.js")
                   if 'M.call("auth_login"' in path.read_text(encoding="utf-8"))
    assert pages == ["auth.js"], f"more than one sign-in page: {pages}"


def test_failed_result_is_falsey():
    """A failed AuthResult must not be truthy: `if auth.authenticate(...)`
    is how callers are written, and a truthy failure would let them in."""
    auth, sessions = _fresh()
    auth.create_account("dhruv", PASSWORD)
    assert not auth.authenticate("dhruv", "wrong-password")
    assert not auth.authenticate("nobody", "wrong-password")
    assert auth.authenticate("dhruv", PASSWORD)


def test_wrong_password_and_unknown_user_are_indistinguishable():
    """The login screen must not become an account-enumeration oracle: an
    invented username has to answer exactly like a real one."""
    import auth.auth_manager as am
    am._UNKNOWN_FAILURES.clear()
    auth, sessions = _fresh()
    auth.create_account("dhruv", PASSWORD)
    for _ in range(3):
        real = auth.authenticate("dhruv", "wrong-password")
        ghost = auth.authenticate("does_not_exist", "wrong-password")
        assert real.message == ghost.message, (real.message, ghost.message)
        assert not real.ok and not ghost.ok
    am._UNKNOWN_FAILURES.clear()


def test_unknown_user_lockout_also_expires():
    """The phantom counter must never permanently refuse a username - the
    real account with that name might be created later."""
    import auth.auth_manager as am
    am._UNKNOWN_FAILURES.clear()
    auth, sessions = _fresh()
    auth.policy["max_failed_attempts"] = "2"
    auth.policy["lockout_seconds"] = "1"
    auth.authenticate("ghost", "x")
    locked = auth.authenticate("ghost", "x")
    assert locked.locked_until, "phantom account should lock like a real one"
    time.sleep(1.1)
    after = auth.authenticate("ghost", "x")
    assert after.locked_until is None, "the lock must expire"
    am._UNKNOWN_FAILURES.clear()


def test_unknown_failure_table_is_bounded(monkeypatch):
    """Password spraying thousands of names must not grow memory forever.

    The hash verification is stubbed for this one test. Authenticating an
    unknown user deliberately verifies against a dummy hash so it costs the
    same as a real account - which is the right behaviour and is asserted by
    the test below - but it made 600 attempts take 84 seconds, long enough
    that a full run looked like it had frozen. The property under test here
    is the BOUND on the table, and that does not need real cryptography.
    """
    import auth.auth_manager as am
    from auth import password_manager as pm

    monkeypatch.setattr(pm, "verify_password", lambda *_a, **_k: False)
    monkeypatch.setattr(am.pm, "verify_password", lambda *_a, **_k: False)

    am._UNKNOWN_FAILURES.clear()
    auth, sessions = _fresh()
    for i in range(600):
        auth.authenticate(f"ghost_{i}", "x")
    assert len(am._UNKNOWN_FAILURES) <= 512
    am._UNKNOWN_FAILURES.clear()


def test_an_unknown_username_still_pays_for_a_hash():
    """The defence the test above stubs out.

    If an unknown username returned without hashing, its response would come
    back measurably faster than a wrong password against a real account, and
    that difference is enough to enumerate who has an account. Nothing
    asserted this, so removing the dummy-hash verification would have passed
    the whole suite.
    """
    import auth.auth_manager as am
    from auth import password_manager as pm

    calls = []
    real_verify = pm.verify_password

    def counting_verify(password, stored):
        calls.append(stored)
        return real_verify(password, stored)

    am.pm.verify_password = counting_verify
    try:
        am._UNKNOWN_FAILURES.clear()
        auth, sessions = _fresh()
        auth.authenticate("definitely_not_a_real_account", "whatever")
    finally:
        am.pm.verify_password = real_verify
        am._UNKNOWN_FAILURES.clear()

    assert calls, ("authenticating an unknown user did no hash verification, so "
                   "it returns faster than a real account and reveals which "
                   "usernames exist")
    assert calls[0] == am._dummy_hash(), "verified against something other than "\
                                         "the dummy hash"
