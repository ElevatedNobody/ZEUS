"""
ZEUS Memory Database
SQLite-backed storage for conversation history, long-term facts, and
task history. Deliberately simple - no ORM - so it stays fast and
debuggable on low-end hardware.
"""

import sqlite3
import time
from pathlib import Path
from typing import List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent

# --------------------------------------------------------------- the file --
#
# The project was called ZEUS before it was called ZEUS, and the database
# file was named after it. An installation that predates the rename holds the
# operator's account, their conversation history, the vault and every plant
# record in `database/jarvis.db`.
#
# Simply pointing at the new name would have found nothing, created an empty
# database beside the old one, and presented a first-run "create your account"
# screen to someone whose account already existed. Their data would still have
# been on disk - which is worse, not better, because nothing would have said so.
#
# So the old file is CARRIED FORWARD, once, by renaming it. After that the new
# name is simply the name. If both files somehow exist, the new one wins and
# the old one is left untouched rather than merged: two databases cannot be
# reconciled automatically, and quietly picking rows from each would lose data
# in a way nobody could see.

LEGACY_DB_NAME = "jarvis.db"
DB_NAME = "medha.db"


def _resolve_db_path() -> Path:
    from core.paths import data_dir
    directory = data_dir()
    current = directory / DB_NAME
    legacy = directory / LEGACY_DB_NAME

    if current.exists() or not legacy.exists():
        return current

    try:
        legacy.rename(current)
        # The write-ahead log and shared-memory files belong to the database
        # and are named after it. Leaving them behind would make SQLite
        # discard any transaction that had not yet been checkpointed.
        for suffix in ("-wal", "-shm"):
            companion = directory / (LEGACY_DB_NAME + suffix)
            if companion.exists():
                companion.rename(directory / (DB_NAME + suffix))
        print(f"[ZEUS] carried your existing data forward: "
              f"{LEGACY_DB_NAME} -> {DB_NAME}")
    except OSError as error:
        # A locked or read-only file must not stop ZEUS from starting, and it
        # must not silently start empty either. Keep using the old file.
        print(f"[ZEUS] could not rename {LEGACY_DB_NAME} to {DB_NAME} "
              f"({error}); continuing to use {LEGACY_DB_NAME}")
        return legacy
    return current


DB_PATH = _resolve_db_path()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT UNIQUE NOT NULL,
    value TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS task_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    description TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    completed_at REAL
);

CREATE TABLE IF NOT EXISTS todos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    completed INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    completed_at REAL
);

CREATE TABLE IF NOT EXISTS calendar_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    event_date TEXT NOT NULL,   -- 'YYYY-MM-DD'
    event_time TEXT,            -- 'HH:MM' or NULL for all-day
    notes TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    data TEXT NOT NULL,      -- JSON blob: the full structured weekly plan
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS document_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_path TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    chunk_text TEXT NOT NULL,
    embedding TEXT NOT NULL,   -- JSON-encoded list[float]
    indexed_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_created ON messages(created_at);
CREATE INDEX IF NOT EXISTS idx_facts_key ON facts(key);
CREATE INDEX IF NOT EXISTS idx_chunks_source ON document_chunks(source_path);
CREATE INDEX IF NOT EXISTS idx_todos_completed ON todos(completed);
CREATE INDEX IF NOT EXISTS idx_events_date ON calendar_events(event_date);
"""

# ---------------------------------------------------------------------------
# Industrial knowledge base (SIH26117 sovereign on-premise workbench).
#
# Kept in the SAME local SQLite file as everything else - no separate vector
# database server, no network service. This is deliberately ADDITIVE: the
# original `document_chunks` RAG table above is untouched and the existing
# index_folder / ask_knowledge_base tools keep working exactly as before.
#
# The difference is provenance: an industrial chunk never loses the document,
# page, section, heading, revision, equipment and extraction method it came
# from, so ZEUS can always answer "which document, which page, what
# evidence" instead of producing an unattributable wall of text.
# ---------------------------------------------------------------------------

_INDUSTRIAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS industrial_documents (
    document_id TEXT PRIMARY KEY,          -- stable hash-based id
    filename TEXT NOT NULL,
    source_path TEXT NOT NULL,
    document_type TEXT,                    -- schema id, e.g. 'inspection_report'
    document_class TEXT,                   -- 'user_document' | 'knowledge_corpus'
    claim_type TEXT,                       -- default claim type for its chunks
    title TEXT,
    revision TEXT,
    doc_date TEXT,
    author TEXT,
    equipment TEXT,
    plant_unit TEXT,
    department TEXT,
    confidentiality TEXT,
    page_count INTEGER DEFAULT 0,
    file_hash TEXT,
    extraction_method TEXT,
    metadata TEXT,                         -- JSON blob: schema fields + extras
    ingested_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS industrial_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    element_type TEXT NOT NULL DEFAULT 'text',  -- text|heading|table|figure|header|footer
    page_number INTEGER,
    section TEXT,
    heading TEXT,
    revision TEXT,
    chunk_date TEXT,
    author TEXT,
    equipment TEXT,
    plant_unit TEXT,
    department TEXT,
    confidentiality TEXT,
    source_location TEXT,                  -- 'file.pdf#page=3' / 'sheet:Data!A1'
    extraction_method TEXT,                -- native_pdf|pymupdf|tesseract|paddleocr|...
    ocr_confidence REAL,
    claim_type TEXT,                       -- historical_fact|engineering_principle|...
    chunk_text TEXT NOT NULL,
    embedding TEXT,                        -- JSON-encoded list[float], NULL if not embedded
    embedding_model TEXT,
    token_estimate INTEGER,
    created_at REAL NOT NULL,
    FOREIGN KEY (document_id) REFERENCES industrial_documents(document_id)
);

CREATE TABLE IF NOT EXISTS industrial_assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id TEXT NOT NULL,
    page_number INTEGER,
    asset_type TEXT,                       -- image|figure|table_image
    asset_path TEXT,                       -- local path under database/industrial_assets
    caption TEXT,
    extraction_method TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS industrial_ingestion_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    source_path TEXT NOT NULL,
    status TEXT NOT NULL,                  -- ok|skipped|failed
    document_id TEXT,
    chunks INTEGER DEFAULT 0,
    detail TEXT,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ind_chunks_doc ON industrial_chunks(document_id);
CREATE INDEX IF NOT EXISTS idx_ind_chunks_equipment ON industrial_chunks(equipment);
CREATE INDEX IF NOT EXISTS idx_ind_chunks_claim ON industrial_chunks(claim_type);
CREATE INDEX IF NOT EXISTS idx_ind_docs_type ON industrial_documents(document_type);
CREATE INDEX IF NOT EXISTS idx_ind_docs_path ON industrial_documents(source_path);
CREATE INDEX IF NOT EXISTS idx_ind_assets_doc ON industrial_assets(document_id);
CREATE INDEX IF NOT EXISTS idx_ind_log_run ON industrial_ingestion_log(run_id);
"""


# ---------------------------------------------------------------------------
# Local authentication (mandatory login).
#
# Same SQLite file as everything else - no second user database, no server.
# Passwords are NEVER stored here in any recoverable form: the column holds
# a self-describing hash string produced by auth/password_manager.py
# (Argon2id / bcrypt / scrypt). See AUTHENTICATION_REPORT.md.
# ---------------------------------------------------------------------------

_AUTH_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name TEXT,
    password_hash TEXT NOT NULL,
    password_algorithm TEXT,
    created_at REAL NOT NULL,
    last_login REAL,
    failed_attempts INTEGER NOT NULL DEFAULT 0,
    locked_until REAL,
    is_active INTEGER NOT NULL DEFAULT 1,
    is_admin INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    created_at REAL NOT NULL,
    last_activity REAL NOT NULL,
    logout_time REAL,
    locked INTEGER NOT NULL DEFAULT 0,
    client TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

-- Authentication audit. Records WHAT happened and to which username -
-- never a password, never a hash.
CREATE TABLE IF NOT EXISTS auth_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT,
    event TEXT NOT NULL,
    detail TEXT,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_auth_events_time ON auth_events(created_at);
"""

# ---------------------------------------------------------------- vault --
#
# The secure file locker and the employee records.
#
# These are deliberately NOT stored with document_chunks or the industrial
# knowledge base. Those two are shared workbench data and are fed to RAG;
# vault contents must never be retrievable by a question, and employee records
# are personal data belonging to one operator's workspace.
#
# The table holds METADATA only. File bytes live encrypted on disk under
# database/vault/, named by an opaque id - a directory listing reveals neither
# what a file is called nor what it contains.
_VAULT_SCHEMA = """
CREATE TABLE IF NOT EXISTS vault_files (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id        TEXT NOT NULL UNIQUE,   -- opaque name on disk
    display_name   TEXT NOT NULL,          -- what the operator called it
    category       TEXT,                   -- free grouping, e.g. 'contracts'
    note           TEXT,
    size_bytes     INTEGER NOT NULL DEFAULT 0,
    sha256         TEXT,                   -- of the PLAINTEXT, for integrity
    original_path  TEXT,                   -- where it came from
    added_at       REAL NOT NULL,
    opened_at      REAL,
    user_id        INTEGER
);

CREATE TABLE IF NOT EXISTS employees (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id    TEXT NOT NULL,          -- the works number / staff id
    name           TEXT NOT NULL,
    role           TEXT,
    department     TEXT,
    email          TEXT,
    phone          TEXT,
    location       TEXT,
    started_on     TEXT,                   -- YYYY-MM-DD
    notes          TEXT,
    is_active      INTEGER NOT NULL DEFAULT 1,
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL,
    user_id        INTEGER
);

-- Every vault action, whether or not it succeeded. Separate from
-- logs/audit.log because this one is queryable and scoped to the operator.
CREATE TABLE IF NOT EXISTS vault_audit (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    action       TEXT NOT NULL,            -- unlock | add | open | export | delete | ...
    subject      TEXT,                     -- which file or employee
    detail       TEXT,
    succeeded    INTEGER NOT NULL DEFAULT 1,
    username     TEXT,
    created_at   REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_vault_files_user ON vault_files(user_id);
CREATE INDEX IF NOT EXISTS idx_employees_user   ON employees(user_id);
CREATE INDEX IF NOT EXISTS idx_employees_name   ON employees(name);
CREATE INDEX IF NOT EXISTS idx_vault_audit_time ON vault_audit(created_at DESC);
"""


# Tables that hold PERSONAL data and are therefore scoped to the logged-in
# user. Industrial documents and the knowledge corpus are deliberately NOT
# in this list: they are shared workbench data, not one person's memory.
_USER_SCOPED_TABLES = ("messages", "facts", "todos", "calendar_events",
                       "notes", "schedules", "vault_files", "employees")


# Columns added to EXISTING tables after they shipped. Same rules as
# _migrate_user_scoping: additive, nullable, idempotent, and a failure can
# never stop ZEUS from starting.
#
# Tasks and the calendar were extended rather than replaced. There was already
# a working todo list and a working calendar with tools, routes and an agent;
# a second "tasks" table beside them would have split the user's data in two
# and left "what's on my list" answering from only half of it.
_COLUMN_MIGRATIONS = {
    # ZEUS role-based access control. NULL means "derive from is_admin", which
    # is what every account created before roles existed has.
    "users": (
        ("role", "TEXT"),              # viewer | operator | engineer | admin
    ),
    # A to-do becomes a task with a deadline, a priority and an owner.
    "todos": (
        ("due_date", "TEXT"),          # YYYY-MM-DD, NULL = no deadline
        ("priority", "TEXT"),          # low | normal | high, NULL = normal
        ("assignee", "TEXT"),          # free text, or an employee name
        ("source", "TEXT"),            # where it came from: user | meeting | agent
    ),
    # A meeting is a calendar event with a kind - one timeline, so "what is on
    # Friday" answers with appointments and meetings together instead of the
    # caller having to ask two subsystems and merge the results.
    "calendar_events": (
        ("kind", "TEXT"),              # event | meeting, NULL = event
        ("attendees", "TEXT"),         # comma-separated
        ("location", "TEXT"),
        ("agenda", "TEXT"),
        ("duration_minutes", "INTEGER"),
    ),
}


def get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_connection()
    try:
        conn.executescript(_SCHEMA)
        conn.executescript(_INDUSTRIAL_SCHEMA)
        conn.executescript(_AUTH_SCHEMA)
        conn.executescript(_VAULT_SCHEMA)
        _migrate_user_scoping(conn)
        _migrate_columns(conn)
        conn.commit()
    finally:
        conn.close()


def _migrate_columns(conn: sqlite3.Connection):
    """Add new columns to tables that already exist on this machine.

    Same contract as _migrate_user_scoping: every column is nullable so
    existing rows stay valid, every step is idempotent, and a failure is
    swallowed rather than allowed to stop startup. There is no down-migration
    and no version table - that is the established pattern here.
    """
    for table, columns in _COLUMN_MIGRATIONS.items():
        try:
            existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        except sqlite3.Error:
            continue
        if not existing:
            continue                       # table not created yet
        for name, column_type in columns:
            if name in existing:
                continue
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {column_type}")
            except sqlite3.Error:
                pass


def _migrate_user_scoping(conn: sqlite3.Connection):
    """Add the user_id column to the personal tables of an EXISTING database.

    Additive and idempotent: existing rows keep user_id NULL, which is what
    preserves single-user behaviour for a database created before login
    existed. Those orphan rows are adopted by the first account that logs in
    (see adopt_orphan_rows), so nobody loses their history.
    """
    for table in _USER_SCOPED_TABLES:
        try:
            columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        except sqlite3.Error:
            continue
        if not columns or "user_id" in columns:
            continue
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN user_id INTEGER")
            conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_user "
                         f"ON {table}(user_id)")
        except sqlite3.Error:
            pass   # never let a migration stop ZEUS from starting


# ------------------------------------------------------------ active user --
# Set by auth/session_manager.py on login and cleared on logout/lock. When it
# is None (CLI without authentication, tests, a pre-login database) every
# query behaves exactly as it did before login existed.

_ACTIVE_USER_ID: Optional[int] = None


def set_active_user(user_id: Optional[int]):
    global _ACTIVE_USER_ID
    _ACTIVE_USER_ID = int(user_id) if user_id is not None else None


def get_active_user() -> Optional[int]:
    return _ACTIVE_USER_ID


def _scope(where_already: bool = False, column: str = "user_id"):
    """Return (sql_fragment, params) restricting a query to the active user."""
    if _ACTIVE_USER_ID is None:
        return "", []
    keyword = " AND " if where_already else " WHERE "
    return f"{keyword}{column} = ?", [_ACTIVE_USER_ID]


# ---------------------------------------------------------------- messages

def add_message(role: str, content: str):
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO messages (role, content, created_at, user_id) VALUES (?, ?, ?, ?)",
            (role, content, time.time(), _ACTIVE_USER_ID),
        )
        conn.commit()
    finally:
        conn.close()


def get_recent_messages(limit: int = 20) -> List[Tuple[str, str]]:
    conn = get_connection()
    try:
        scope, params = _scope()
        rows = conn.execute(
            f"SELECT role, content FROM messages{scope} ORDER BY id DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        return [(r["role"], r["content"]) for r in reversed(rows)]
    finally:
        conn.close()


def search_messages(query: str, limit: int = 20) -> List[Tuple[str, str, float]]:
    conn = get_connection()
    try:
        scope, params = _scope(where_already=True)
        rows = conn.execute(
            f"SELECT role, content, created_at FROM messages WHERE content LIKE ?{scope} "
            f"ORDER BY id DESC LIMIT ?",
            (f"%{query}%", *params, limit),
        ).fetchall()
        return [(r["role"], r["content"], r["created_at"]) for r in rows]
    finally:
        conn.close()


# ------------------------------------------------------------------- facts

def _fact_key(key: str) -> str:
    """Namespace a fact key by the active user.

    `facts.key` carries a UNIQUE constraint from the original schema, so two
    users storing a fact with the same derived key would otherwise overwrite
    each other. Prefixing keeps that constraint intact without rebuilding the
    table, and the prefix is stripped again on read, so nothing above this
    module ever sees it.
    """
    if _ACTIVE_USER_ID is None or key.startswith(f"u{_ACTIVE_USER_ID}::"):
        return key
    return f"u{_ACTIVE_USER_ID}::{key}"


def _strip_fact_key(key: str) -> str:
    if "::" in key and key.startswith("u"):
        prefix, _, rest = key.partition("::")
        if prefix[1:].isdigit():
            return rest
    return key


def set_fact(key: str, value: str):
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO facts (key, value, updated_at, user_id) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (_fact_key(key), value, time.time(), _ACTIVE_USER_ID),
        )
        conn.commit()
    finally:
        conn.close()


def get_fact(key: str) -> Optional[str]:
    conn = get_connection()
    try:
        scope, params = _scope(where_already=True)
        row = conn.execute(f"SELECT value FROM facts WHERE key = ?{scope}",
                           (_fact_key(key), *params)).fetchone()
        return row["value"] if row else None
    finally:
        conn.close()


def get_all_facts() -> dict:
    conn = get_connection()
    try:
        scope, params = _scope()
        rows = conn.execute(f"SELECT key, value FROM facts{scope}", params).fetchall()
        return {_strip_fact_key(r["key"]): r["value"] for r in rows}
    finally:
        conn.close()


# ------------------------------------------------------------- task history

def log_task(description: str, status: str, completed_at: Optional[float] = None):
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO task_history (description, status, created_at, completed_at) "
            "VALUES (?, ?, ?, ?)",
            (description, status, time.time(), completed_at),
        )
        conn.commit()
    finally:
        conn.close()


# -------------------------------------------------------------------- todos

def add_todo(text: str, due_date: Optional[str] = None,
             priority: Optional[str] = None, assignee: Optional[str] = None,
             source: Optional[str] = None) -> int:
    """Add a task.

    The four extra fields are all optional and all default to None, so every
    existing caller - `add_todo(text)` - behaves exactly as before. That is the
    whole reason this table was extended rather than replaced by a new one.
    """
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO todos (text, completed, created_at, user_id, "
            "due_date, priority, assignee, source) VALUES (?, 0, ?, ?, ?, ?, ?, ?)",
            (text, time.time(), _ACTIVE_USER_ID,
             due_date or None, priority or None, assignee or None, source or None),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def list_todos(include_completed: bool = True,
               due_before: Optional[str] = None) -> List[dict]:
    """Tasks, open ones first.

    Ordering puts anything with a deadline ahead of anything without, then
    orders by that deadline - a list where an overdue item sits below a
    dateless idea is not a task list anybody can work from. `due_before`
    (YYYY-MM-DD) narrows to what is due by then.
    """
    conn = get_connection()
    try:
        query = ("SELECT id, text, completed, created_at, completed_at, "
                 "due_date, priority, assignee, source FROM todos")
        params: list = []
        clauses = []
        if not include_completed:
            clauses.append("completed = 0")
        if due_before:
            clauses.append("due_date IS NOT NULL AND due_date <= ?")
            params.append(due_before)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
            scope, scope_params = _scope(where_already=True)
        else:
            scope, scope_params = _scope()
        query += scope
        params.extend(scope_params)
        query += (" ORDER BY completed ASC, "
                  "CASE WHEN due_date IS NULL THEN 1 ELSE 0 END ASC, "
                  "due_date ASC, created_at DESC")
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def update_todo(todo_id: int, **fields) -> bool:
    """Change a task in place. Only the named fields are touched.

    Editing the text of a task was not previously possible from anywhere -
    the old UI could only add, complete and delete.
    """
    allowed = {"text", "due_date", "priority", "assignee", "source"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False
    conn = get_connection()
    try:
        scope, scope_params = _scope(where_already=True)
        assignments = ", ".join(f"{k} = ?" for k in updates)
        cur = conn.execute(
            f"UPDATE todos SET {assignments} WHERE id = ?{scope}",
            (*updates.values(), todo_id, *scope_params))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def set_todo_completed(todo_id: int, completed: bool) -> bool:
    conn = get_connection()
    try:
        scope, scope_params = _scope(where_already=True)
        cur = conn.execute(
            f"UPDATE todos SET completed = ?, completed_at = ? WHERE id = ?{scope}",
            (1 if completed else 0, time.time() if completed else None, todo_id,
             *scope_params),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def delete_todo(todo_id: int) -> bool:
    conn = get_connection()
    try:
        scope, scope_params = _scope(where_already=True)
        cur = conn.execute(f"DELETE FROM todos WHERE id = ?{scope}",
                           (todo_id, *scope_params))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ---------------------------------------------------------- calendar events

def add_event(title: str, event_date: str, event_time: Optional[str] = None,
              notes: Optional[str] = None, kind: Optional[str] = None,
              attendees: Optional[str] = None, location: Optional[str] = None,
              agenda: Optional[str] = None,
              duration_minutes: Optional[int] = None) -> int:
    """Add a calendar entry.

    A MEETING is `kind="meeting"` with attendees and an agenda - not a separate
    table. One timeline means "what is on Friday" answers with appointments and
    meetings together, instead of the caller having to query two subsystems and
    merge the results in the right order.

    The five new fields default to None, so `add_event(title, date)` behaves
    exactly as it did before.
    """
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO calendar_events (title, event_date, event_time, notes, "
            "created_at, user_id, kind, attendees, location, agenda, duration_minutes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (title, event_date, event_time, notes, time.time(), _ACTIVE_USER_ID,
             kind or None, attendees or None, location or None, agenda or None,
             duration_minutes),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def list_events(start_date: Optional[str] = None, end_date: Optional[str] = None,
                kind: Optional[str] = None) -> List[dict]:
    """Calendar entries, earliest first.

    `start_date` alone means that exact day (this is how the day view fetches);
    both dates mean an inclusive range. `kind="meeting"` narrows to meetings.
    """
    conn = get_connection()
    try:
        query = ("SELECT id, title, event_date, event_time, notes, kind, "
                 "attendees, location, agenda, duration_minutes FROM calendar_events")
        params: list = []
        clauses = []
        if start_date and end_date:
            clauses.append("event_date BETWEEN ? AND ?")
            params.extend([start_date, end_date])
        elif start_date:
            clauses.append("event_date = ?")
            params.append(start_date)
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        scope, scope_params = _scope(where_already=bool(clauses))
        query += scope
        params.extend(scope_params)
        query += " ORDER BY event_date ASC, event_time ASC"
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def update_event(event_id: int, **fields) -> bool:
    """Change a calendar entry in place. There was previously no way to edit
    one at all - only add and delete."""
    allowed = {"title", "event_date", "event_time", "notes", "kind",
               "attendees", "location", "agenda", "duration_minutes"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False
    conn = get_connection()
    try:
        scope, scope_params = _scope(where_already=True)
        assignments = ", ".join(f"{k} = ?" for k in updates)
        cur = conn.execute(
            f"UPDATE calendar_events SET {assignments} WHERE id = ?{scope}",
            (*updates.values(), event_id, *scope_params))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def delete_event(event_id: int) -> bool:
    conn = get_connection()
    try:
        scope, scope_params = _scope(where_already=True)
        cur = conn.execute(f"DELETE FROM calendar_events WHERE id = ?{scope}",
                           (event_id, *scope_params))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ----------------------------------------------------------------- notes

def add_note(title: str, content: str) -> int:
    conn = get_connection()
    try:
        now = time.time()
        cur = conn.execute(
            "INSERT INTO notes (title, content, created_at, updated_at, user_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (title, content, now, now, _ACTIVE_USER_ID),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def list_notes() -> List[dict]:
    conn = get_connection()
    try:
        scope, params = _scope()
        rows = conn.execute(
            f"SELECT id, title, content, created_at, updated_at FROM notes{scope} "
            f"ORDER BY updated_at DESC", params
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def update_note(note_id: int, content: str) -> bool:
    conn = get_connection()
    try:
        scope, scope_params = _scope(where_already=True)
        cur = conn.execute(
            f"UPDATE notes SET content = ?, updated_at = ? WHERE id = ?{scope}",
            (content, time.time(), note_id, *scope_params),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def delete_note(note_id: int) -> bool:
    conn = get_connection()
    try:
        scope, scope_params = _scope(where_already=True)
        cur = conn.execute(f"DELETE FROM notes WHERE id = ?{scope}",
                           (note_id, *scope_params))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# -------------------------------------------------------------- schedules

def save_schedule(title: str, data_json: str) -> int:
    """Deactivates any previous active schedule and stores a new one as the
    single current active plan."""
    conn = get_connection()
    try:
        now = time.time()
        scope, scope_params = _scope(where_already=True)
        conn.execute(f"UPDATE schedules SET is_active = 0 WHERE is_active = 1{scope}",
                     scope_params)
        cur = conn.execute(
            "INSERT INTO schedules (title, data, is_active, created_at, updated_at, user_id) "
            "VALUES (?, ?, 1, ?, ?, ?)",
            (title, data_json, now, now, _ACTIVE_USER_ID),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_active_schedule() -> Optional[dict]:
    conn = get_connection()
    try:
        scope, params = _scope(where_already=True)
        row = conn.execute(
            f"SELECT id, title, data, created_at, updated_at FROM schedules "
            f"WHERE is_active = 1{scope} ORDER BY updated_at DESC LIMIT 1", params
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def update_active_schedule(data_json: str) -> bool:
    conn = get_connection()
    try:
        scope, scope_params = _scope(where_already=True)
        cur = conn.execute(
            f"UPDATE schedules SET data = ?, updated_at = ? WHERE is_active = 1{scope}",
            (data_json, time.time(), *scope_params),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ------------------------------------------------------------ RAG / knowledge

def add_chunk(source_path: str, chunk_index: int, chunk_text: str, embedding: list) -> int:
    import json
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO document_chunks (source_path, chunk_index, chunk_text, embedding, indexed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (source_path, chunk_index, chunk_text, json.dumps(embedding), time.time()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_all_chunks() -> List[dict]:
    import json
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT id, source_path, chunk_index, chunk_text, embedding FROM document_chunks"
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["embedding"] = json.loads(d["embedding"])
            result.append(d)
        return result
    finally:
        conn.close()


def delete_chunks_by_source(source_path: str) -> int:
    conn = get_connection()
    try:
        cur = conn.execute("DELETE FROM document_chunks WHERE source_path = ?", (source_path,))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def clear_all_chunks() -> int:
    conn = get_connection()
    try:
        cur = conn.execute("DELETE FROM document_chunks")
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def list_indexed_sources() -> List[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT source_path, COUNT(*) as chunk_count, MAX(indexed_at) as indexed_at "
            "FROM document_chunks GROUP BY source_path ORDER BY indexed_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ------------------------------------------------- industrial knowledge base
# All functions below are additive helpers for the industrial workbench.
# They never touch the original tables, so existing behaviour is unchanged.

def upsert_industrial_document(doc: dict) -> str:
    """Insert or replace an industrial document header row. `doc` must carry
    at least document_id, filename, source_path. Returns the document_id."""
    import json as _json
    fields = ["document_id", "filename", "source_path", "document_type", "document_class",
              "claim_type", "title", "revision", "doc_date", "author", "equipment",
              "plant_unit", "department", "confidentiality", "page_count", "file_hash",
              "extraction_method"]
    values = [doc.get(f) for f in fields]
    metadata = doc.get("metadata") or {}
    if not isinstance(metadata, str):
        metadata = _json.dumps(metadata, ensure_ascii=False)
    conn = get_connection()
    try:
        conn.execute(
            f"INSERT OR REPLACE INTO industrial_documents "
            f"({', '.join(fields)}, metadata, ingested_at) "
            f"VALUES ({', '.join(['?'] * len(fields))}, ?, ?)",
            (*values, metadata, time.time()),
        )
        conn.commit()
        return doc["document_id"]
    finally:
        conn.close()


def add_industrial_chunks(document_id: str, chunks: List[dict]) -> int:
    """Bulk-insert chunks for one document. Each chunk dict may carry any of
    the provenance columns; missing ones are stored as NULL."""
    import json as _json
    cols = ["chunk_index", "element_type", "page_number", "section", "heading", "revision",
            "chunk_date", "author", "equipment", "plant_unit", "department", "confidentiality",
            "source_location", "extraction_method", "ocr_confidence", "claim_type",
            "chunk_text", "embedding", "embedding_model", "token_estimate"]
    rows = []
    now = time.time()
    for c in chunks:
        emb = c.get("embedding")
        if emb is not None and not isinstance(emb, str):
            emb = _json.dumps(emb)
        row = [c.get("chunk_index"), c.get("element_type", "text"), c.get("page_number"),
               c.get("section"), c.get("heading"), c.get("revision"), c.get("chunk_date"),
               c.get("author"), c.get("equipment"), c.get("plant_unit"), c.get("department"),
               c.get("confidentiality"), c.get("source_location"), c.get("extraction_method"),
               c.get("ocr_confidence"), c.get("claim_type"), c.get("chunk_text"), emb,
               c.get("embedding_model"), c.get("token_estimate")]
        rows.append((document_id, *row, now))
    conn = get_connection()
    try:
        conn.executemany(
            f"INSERT INTO industrial_chunks (document_id, {', '.join(cols)}, created_at) "
            f"VALUES (?, {', '.join(['?'] * len(cols))}, ?)",
            rows,
        )
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def get_industrial_chunks(document_id: Optional[str] = None, with_embeddings: bool = True,
                          claim_types: Optional[List[str]] = None) -> List[dict]:
    """Load industrial chunks (optionally for one document / claim types).
    Embeddings are decoded back into python lists."""
    import json as _json
    query = ("SELECT c.*, d.filename, d.source_path, d.document_type, d.title, "
             "d.document_class FROM industrial_chunks c "
             "JOIN industrial_documents d ON d.document_id = c.document_id")
    where, params = [], []
    if document_id:
        where.append("c.document_id = ?")
        params.append(document_id)
    if with_embeddings:
        where.append("c.embedding IS NOT NULL")
    if claim_types:
        where.append(f"c.claim_type IN ({', '.join(['?'] * len(claim_types))})")
        params.extend(claim_types)
    if where:
        query += " WHERE " + " AND ".join(where)
    conn = get_connection()
    try:
        rows = conn.execute(query, params).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            if d.get("embedding"):
                try:
                    d["embedding"] = _json.loads(d["embedding"])
                except ValueError:
                    d["embedding"] = None
            out.append(d)
        return out
    finally:
        conn.close()


def list_industrial_documents() -> List[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT d.document_id, d.filename, d.source_path, d.document_type, "
            "d.document_class, d.claim_type, d.revision, d.equipment, d.plant_unit, "
            "d.page_count, d.ingested_at, COUNT(c.id) AS chunk_count "
            "FROM industrial_documents d "
            "LEFT JOIN industrial_chunks c ON c.document_id = d.document_id "
            "GROUP BY d.document_id ORDER BY d.ingested_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_industrial_document(document_id: str) -> Optional[dict]:
    import json as _json
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM industrial_documents WHERE document_id = ?", (document_id,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            d["metadata"] = _json.loads(d.get("metadata") or "{}")
        except ValueError:
            d["metadata"] = {}
        return d
    finally:
        conn.close()


def find_industrial_document_by_path(source_path: str) -> Optional[dict]:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT document_id, file_hash FROM industrial_documents WHERE source_path = ?",
            (source_path,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def delete_industrial_document(document_id: str) -> int:
    conn = get_connection()
    try:
        cur = conn.execute("DELETE FROM industrial_chunks WHERE document_id = ?", (document_id,))
        conn.execute("DELETE FROM industrial_assets WHERE document_id = ?", (document_id,))
        conn.execute("DELETE FROM industrial_documents WHERE document_id = ?", (document_id,))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def clear_industrial_knowledge_base() -> int:
    conn = get_connection()
    try:
        cur = conn.execute("DELETE FROM industrial_chunks")
        conn.execute("DELETE FROM industrial_assets")
        conn.execute("DELETE FROM industrial_documents")
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def add_industrial_asset(document_id: str, page_number: Optional[int], asset_type: str,
                         asset_path: str, caption: str = "", extraction_method: str = "") -> int:
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO industrial_assets (document_id, page_number, asset_type, asset_path, "
            "caption, extraction_method, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (document_id, page_number, asset_type, asset_path, caption, extraction_method, time.time()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def list_industrial_assets(document_id: Optional[str] = None) -> List[dict]:
    conn = get_connection()
    try:
        if document_id:
            rows = conn.execute(
                "SELECT * FROM industrial_assets WHERE document_id = ? ORDER BY page_number",
                (document_id,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM industrial_assets ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def log_ingestion(run_id: str, source_path: str, status: str, document_id: Optional[str] = None,
                  chunks: int = 0, detail: str = "") -> int:
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO industrial_ingestion_log (run_id, source_path, status, document_id, "
            "chunks, detail, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_id, source_path, status, document_id, chunks, detail, time.time()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_ingestion_run(run_id: str) -> List[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM industrial_ingestion_log WHERE run_id = ? ORDER BY id", (run_id,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def industrial_kb_stats() -> dict:
    conn = get_connection()
    try:
        docs = conn.execute("SELECT COUNT(*) AS n FROM industrial_documents").fetchone()["n"]
        chunks = conn.execute("SELECT COUNT(*) AS n FROM industrial_chunks").fetchone()["n"]
        embedded = conn.execute(
            "SELECT COUNT(*) AS n FROM industrial_chunks WHERE embedding IS NOT NULL").fetchone()["n"]
        assets = conn.execute("SELECT COUNT(*) AS n FROM industrial_assets").fetchone()["n"]
        by_class = conn.execute(
            "SELECT document_class, COUNT(*) AS n FROM industrial_documents "
            "GROUP BY document_class").fetchall()
        by_type = conn.execute(
            "SELECT document_type, COUNT(*) AS n FROM industrial_documents "
            "GROUP BY document_type").fetchall()
        return {
            "documents": docs, "chunks": chunks, "embedded_chunks": embedded, "assets": assets,
            "by_class": {r["document_class"] or "unknown": r["n"] for r in by_class},
            "by_type": {r["document_type"] or "unknown": r["n"] for r in by_type},
        }
    finally:
        conn.close()


# --------------------------------------------------------------------- auth
# Users, sessions and the authentication audit trail. No password, plaintext
# or hash, is ever written to a log by these functions.

def create_user(username: str, password_hash: str, algorithm: str = "",
                display_name: str = "", is_admin: bool = False, role: str = "") -> int:
    role = role or ("admin" if is_admin else "operator")
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO users (username, display_name, password_hash, "
            "password_algorithm, created_at, is_active, is_admin, role) "
            "VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
            (username.strip(), display_name or username.strip(), password_hash,
             algorithm, time.time(), 1 if (is_admin or role == "admin") else 0, role),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_user(username: str) -> Optional[dict]:
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM users WHERE username = ?",
                           (username.strip(),)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_user_by_id(user_id: int) -> Optional[dict]:
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_users() -> List[dict]:
    """Never returns password material."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT id, username, display_name, created_at, last_login, "
            "failed_attempts, locked_until, is_active, is_admin FROM users "
            "ORDER BY id").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def count_users() -> int:
    conn = get_connection()
    try:
        return conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
    finally:
        conn.close()


def update_user_password(user_id: int, password_hash: str, algorithm: str = "") -> bool:
    conn = get_connection()
    try:
        cur = conn.execute(
            "UPDATE users SET password_hash = ?, password_algorithm = ? WHERE id = ?",
            (password_hash, algorithm, user_id))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def rename_user(user_id: int, new_username: str, new_display_name: Optional[str] = None) -> bool:
    new_u = (new_username or "").strip()
    if not new_u:
        raise ValueError("New username cannot be empty.")
    conn = get_connection()
    try:
        existing = conn.execute(
            "SELECT id FROM users WHERE username = ? AND id != ?",
            (new_u, int(user_id)),
        ).fetchone()
        if existing:
            raise ValueError(f"An account named '{new_u}' already exists.")
        cur = conn.execute(
            "UPDATE users SET username = ?, display_name = ? WHERE id = ?",
            (new_u, (new_display_name or new_u).strip(), int(user_id)),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()



def record_login_success(user_id: int):
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE users SET last_login = ?, failed_attempts = 0, locked_until = NULL "
            "WHERE id = ?", (time.time(), user_id))
        conn.commit()
    finally:
        conn.close()


def record_login_failure(user_id: int, lock_until: Optional[float] = None) -> int:
    """Increment the failure counter and optionally lock. Returns the new count."""
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE users SET failed_attempts = failed_attempts + 1, "
            "locked_until = COALESCE(?, locked_until) WHERE id = ?",
            (lock_until, user_id))
        conn.commit()
        row = conn.execute("SELECT failed_attempts FROM users WHERE id = ?",
                           (user_id,)).fetchone()
        return row["failed_attempts"] if row else 0
    finally:
        conn.close()


def set_user_lock(user_id: int, locked_until: Optional[float]):
    conn = get_connection()
    try:
        conn.execute("UPDATE users SET locked_until = ? WHERE id = ?",
                     (locked_until, user_id))
        conn.commit()
    finally:
        conn.close()


def set_user_active(user_id: int, active: bool) -> bool:
    conn = get_connection()
    try:
        cur = conn.execute("UPDATE users SET is_active = ? WHERE id = ?",
                           (1 if active else 0, user_id))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def delete_user(user_id: int) -> bool:
    conn = get_connection()
    try:
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        cur = conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ------------------------------------------------------------------ sessions

def create_session(session_id: str, user_id: int, client: str = "") -> str:
    conn = get_connection()
    try:
        now = time.time()
        conn.execute(
            "INSERT INTO sessions (id, user_id, created_at, last_activity, client) "
            "VALUES (?, ?, ?, ?, ?)", (session_id, user_id, now, now, client))
        conn.commit()
        return session_id
    finally:
        conn.close()


def get_session(session_id: str) -> Optional[dict]:
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM sessions WHERE id = ?",
                           (session_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def touch_session(session_id: str):
    conn = get_connection()
    try:
        conn.execute("UPDATE sessions SET last_activity = ? WHERE id = ?",
                     (time.time(), session_id))
        conn.commit()
    finally:
        conn.close()


def set_session_locked(session_id: str, locked: bool):
    conn = get_connection()
    try:
        conn.execute("UPDATE sessions SET locked = ?, last_activity = ? WHERE id = ?",
                     (1 if locked else 0, time.time(), session_id))
        conn.commit()
    finally:
        conn.close()


def close_session(session_id: str):
    conn = get_connection()
    try:
        conn.execute("UPDATE sessions SET logout_time = ?, last_activity = ? "
                     "WHERE id = ?", (time.time(), time.time(), session_id))
        conn.commit()
    finally:
        conn.close()


def list_sessions(user_id: Optional[int] = None, active_only: bool = False) -> List[dict]:
    conn = get_connection()
    try:
        query = "SELECT * FROM sessions"
        clauses, params = [], []
        if user_id is not None:
            clauses.append("user_id = ?")
            params.append(user_id)
        if active_only:
            clauses.append("logout_time IS NULL")
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC"
        return [dict(r) for r in conn.execute(query, params).fetchall()]
    finally:
        conn.close()


# -------------------------------------------------------------- auth events

def log_auth_event(event: str, username: str = "", detail: str = ""):
    """Audit trail for authentication. `detail` must never contain a password."""
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO auth_events (username, event, detail, created_at) "
            "VALUES (?, ?, ?, ?)", (username, event, detail, time.time()))
        conn.commit()
    finally:
        conn.close()
    try:
        from security.audit_chain import audit_chain
        audit_chain.append("auth", f"{event} {detail}".strip(), actor=username or "")
        if event in ("login_failed", "login_blocked", "account_locked"):
            from security import events
            events.record("login_failed" if event != "account_locked" else "account_locked",
                          f"{username}: {detail}", severity="notice", username=username or "")
    except Exception:
        pass


def user_role(user: dict) -> str:
    """The effective role of a user row."""
    role = (user or {}).get("role") or ""
    if role in ("viewer", "operator", "engineer", "admin"):
        return role
    return "admin" if (user or {}).get("is_admin") else "operator"


def set_user_role(user_id: int, role: str) -> bool:
    if role not in ("viewer", "operator", "engineer", "admin"):
        raise ValueError(f"unknown role '{role}'")
    conn = get_connection()
    try:
        cur = conn.execute("UPDATE users SET role = ?, is_admin = ? WHERE id = ?",
                           (role, 1 if role == "admin" else 0, int(user_id)))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def recent_auth_events(limit: int = 20) -> List[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT username, event, detail, created_at FROM auth_events "
            "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ------------------------------------------------------------- data adoption

def orphan_row_counts() -> dict:
    """How many rows predate login (user_id IS NULL) in each personal table."""
    conn = get_connection()
    counts = {}
    try:
        for table in _USER_SCOPED_TABLES:
            try:
                counts[table] = conn.execute(
                    f"SELECT COUNT(*) AS n FROM {table} WHERE user_id IS NULL"
                ).fetchone()["n"]
            except sqlite3.Error:
                counts[table] = 0
        return counts
    finally:
        conn.close()


def adopt_orphan_rows(user_id: int) -> dict:
    """Give pre-login rows to `user_id`.

    Called once, when the FIRST account logs in, so a database created before
    authentication existed keeps all of its history instead of appearing
    empty. Fact keys are namespaced at the same time so they cannot collide
    with another user's facts later.
    """
    conn = get_connection()
    adopted = {}
    try:
        for table in _USER_SCOPED_TABLES:
            try:
                cur = conn.execute(f"UPDATE {table} SET user_id = ? WHERE user_id IS NULL",
                                   (user_id,))
                adopted[table] = cur.rowcount
            except sqlite3.Error:
                adopted[table] = 0
        try:
            rows = conn.execute(
                "SELECT id, key FROM facts WHERE user_id = ?", (user_id,)).fetchall()
            for row in rows:
                key = row["key"]
                if not (key.startswith("u") and "::" in key
                        and key.partition("::")[0][1:].isdigit()):
                    conn.execute("UPDATE facts SET key = ? WHERE id = ?",
                                 (f"u{user_id}::{key}", row["id"]))
        except sqlite3.Error:
            pass
        conn.commit()
        return adopted
    finally:
        conn.close()


# ============================================================== vault ======
#
# METADATA ONLY. The file bytes live encrypted on disk under database/vault/,
# named by `file_id`, so a directory listing reveals neither the name of a
# file nor its contents. Decryption lives in core/vault.py; this module never
# sees a passphrase or a key.

def add_vault_file(file_id: str, display_name: str, size_bytes: int,
                   sha256: str = "", category: str = "", note: str = "",
                   original_path: str = "") -> int:
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO vault_files (file_id, display_name, category, note, "
            "size_bytes, sha256, original_path, added_at, user_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (file_id, display_name, category or None, note or None,
             int(size_bytes), sha256 or None, original_path or None,
             time.time(), _ACTIVE_USER_ID))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def list_vault_files(category: str = "") -> List[dict]:
    conn = get_connection()
    try:
        query = ("SELECT id, file_id, display_name, category, note, size_bytes, "
                 "sha256, original_path, added_at, opened_at FROM vault_files")
        params: list = []
        has_where = False
        if category:
            query += " WHERE category = ?"
            params.append(category)
            has_where = True
        scope, scope_params = _scope(where_already=has_where)
        query += scope
        params.extend(scope_params)
        query += " ORDER BY added_at DESC"
        return [dict(r) for r in conn.execute(query, params).fetchall()]
    finally:
        conn.close()


def get_vault_file(file_id: str) -> Optional[dict]:
    conn = get_connection()
    try:
        scope, scope_params = _scope(where_already=True)
        row = conn.execute(
            f"SELECT * FROM vault_files WHERE file_id = ?{scope}",
            (file_id, *scope_params)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def find_vault_file(name: str) -> Optional[dict]:
    """Look a file up by the name the operator gave it, case-insensitively.
    An exact match wins over a partial one, so 'report' does not open
    'report_draft_v2' when 'report' itself exists."""
    conn = get_connection()
    try:
        scope, scope_params = _scope(where_already=True)
        row = conn.execute(
            f"SELECT * FROM vault_files WHERE display_name = ? COLLATE NOCASE{scope}",
            (name, *scope_params)).fetchone()
        if row:
            return dict(row)
        row = conn.execute(
            f"SELECT * FROM vault_files WHERE display_name LIKE ? COLLATE NOCASE{scope} "
            f"ORDER BY added_at DESC",
            (f"%{name}%", *scope_params)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def touch_vault_file(file_id: str):
    conn = get_connection()
    try:
        conn.execute("UPDATE vault_files SET opened_at = ? WHERE file_id = ?",
                     (time.time(), file_id))
        conn.commit()
    finally:
        conn.close()


def delete_vault_file(file_id: str) -> bool:
    conn = get_connection()
    try:
        scope, scope_params = _scope(where_already=True)
        cur = conn.execute(f"DELETE FROM vault_files WHERE file_id = ?{scope}",
                           (file_id, *scope_params))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def vault_stats() -> dict:
    conn = get_connection()
    try:
        scope, params = _scope()
        row = conn.execute(
            f"SELECT COUNT(*) AS files, COALESCE(SUM(size_bytes), 0) AS bytes "
            f"FROM vault_files{scope}", params).fetchone()
        categories = conn.execute(
            f"SELECT category, COUNT(*) AS n FROM vault_files{scope} "
            f"GROUP BY category", params).fetchall()
        return {"files": row["files"], "bytes": row["bytes"],
                "categories": {(r["category"] or "uncategorised"): r["n"]
                               for r in categories}}
    finally:
        conn.close()


# ========================================================== employees ======

def add_employee(employee_id: str, name: str, role: str = "", department: str = "",
                 email: str = "", phone: str = "", location: str = "",
                 started_on: str = "", notes: str = "") -> int:
    conn = get_connection()
    try:
        now = time.time()
        cur = conn.execute(
            "INSERT INTO employees (employee_id, name, role, department, email, "
            "phone, location, started_on, notes, is_active, created_at, updated_at, "
            "user_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
            (employee_id, name, role or None, department or None, email or None,
             phone or None, location or None, started_on or None, notes or None,
             now, now, _ACTIVE_USER_ID))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def list_employees(department: str = "", include_inactive: bool = False) -> List[dict]:
    conn = get_connection()
    try:
        query = "SELECT * FROM employees"
        params: list = []
        clauses = []
        if department:
            clauses.append("department = ? COLLATE NOCASE")
            params.append(department)
        if not include_inactive:
            clauses.append("is_active = 1")
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        scope, scope_params = _scope(where_already=bool(clauses))
        query += scope
        params.extend(scope_params)
        query += " ORDER BY name ASC"
        return [dict(r) for r in conn.execute(query, params).fetchall()]
    finally:
        conn.close()


def find_employee(query_text: str) -> Optional[dict]:
    """By works number first, then by name. The id is the unambiguous handle,
    so it wins - two people can share a name, they cannot share a number."""
    conn = get_connection()
    try:
        scope, scope_params = _scope(where_already=True)
        for sql, value in (
                (f"SELECT * FROM employees WHERE employee_id = ? COLLATE NOCASE{scope}",
                 query_text),
                (f"SELECT * FROM employees WHERE name = ? COLLATE NOCASE{scope}",
                 query_text),
                (f"SELECT * FROM employees WHERE name LIKE ? COLLATE NOCASE{scope}",
                 f"%{query_text}%")):
            row = conn.execute(sql, (value, *scope_params)).fetchone()
            if row:
                return dict(row)
        return None
    finally:
        conn.close()


def update_employee(employee_id: str, **fields) -> bool:
    allowed = {"name", "role", "department", "email", "phone", "location",
               "started_on", "notes", "is_active"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False
    conn = get_connection()
    try:
        scope, scope_params = _scope(where_already=True)
        assignments = ", ".join(f"{k} = ?" for k in updates)
        cur = conn.execute(
            f"UPDATE employees SET {assignments}, updated_at = ? "
            f"WHERE employee_id = ? COLLATE NOCASE{scope}",
            (*updates.values(), time.time(), employee_id, *scope_params))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def delete_employee(employee_id: str) -> bool:
    conn = get_connection()
    try:
        scope, scope_params = _scope(where_already=True)
        cur = conn.execute(
            f"DELETE FROM employees WHERE employee_id = ? COLLATE NOCASE{scope}",
            (employee_id, *scope_params))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ======================================================= vault audit ======

def log_vault_event(action: str, subject: str = "", detail: str = "",
                    succeeded: bool = True, username: str = ""):
    """Record a vault action. A FAILED unlock is exactly as important to record
    as a successful one, which is why `succeeded` is stored rather than only
    the happy path being logged."""
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO vault_audit (action, subject, detail, succeeded, "
            "username, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (action, subject or None, detail or None, 1 if succeeded else 0,
             username or None, time.time()))
        conn.commit()
    finally:
        conn.close()


def recent_vault_events(limit: int = 50) -> List[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT action, subject, detail, succeeded, username, created_at "
            "FROM vault_audit ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
