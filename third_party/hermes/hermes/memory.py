"""SQLite-backed memory store with TF-IDF / cosine-similarity recall.

Standard library only. The store persists "memories" (free text plus metadata) to a
SQLite file and ranks them at query time using TF-IDF vectors compared by cosine
similarity, with an optional gentle recency boost.

Design goals:
  * Model-agnostic: no LLM or embedding backend involved.
  * Portable: the whole memory is one SQLite file you can copy anywhere.
  * Inspectable: ranking is plain, auditable math over plain rows.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence

# ---------------------------------------------------------------------------
# Tokenization
# ---------------------------------------------------------------------------

_WORD_RE = re.compile(r"[a-z0-9]+")

# A small, conservative English stopword set. Kept short on purpose: aggressive
# stopword removal hurts short-memory recall more than it helps.
_DEFAULT_STOPWORDS = frozenset(
    """
    a an the and or but if then else of to in on at by for with from into over
    is are was were be been being am this that these those it its as so such no
    not do does did done have has had having i you he she they we me him her them
    us my your his their our will would can could should may might must shall
    about above after again against all any because before below between both
    during each few more most other some than too very what which who whom
    """.split()
)


def tokenize(text: str, *, remove_stopwords: bool = True) -> List[str]:
    """Lowercase, split into alphanumeric tokens, optionally drop stopwords."""
    tokens = _WORD_RE.findall(text.lower())
    if remove_stopwords:
        tokens = [t for t in tokens if t not in _DEFAULT_STOPWORDS]
    return tokens


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class Memory:
    """A single stored memory."""

    id: int
    text: str
    tags: List[str] = field(default_factory=list)
    source: Optional[str] = None
    metadata: Dict[str, object] = field(default_factory=dict)
    created_at: float = 0.0
    updated_at: float = 0.0

    def to_dict(self) -> Dict[str, object]:
        return {
            "id": self.id,
            "text": self.text,
            "tags": list(self.tags),
            "source": self.source,
            "metadata": dict(self.metadata),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class RecallHit:
    """A memory plus the score it earned for a particular query."""

    memory: Memory
    score: float

    def to_dict(self) -> Dict[str, object]:
        d = self.memory.to_dict()
        d["score"] = self.score
        return d


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    text        TEXT NOT NULL,
    tags        TEXT NOT NULL DEFAULT '[]',
    source      TEXT,
    metadata    TEXT NOT NULL DEFAULT '{}',
    tokens      TEXT NOT NULL DEFAULT '[]',
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS doc_freq (
    term  TEXT PRIMARY KEY,
    df    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_memories_created ON memories(created_at);
"""

# Bumped whenever the on-disk schema changes in a breaking way.
_SCHEMA_VERSION = "1"

# Default half-life (in days) for the recency boost. A memory this old contributes
# half of the maximum recency bonus relative to "now".
_DEFAULT_RECENCY_HALFLIFE_DAYS = 30.0


class MemoryStore:
    """A persistent, model-agnostic agent memory.

    Parameters
    ----------
    path:
        Path to the SQLite file. Use ``":memory:"`` for an ephemeral in-process
        store (useful in tests).
    remove_stopwords:
        Whether tokenization drops common English stopwords.
    recency_halflife_days:
        Controls how strongly recency boosts recall. Larger = recency matters less.
        Set to ``None`` or ``0`` to disable the recency boost entirely.
    recency_weight:
        Maximum fraction of the final score that recency can contribute (0..1).
    """

    def __init__(
        self,
        path: str = "hermes_memory.sqlite",
        *,
        remove_stopwords: bool = True,
        recency_halflife_days: Optional[float] = _DEFAULT_RECENCY_HALFLIFE_DAYS,
        recency_weight: float = 0.15,
    ) -> None:
        self.path = path
        self.remove_stopwords = remove_stopwords
        self.recency_halflife_days = recency_halflife_days
        self.recency_weight = max(0.0, min(1.0, recency_weight))
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._ensure_meta()
        self._conn.commit()

    # -- lifecycle ---------------------------------------------------------

    def _ensure_meta(self) -> None:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        if row is None:
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', ?)",
                (_SCHEMA_VERSION,),
            )

    def close(self) -> None:
        try:
            self._conn.commit()
        finally:
            self._conn.close()

    def __enter__(self) -> "MemoryStore":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # -- write -------------------------------------------------------------

    def remember(
        self,
        text: str,
        *,
        tags: Optional[Sequence[str]] = None,
        source: Optional[str] = None,
        metadata: Optional[Dict[str, object]] = None,
    ) -> Memory:
        """Persist a new memory and return it."""
        text = (text or "").strip()
        if not text:
            raise ValueError("cannot remember empty text")

        tags_list = [str(t).strip() for t in (tags or []) if str(t).strip()]
        metadata = dict(metadata or {})
        tokens = tokenize(text, remove_stopwords=self.remove_stopwords)
        now = time.time()

        cur = self._conn.execute(
            """
            INSERT INTO memories(text, tags, source, metadata, tokens, created_at, updated_at)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                text,
                json.dumps(tags_list),
                source,
                json.dumps(metadata),
                json.dumps(tokens),
                now,
                now,
            ),
        )
        self._increment_doc_freq(tokens)
        self._conn.commit()
        return Memory(
            id=int(cur.lastrowid),
            text=text,
            tags=tags_list,
            source=source,
            metadata=metadata,
            created_at=now,
            updated_at=now,
        )

    def forget(self, memory_id: int) -> bool:
        """Delete a memory by id. Returns True if a row was removed."""
        row = self._conn.execute(
            "SELECT tokens FROM memories WHERE id = ?", (memory_id,)
        ).fetchone()
        if row is None:
            return False
        tokens = json.loads(row["tokens"])
        self._conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        self._decrement_doc_freq(tokens)
        self._conn.commit()
        return True

    def clear(self) -> None:
        """Delete every memory."""
        self._conn.execute("DELETE FROM memories")
        self._conn.execute("DELETE FROM doc_freq")
        self._conn.commit()

    # -- read --------------------------------------------------------------

    def get(self, memory_id: int) -> Optional[Memory]:
        row = self._conn.execute(
            "SELECT * FROM memories WHERE id = ?", (memory_id,)
        ).fetchone()
        return self._row_to_memory(row) if row else None

    def count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0])

    def list(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        tag: Optional[str] = None,
        newest_first: bool = True,
    ) -> List[Memory]:
        """List stored memories, most-recent first by default."""
        order = "DESC" if newest_first else "ASC"
        rows = self._conn.execute(
            f"SELECT * FROM memories ORDER BY created_at {order}"
        ).fetchall()
        memories = [self._row_to_memory(r) for r in rows]
        if tag is not None:
            memories = [m for m in memories if tag in m.tags]
        return memories[offset : offset + limit]

    def recall(
        self,
        query: str,
        *,
        limit: int = 5,
        tag: Optional[str] = None,
        min_score: float = 0.0,
    ) -> List[RecallHit]:
        """Return the memories most relevant to ``query``, ranked by score.

        Relevance is cosine similarity between the TF-IDF vector of the query and
        each candidate memory, optionally blended with a recency boost. Memories
        that share no terms with the query score 0 and are filtered out.
        """
        query = (query or "").strip()
        if not query:
            return []

        q_tokens = tokenize(query, remove_stopwords=self.remove_stopwords)
        if not q_tokens:
            return []

        total_docs = self.count()
        if total_docs == 0:
            return []

        df = self._doc_freq_map()
        # IDF with the standard +1 smoothing so a term present in every doc still
        # carries a tiny positive weight and we never divide by zero.
        def idf(term: str) -> float:
            return math.log((1.0 + total_docs) / (1.0 + df.get(term, 0))) + 1.0

        # Build the query TF-IDF vector.
        q_tf = _term_freq(q_tokens)
        q_vec = {t: tf * idf(t) for t, tf in q_tf.items()}
        q_norm = _l2_norm(q_vec.values())
        if q_norm == 0.0:
            return []

        now = time.time()
        rows = self._conn.execute("SELECT * FROM memories").fetchall()
        hits: List[RecallHit] = []
        for row in rows:
            mem = self._row_to_memory(row)
            if tag is not None and tag not in mem.tags:
                continue
            d_tokens = json.loads(row["tokens"])
            if not d_tokens:
                continue
            d_tf = _term_freq(d_tokens)
            # Only need terms that overlap the query for the dot product.
            dot = 0.0
            for term, qw in q_vec.items():
                dtf = d_tf.get(term)
                if dtf:
                    dot += qw * (dtf * idf(term))
            if dot <= 0.0:
                continue
            d_vec_norm = _l2_norm(
                tf * idf(term) for term, tf in d_tf.items()
            )
            if d_vec_norm == 0.0:
                continue
            cosine = dot / (q_norm * d_vec_norm)
            score = self._apply_recency(cosine, mem.created_at, now)
            if score >= min_score:
                hits.append(RecallHit(memory=mem, score=score))

        hits.sort(key=lambda h: (h.score, h.memory.created_at), reverse=True)
        return hits[:limit]

    # -- bulk import / export ---------------------------------------------

    def export_memories(
        self, *, tag: Optional[str] = None, newest_first: bool = False
    ) -> List[Dict[str, object]]:
        """Return every stored memory as a list of plain dicts.

        Suitable for serialising to JSON or JSON Lines. The dict shape matches
        :meth:`Memory.to_dict` and round-trips through :meth:`import_memories`.
        Defaults to oldest-first so an export reads in chronological order.
        """
        mems = self.list(limit=self.count() or 1, tag=tag, newest_first=newest_first)
        return [m.to_dict() for m in mems]

    def import_memories(
        self,
        records: Iterable[Dict[str, object]],
        *,
        preserve_timestamps: bool = True,
    ) -> int:
        """Bulk-load memories from an iterable of dicts. Returns the count added.

        Each record needs at least a ``text`` field; ``tags``, ``source``,
        ``metadata``, and ``created_at`` / ``updated_at`` are honoured when
        present. Unknown ids are ignored (new ids are always assigned) so two
        stores can be merged without primary-key collisions. Records whose
        ``text`` is empty/blank are skipped rather than raising.
        """
        added = 0
        for rec in records:
            if not isinstance(rec, dict):
                continue
            text = str(rec.get("text") or "").strip()
            if not text:
                continue
            tags_list = [
                str(t).strip()
                for t in (rec.get("tags") or [])
                if str(t).strip()
            ]
            source = rec.get("source")
            source = str(source) if source not in (None, "") else None
            metadata = rec.get("metadata") or {}
            if not isinstance(metadata, dict):
                metadata = {}
            tokens = tokenize(text, remove_stopwords=self.remove_stopwords)
            now = time.time()
            if preserve_timestamps:
                created = float(rec.get("created_at") or now)
                updated = float(rec.get("updated_at") or created)
            else:
                created = updated = now
            self._conn.execute(
                """
                INSERT INTO memories(text, tags, source, metadata, tokens, created_at, updated_at)
                VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    text,
                    json.dumps(tags_list),
                    source,
                    json.dumps(dict(metadata)),
                    json.dumps(tokens),
                    created,
                    updated,
                ),
            )
            self._increment_doc_freq(tokens)
            added += 1
        self._conn.commit()
        return added

    # -- stats -------------------------------------------------------------

    def stats(self) -> Dict[str, object]:
        total = self.count()
        terms = int(self._conn.execute("SELECT COUNT(*) FROM doc_freq").fetchone()[0])
        first = self._conn.execute(
            "SELECT MIN(created_at), MAX(created_at) FROM memories"
        ).fetchone()
        return {
            "path": self.path,
            "memories": total,
            "vocabulary_terms": terms,
            "oldest_created_at": first[0],
            "newest_created_at": first[1],
            "recency_halflife_days": self.recency_halflife_days,
            "recency_weight": self.recency_weight,
            "schema_version": _SCHEMA_VERSION,
        }

    # -- internals ---------------------------------------------------------

    def _apply_recency(self, cosine: float, created_at: float, now: float) -> float:
        if not self.recency_halflife_days or self.recency_weight <= 0.0:
            return cosine
        age_days = max(0.0, (now - created_at) / 86400.0)
        # Exponential decay -> 1.0 for brand new, 0.5 at one half-life, etc.
        recency = 0.5 ** (age_days / self.recency_halflife_days)
        w = self.recency_weight
        return cosine * (1.0 - w) + cosine * recency * w

    def _increment_doc_freq(self, tokens: Iterable[str]) -> None:
        for term in set(tokens):
            self._conn.execute(
                """
                INSERT INTO doc_freq(term, df) VALUES(?, 1)
                ON CONFLICT(term) DO UPDATE SET df = df + 1
                """,
                (term,),
            )

    def _decrement_doc_freq(self, tokens: Iterable[str]) -> None:
        for term in set(tokens):
            self._conn.execute(
                "UPDATE doc_freq SET df = df - 1 WHERE term = ?", (term,)
            )
        self._conn.execute("DELETE FROM doc_freq WHERE df <= 0")

    def _doc_freq_map(self) -> Dict[str, int]:
        rows = self._conn.execute("SELECT term, df FROM doc_freq").fetchall()
        return {r["term"]: int(r["df"]) for r in rows}

    @staticmethod
    def _row_to_memory(row: sqlite3.Row) -> Memory:
        return Memory(
            id=int(row["id"]),
            text=row["text"],
            tags=json.loads(row["tags"]),
            source=row["source"],
            metadata=json.loads(row["metadata"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )


# ---------------------------------------------------------------------------
# Vector helpers
# ---------------------------------------------------------------------------


def _term_freq(tokens: Sequence[str]) -> Dict[str, float]:
    """Sub-linear (log-scaled) term frequency, which dampens repeated terms."""
    counts: Dict[str, int] = {}
    for t in tokens:
        counts[t] = counts.get(t, 0) + 1
    return {t: 1.0 + math.log(c) for t, c in counts.items()}


def _l2_norm(values: Iterable[float]) -> float:
    return math.sqrt(sum(v * v for v in values))
