"""
ZEUS's long-term memory store, with Hermes as the retrieval engine.

    MemoryService.remember(...)  -> policy check, dedupe/supersede, write
    MemoryService.recall(query)  -> List[MemoryResult], ranked
    MemoryService.forget(...)    -> removal by id or by matching text

Backends (config.ini [memory] backend):
    hermes   the vendored cognis-hermes MemoryStore (third_party/hermes):
             SQLite + TF-IDF/cosine recall with a recency boost. Default.
    native   ZEUS's own store: SQLite FTS5 with BM25 ranking. Shares no code
             with Hermes; used when Hermes is switched off, fails its
             integrity check, or cannot be imported. See the license note in
             third_party/hermes/AGNI_VENDOR.md.

One store FILE per account (database/memory/<backend>_u<id>.sqlite), so one
operator's memories can never be recalled into another's prompts - the
isolation does not depend on a WHERE clause being right.

Hermes' MemoryStore opens its SQLite connection in the constructor with the
default same-thread check, and ZEUS calls memory from Qt worker threads, so a
store is opened per operation and closed after it.
"""

from __future__ import annotations

import configparser
import hashlib
import json
import re
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from core.logger import get_logger
from memory.contracts import CATEGORIES, MemoryResult, USER_MEMORY
from memory.write_policy import WriteDecision, evaluate

logger = get_logger("memory.store")

ROOT = Path(__file__).resolve().parent.parent
HERMES_DIR = ROOT / "third_party" / "hermes"
# SHA-256 of the vendored files (third_party/hermes/AGNI_VENDOR.md). A changed
# copy is refused: this code's output goes into every prompt.
HERMES_SHA256 = {
    "hermes/__init__.py": "02fb347c0e4f01ad68f32ee16714d8b4604b27d89fffb04d6c644e7bf4a2ef80",
    "hermes/memory.py": "a83926c64d41b63fc63a98d566dd0a9e4a18a8549d9a58bcc89dfc4f8d568212",
}

# Recall scores (0..1) at or above which a new memory is the SAME memory as
# an old one (update it) - used when no subject key decides it.
_SAME_MEMORY_SCORE = 0.80


def config() -> Dict:
    parser = configparser.ConfigParser()
    try:
        parser.read(ROOT / "config.ini", encoding="utf-8")
    except configparser.Error:
        pass
    get = lambda key, default: parser.get("memory", key, fallback=default).strip()   # noqa: E731

    def number(key, default, kind=float):
        try:
            return kind(get(key, str(default)))
        except ValueError:
            return default
    backend = get("backend", "hermes").lower()
    return {"backend": backend if backend in ("hermes", "native") else "hermes",
            "min_score": number("min_score", 0.12),
            "max_results": number("max_results", 6, int),
            "context_chars": number("context_chars", 1600, int),
            "on_unavailable": get("on_unavailable", "answer").lower()}


def _store_dir() -> Path:
    from core.paths import data_dir
    folder = data_dir() / "memory"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


# ============================================================== Hermes ====

class HermesUnavailable(RuntimeError):
    pass


_hermes_checked: Optional[str] = None       # None = not checked; "" = ok; else the reason


def _load_hermes():
    """Import the vendored Hermes after verifying its SHA-256."""
    global _hermes_checked
    if _hermes_checked is None:
        problems = []
        for relative, expected in HERMES_SHA256.items():
            path = HERMES_DIR / relative
            if not path.is_file():
                problems.append(f"{relative} missing")
                continue
            # Raw bytes, no normalisation: upstream stores these files with
            # CRLF endings, and the values pinned here are of those bytes
            # (they match the upstream git blob ids - see AGNI_VENDOR.md).
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                problems.append(f"{relative} does not match its vendored SHA-256")
        _hermes_checked = "; ".join(problems)
    if _hermes_checked:
        raise HermesUnavailable(_hermes_checked)
    if str(HERMES_DIR) not in sys.path:
        sys.path.insert(0, str(HERMES_DIR))
    try:
        import hermes                                              # noqa: F401
        from hermes import MemoryStore
    except Exception as error:
        raise HermesUnavailable(f"import failed: {error}")
    return MemoryStore


class HermesBackend:
    name = "hermes"

    def __init__(self, path: Path):
        self.path = path
        self._MemoryStore = _load_hermes()

    def _open(self):
        return self._MemoryStore(str(self.path))

    def _result(self, memory, score: float) -> MemoryResult:
        meta = dict(memory.metadata or {})
        return MemoryResult(
            memory_id=f"hermes:{memory.id}", text=memory.text, score=round(float(score), 4),
            timestamp=float(memory.updated_at or memory.created_at),
            category=meta.pop("category", None) or next(
                (t for t in memory.tags if t in CATEGORIES), USER_MEMORY),
            source=memory.source or "", metadata=meta,
            provenance={"backend": "hermes", "store": self.path.name,
                        "first_recorded": meta.get("first_recorded", memory.created_at),
                        "supersedes": meta.get("supersedes", [])})

    def remember(self, text: str, category: str, source: str, metadata: Dict) -> MemoryResult:
        tags = [category] + ([f"subject:{metadata['subject']}"] if metadata.get("subject") else [])
        with self._open() as store:
            memory = store.remember(text, tags=tags, source=source,
                                    metadata={**metadata, "category": category})
        return self._result(memory, 1.0)

    def recall(self, query: str, limit: int, category: Optional[str] = None,
               min_score: float = 0.0) -> List[MemoryResult]:
        with self._open() as store:
            hits = store.recall(query, limit=limit, tag=category, min_score=min_score)
        return [self._result(hit.memory, hit.score) for hit in hits]

    def list(self, limit: int = 50, category: Optional[str] = None) -> List[MemoryResult]:
        with self._open() as store:
            return [self._result(m, 1.0) for m in store.list(limit=limit, tag=category)]

    def forget(self, memory_id: str) -> bool:
        with self._open() as store:
            return store.forget(int(memory_id.split(":", 1)[1]))

    def count(self) -> int:
        with self._open() as store:
            return store.count()


# ============================================================== native ====

_NATIVE_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL, category TEXT NOT NULL,
    source TEXT, metadata TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL);
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(text, content='memories', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, text) VALUES (new.id, new.text); END;
CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, text) VALUES ('delete', old.id, old.text); END;
"""
_WORD = re.compile(r"[a-z0-9]+")
_QUERY_STOP = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "at", "for", "is", "are",
               "was", "were", "what", "which", "who", "how", "did", "do", "does", "we", "i",
               "you", "my", "our", "about", "earlier", "remember", "me", "tell", "it", "that"}


class NativeBackend:
    """ZEUS's own store: SQLite FTS5 full-text index with BM25 ranking."""
    name = "native"

    def __init__(self, path: Path):
        self.path = path
        with self._open() as conn:
            conn.executescript(_NATIVE_SCHEMA)

    def _open(self):
        conn = sqlite3.connect(str(self.path), timeout=10)
        conn.row_factory = sqlite3.Row
        return _Closing(conn)

    def _result(self, row, score: float) -> MemoryResult:
        meta = json.loads(row["metadata"] or "{}")
        return MemoryResult(
            memory_id=f"native:{row['id']}", text=row["text"], score=round(float(score), 4),
            timestamp=float(row["created_at"]), category=row["category"], source=row["source"] or "",
            metadata=meta, provenance={"backend": "native", "store": self.path.name,
                                       "first_recorded": meta.get("first_recorded", row["created_at"]),
                                       "supersedes": meta.get("supersedes", [])})

    def remember(self, text: str, category: str, source: str, metadata: Dict) -> MemoryResult:
        with self._open() as conn:
            cursor = conn.execute("INSERT INTO memories (text, category, source, metadata, created_at) "
                                  "VALUES (?, ?, ?, ?, ?)",
                                  (text, category, source, json.dumps(metadata), time.time()))
            conn.commit()
            row = conn.execute("SELECT * FROM memories WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return self._result(row, 1.0)

    def recall(self, query: str, limit: int, category: Optional[str] = None,
               min_score: float = 0.0) -> List[MemoryResult]:
        terms = [t for t in _WORD.findall((query or "").lower()) if t not in _QUERY_STOP]
        if not terms:
            return []
        match = " OR ".join(f'"{t}"' for t in dict.fromkeys(terms))
        sql = ("SELECT m.*, bm25(memories_fts) AS rank FROM memories_fts "
               "JOIN memories m ON m.id = memories_fts.rowid WHERE memories_fts MATCH ?")
        params: list = [match]
        if category:
            sql += " AND m.category = ?"
            params.append(category)
        sql += " ORDER BY rank LIMIT ?"
        params.append(limit)
        results = []
        with self._open() as conn:
            for row in conn.execute(sql, params):
                strength = -float(row["rank"])        # bm25: more negative = better
                # Squash to 0..1 and blend in how much of the query it covers,
                # so the scale is comparable with the Hermes cosine scores.
                words = set(_WORD.findall(row["text"].lower()))
                coverage = sum(1 for t in set(terms) if t in words) / max(1, len(set(terms)))
                score = 0.5 * (strength / (strength + 2.0)) + 0.5 * coverage if strength > 0 else 0.0
                if score >= min_score:
                    results.append(self._result(row, score))
        return sorted(results, key=lambda r: r.score, reverse=True)

    def list(self, limit: int = 50, category: Optional[str] = None) -> List[MemoryResult]:
        sql, params = "SELECT * FROM memories", []
        if category:
            sql += " WHERE category = ?"
            params.append(category)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._open() as conn:
            return [self._result(row, 1.0) for row in conn.execute(sql, params)]

    def forget(self, memory_id: str) -> bool:
        with self._open() as conn:
            removed = conn.execute("DELETE FROM memories WHERE id = ?",
                                   (int(memory_id.split(":", 1)[1]),)).rowcount
            conn.commit()
        return bool(removed)

    def count(self) -> int:
        with self._open() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0])


class _Closing:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self.conn

    def __exit__(self, *exc):
        self.conn.close()
        return False


# ============================================================= service ====

class MemoryUnavailable(RuntimeError):
    pass


class MemoryService:
    """The memory of ONE account (or of the no-login session)."""

    def __init__(self, user_id: Optional[int], backend: Optional[str] = None):
        self.user_id = user_id
        wanted = backend or config()["backend"]
        suffix = f"u{user_id}" if user_id is not None else "local"
        self.degraded_reason = ""
        self.backend = None
        if wanted == "hermes":
            try:
                self.backend = HermesBackend(_store_dir() / f"hermes_{suffix}.sqlite")
            except Exception as error:
                self.degraded_reason = f"Hermes unavailable ({error}); using ZEUS's native store"
                logger.warning(self.degraded_reason)
        if self.backend is None:
            try:
                self.backend = NativeBackend(_store_dir() / f"native_{suffix}.sqlite")
            except Exception as error:
                raise MemoryUnavailable(f"no memory backend could open: {error}")
        self._migrate_legacy_facts()

    @property
    def backend_name(self) -> str:
        return self.backend.name if self.backend else "none"

    # ------------------------------------------------------------- write --

    def remember(self, text: str, explicit: bool = True, source: str = "user",
                 category: Optional[str] = None, metadata: Optional[Dict] = None
                 ) -> Tuple[Optional[MemoryResult], WriteDecision, List[MemoryResult]]:
        """(stored memory or None, the policy decision, memories it replaced).

        An identical memory is not written twice. A newer version of the same
        fact (same subject, or near-identical text) REPLACES the old one - the
        old text is kept in the new memory's provenance, not left beside it as
        a contradiction."""
        decision = evaluate(text, explicit=explicit, category_hint=category)
        if not decision.allowed:
            return None, decision, []
        candidates = self.backend.recall(decision.text, limit=8, category=decision.category)
        for hit in candidates:
            if _norm(hit.text) == _norm(decision.text):
                decision.reason = "already remembered"
                return hit, decision, []
        replaced = [hit for hit in candidates
                    if (decision.subject and hit.metadata.get("subject") == decision.subject)
                    or hit.score >= _SAME_MEMORY_SCORE]
        first_recorded = min([hit.provenance.get("first_recorded") or hit.timestamp
                              for hit in replaced] + [time.time()])
        meta = {**(metadata or {}), "subject": decision.subject, "explicit": explicit,
                "first_recorded": first_recorded,
                "supersedes": [{"id": h.memory_id, "text": h.text[:300]} for h in replaced]}
        if decision.flags:
            meta["flags"] = decision.flags
        stored = self.backend.remember(decision.text, decision.category, source, meta)
        for hit in replaced:
            self.backend.forget(hit.memory_id)
        if replaced:
            decision.reason = f"updated (replaced {len(replaced)} older version(s))"
        logger.info(f"memory {stored.memory_id} [{stored.category}] {decision.reason}")
        return stored, decision, replaced

    # -------------------------------------------------------------- read --

    def recall(self, query: str, limit: Optional[int] = None,
               min_score: Optional[float] = None) -> List[MemoryResult]:
        cfg = config()
        limit = limit or cfg["max_results"]
        floor = cfg["min_score"] if min_score is None else min_score
        return self.backend.recall(query, limit=limit, min_score=floor)

    def list(self, limit: int = 100, category: Optional[str] = None) -> List[MemoryResult]:
        return self.backend.list(limit=limit, category=category)

    def forget(self, memory_id: str) -> bool:
        return self.backend.forget(memory_id)

    def forget_matching(self, needle: str) -> List[str]:
        needle = (needle or "").strip().rstrip(".").lower()
        removed = []
        if not needle:
            return removed
        for memory in self.backend.list(limit=10000):
            if needle in memory.text.lower() and self.backend.forget(memory.memory_id):
                removed.append(memory.text)
        return removed

    def count(self) -> int:
        return self.backend.count()

    # ------------------------------------------------------------ migrate --

    def _migrate_legacy_facts(self) -> None:
        """Carry the facts stored before Hermes into it, once per store.

        Those facts sit in the `facts` table under `note_<hash>` keys; the hash
        was Python's per-process randomised str hash, so the same fact was
        filed under a new key every run. Nothing is deleted from the old
        table - it stays as it was."""
        marker = _store_dir() / f".migrated_{self.backend_name}_{'u%s' % self.user_id if self.user_id is not None else 'local'}"
        if marker.exists():
            return
        try:
            from memory import database
            facts = database.get_all_facts()
        except Exception as error:
            logger.debug(f"legacy fact migration skipped: {error}")
            return
        moved = 0
        for key, value in facts.items():
            if not value:
                continue
            stored, decision, _ = self.remember(value, explicit=True, source="migration",
                                                metadata={"legacy_key": key})
            moved += 1 if stored is not None and decision.reason != "already remembered" else 0
        try:
            marker.write_text(json.dumps({"migrated": moved, "at": time.time()}), encoding="utf-8")
        except OSError:
            pass
        if moved:
            logger.info(f"migrated {moved} legacy fact(s) into the {self.backend_name} memory store")


_services: Dict[Tuple[Optional[int], str], MemoryService] = {}
_lock = threading.Lock()


def get_service(user_id: Optional[int] = "active") -> MemoryService:     # type: ignore[assignment]
    """The memory of the signed-in account (default) or of `user_id`."""
    if user_id == "active":
        try:
            from memory import database
            user_id = database.get_active_user()
        except Exception:
            user_id = None
    key = (user_id, config()["backend"])
    with _lock:
        service = _services.get(key)
        if service is None:
            service = MemoryService(user_id)
            _services[key] = service
        return service


def reset_services() -> None:
    """Forget cached services (after a backend switch, and in tests)."""
    with _lock:
        _services.clear()


def initialize_stores() -> Dict[str, int]:
    """Create every account's memory store now and carry its pre-Hermes facts
    into it (STEP 1 : INSTALL / REPAIR.bat), instead of waiting for each
    account's first sign-in. Non-destructive: the facts table is kept.
    Returns {username: memories in the store}."""
    from memory import database
    database.init_db()
    previous = database.get_active_user()
    counts: Dict[str, int] = {}
    try:
        for user in database.list_users():
            # get_all_facts() is scoped to the active user, which is what makes
            # each account's migration read only its own facts.
            database.set_active_user(user["id"])
            service = get_service(user["id"])
            counts[user["username"]] = service.count()
    finally:
        database.set_active_user(previous)
    return counts
