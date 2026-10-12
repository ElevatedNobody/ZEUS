"""
The ZEUS server's database (SQLite, one file in the server's data folder).

Secrets are never stored: an API key is kept as its SHA-256 hash plus its last
four characters (for the masked display), and a device is known by its PUBLIC
key only. Everything that changes access - registration, pairing, grants,
revocation, key issue - is written to the audit table.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from crudex_server import protocol as P

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    account_id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at REAL NOT NULL,
    disabled INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS api_keys (
    key_id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(account_id),
    key_hash TEXT NOT NULL UNIQUE, last4 TEXT NOT NULL, label TEXT, created_at REAL NOT NULL,
    revoked_at REAL);
CREATE TABLE IF NOT EXISTS devices (
    device_id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(account_id),
    name TEXT NOT NULL, platform TEXT, version TEXT, public_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active', registered_at REAL NOT NULL, last_seen REAL,
    revoked_at REAL, revoked_by TEXT);
CREATE TABLE IF NOT EXISTS pairings (
    a TEXT NOT NULL, b TEXT NOT NULL, account_id TEXT NOT NULL, created_at REAL NOT NULL,
    created_by TEXT, active INTEGER NOT NULL DEFAULT 1, ended_at REAL, ended_by TEXT,
    PRIMARY KEY (a, b), CHECK (a < b));
CREATE TABLE IF NOT EXISTS grants (
    granter TEXT NOT NULL, grantee TEXT NOT NULL, permission TEXT NOT NULL,
    PRIMARY KEY (granter, grantee, permission));
CREATE TABLE IF NOT EXISTS pairing_codes (
    code TEXT NOT NULL, account_id TEXT NOT NULL, device_id TEXT NOT NULL,
    created_at REAL NOT NULL, expires_at REAL NOT NULL, used_at REAL, used_by TEXT,
    PRIMARY KEY (account_id, code));
CREATE TABLE IF NOT EXISTS queue (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL, from_device TEXT,
    to_device TEXT NOT NULL, msg_id TEXT NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL,
    created_at REAL NOT NULL, delivered_at REAL, state TEXT NOT NULL DEFAULT 'queued',
    UNIQUE (from_device, to_device, msg_id));
CREATE INDEX IF NOT EXISTS queue_pending ON queue (to_device, state, seq);
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY, from_device TEXT NOT NULL, to_device TEXT NOT NULL,
    task_type TEXT, state TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS transfers (
    transfer_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, from_device TEXT NOT NULL,
    to_device TEXT NOT NULL, name TEXT NOT NULL, size INTEGER NOT NULL, sha256 TEXT NOT NULL,
    mime TEXT, kind TEXT NOT NULL, meta TEXT, received INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL, created_at REAL NOT NULL, completed_at REAL, delivered_at REAL);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, account_id TEXT, device_id TEXT,
    action TEXT NOT NULL, detail TEXT, address TEXT);
"""


def _now() -> float:
    return time.time()


def hash_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def new_api_key() -> str:
    """crx_ + 40 url-safe characters (240 random bits)."""
    return "crx_" + base64.urlsafe_b64encode(secrets.token_bytes(30)).decode("ascii").rstrip("=")


def pair_key(a: str, b: str) -> Tuple[str, str]:
    return (a, b) if a < b else (b, a)


class ServerStore:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.blob_dir = self.data_dir / "blobs"
        self.blob_dir.mkdir(exist_ok=True)
        self.path = self.data_dir / "crudex_server.db"
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    # ------------------------------------------------------------ helpers ----
    def q(self, sql: str, params=()) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def one(self, sql: str, params=()) -> Optional[Dict[str, Any]]:
        rows = self.q(sql, params)
        return rows[0] if rows else None

    def x(self, sql: str, params=()) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.execute(sql, params)

    def audit(self, action: str, account_id: str = None, device_id: str = None, detail: Any = "",
              address: str = "") -> None:
        text = detail if isinstance(detail, str) else json.dumps(detail, default=str)
        self.x("INSERT INTO audit (ts, account_id, device_id, action, detail, address) VALUES (?,?,?,?,?,?)",
               (_now(), account_id, device_id, action, text[:2000], address))

    # ------------------------------------------------------------ accounts ----
    def create_account(self, name: str, label: str = "first key") -> Tuple[str, str]:
        account_id = "acc_" + uuid.uuid4().hex[:16]
        self.x("INSERT INTO accounts (account_id, name, created_at) VALUES (?,?,?)", (account_id, name.strip(), _now()))
        key = self.issue_key(account_id, label)
        self.audit("account_created", account_id, detail=name)
        return account_id, key

    def issue_key(self, account_id: str, label: str = "") -> str:
        if not self.one("SELECT 1 FROM accounts WHERE account_id = ?", (account_id,)):
            raise ValueError(f"no account {account_id}")
        key = new_api_key()
        key_id = "key_" + uuid.uuid4().hex[:12]
        self.x("INSERT INTO api_keys (key_id, account_id, key_hash, last4, label, created_at) VALUES (?,?,?,?,?,?)",
               (key_id, account_id, hash_key(key), key[-4:], label, _now()))
        self.audit("api_key_issued", account_id, detail={"key_id": key_id, "last4": key[-4:], "label": label})
        return key

    def revoke_key(self, key_id: str) -> bool:
        row = self.one("SELECT account_id FROM api_keys WHERE key_id = ? AND revoked_at IS NULL", (key_id,))
        if not row:
            return False
        self.x("UPDATE api_keys SET revoked_at = ? WHERE key_id = ?", (_now(), key_id))
        self.audit("api_key_revoked", row["account_id"], detail=key_id)
        return True

    def account_for_key(self, api_key: str) -> Optional[Dict[str, Any]]:
        if not isinstance(api_key, str) or not api_key.startswith("crx_") or len(api_key) > 200:
            return None
        return self.one("SELECT a.account_id, a.name, k.key_id, k.last4 FROM api_keys k "
                        "JOIN accounts a ON a.account_id = k.account_id "
                        "WHERE k.key_hash = ? AND k.revoked_at IS NULL AND a.disabled = 0", (hash_key(api_key),))

    # ------------------------------------------------------------- devices ----
    def device(self, device_id: str) -> Optional[Dict[str, Any]]:
        return self.one("SELECT * FROM devices WHERE device_id = ?", (device_id,))

    def register_device(self, account_id: str, identity: Dict[str, Any]) -> Dict[str, Any]:
        now = _now()
        self.x("INSERT INTO devices (device_id, account_id, name, platform, version, public_key, registered_at, "
               "last_seen) VALUES (?,?,?,?,?,?,?,?)",
               (identity["device_id"], account_id, identity["name"][:80], identity.get("platform", "")[:80],
                identity.get("version", "")[:40], identity["public_key"], now, now))
        self.audit("device_registered", account_id, identity["device_id"],
                   {"name": identity["name"][:80], "platform": identity.get("platform")})
        return self.device(identity["device_id"])

    def touch_device(self, device_id: str, identity: Dict[str, Any] = None) -> None:
        if identity:
            self.x("UPDATE devices SET last_seen = ?, name = ?, platform = ?, version = ? WHERE device_id = ?",
                   (_now(), identity["name"][:80], identity.get("platform", "")[:80],
                    identity.get("version", "")[:40], device_id))
        else:
            self.x("UPDATE devices SET last_seen = ? WHERE device_id = ?", (_now(), device_id))

    def account_devices(self, account_id: str) -> List[Dict[str, Any]]:
        return self.q("SELECT device_id, name, platform, version, status, registered_at, last_seen, revoked_at "
                      "FROM devices WHERE account_id = ? ORDER BY registered_at", (account_id,))

    def revoke_device(self, device_id: str, by: str) -> List[str]:
        """Revoke a device: it can no longer authenticate, every pairing ends, and
        nothing queued for it or from it is delivered. Returns the peers it had."""
        row = self.device(device_id)
        if not row or row["status"] == "revoked":
            return []
        now = _now()
        peers = self.peers(device_id)
        with self._lock:
            self.conn.execute("BEGIN")
            try:
                self.conn.execute("UPDATE devices SET status = 'revoked', revoked_at = ?, revoked_by = ? "
                                  "WHERE device_id = ?", (now, by, device_id))
                self.conn.execute("UPDATE pairings SET active = 0, ended_at = ?, ended_by = ? "
                                  "WHERE (a = ? OR b = ?) AND active = 1", (now, by, device_id, device_id))
                self.conn.execute("DELETE FROM grants WHERE granter = ? OR grantee = ?", (device_id, device_id))
                self.conn.execute("UPDATE queue SET state = 'purged' WHERE state = 'queued' AND "
                                  "(to_device = ? OR from_device = ?)", (device_id, device_id))
                self.conn.execute("UPDATE pairing_codes SET used_at = ?, used_by = 'revoked' "
                                  "WHERE device_id = ? AND used_at IS NULL", (now, device_id))
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        for transfer in self.q("SELECT transfer_id FROM transfers WHERE (from_device = ? OR to_device = ?) "
                               "AND state IN ('uploading', 'stored')", (device_id, device_id)):
            self.cancel_transfer(transfer["transfer_id"], "device revoked")
        self.audit("device_revoked", row["account_id"], device_id, {"by": by, "peers": peers})
        return peers

    # ------------------------------------------------------------ pairings ----
    def pairing(self, a: str, b: str) -> Optional[Dict[str, Any]]:
        x, y = pair_key(a, b)
        return self.one("SELECT * FROM pairings WHERE a = ? AND b = ? AND active = 1", (x, y))

    def peers(self, device_id: str) -> List[str]:
        rows = self.q("SELECT a, b FROM pairings WHERE (a = ? OR b = ?) AND active = 1", (device_id, device_id))
        return [r["b"] if r["a"] == device_id else r["a"] for r in rows]

    def create_code(self, account_id: str, device_id: str) -> Dict[str, Any]:
        now = _now()
        self.x("UPDATE pairing_codes SET used_at = ?, used_by = 'superseded' WHERE device_id = ? AND used_at IS NULL",
               (now, device_id))
        for _ in range(20):
            code = f"{secrets.randbelow(1_000_000):06d}"
            if not self.one("SELECT 1 FROM pairing_codes WHERE account_id = ? AND code = ? AND used_at IS NULL "
                            "AND expires_at > ?", (account_id, code, now)):
                break
        self.x("INSERT OR REPLACE INTO pairing_codes (code, account_id, device_id, created_at, expires_at) "
               "VALUES (?,?,?,?,?)", (code, account_id, device_id, now, now + P.PAIRING_CODE_TTL))
        self.audit("pairing_code_created", account_id, device_id)
        return {"code": code, "expires_at": now + P.PAIRING_CODE_TTL}

    def redeem_code(self, account_id: str, device_id: str, code: str) -> Optional[str]:
        """The device that created the code, if the code is valid; the code is spent."""
        now = _now()
        row = self.one("SELECT * FROM pairing_codes WHERE account_id = ? AND code = ? AND used_at IS NULL "
                       "AND expires_at > ?", (account_id, str(code).strip(), now))
        if not row or row["device_id"] == device_id:
            return None
        self.x("UPDATE pairing_codes SET used_at = ?, used_by = ? WHERE account_id = ? AND code = ?",
               (now, device_id, account_id, row["code"]))
        return row["device_id"]

    def create_pairing(self, account_id: str, a: str, b: str, by: str) -> None:
        x, y = pair_key(a, b)
        now = _now()
        self.x("INSERT INTO pairings (a, b, account_id, created_at, created_by, active) VALUES (?,?,?,?,?,1) "
               "ON CONFLICT (a, b) DO UPDATE SET active = 1, created_at = excluded.created_at, "
               "created_by = excluded.created_by, ended_at = NULL, ended_by = NULL", (x, y, account_id, now, by))
        for granter, grantee in ((a, b), (b, a)):
            self.x("DELETE FROM grants WHERE granter = ? AND grantee = ?", (granter, grantee))
            for permission in P.DEFAULT_GRANTS:
                self.x("INSERT INTO grants (granter, grantee, permission) VALUES (?,?,?)", (granter, grantee, permission))
        self.audit("paired", account_id, by, {"a": x, "b": y})

    def end_pairing(self, a: str, b: str, by: str) -> bool:
        x, y = pair_key(a, b)
        cursor = self.x("UPDATE pairings SET active = 0, ended_at = ?, ended_by = ? WHERE a = ? AND b = ? AND active = 1",
                        (_now(), by, x, y))
        self.x("DELETE FROM grants WHERE (granter = ? AND grantee = ?) OR (granter = ? AND grantee = ?)", (a, b, b, a))
        self.x("UPDATE queue SET state = 'purged' WHERE state = 'queued' AND ((from_device = ? AND to_device = ?) "
               "OR (from_device = ? AND to_device = ?))", (a, b, b, a))
        if cursor.rowcount:
            self.audit("unpaired", None, by, {"a": x, "b": y})
        return bool(cursor.rowcount)

    # -------------------------------------------------------------- grants ----
    def grants(self, granter: str, grantee: str) -> Tuple[str, ...]:
        return tuple(sorted(r["permission"] for r in self.q(
            "SELECT permission FROM grants WHERE granter = ? AND grantee = ?", (granter, grantee))))

    def has(self, granter: str, grantee: str, permission: str) -> bool:
        return bool(self.one("SELECT 1 FROM grants WHERE granter = ? AND grantee = ? AND permission = ?",
                             (granter, grantee, permission)))

    def set_grants(self, granter: str, grantee: str, permissions) -> Tuple[str, ...]:
        wanted = P.valid_grants(permissions)
        with self._lock:
            self.conn.execute("BEGIN")
            self.conn.execute("DELETE FROM grants WHERE granter = ? AND grantee = ?", (granter, grantee))
            for permission in wanted:
                self.conn.execute("INSERT INTO grants (granter, grantee, permission) VALUES (?,?,?)",
                                  (granter, grantee, permission))
            self.conn.execute("COMMIT")
        self.audit("grants_set", None, granter, {"to": grantee, "grants": wanted})
        return wanted

    # --------------------------------------------------------------- queue ----
    def enqueue(self, account_id: str, from_device: Optional[str], to_device: str, msg_id: str, kind: str,
                body: Any) -> Tuple[int, bool]:
        """(seq, created). The same msg_id from the same sender to the same device is stored
        once - a resend after a lost acknowledgement returns the original seq."""
        existing = self.one("SELECT seq FROM queue WHERE from_device IS ? AND to_device = ? AND msg_id = ?",
                            (from_device, to_device, msg_id))
        if existing:
            return existing["seq"], False
        cursor = self.x("INSERT INTO queue (account_id, from_device, to_device, msg_id, kind, body, created_at) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (account_id, from_device, to_device, msg_id, kind, json.dumps(body, default=str), _now()))
        return cursor.lastrowid, True

    def pending(self, device_id: str, after_seq: int = 0, limit: int = 200) -> List[Dict[str, Any]]:
        return self.q("SELECT q.*, d.name AS from_name FROM queue q LEFT JOIN devices d ON d.device_id = q.from_device "
                      "WHERE q.to_device = ? AND q.state = 'queued' AND q.seq > ? ORDER BY q.seq LIMIT ?",
                      (device_id, after_seq, limit))

    def mark_delivered(self, device_id: str, seq: int) -> Optional[Dict[str, Any]]:
        row = self.one("SELECT * FROM queue WHERE seq = ? AND to_device = ?", (seq, device_id))
        if not row or row["state"] != "queued":
            return None
        self.x("UPDATE queue SET state = 'delivered', delivered_at = ? WHERE seq = ?", (_now(), seq))
        return row

    # --------------------------------------------------------------- tasks ----
    def record_task(self, task_id: str, from_device: str, to_device: str, task_type: str) -> None:
        now = _now()
        self.x("INSERT OR IGNORE INTO tasks (task_id, from_device, to_device, task_type, state, created_at, updated_at) "
               "VALUES (?,?,?,?,?,?,?)", (task_id, from_device, to_device, task_type, "PENDING", now, now))

    def task(self, task_id: str) -> Optional[Dict[str, Any]]:
        return self.one("SELECT * FROM tasks WHERE task_id = ?", (task_id,))

    def set_task_state(self, task_id: str, state: str) -> None:
        self.x("UPDATE tasks SET state = ?, updated_at = ? WHERE task_id = ?", (state, _now(), task_id))

    # ----------------------------------------------------------- transfers ----
    def blob_path(self, transfer_id: str) -> Path:
        return self.blob_dir / f"{uuid.UUID(transfer_id).hex}.blob"

    def transfer(self, transfer_id: str) -> Optional[Dict[str, Any]]:
        return self.one("SELECT * FROM transfers WHERE transfer_id = ?", (transfer_id,))

    def create_transfer(self, row: Dict[str, Any]) -> None:
        self.x("INSERT INTO transfers (transfer_id, account_id, from_device, to_device, name, size, sha256, mime, kind, "
               "meta, received, state, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,0,'uploading',?)",
               (row["transfer_id"], row["account_id"], row["from_device"], row["to_device"], row["name"], row["size"],
                row["sha256"], row.get("mime", ""), row["kind"], json.dumps(row.get("meta") or {}), _now()))
        self.audit("transfer_offered", row["account_id"], row["from_device"],
                   {"transfer_id": row["transfer_id"], "to": row["to_device"], "name": row["name"], "size": row["size"]})

    def set_transfer(self, transfer_id: str, **fields) -> None:
        if not fields:
            return
        names = ", ".join(f"{k} = ?" for k in fields)
        self.x(f"UPDATE transfers SET {names} WHERE transfer_id = ?", (*fields.values(), transfer_id))

    def cancel_transfer(self, transfer_id: str, reason: str) -> None:
        self.set_transfer(transfer_id, state="cancelled")
        try:
            self.blob_path(transfer_id).unlink(missing_ok=True)
        except OSError:
            pass
        self.audit("transfer_cancelled", None, None, {"transfer_id": transfer_id, "reason": reason})

    # --------------------------------------------------------- housekeeping ----
    def housekeeping(self, queue_days: int = 30, upload_days: int = 7) -> Dict[str, int]:
        now = _now()
        purged = self.x("UPDATE queue SET state = 'expired' WHERE state = 'queued' AND created_at < ?",
                        (now - queue_days * 86400,)).rowcount
        stale = self.q("SELECT transfer_id FROM transfers WHERE state IN ('uploading', 'stored') AND created_at < ?",
                       (now - upload_days * 86400,))
        for row in stale:
            self.cancel_transfer(row["transfer_id"], f"not delivered within {upload_days} days")
        codes = self.x("DELETE FROM pairing_codes WHERE expires_at < ?", (now - 86400,)).rowcount
        return {"queue_expired": purged, "transfers_expired": len(stale), "codes_deleted": codes}
