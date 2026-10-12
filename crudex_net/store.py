"""
The ZEUS NETWORK client's local database: <data_dir>/crudex_net.db.

Everything the device must not lose across a restart or a dropped connection
is written here BEFORE it is acknowledged or reported:

    outbox      events waiting to be sent (pending -> sent -> delivered | failed)
    received    every delivery ever acknowledged, by (sender, id) - the dedupe
                that makes a re-delivery after a lost ack harmless
    messages    the conversation with each device, both directions
    tasks       remote tasks, both directions, with every state change
    transfers   file transfers, both directions, with the byte offset reached
    items       shared items received (memory, knowledge, preferences, ...)
    devices     the last device list the server sent (never shown as live
                presence while this device is not connected)
    folders / folder_files   bidirectional folder sync state
    settings    mode, device identity (public part), sharing policy, flags

No secret is ever stored here: the API key and the device private key live in
Windows Credential Manager (crudex_net/keystore.py).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS outbox (
    msg_id TEXT PRIMARY KEY, to_device TEXT NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending', seq INTEGER, error TEXT, attempts INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL, sent_at REAL, delivered_at REAL);
CREATE INDEX IF NOT EXISTS outbox_state ON outbox (state, created_at);
CREATE TABLE IF NOT EXISTS received (
    from_device TEXT NOT NULL, msg_id TEXT NOT NULL, kind TEXT NOT NULL, seq INTEGER,
    received_at REAL NOT NULL, PRIMARY KEY (from_device, msg_id));
CREATE TABLE IF NOT EXISTS messages (
    msg_id TEXT PRIMARY KEY, peer TEXT NOT NULL, direction TEXT NOT NULL, kind TEXT NOT NULL,
    text TEXT NOT NULL, created_at REAL NOT NULL, state TEXT NOT NULL, read_at REAL);
CREATE INDEX IF NOT EXISTS messages_peer ON messages (peer, created_at);
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY, peer TEXT NOT NULL, direction TEXT NOT NULL, task_type TEXT NOT NULL,
    params TEXT NOT NULL, state TEXT NOT NULL, result TEXT, error TEXT, created_at REAL NOT NULL,
    updated_at REAL NOT NULL, history TEXT NOT NULL DEFAULT '[]', cancel_requested INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS transfers (
    transfer_id TEXT PRIMARY KEY, peer TEXT NOT NULL, direction TEXT NOT NULL, name TEXT NOT NULL,
    source_path TEXT, part_path TEXT, final_path TEXT, size INTEGER NOT NULL, sha256 TEXT NOT NULL,
    mime TEXT, file_kind TEXT NOT NULL, meta TEXT NOT NULL DEFAULT '{}', state TEXT NOT NULL,
    bytes_done INTEGER NOT NULL DEFAULT 0, error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
    conflict INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS items (
    item_id TEXT PRIMARY KEY, peer TEXT NOT NULL, kind TEXT NOT NULL, item_type TEXT NOT NULL,
    title TEXT, body TEXT NOT NULL, received_at REAL NOT NULL, imported_at REAL, import_result TEXT);
CREATE TABLE IF NOT EXISTS devices (
    device_id TEXT PRIMARY KEY, record TEXT NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS folders (
    folder_key TEXT NOT NULL, peer TEXT NOT NULL, local_path TEXT, name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1, state TEXT NOT NULL DEFAULT 'active', created_at REAL NOT NULL,
    last_sync_at REAL, last_error TEXT, PRIMARY KEY (folder_key, peer));
CREATE TABLE IF NOT EXISTS folder_files (
    folder_key TEXT NOT NULL, rel_path TEXT NOT NULL, size INTEGER, mtime REAL, sha256 TEXT,
    synced_sha256 TEXT, synced_at REAL, PRIMARY KEY (folder_key, rel_path));
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, action TEXT NOT NULL, detail TEXT);
"""

_lock = threading.RLock()
_initialised: Dict[str, bool] = {}


def db_path() -> Path:
    from core.paths import data_dir
    return data_dir() / "crudex_net.db"


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            if not _initialised.get(str(path)):
                conn.execute("PRAGMA journal_mode=WAL")
                conn.executescript(SCHEMA)
                _initialised[str(path)] = True
            yield conn
        finally:
            conn.close()


def q(sql: str, params=()) -> List[Dict[str, Any]]:
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def one(sql: str, params=()) -> Optional[Dict[str, Any]]:
    rows = q(sql, params)
    return rows[0] if rows else None


def x(sql: str, params=()) -> int:
    with connect() as conn:
        return conn.execute(sql, params).rowcount


def get(key: str, default: Any = None) -> Any:
    row = one("SELECT value FROM settings WHERE key = ?", (key,))
    if row is None:
        return default
    try:
        return json.loads(row["value"])
    except ValueError:
        return row["value"]


def put(key: str, value: Any) -> None:
    x("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
      (key, json.dumps(value)))


def audit(action: str, detail: Any = "") -> None:
    """Local record + the ZEUS HMAC audit chain. Never pass a secret here."""
    text = detail if isinstance(detail, str) else json.dumps(detail, default=str)
    x("INSERT INTO audit (ts, action, detail) VALUES (?, ?, ?)", (time.time(), action, text[:2000]))
    try:
        from security.audit_chain import audit_chain
        audit_chain.append(f"crudex_{action}", text[:500])
    except Exception:
        pass


def loads(text: Optional[str], default: Any = None) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except ValueError:
        return default
