"""
Artifact registry: every file ZEUS generates, with its provenance.

Files used to be just files in ~/Documents/ZEUS_Documents - nothing recorded
who asked for them, which agent and model made them, what they were built
from, or whether they were checked. Each generated file is now registered
here with:

    name, kind, path, size, SHA-256
    created_at, creator (the signed-in operator), agent, model, tool
    the request that produced it, the source files it was built from
    whether it passed verification when it was written

`verify(path)` re-checks a file later: it still exists, still opens, and its
hash still matches what was recorded - so a document that was edited or
replaced after generation shows as changed rather than as verified.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Dict, List, Optional

from core.logger import get_logger

logger = get_logger("artifacts")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS artifacts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    path          TEXT NOT NULL,
    name          TEXT NOT NULL,
    kind          TEXT,
    size_bytes    INTEGER,
    sha256        TEXT,
    created_at    REAL NOT NULL,
    creator       TEXT,
    agent         TEXT,
    model         TEXT,
    tool          TEXT,
    request       TEXT,
    sources       TEXT,
    verified      INTEGER NOT NULL DEFAULT 0,
    user_id       INTEGER
);
CREATE INDEX IF NOT EXISTS idx_artifacts_created ON artifacts(created_at DESC);
"""


def _connection():
    from memory import database
    conn = database.get_connection()
    conn.executescript(_SCHEMA)
    return conn


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def register_artifact(path: str, kind: str = "", creator_agent: str = "", request: str = "",
                      model: str = "", tool: str = "", sources: Optional[List[str]] = None,
                      verified: bool = False) -> Optional[int]:
    target = Path(path)
    if not target.is_file():
        return None
    try:
        from auth.session_manager import session_manager
        from memory import database
        creator = session_manager.username or ""
        user_id = database.get_active_user()
    except Exception:
        creator, user_id = "", None
    conn = _connection()
    try:
        # The same file registered twice in quick succession (office.finish,
        # then the agent that called it adding its own provenance) is ONE
        # artifact: the later call fills in what the earlier one did not know.
        recent = conn.execute("SELECT id FROM artifacts WHERE path = ? AND created_at > ? "
                              "ORDER BY id DESC LIMIT 1", (str(target), time.time() - 300)).fetchone()
        if recent:
            conn.execute("UPDATE artifacts SET agent = COALESCE(NULLIF(?, ''), agent), "
                         "model = COALESCE(NULLIF(?, ''), model), tool = COALESCE(NULLIF(?, ''), tool), "
                         "request = COALESCE(NULLIF(?, ''), request), "
                         "sources = CASE WHEN ? != '[]' THEN ? ELSE sources END, "
                         "verified = MAX(verified, ?), sha256 = ?, size_bytes = ? WHERE id = ?",
                         (creator_agent, model, tool, (request or "")[:500],
                          json.dumps(sources or []), json.dumps(sources or []),
                          1 if verified else 0, _sha256(target), target.stat().st_size, recent["id"]))
            conn.commit()
            return recent["id"]
        cursor = conn.execute(
            "INSERT INTO artifacts (path, name, kind, size_bytes, sha256, created_at, creator, "
            "agent, model, tool, request, sources, verified, user_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (str(target), target.name, kind or target.suffix.lstrip("."), target.stat().st_size,
             _sha256(target), time.time(), creator, creator_agent, model, tool,
             (request or "")[:500], json.dumps(sources or []), 1 if verified else 0, user_id))
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def list_artifacts(limit: int = 50) -> List[Dict]:
    from memory import database
    conn = _connection()
    try:
        query = "SELECT * FROM artifacts"
        params: list = []
        user = database.get_active_user()
        if user is not None:
            query += " WHERE user_id = ? OR user_id IS NULL"
            params.append(user)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(int(limit))
        rows = [dict(r) for r in conn.execute(query, params).fetchall()]
    finally:
        conn.close()
    for row in rows:
        row["exists"] = Path(row["path"]).is_file()
        try:
            row["sources"] = json.loads(row.get("sources") or "[]")
        except ValueError:
            row["sources"] = []
        row["analysis_id"] = analysis_of(row)
        row["format"] = Path(row["path"]).suffix.lstrip(".").upper()
    return rows


def get_artifact(artifact_id: int) -> Optional[Dict]:
    """One registered artifact of the signed-in account (or of no account)."""
    from memory import database
    conn = _connection()
    try:
        row = conn.execute("SELECT * FROM artifacts WHERE id = ?", (int(artifact_id),)).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    row = dict(row)
    user = database.get_active_user()
    if user is not None and row.get("user_id") not in (None, user):
        return None                                   # another account's file is not listed, not even by id
    row["exists"] = Path(row["path"]).is_file()
    return row


def forget_record(artifact_id: int) -> bool:
    """Remove an artifact from the list. The FILE is not touched."""
    if get_artifact(artifact_id) is None:
        return False
    conn = _connection()
    try:
        conn.execute("DELETE FROM artifacts WHERE id = ?", (int(artifact_id),))
        conn.commit()
    finally:
        conn.close()
    return True


def analysis_of(row: Dict) -> Optional[str]:
    """The analysis a file was rendered from ('analysis:<id>' in its sources)."""
    for source in row.get("sources") or []:
        if isinstance(source, str) and source.startswith("analysis:"):
            return source.split(":", 1)[1]
    return None


def verify(path: str) -> Dict:
    """Does the file still exist, still open, and still match its recorded hash?"""
    target = Path(path)
    conn = _connection()
    try:
        row = conn.execute("SELECT sha256, verified FROM artifacts WHERE path = ? "
                           "ORDER BY id DESC LIMIT 1", (str(target),)).fetchone()
    finally:
        conn.close()
    result = {"path": str(target), "exists": target.is_file(), "registered": bool(row)}
    if not target.is_file():
        result.update(ok=False, detail="the file no longer exists")
        return result
    try:
        from core.office import verify_office_file
        check = verify_office_file(target, target.suffix)
        result["opens"] = bool(check.get("ok"))
    except Exception as error:
        result["opens"] = False
        result["open_error"] = str(error)
    if row:
        current = _sha256(target)
        result["unchanged"] = current == row["sha256"]
        result["verified_at_creation"] = bool(row["verified"])
    result["ok"] = bool(result.get("opens")) and result.get("unchanged", True)
    result["detail"] = ("opens and matches the recorded hash" if result["ok"] else
                        "changed since it was generated" if row and not result.get("unchanged")
                        else "does not open cleanly")
    return result
