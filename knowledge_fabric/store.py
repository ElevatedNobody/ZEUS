"""
The knowledge fabric's own SQLite database.

One file, `<data_dir>/knowledge_fabric.db`, beside medha.db. Schema in
docs/RAG_DATA_MODEL.md. Every read that returns document content goes through
`access_clause()` so the security filter is part of the SQL that finds the
rows - nothing unauthorised is fetched and then hidden.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kf_meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS kf_documents (
    document_id TEXT PRIMARY KEY,
    source_path TEXT, filename TEXT, title TEXT,
    document_type TEXT,
    doc_date TEXT, year INTEGER, quarter INTEGER, month INTEGER, fiscal_year TEXT,
    time_start INTEGER, time_end INTEGER,
    department TEXT, business_unit TEXT, location TEXT,
    confidentiality TEXT NOT NULL DEFAULT 'internal',
    department_restricted INTEGER NOT NULL DEFAULT 0,
    vault_linked INTEGER NOT NULL DEFAULT 0,
    author TEXT, revision TEXT, version TEXT,
    status TEXT,
    authority INTEGER NOT NULL DEFAULT 5,
    origin TEXT NOT NULL DEFAULT 'company',
    source_hash TEXT, page_count INTEGER, language TEXT,
    supersedes TEXT, superseded_by TEXT,
    metadata TEXT,
    ingested_at REAL, updated_at REAL
);
CREATE INDEX IF NOT EXISTS kf_doc_year ON kf_documents(year);
CREATE INDEX IF NOT EXISTS kf_doc_type ON kf_documents(document_type);
CREATE INDEX IF NOT EXISTS kf_doc_hash ON kf_documents(source_hash);
CREATE INDEX IF NOT EXISTS kf_doc_path ON kf_documents(source_path);

CREATE TABLE IF NOT EXISTS kf_nodes (
    node_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL,
    parent_id TEXT,
    kind TEXT NOT NULL,
    ordinal INTEGER NOT NULL DEFAULT 0,
    depth INTEGER NOT NULL DEFAULT 0,
    page INTEGER,
    section_path TEXT,
    heading TEXT,
    text TEXT,
    text_hash TEXT,
    date TEXT, year INTEGER,
    topics TEXT,
    element_type TEXT, extraction_method TEXT, ocr_confidence REAL,
    is_inference INTEGER NOT NULL DEFAULT 0,
    source_location TEXT
);
CREATE INDEX IF NOT EXISTS kf_node_doc ON kf_nodes(document_id);
CREATE INDEX IF NOT EXISTS kf_node_parent ON kf_nodes(parent_id);
CREATE INDEX IF NOT EXISTS kf_node_year ON kf_nodes(year);

CREATE VIRTUAL TABLE IF NOT EXISTS kf_fts USING fts5(
    text, heading, title, node_id UNINDEXED, document_id UNINDEXED,
    tokenize = 'porter unicode61'
);

CREATE TABLE IF NOT EXISTS kf_vectors (
    node_id TEXT PRIMARY KEY, model TEXT, dim INTEGER, vector BLOB
);

CREATE TABLE IF NOT EXISTS kf_tables (
    table_id TEXT PRIMARY KEY, document_id TEXT NOT NULL, node_id TEXT,
    page INTEGER, title TEXT, headers TEXT, units TEXT,
    n_rows INTEGER, n_cols INTEGER, orientation TEXT, notes TEXT
);
CREATE INDEX IF NOT EXISTS kf_table_doc ON kf_tables(document_id);

CREATE TABLE IF NOT EXISTS kf_cells (
    table_id TEXT NOT NULL, row_index INTEGER NOT NULL, col_index INTEGER NOT NULL,
    header TEXT, row_label TEXT, value_text TEXT, value_num REAL, unit TEXT,
    PRIMARY KEY (table_id, row_index, col_index)
);

CREATE TABLE IF NOT EXISTS kf_registers (
    register_id TEXT PRIMARY KEY, document_id TEXT NOT NULL, table_node TEXT, name TEXT,
    key_column TEXT, columns TEXT, n_records INTEGER
);
CREATE INDEX IF NOT EXISTS kf_register_doc ON kf_registers(document_id);

CREATE TABLE IF NOT EXISTS kf_records (
    register_id TEXT NOT NULL, row_index INTEGER NOT NULL, record_key TEXT, node_id TEXT,
    codes TEXT, data TEXT NOT NULL,
    PRIMARY KEY (register_id, row_index)
);
CREATE INDEX IF NOT EXISTS kf_record_key ON kf_records(record_key);

CREATE TABLE IF NOT EXISTS kf_section_tags (
    node_id TEXT PRIMARY KEY, document_id TEXT NOT NULL, year INTEGER,
    document_type TEXT, department TEXT, site TEXT
);
CREATE INDEX IF NOT EXISTS kf_section_tag_doc ON kf_section_tags(document_id);
CREATE INDEX IF NOT EXISTS kf_section_tag_year ON kf_section_tags(year);

CREATE TABLE IF NOT EXISTS kf_facts (
    fact_id INTEGER PRIMARY KEY AUTOINCREMENT,
    metric TEXT NOT NULL, metric_norm TEXT NOT NULL,
    entity_id TEXT, entity_label TEXT,
    period TEXT, year INTEGER, quarter INTEGER, month INTEGER,
    value REAL NOT NULL, unit TEXT,
    document_id TEXT NOT NULL, node_id TEXT, table_id TEXT, cell_ref TEXT,
    source_text TEXT, confidence REAL NOT NULL DEFAULT 1.0
);
CREATE INDEX IF NOT EXISTS kf_fact_metric ON kf_facts(metric_norm, year);
CREATE INDEX IF NOT EXISTS kf_fact_doc ON kf_facts(document_id);
CREATE INDEX IF NOT EXISTS kf_fact_entity ON kf_facts(entity_id);

CREATE TABLE IF NOT EXISTS kf_entities (
    entity_id TEXT PRIMARY KEY, type TEXT NOT NULL, canonical_name TEXT NOT NULL,
    attributes TEXT, status TEXT NOT NULL DEFAULT 'active', created_at REAL
);
CREATE INDEX IF NOT EXISTS kf_entity_type ON kf_entities(type);

CREATE TABLE IF NOT EXISTS kf_aliases (
    alias_norm TEXT NOT NULL, alias TEXT NOT NULL, entity_id TEXT NOT NULL,
    confidence REAL NOT NULL, source TEXT NOT NULL, evidence_node TEXT,
    PRIMARY KEY (alias_norm, entity_id)
);

CREATE TABLE IF NOT EXISTS kf_mentions (
    node_id TEXT NOT NULL, entity_id TEXT NOT NULL, confidence REAL NOT NULL DEFAULT 1.0,
    surface TEXT,
    PRIMARY KEY (node_id, entity_id)
);
CREATE INDEX IF NOT EXISTS kf_mention_entity ON kf_mentions(entity_id);

CREATE TABLE IF NOT EXISTS kf_relations (
    rel_id INTEGER PRIMARY KEY AUTOINCREMENT,
    src TEXT NOT NULL, relation TEXT NOT NULL, dst TEXT NOT NULL,
    evidence_node TEXT, document_id TEXT, confidence REAL NOT NULL DEFAULT 0.5,
    valid_from TEXT, valid_to TEXT, source TEXT NOT NULL DEFAULT 'pattern'
);
CREATE INDEX IF NOT EXISTS kf_rel_src ON kf_relations(src);
CREATE INDEX IF NOT EXISTS kf_rel_dst ON kf_relations(dst);

CREATE TABLE IF NOT EXISTS kf_corrections (
    correction_id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL, payload TEXT NOT NULL, username TEXT, at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS kf_analyses (
    analysis_id TEXT PRIMARY KEY, username TEXT, question TEXT,
    created_at REAL, verified INTEGER NOT NULL DEFAULT 0, payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kf_user_departments (
    username TEXT NOT NULL, department TEXT NOT NULL,
    PRIMARY KEY (username, department)
);

CREATE TABLE IF NOT EXISTS kf_ingestion_log (
    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT, source_path TEXT, status TEXT, document_id TEXT,
    detail TEXT, at REAL
);
"""

_lock = threading.RLock()
_initialised: set = set()

# The RAG evaluation runs on its own temporary database. The switch is
# context-local (per thread), so an evaluation started from the running app
# never redirects anybody else's queries away from the real knowledge base.
_path_override: ContextVar[Optional[Path]] = ContextVar("knowledge_fabric_db", default=None)


def db_path() -> Path:
    override = _path_override.get()
    if override is not None:
        return override
    from core.paths import data_dir
    return data_dir() / "knowledge_fabric.db"


@contextmanager
def using_database(path: Path) -> Iterator[Path]:
    """Point this thread's knowledge-fabric calls at another database file."""
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
    conn.execute("PRAGMA synchronous=NORMAL")
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
            conn.execute("INSERT OR IGNORE INTO kf_meta(key, value) VALUES ('schema_version', ?)",
                         (str(SCHEMA_VERSION),))
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
    """Tests switch data directories; forget which paths were initialised."""
    with _lock:
        _initialised.clear()


# ------------------------------------------------------------------ helpers --

def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def loads(value: Optional[str], default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def rows(cursor: Iterable[sqlite3.Row]) -> List[Dict[str, Any]]:
    return [dict(row) for row in cursor]


def placeholders(values: Sequence[Any]) -> str:
    return ",".join("?" for _ in values)


def now() -> float:
    return time.time()


def fts_query(text: str, max_terms: int = 24) -> str:
    """A safe FTS5 MATCH expression: bare word tokens OR-ed together.

    User text is never passed to MATCH directly - quotes, colons, NEAR and
    column filters are syntax there, and a stray one is an error (or a
    column-restriction trick). Tokens are quoted individually."""
    import re
    tokens = []
    for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9\-]{1,}", text.lower()):
        token = token.strip("-")
        if len(token) < 2 or token in _STOPWORDS:
            continue
        if token not in tokens:
            tokens.append(token)
        if len(tokens) >= max_terms:
            break
    return " OR ".join(f'"{token}"' for token in tokens)


_STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "by", "with", "at", "from",
    "is", "are", "was", "were", "be", "been", "it", "its", "this", "that", "these", "those",
    "what", "which", "who", "whom", "why", "how", "when", "where", "do", "does", "did", "we",
    "our", "us", "i", "me", "my", "you", "your", "they", "their", "them", "all", "any", "can",
    "could", "should", "would", "will", "shall", "about", "tell", "give", "show", "find", "please",
    "between", "after", "before", "since", "until", "during", "than", "then", "there", "have",
    "has", "had", "not", "no", "so", "as", "into", "over", "under", "also", "just", "get",
}
