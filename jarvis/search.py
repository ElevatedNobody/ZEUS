"""
Project knowledge search - hybrid, filtered and TEMPORAL.

Indexed items: evidence (with its original text, its vernacular reading, the
vision model's inference marked as such), correspondence, activities, events,
forecast changes. Each carries project, activity, when it happened, when it was
captured, its source and an authority rank.

    lexical    FTS5 (porter) over the text, with the knowledge fabric's safe
               MATCH builder - user text is never FTS syntax
    semantic   bge-m3 vectors (optional; only when the model answers)
    filters    project, activity, kinds, source type, minimum authority
    temporal   happened between two dates; KNOWN BEFORE a moment (captured_at)
               - "what evidence existed before the delay was reported?"

Results are fused by reciprocal rank and each keeps its provenance.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional

from jarvis import dates, store

_AUTHORITY = {("office", "correspondence"): 8, ("contractor", "correspondence"): 8, ("client", "correspondence"): 9,
              ("import", "schedule_file"): 9}


def authority(source_type: str, kind: str) -> int:
    """How much weight a source carries: a signed letter or an approved
    schedule outranks a chat note; an AI description ranks lowest."""
    if (source_type, kind) in _AUTHORITY:
        return _AUTHORITY[(source_type, kind)]
    return {"document": 7, "spreadsheet": 7, "image": 6, "voice": 5, "text": 5, "correspondence": 8}.get(kind, 5)


def index_item(conn: sqlite3.Connection, key: str, kind: str, ref_id: str, project_id: str,
               activity_id: Optional[str], occurred_at: Optional[str], captured_at: Optional[str],
               source: Optional[str], authority_rank: int, origin: Optional[str], title: str, text: str) -> None:
    text = (text or "")[:20000]
    conn.execute("DELETE FROM jv_search WHERE key = ?", (key,))
    conn.execute("INSERT INTO jv_search (text, title, key) VALUES (?, ?, ?)", (text, title or "", key))
    conn.execute("INSERT OR REPLACE INTO jv_search_meta (key, kind, ref_id, project_id, activity_id, occurred_at, "
                 "captured_at, source, authority, origin, title, text) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (key, kind, ref_id, project_id, activity_id, occurred_at, captured_at, source, int(authority_rank),
                  origin, title, text[:2000]))


def reindex(project_id: Optional[str] = None, embed: bool = False) -> Dict[str, Any]:
    """Rebuild the index from the store (RAG_REINDEX.bat)."""
    counts = {"evidence": 0, "correspondence": 0, "activities": 0, "forecast_changes": 0, "vectors": 0}
    with store.connection() as conn:
        where, params = ("WHERE project_id = ?", (project_id,)) if project_id else ("", ())
        if project_id:
            keys = [r[0] for r in conn.execute("SELECT key FROM jv_search_meta WHERE project_id = ?", (project_id,))]
            for key in keys:
                conn.execute("DELETE FROM jv_search WHERE key = ?", (key,))
            conn.execute("DELETE FROM jv_search_meta WHERE project_id = ?", (project_id,))
        else:
            conn.execute("DELETE FROM jv_search")
            conn.execute("DELETE FROM jv_search_meta")
        for r in conn.execute(f"SELECT * FROM jv_evidence {where}", params).fetchall():
            meta = store.loads(r["metadata"], {})
            reading = (meta.get("normalization") or {})
            index_item(conn, f"evidence:{r['evidence_id']}", "evidence", r["evidence_id"], r["project_id"], None,
                       r["occurred_at"], r["captured_at"], r["source_type"], authority(r["source_type"], r["kind"]),
                       r["origin"], r["title"] or "", "\n".join(x for x in (r["original_text"], reading.get("normalized")
                                                                            if reading.get("vernacular") else "",
                                                                            reading.get("translation")) if x))
            counts["evidence"] += 1
        for r in conn.execute(f"SELECT * FROM jv_correspondence {where}", params).fetchall():
            index_item(conn, f"correspondence:{r['correspondence_id']}", "correspondence", r["correspondence_id"],
                       r["project_id"], None, r["letter_date"], None, "correspondence", 8, None, r["subject"] or "",
                       r["summary"] or "")
            counts["correspondence"] += 1
        for r in conn.execute(f"SELECT * FROM jv_activities {where}", params).fetchall():
            index_item(conn, f"activity:{r['activity_id']}", "activity", r["activity_id"], r["project_id"],
                       r["activity_id"], None, None, "schedule", 9, None, f"{r['code']} {r['name']}",
                       f"{r['code']} {r['name']} {r['wbs_code'] or ''} {r['location'] or ''} {r['contractor'] or ''}")
            counts["activities"] += 1
        for r in conn.execute(f"SELECT * FROM jv_forecast_changes {where}", params).fetchall():
            chain = store.loads(r["chain"], [])
            index_item(conn, f"forecast:{r['change_id']}", "forecast_change", r["change_id"], r["project_id"],
                       r["root_activity_id"], r["captured_at"], r["captured_at"], "cpm", 9, None,
                       f"Forecast change {r['previous_finish']} -> {r['new_finish']}",
                       " -> ".join(f"{s.get('code')} {s.get('name')}" for s in chain))
            counts["forecast_changes"] += 1
    if embed:
        counts["vectors"] = embed_missing(project_id)
    return {"ok": True, **counts}


def embed_missing(project_id: Optional[str] = None, limit: int = 2000) -> int:
    try:
        import numpy as np
        from core.capabilities import embedding_provider
        model = embedding_provider.resolve()
    except Exception:
        return 0
    if not model:
        return 0
    done = 0
    with store.connection() as conn:
        sql = ("SELECT m.key, m.title, m.text FROM jv_search_meta m LEFT JOIN jv_vectors v ON v.key = m.key "
               "WHERE (v.key IS NULL OR v.model != ?)")
        params: List[Any] = [model]
        if project_id:
            sql += " AND m.project_id = ?"
            params.append(project_id)
        rows = conn.execute(sql + " LIMIT ?", params + [limit]).fetchall()
    for row in rows:
        vector = embedding_provider.embed(f"{row['title']}\n{row['text']}"[:2000], timeout=60)
        if not vector:
            continue
        with store.connection() as conn:
            conn.execute("INSERT OR REPLACE INTO jv_vectors (key, model, dim, vector) VALUES (?, ?, ?, ?)",
                         (row["key"], model, len(vector), np.asarray(vector, dtype="float32").tobytes()))
        done += 1
    return done


def search(query: str, project_id: Optional[str] = None, kinds: Optional[List[str]] = None,
           activity_id: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None,
           known_before: Optional[str] = None, source: Optional[str] = None, min_authority: int = 0,
           semantic: bool = True, limit: int = 20) -> Dict[str, Any]:
    filters = ["1=1"]
    params: List[Any] = []
    if project_id:
        filters.append("m.project_id = ?")
        params.append(project_id)
    if kinds:
        filters.append(f"m.kind IN ({','.join('?' for _ in kinds)})")
        params.extend(kinds)
    if activity_id:
        filters.append("(m.activity_id = ? OR m.ref_id IN (SELECT from_id FROM jv_links WHERE to_kind = 'activity' "
                       "AND to_id = ?))")
        params.extend([activity_id, activity_id])
    if date_from:
        filters.append("substr(COALESCE(m.occurred_at, m.captured_at), 1, 10) >= ?")
        params.append(dates.iso(date_from))
    if date_to:
        filters.append("substr(COALESCE(m.occurred_at, m.captured_at), 1, 10) <= ?")
        params.append(dates.iso(date_to))
    if known_before:
        filters.append("m.captured_at IS NOT NULL AND m.captured_at < ?")
        params.append(known_before)
    if source:
        filters.append("m.source = ?")
        params.append(source)
    if min_authority:
        filters.append("m.authority >= ?")
        params.append(int(min_authority))
    where = " AND ".join(filters)
    lexical: List[Dict[str, Any]] = []
    match = store.fts_query(query or "")
    with store.connection() as conn:
        if match:
            lexical = [dict(r) for r in conn.execute(
                f"SELECT m.*, bm25(jv_search) AS rank FROM jv_search JOIN jv_search_meta m ON m.key = jv_search.key "
                f"WHERE jv_search MATCH ? AND {where} ORDER BY rank LIMIT 200", [match] + params)]
        elif not query:
            lexical = [dict(r) for r in conn.execute(
                f"SELECT m.*, 0 AS rank FROM jv_search_meta m WHERE {where} ORDER BY COALESCE(m.occurred_at, "
                f"m.captured_at) DESC LIMIT 200", params)]
        dense: List[Dict[str, Any]] = []
        backend = "lexical (FTS5)"
        if semantic and query:
            dense = _dense(conn, query, where, params)
            if dense:
                backend = "hybrid (FTS5 + bge-m3 vectors, reciprocal-rank fusion)"
    fused: Dict[str, Dict[str, Any]] = {}
    for rank, row in enumerate(lexical):
        entry = fused.setdefault(row["key"], dict(row, score=0.0, signals=[]))
        entry["score"] += 1.0 / (60 + rank)
        entry["signals"].append(f"lexical #{rank + 1}")
    for rank, row in enumerate(dense):
        entry = fused.setdefault(row["key"], dict(row, score=0.0, signals=[]))
        entry["score"] += 1.0 / (60 + rank)
        entry["signals"].append(f"semantic #{rank + 1} ({row['similarity']:.2f})")
    for entry in fused.values():
        entry["score"] += 0.0005 * int(entry.get("authority") or 5)
    results = sorted(fused.values(), key=lambda r: -r["score"])[:max(limit, 40)]
    if query and len(results) > 1:
        results, rerank_backend = _rerank(query, results)
        if rerank_backend:
            backend += f", reranked by {rerank_backend}"
    results = results[:limit]
    for r in results:
        r.pop("rank", None)
        r["excerpt"] = _excerpt(r.get("text") or "", query)
        r.pop("text", None)
    return {"ok": True, "query": query, "backend": backend, "count": len(results), "results": results,
            "filters": {"project_id": project_id, "kinds": kinds, "activity_id": activity_id, "date_from": date_from,
                        "date_to": date_to, "known_before": known_before, "source": source,
                        "min_authority": min_authority}}


def _rerank(query: str, rows: List[Dict[str, Any]]):
    """ZEUS's reranker (a local cross-encoder when installed, otherwise its
    deterministic hybrid ranker), blended with the fused rank."""
    try:
        from industrial.reranker import rerank
        candidates = [dict(r, chunk_text=f"{r.get('title') or ''}\n{r.get('text') or ''}"[:2000],
                           retrieval_score=r["score"]) for r in rows]
        ordered, info = rerank(query, candidates, dense_scores=[r["score"] for r in rows])
        for position, row in enumerate(ordered):
            row["signals"] = list(row.get("signals") or []) + [f"rerank #{position + 1}"]
            row.pop("chunk_text", None)
            row.pop("rerank_detail", None)
        return ordered, info.get("backend", "")
    except Exception:
        return rows, ""


def _dense(conn: sqlite3.Connection, query: str, where: str, params: List[Any]) -> List[Dict[str, Any]]:
    try:
        import numpy as np
        if not conn.execute("SELECT 1 FROM jv_vectors LIMIT 1").fetchone():
            return []
        from core.capabilities import embedding_provider
        vector = embedding_provider.embed(query[:500], timeout=30)
        if not vector:
            return []
        q = np.asarray(vector, dtype="float32")
        rows = conn.execute(f"SELECT m.*, v.vector FROM jv_vectors v JOIN jv_search_meta m ON m.key = v.key "
                            f"WHERE v.dim = ? AND {where}", [len(vector)] + params).fetchall()
        scored = []
        for row in rows:
            v = np.frombuffer(row["vector"], dtype="float32")
            similarity = float(q @ v / (np.linalg.norm(q) * np.linalg.norm(v) + 1e-9))
            item = {k: row[k] for k in row.keys() if k != "vector"}
            item["similarity"] = similarity
            scored.append(item)
        return [s for s in sorted(scored, key=lambda s: -s["similarity"])[:50] if s["similarity"] > 0.35]
    except Exception:
        return []


def _excerpt(text: str, query: str, width: int = 260) -> str:
    low = text.lower()
    for word in sorted(set((query or "").lower().split()), key=len, reverse=True):
        if len(word) > 3 and word in low:
            at = low.index(word)
            start = max(0, at - width // 3)
            return ("…" if start else "") + text[start:start + width].replace("\n", " ") + "…"
    return text[:width].replace("\n", " ")
