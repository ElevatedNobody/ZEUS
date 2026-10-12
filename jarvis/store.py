"""
The ZEUS project database: `<data_dir>/jarvis_projects.db`.

Relational tables for projects, schedules and their versions, activities,
dependencies, evidence (documents, images, voice), correspondence, extractions,
field observations, entity mappings, blockers, delays, snapshots, forecast
changes, chronology, contract notices, historical activities, learning cards,
synchronisation and audit - plus one append-only, hash-chained event table that
every state change is derived from. JSON columns hold only genuinely open
metadata (a model's raw output, a computed snapshot).

Kept beside medha.db and knowledge_fabric.db in ZEUS's data directory, which is
a protected root: no prompt-driven file tool can read or overwrite it.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence

SCHEMA_VERSION = 1
DB_NAME = "jarvis_projects.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jv_meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS jv_projects (
    project_id TEXT PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    client TEXT, contractor TEXT, location TEXT, description TEXT,
    origin TEXT NOT NULL DEFAULT 'company',
    status TEXT NOT NULL DEFAULT 'active',
    start_date TEXT,
    near_critical_days INTEGER NOT NULL DEFAULT 5,
    created_by TEXT, created_at REAL NOT NULL,
    metadata TEXT
);

CREATE TABLE IF NOT EXISTS jv_calendars (
    calendar_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES jv_projects(project_id),
    name TEXT NOT NULL,
    workdays TEXT NOT NULL DEFAULT '0123456',      -- Monday = 0
    holidays TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS jv_schedules (
    schedule_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES jv_projects(project_id),
    name TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS jv_schedule_versions (
    version_id TEXT PRIMARY KEY,
    schedule_id TEXT NOT NULL REFERENCES jv_schedules(schedule_id),
    project_id TEXT NOT NULL REFERENCES jv_projects(project_id),
    label TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'update',             -- baseline | update
    data_date TEXT,
    provider TEXT, source_file TEXT, source_hash TEXT, evidence_id TEXT,
    status TEXT NOT NULL DEFAULT 'pending',          -- pending | approved | rejected
    created_by TEXT, created_at REAL NOT NULL, captured_at TEXT,
    approved_by TEXT, approved_at TEXT,
    notes TEXT
);
CREATE INDEX IF NOT EXISTS jv_sv_project ON jv_schedule_versions(project_id, created_at);

CREATE TABLE IF NOT EXISTS jv_wbs (
    wbs_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES jv_projects(project_id),
    code TEXT NOT NULL, name TEXT, parent_code TEXT,
    UNIQUE (project_id, code)
);

CREATE TABLE IF NOT EXISTS jv_activities (
    activity_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES jv_projects(project_id),
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    wbs_code TEXT,
    activity_type TEXT NOT NULL DEFAULT 'task',      -- task | start_milestone | finish_milestone
    location TEXT, contractor TEXT, unit TEXT, planned_qty REAL,
    work_category TEXT,
    created_at REAL NOT NULL,
    UNIQUE (project_id, code)
);
CREATE INDEX IF NOT EXISTS jv_act_project ON jv_activities(project_id);

CREATE TABLE IF NOT EXISTS jv_activity_versions (
    version_id TEXT NOT NULL REFERENCES jv_schedule_versions(version_id),
    activity_id TEXT NOT NULL REFERENCES jv_activities(activity_id),
    planned_start TEXT, planned_finish TEXT, duration_days INTEGER,
    baseline_start TEXT, baseline_finish TEXT,
    actual_start TEXT, actual_finish TEXT,
    percent_complete REAL, remaining_days INTEGER,
    constraint_type TEXT, constraint_date TEXT,
    planned_qty REAL,
    PRIMARY KEY (version_id, activity_id)
);

CREATE TABLE IF NOT EXISTS jv_dependencies (
    version_id TEXT NOT NULL REFERENCES jv_schedule_versions(version_id),
    pred_id TEXT NOT NULL REFERENCES jv_activities(activity_id),
    succ_id TEXT NOT NULL REFERENCES jv_activities(activity_id),
    dep_type TEXT NOT NULL DEFAULT 'FS',             -- FS | SS | FF | SF
    lag_days INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (version_id, pred_id, succ_id)
);

CREATE VIEW IF NOT EXISTS jv_milestones AS
    SELECT a.activity_id, a.project_id, a.code, a.name, a.activity_type,
           v.version_id, v.planned_finish, v.baseline_finish, v.actual_finish
    FROM jv_activities a JOIN jv_activity_versions v ON v.activity_id = a.activity_id
    WHERE a.activity_type LIKE '%milestone%';

CREATE TABLE IF NOT EXISTS jv_evidence (
    evidence_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES jv_projects(project_id),
    kind TEXT NOT NULL,                              -- text | document | image | voice | spreadsheet | correspondence | schedule_file
    title TEXT, filename TEXT, stored_path TEXT,
    sha256 TEXT, bytes INTEGER, mime TEXT,
    occurred_at TEXT, captured_at TEXT NOT NULL, received_at REAL NOT NULL,
    captured_by TEXT, device_id TEXT,
    source_type TEXT NOT NULL DEFAULT 'field',       -- field | office | contractor | client | import | system
    original_text TEXT,
    language TEXT, scripts TEXT,
    origin TEXT NOT NULL DEFAULT 'company',
    metadata TEXT
);
CREATE INDEX IF NOT EXISTS jv_ev_project ON jv_evidence(project_id, captured_at);
CREATE INDEX IF NOT EXISTS jv_ev_hash ON jv_evidence(project_id, sha256);

CREATE TABLE IF NOT EXISTS jv_documents (
    evidence_id TEXT PRIMARY KEY REFERENCES jv_evidence(evidence_id),
    doc_type TEXT, title TEXT, doc_date TEXT, author TEXT, reference TEXT,
    pages INTEGER, extraction_methods TEXT, ocr_confidence REAL
);

CREATE TABLE IF NOT EXISTS jv_image_evidence (
    evidence_id TEXT PRIMARY KEY REFERENCES jv_evidence(evidence_id),
    width INTEGER, height INTEGER, exif_datetime TEXT,
    ocr_text TEXT, ocr_engine TEXT, ocr_confidence REAL,
    vision_description TEXT, vision_model TEXT
);

CREATE TABLE IF NOT EXISTS jv_voice_evidence (
    evidence_id TEXT PRIMARY KEY REFERENCES jv_evidence(evidence_id),
    duration_s REAL, transcript TEXT, stt_engine TEXT,
    status TEXT NOT NULL, detail TEXT
);

CREATE TABLE IF NOT EXISTS jv_correspondence (
    correspondence_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES jv_projects(project_id),
    evidence_id TEXT REFERENCES jv_evidence(evidence_id),
    reference TEXT, direction TEXT, from_party TEXT, to_party TEXT,
    subject TEXT, letter_date TEXT, received_date TEXT, summary TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS jv_extractions (
    extraction_id TEXT PRIMARY KEY,
    evidence_id TEXT NOT NULL REFERENCES jv_evidence(evidence_id),
    project_id TEXT NOT NULL,
    method TEXT NOT NULL,                            -- rules | model | ocr | vision | stt | document_pipeline
    model TEXT, created_at REAL NOT NULL, duration_ms INTEGER,
    status TEXT NOT NULL, confidence REAL,
    output TEXT, error TEXT
);
CREATE INDEX IF NOT EXISTS jv_ex_evidence ON jv_extractions(evidence_id);

CREATE TABLE IF NOT EXISTS jv_field_observations (
    observation_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES jv_projects(project_id),
    evidence_id TEXT NOT NULL REFERENCES jv_evidence(evidence_id),
    extraction_id TEXT,
    observed_at TEXT,
    original_text TEXT, normalized_text TEXT, language TEXT,
    activity_ref TEXT, activity_id TEXT, link_confidence REAL,
    link_status TEXT NOT NULL DEFAULT 'unlinked',   -- linked | needs_confirmation | unlinked
    link_reasons TEXT,
    kind TEXT,                                       -- progress | started | completed | blocker | blocker_cleared | delay | note | correspondence
    percent_complete REAL, quantity REAL, unit TEXT,
    status_word TEXT, blocker_text TEXT, blocker_category TEXT, expected_date TEXT,
    remaining_days INTEGER,
    contractor TEXT, location_text TEXT,
    confidence REAL,
    event_id TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS jv_obs_project ON jv_field_observations(project_id, observed_at);

CREATE TABLE IF NOT EXISTS jv_entity_mappings (
    mapping_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    alias_norm TEXT NOT NULL, alias TEXT NOT NULL,
    activity_id TEXT NOT NULL REFERENCES jv_activities(activity_id),
    confidence REAL NOT NULL,
    source TEXT NOT NULL,                            -- import | confirmed | rule
    confirmed_by TEXT, created_at REAL NOT NULL,
    UNIQUE (project_id, alias_norm, activity_id)
);
CREATE INDEX IF NOT EXISTS jv_map_alias ON jv_entity_mappings(project_id, alias_norm);

CREATE TABLE IF NOT EXISTS jv_links (
    link_id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    from_kind TEXT NOT NULL, from_id TEXT NOT NULL,
    to_kind TEXT NOT NULL, to_id TEXT NOT NULL,
    relation TEXT NOT NULL,
    confidence REAL, method TEXT, created_by TEXT, created_at REAL NOT NULL,
    UNIQUE (from_kind, from_id, to_kind, to_id, relation)
);
CREATE INDEX IF NOT EXISTS jv_link_to ON jv_links(to_kind, to_id);
CREATE INDEX IF NOT EXISTS jv_link_from ON jv_links(from_kind, from_id);

CREATE TABLE IF NOT EXISTS jv_events (
    event_id TEXT PRIMARY KEY,
    seq INTEGER NOT NULL UNIQUE,
    project_id TEXT NOT NULL REFERENCES jv_projects(project_id),
    event_type TEXT NOT NULL,
    activity_id TEXT,
    occurred_at TEXT,
    captured_at TEXT NOT NULL,
    received_at REAL NOT NULL,
    device_id TEXT NOT NULL,
    actor TEXT,
    source TEXT,
    payload TEXT NOT NULL,
    evidence_ids TEXT NOT NULL DEFAULT '[]',
    observation_id TEXT,
    confidence REAL,
    base_head TEXT,
    lamport INTEGER NOT NULL DEFAULT 0,
    origin TEXT NOT NULL DEFAULT 'company',
    sync_state TEXT NOT NULL DEFAULT 'local',        -- local | synced | received
    content_hash TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    chain_hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS jv_evt_project ON jv_events(project_id, captured_at);
CREATE INDEX IF NOT EXISTS jv_evt_activity ON jv_events(activity_id, captured_at);
CREATE INDEX IF NOT EXISTS jv_evt_type ON jv_events(project_id, event_type);

CREATE TABLE IF NOT EXISTS jv_event_status (
    event_id TEXT PRIMARY KEY REFERENCES jv_events(event_id),
    status TEXT NOT NULL,                            -- submitted | approved | rejected | conflicted | superseded
    decided_by TEXT, decided_at TEXT, decision_event_id TEXT, reason TEXT
);

CREATE VIEW IF NOT EXISTS jv_progress_events AS
    SELECT e.*, s.status FROM jv_events e LEFT JOIN jv_event_status s ON s.event_id = e.event_id
    WHERE e.event_type IN ('progress_update', 'activity_started', 'activity_completed', 'remaining_update');

CREATE TABLE IF NOT EXISTS jv_blockers (
    blocker_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES jv_projects(project_id),
    activity_id TEXT REFERENCES jv_activities(activity_id),
    raised_event_id TEXT, description TEXT, original_text TEXT, category TEXT,
    raised_on TEXT, expected_clear TEXT,
    cleared_on TEXT, cleared_event_id TEXT,
    responsible_party TEXT,
    status TEXT NOT NULL DEFAULT 'open'
);

CREATE TABLE IF NOT EXISTS jv_delay_events (
    delay_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES jv_projects(project_id),
    activity_id TEXT REFERENCES jv_activities(activity_id),
    blocker_id TEXT, event_id TEXT,
    start_date TEXT, end_date TEXT, days INTEGER,
    cause_category TEXT, responsible_party TEXT,
    description TEXT, created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS jv_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES jv_projects(project_id),
    version_id TEXT,
    seq INTEGER NOT NULL,
    created_at REAL NOT NULL, captured_at TEXT NOT NULL,
    data_date TEXT,
    trigger_event_id TEXT, reason TEXT,
    project_finish TEXT, finish_activity_id TEXT,
    activities TEXT NOT NULL,
    UNIQUE (project_id, seq)
);

CREATE TABLE IF NOT EXISTS jv_forecast_changes (
    change_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES jv_projects(project_id),
    from_snapshot TEXT, to_snapshot TEXT NOT NULL,
    trigger_event_id TEXT, trigger_kind TEXT,
    previous_finish TEXT, new_finish TEXT, delta_days INTEGER,
    root_activity_id TEXT,
    chain TEXT, affected TEXT, milestones TEXT,
    created_at REAL NOT NULL, captured_at TEXT
);

CREATE TABLE IF NOT EXISTS jv_chronology (
    chron_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES jv_projects(project_id),
    occurred_at TEXT, captured_at TEXT,
    kind TEXT NOT NULL, title TEXT NOT NULL, detail TEXT,
    activity_id TEXT, source_kind TEXT, source_id TEXT,
    evidence_ids TEXT NOT NULL DEFAULT '[]',
    contractor TEXT, delay_type TEXT, source_type TEXT,
    origin TEXT NOT NULL DEFAULT 'company',
    created_at REAL NOT NULL,
    UNIQUE (source_kind, source_id, kind)
);
CREATE INDEX IF NOT EXISTS jv_chron_project ON jv_chronology(project_id, occurred_at);

CREATE TABLE IF NOT EXISTS jv_contract_refs (
    ref_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES jv_projects(project_id),
    clause TEXT NOT NULL, title TEXT,
    notice_type TEXT NOT NULL,
    notice_period_days INTEGER NOT NULL,
    trigger_types TEXT NOT NULL DEFAULT '[]',
    recipient TEXT, description TEXT,
    origin TEXT NOT NULL DEFAULT 'company',
    created_by TEXT, created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS jv_notices (
    notice_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES jv_projects(project_id),
    ref_id TEXT, trigger_kind TEXT, trigger_id TEXT,
    notice_type TEXT, issued_on TEXT, issued_by TEXT, to_party TEXT, reference TEXT,
    evidence_id TEXT, event_id TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS jv_notice_deadlines (
    deadline_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES jv_projects(project_id),
    ref_id TEXT NOT NULL REFERENCES jv_contract_refs(ref_id),
    trigger_kind TEXT NOT NULL, trigger_id TEXT NOT NULL,
    trigger_date TEXT NOT NULL, due_date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',             -- open | met | missed
    notice_id TEXT,
    created_at REAL NOT NULL,
    UNIQUE (ref_id, trigger_kind, trigger_id)
);

CREATE TABLE IF NOT EXISTS jv_historical_activities (
    hist_id TEXT PRIMARY KEY,
    project_id TEXT, project_name TEXT,
    activity_id TEXT,
    code TEXT, name TEXT NOT NULL, work_category TEXT,
    unit TEXT, quantity REAL,
    planned_duration_days INTEGER,
    actual_start TEXT, actual_finish TEXT, actual_duration_days INTEGER,
    productivity_per_day REAL,
    contractor TEXT, location TEXT,
    conditions TEXT, blockers TEXT,
    evidence_count INTEGER NOT NULL DEFAULT 0,
    origin TEXT NOT NULL DEFAULT 'company',
    source TEXT NOT NULL,                            -- derived | imported
    source_file TEXT,
    created_at REAL NOT NULL,
    UNIQUE (project_id, code, source)
);

CREATE TABLE IF NOT EXISTS jv_learning_cards (
    card_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL, activity_id TEXT NOT NULL,
    generated_at REAL NOT NULL, generated_by TEXT,
    comparables TEXT NOT NULL, stats TEXT, implication TEXT, basis TEXT
);

CREATE TABLE IF NOT EXISTS jv_sync_state (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS jv_sync_events (
    sync_id TEXT PRIMARY KEY,
    direction TEXT NOT NULL,                         -- export | import
    device_id TEXT, peer_device TEXT,
    bundle_path TEXT, bundle_hash TEXT,
    events_total INTEGER, events_new INTEGER, duplicates INTEGER, conflicts INTEGER,
    status TEXT NOT NULL, detail TEXT, at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS jv_sync_conflicts (
    conflict_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL, activity_id TEXT, field TEXT NOT NULL,
    event_a TEXT NOT NULL, event_b TEXT NOT NULL,
    value_a TEXT, value_b TEXT,
    device_a TEXT, device_b TEXT, actor_a TEXT, actor_b TEXT,
    captured_a TEXT, captured_b TEXT,
    reason TEXT, recommendation TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    resolution_event_id TEXT, chosen_event TEXT, chosen_value TEXT,
    resolved_by TEXT, resolved_at TEXT,
    created_at REAL NOT NULL,
    UNIQUE (event_a, event_b, field)
);

CREATE TABLE IF NOT EXISTS jv_audit (
    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    at REAL NOT NULL, actor TEXT, action TEXT NOT NULL,
    object_kind TEXT, object_id TEXT, project_id TEXT, detail TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS jv_search USING fts5(
    text, title, key UNINDEXED, tokenize = 'porter unicode61'
);
CREATE TABLE IF NOT EXISTS jv_search_meta (
    key TEXT PRIMARY KEY,
    kind TEXT NOT NULL, ref_id TEXT NOT NULL,
    project_id TEXT, activity_id TEXT,
    occurred_at TEXT, captured_at TEXT,
    source TEXT, authority INTEGER NOT NULL DEFAULT 5,
    origin TEXT, title TEXT, text TEXT
);
CREATE INDEX IF NOT EXISTS jv_sm_project ON jv_search_meta(project_id, captured_at);
CREATE TABLE IF NOT EXISTS jv_vectors (key TEXT PRIMARY KEY, model TEXT, dim INTEGER, vector BLOB);

CREATE TABLE IF NOT EXISTS jv_settings (
    username TEXT NOT NULL, key TEXT NOT NULL, value TEXT,
    PRIMARY KEY (username, key)
);
"""

_lock = threading.RLock()
_initialised: set = set()
_path_override: ContextVar[Optional[Path]] = ContextVar("jarvis_db", default=None)


def db_path() -> Path:
    override = _path_override.get()
    if override is not None:
        return override
    from core.paths import data_dir
    return data_dir() / DB_NAME


def evidence_root() -> Path:
    """Content-addressed copies of every captured file, beside the database."""
    return db_path().parent / "jarvis_evidence"


@contextmanager
def using_database(path: Path) -> Iterator[Path]:
    """Point this thread's ZEUS calls at another database (tests, devices)."""
    token = _path_override.set(Path(path))
    try:
        yield Path(path)
    finally:
        _path_override.reset(token)


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")        # field events must survive a power cut
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init(path: Optional[Path] = None) -> Path:
    path = path or db_path()
    key = str(path)
    with _lock:
        if key in _initialised and path.exists():
            return path
        conn = _connect(path)
        try:
            conn.executescript(_SCHEMA)
            conn.execute("INSERT OR IGNORE INTO jv_meta(key, value) VALUES ('schema_version', ?)",
                         (str(SCHEMA_VERSION),))
            conn.execute("INSERT OR IGNORE INTO jv_meta(key, value) VALUES ('created_at', ?)",
                         (str(time.time()),))
            conn.commit()
        finally:
            conn.close()
        _initialised.add(key)
    return path


@contextmanager
def connection(path: Optional[Path] = None) -> Iterator[sqlite3.Connection]:
    path = init(path)
    conn = _connect(path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def reset_cache() -> None:
    with _lock:
        _initialised.clear()


# ------------------------------------------------------------------ helpers --

def query(sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
    with connection() as conn:
        return [dict(row) for row in conn.execute(sql, tuple(params)).fetchall()]


def one(sql: str, params: Sequence[Any] = ()) -> Optional[Dict[str, Any]]:
    with connection() as conn:
        row = conn.execute(sql, tuple(params)).fetchone()
        return dict(row) if row else None


def execute(sql: str, params: Sequence[Any] = ()) -> int:
    with connection() as conn:
        return conn.execute(sql, tuple(params)).rowcount


def insert(conn: sqlite3.Connection, table: str, row: Dict[str, Any], or_ignore: bool = False) -> None:
    columns = list(row)
    verb = "INSERT OR IGNORE" if or_ignore else "INSERT"
    conn.execute(f"{verb} INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
                 tuple(row[c] for c in columns))


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)


def loads(value: Optional[str], default: Any = None) -> Any:
    if value is None or value == "":
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def now() -> float:
    return time.time()


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def current_user() -> str:
    try:
        from security.policy import current_identity
        return current_identity().username or ""
    except Exception:
        return ""


def audit(action: str, object_kind: str = "", object_id: str = "", project_id: str = "",
          detail: Optional[Dict[str, Any]] = None, conn: Optional[sqlite3.Connection] = None) -> None:
    """Record a ZEUS action in its own table AND in ZEUS's HMAC audit chain."""
    actor = current_user()
    row = {"at": now(), "actor": actor, "action": action, "object_kind": object_kind,
           "object_id": object_id, "project_id": project_id, "detail": dumps(detail or {})}
    if conn is not None:
        insert(conn, "jv_audit", row)
    else:
        with connection() as own:
            insert(own, "jv_audit", row)
    try:
        from security.audit_chain import audit_chain
        audit_chain.append("jarvis", f"{action} {object_kind} {object_id}".strip(), actor=actor,
                           project=project_id or "")
    except Exception:
        pass


def fts_query(text: str, max_terms: int = 24) -> str:
    """A safe FTS5 MATCH expression - the knowledge fabric's rule, reused."""
    from knowledge_fabric.store import fts_query as kf_fts_query
    return kf_fts_query(text, max_terms=max_terms)


def get_setting(key: str, default: str = "") -> str:
    row = one("SELECT value FROM jv_settings WHERE username = ? AND key = ?", (current_user() or "_", key))
    return row["value"] if row and row["value"] is not None else default


def set_setting(key: str, value: str) -> None:
    with connection() as conn:
        conn.execute("INSERT INTO jv_settings (username, key, value) VALUES (?, ?, ?) "
                     "ON CONFLICT(username, key) DO UPDATE SET value = excluded.value",
                     (current_user() or "_", key, value))


def integrity() -> Dict[str, Any]:
    """PRAGMA integrity_check + foreign keys - DATABASE_CHECK.bat."""
    with connection() as conn:
        check = [row[0] for row in conn.execute("PRAGMA integrity_check").fetchall()]
        fk = [dict(row) for row in conn.execute("PRAGMA foreign_key_check").fetchall()]
        tables = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') AND name LIKE 'jv_%' ORDER BY name")]
        counts = {}
        for table in tables:
            if table.startswith("jv_search") and table not in ("jv_search_meta",):
                continue
            try:
                counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except sqlite3.DatabaseError:
                counts[table] = None
    return {"ok": check == ["ok"] and not fk, "integrity_check": check, "foreign_key_violations": fk[:20],
            "tables": counts, "path": str(db_path())}
