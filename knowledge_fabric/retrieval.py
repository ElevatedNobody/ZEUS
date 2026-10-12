"""
Security-first hybrid retrieval.

    access filter (in SQL) -> temporal / metadata filters (in SQL)
      -> candidate lists: full-text (BM25) | vectors | entity mentions | graph | table facts | legacy stores
      -> reciprocal-rank fusion
      -> source authority + origin weighting
      -> rerank (industrial.reranker: cross-encoder when installed, deterministic hybrid otherwise)
      -> parent context, injection scan, provenance
      -> Evidence E1..En  (+ the numeric facts, kept apart for the analytics engine)

No leg reads a document the caller may not see, and the diagnostics never say
how many were withheld - a count of hidden documents is itself a leak.
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from core.logger import get_logger
from knowledge_fabric import authority, store
from knowledge_fabric.access import AccessContext
from knowledge_fabric.entities import Extractor, norm, resolve

logger = get_logger("knowledge_fabric.retrieval")

RRF_K = 60
# A procedure question wants the procedure, a policy question the policy.
INTENT_TYPES = {"PROCEDURE": ("sop", "manual", "work_instruction"), "POLICY": ("policy",)}
ORIGIN_WEIGHT = {"company": 1.0, "general_knowledge": 0.8, "sample": 0.65, "demo": 0.65, "test_fixture": 1.0}


@dataclass
class EvidenceItem:
    evidence_id: str
    node_id: str
    document_id: str
    title: str
    text: str
    source: str = "fabric"                    # fabric | legacy_industrial | legacy_chunks
    filename: str = ""
    page: Optional[int] = None
    section_path: str = ""
    heading: Optional[str] = None
    date: Optional[str] = None
    year: Optional[int] = None
    document_type: str = ""
    status: str = ""
    authority: int = 5
    authority_label: str = ""
    classification: str = "internal"
    origin: str = "company"
    kind: str = "paragraph"
    source_hash: str = ""
    parent_context: str = ""
    scores: Dict[str, float] = field(default_factory=dict)
    score: float = 0.0
    legs: List[str] = field(default_factory=list)
    flags: List[str] = field(default_factory=list)

    def citation(self) -> str:
        bits = [self.title or self.filename or self.document_id]
        if self.page:
            bits.append(f"p.{self.page}")
        if self.section_path:
            bits.append(self.section_path.split(" > ")[-1][:60])
        if self.year:
            bits.append(str(self.year))
        return ", ".join(bits)

    def provenance(self) -> Dict[str, Any]:
        return {"evidence_id": self.evidence_id, "node_id": self.node_id, "document_id": self.document_id,
                "title": self.title, "filename": self.filename, "page": self.page, "section": self.section_path,
                "date": self.date, "year": self.year, "source": self.source, "document_type": self.document_type,
                "authority": self.authority_label, "classification": self.classification, "origin": self.origin,
                "source_hash": self.source_hash, "retrieval_score": round(self.score, 4), "legs": self.legs,
                "flags": self.flags}

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class RetrievalResult:
    evidence: List[EvidenceItem]
    facts: List[Dict[str, Any]]
    graph_edges: List[Dict[str, Any]]
    entities: List[Dict[str, Any]]
    diagnostics: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {"evidence": [e.to_dict() for e in self.evidence], "facts": self.facts,
                "graph_edges": self.graph_edges, "entities": self.entities, "diagnostics": self.diagnostics}


# ------------------------------------------------------------------ filters --

def _temporal_clause(window: Optional[Tuple[int, int]]) -> Tuple[str, List[Any]]:
    if not window:
        return "1", []
    lo, hi = window
    return ("((n.year BETWEEN ? AND ?) OR (n.year IS NULL AND d.time_start IS NOT NULL AND "
            "d.time_start <= ? AND COALESCE(d.time_end, d.time_start) >= ?))", [lo, hi, hi, lo])


def _filter_clause(filters: Optional[Dict[str, Any]]) -> Tuple[str, List[Any]]:
    if not filters:
        return "1", []
    parts, params = [], []
    for key, column in (("document_type", "d.document_type"), ("department", "d.department"),
                        ("origin", "d.origin"), ("status", "d.status"), ("location", "d.location"),
                        ("business_unit", "d.business_unit")):
        value = filters.get(key)
        if value:
            values = value if isinstance(value, list) else [value]
            parts.append(f"{column} IN ({store.placeholders(values)})")
            params += values
    if filters.get("document_id"):
        parts.append("d.document_id = ?")
        params.append(filters["document_id"])
    if filters.get("exclude_origins"):
        values = filters["exclude_origins"]
        parts.append(f"d.origin NOT IN ({store.placeholders(values)})")
        params += values
    return (" AND ".join(parts) or "1"), params


_IDENTIFIER = re.compile(r"(?<![\w-])([A-Za-z]{1,4}-?\d{1,5}[A-Za-z]?)(?![\w-])")
_FRONT_MATTER = re.compile(r"^\s*-{3}\s*(?:[\w-]+\s*:[^\n]*?\s*)+-{3}\s*$", re.S)


def _identifiers(question: str) -> List["re.Pattern"]:
    """Grade, unit and tag codes in the question ('L3', 'U5', 'C-501') - not years."""
    out = []
    for code in dict.fromkeys(_IDENTIFIER.findall(question or "")):
        if code.isdigit() or re.fullmatch(r"(?:fy|q)?\d+", code, re.I):
            continue
        out.append(re.compile(rf"(?<![\w-]){re.escape(code)}(?![\w-]|\d)", re.I))
    return out


def _only_questions(text: str) -> bool:
    """A passage made of questions ('What is the notice period for L3?') - an FAQ
    header or a list of test prompts. It shares the question's words exactly
    and would outrank the passage that answers it."""
    parts = [p.strip(" -*•") for p in re.split(r"(?<=[?.!”\"])\s+|\n+|\s+-\s+", text or "") if p.strip(" -*•")]
    if len(parts) < 2:
        return False
    quoted = re.compile(r"^[\"“'‘].{3,}[\"”'’][.?!]?$")
    asked = sum(1 for p in parts if p.rstrip("\"”'’").endswith("?") or quoted.match(p))
    return asked >= 2 and asked >= 0.6 * len(parts)


def _base_where(ctx: AccessContext, window, filters) -> Tuple[str, List[Any]]:
    access_sql, access_params = ctx.clause("d")
    time_sql, time_params = _temporal_clause(window)
    filter_sql, filter_params = _filter_clause(filters)
    # Register rows are answered by the register engine (registers.py), cited row by
    # row; in passage search sixty near-identical rows would crowd out the prose.
    return (f"({access_sql}) AND ({time_sql}) AND ({filter_sql}) AND n.kind != 'record'",
            access_params + time_params + filter_params)


# ------------------------------------------------------------------- legs ---

def _expanded_query(question: str) -> str:
    extra = []
    try:
        from industrial.taxonomy import get_taxonomy
        expansion = get_taxonomy().expand_query(question, max_terms=6)
        extra = expansion.get("expansions") or expansion.get("terms") or []
        if isinstance(extra, dict):
            extra = list(extra.values())
        extra = [str(x) for x in extra][:6]
    except Exception:
        pass
    return question + " " + " ".join(extra)


def _fts_leg(conn, question: str, where: str, params: List[Any], limit: int = 80) -> List[Tuple[str, float]]:
    match = store.fts_query(_expanded_query(question))
    if not match:
        return []
    sql = (f"SELECT f.node_id, bm25(kf_fts, 1.0, 0.6, 0.3) AS score FROM kf_fts f "
           f"JOIN kf_nodes n ON n.node_id = f.node_id JOIN kf_documents d ON d.document_id = n.document_id "
           f"WHERE kf_fts MATCH ? AND {where} ORDER BY score LIMIT ?")
    try:
        return [(r["node_id"], -float(r["score"])) for r in conn.execute(sql, [match] + params + [limit])]
    except Exception as error:
        logger.warning(f"full-text leg failed: {error}")
        return []


def _vector_leg(conn, question: str, where: str, params: List[Any], limit: int = 80,
                max_candidates: int = 40000, info: Optional[Dict[str, Any]] = None) -> Tuple[List[Tuple[str, float]], str]:
    info = info if info is not None else {}
    have = conn.execute("SELECT COUNT(*) FROM kf_vectors").fetchone()[0]
    info["stored_vectors"] = int(have)
    if not have:
        return [], "no vectors stored (run `rag reembed` once an embedding model is available)"
    try:
        import numpy as np
        from core.capabilities import embedding_provider
        query = embedding_provider.embed(question)
    except Exception as error:
        return [], f"embedding unavailable: {error}"
    if not query:
        return [], "embedding model did not answer - vector search off for this question"
    q = np.asarray(query, dtype="float32")
    q /= (np.linalg.norm(q) or 1.0)
    info["query_dims"] = int(q.size)
    try:
        info["model"] = embedding_provider.resolve()          # cached after the embed above
    except Exception:
        pass
    rows = conn.execute(f"SELECT v.node_id, v.vector, v.dim FROM kf_vectors v JOIN kf_nodes n ON n.node_id = v.node_id "
                        f"JOIN kf_documents d ON d.document_id = n.document_id WHERE {where} LIMIT ?",
                        params + [max_candidates]).fetchall()
    stored_dims = sorted({r["dim"] for r in rows})
    rows = [r for r in rows if r["dim"] == q.size]
    info["vectors_same_dim"] = len(rows)
    if not rows:
        return [], (f"no stored vectors of the query's dimension ({q.size}; stored: {stored_dims}) - "
                    f"the embedding model changed: run RAG_REINDEX.bat")
    matrix = np.frombuffer(b"".join(r["vector"] for r in rows), dtype="float32").reshape(len(rows), q.size)
    norms = np.linalg.norm(matrix, axis=1)
    norms[norms == 0] = 1.0
    scores = (matrix @ q) / norms
    order = np.argsort(-scores)[:limit]
    note = "" if len(rows) < max_candidates else f"vector search capped at {max_candidates} candidates"
    return [(rows[i]["node_id"], float(scores[i])) for i in order], note


def question_entities(conn, question: str) -> List[Dict[str, Any]]:
    """Entities the question names: pattern mentions plus exact alias hits on
    word n-grams (so a configured name like 'Hydrogen Generation Unit' is found)."""
    found: Dict[str, Dict[str, Any]] = {}
    for mention in Extractor().mentions(question):
        for row in resolve(conn, mention.surface):
            found.setdefault(row["entity_id"], {**row, "surface": mention.surface})
        if mention.entity_id not in found:
            exists = conn.execute("SELECT entity_id, type, canonical_name, status FROM kf_entities "
                                  "WHERE entity_id = ? OR entity_id LIKE ?",
                                  (mention.entity_id, mention.entity_id + "@%")).fetchall()
            for row in exists:
                found.setdefault(row["entity_id"], {**dict(row), "confidence": mention.confidence,
                                                    "surface": mention.surface})
    words = re.findall(r"[A-Za-z0-9\-&]+", question)
    for n in (4, 3, 2, 1):
        for i in range(len(words) - n + 1):
            phrase = " ".join(words[i:i + n])
            if len(phrase) < 3 or phrase.lower() in store._STOPWORDS:
                continue
            for row in conn.execute("SELECT a.entity_id, a.confidence, e.type, e.canonical_name, e.status "
                                    "FROM kf_aliases a JOIN kf_entities e ON e.entity_id = a.entity_id "
                                    "WHERE a.alias_norm = ? AND e.status != 'merged'", (norm(phrase),)):
                if row["type"] in ("FINANCIAL_METRIC", "KPI") and n == 1 and len(phrase) < 5:
                    continue
                found.setdefault(row["entity_id"], {**dict(row), "surface": phrase})
    return list(found.values())


def _mentions_period(text: str, window: Tuple[int, int]) -> bool:
    from knowledge_fabric.temporal import years_in_text
    return any(window[0] <= year <= window[1] for year in years_in_text(text))


def subject_entities(entities: List[Dict[str, Any]], metrics: Optional[List[str]] = None) -> List[str]:
    """The entities a figure can be ABOUT - not the word 'maintenance' inside
    'maintenance cost', which names the metric, not the Maintenance department."""
    from knowledge_fabric.entities import SUBJECT_TYPES
    metric_text = " ".join(metrics or []).lower()
    out = []
    for e in entities:
        if e.get("type") not in SUBJECT_TYPES:
            continue
        surface = (e.get("surface") or e.get("canonical_name") or "").lower().strip()
        if surface and surface in metric_text:
            continue
        out.append(e["entity_id"])
    return list(dict.fromkeys(out))


def _entity_leg(conn, entity_ids: List[str], where: str, params: List[Any], limit: int = 80) -> List[Tuple[str, float]]:
    if not entity_ids:
        return []
    sql = (f"SELECT m.node_id, SUM(m.confidence) AS score, COUNT(DISTINCT m.entity_id) AS hits FROM kf_mentions m "
           f"JOIN kf_nodes n ON n.node_id = m.node_id JOIN kf_documents d ON d.document_id = n.document_id "
           f"WHERE m.entity_id IN ({store.placeholders(entity_ids)}) AND {where} "
           f"GROUP BY m.node_id ORDER BY hits DESC, score DESC LIMIT ?")
    return [(r["node_id"], float(r["hits"]) + float(r["score"]) / 10) for r in conn.execute(sql, entity_ids + params + [limit])]


def _graph_leg(conn, entity_ids: List[str], allowed_docs: Set[str], relations: Optional[List[str]] = None,
               limit: int = 60) -> Tuple[List[Tuple[str, float]], List[Dict[str, Any]]]:
    from knowledge_fabric import graph
    edges = graph.neighbours(conn, entity_ids, relations=relations, min_confidence=0.5,
                             allowed_documents=allowed_docs, limit=limit)
    scored: Dict[str, float] = {}
    for edge in edges:
        if edge.get("evidence_node"):
            scored[edge["evidence_node"]] = max(scored.get(edge["evidence_node"], 0.0), float(edge["confidence"]))
    return sorted(scored.items(), key=lambda kv: -kv[1]), edges


def structured_facts(conn, ctx: AccessContext, metrics: List[str], window: Optional[Tuple[int, int]] = None,
                     entity_ids: Optional[List[str]] = None, limit: int = 2000) -> List[Dict[str, Any]]:
    """Numeric facts for the analytics engine - access-filtered like everything else."""
    if not metrics:
        return []
    access_sql, access_params = ctx.clause("d")
    names: List[str] = []
    for metric in metrics:
        names.append(metric)
        if metric == "net profit":
            names += ["net loss", "profit", "loss", "profit before tax"]
    sql = (f"SELECT f.*, d.title, d.document_type, d.status, d.authority, d.origin, d.doc_date, d.year AS doc_year, d.revision, "
           f"d.confidentiality, d.filename, n.page, n.section_path FROM kf_facts f "
           f"JOIN kf_documents d ON d.document_id = f.document_id LEFT JOIN kf_nodes n ON n.node_id = f.node_id "
           f"WHERE ({access_sql}) AND (f.metric_norm IN ({store.placeholders(names)}) OR "
           + " OR ".join("f.metric_norm LIKE ?" for _ in metrics) + ")")
    params: List[Any] = access_params + names + [f"%{m}%" for m in metrics]
    if window:
        sql += " AND (f.year IS NULL OR f.year BETWEEN ? AND ?)"
        params += [window[0], window[1]]
    if entity_ids:
        exact = [e for e in entity_ids if not e.endswith("%")]
        patterns = [e for e in entity_ids if e.endswith("%")]
        parts = ["f.entity_id IS NULL"]
        if exact:
            parts.append(f"f.entity_id IN ({store.placeholders(exact)})")
        parts += ["f.entity_id LIKE ?" for _ in patterns]
        sql += " AND (" + " OR ".join(parts) + ")"
        params += exact + patterns
    # Annual figures first: thousands of monthly rows must never push a year's
    # headline figure past the limit.
    sql += " ORDER BY (f.month IS NOT NULL OR f.quarter IS NOT NULL), f.year, d.authority LIMIT ?"
    params.append(limit)
    return [dict(r) for r in conn.execute(sql, params)]


# --------------------------------------------------------------- assemble ---

def _rrf(lists: Dict[str, List[Tuple[str, float]]]) -> Dict[str, Dict[str, Any]]:
    fused: Dict[str, Dict[str, Any]] = {}
    for leg, items in lists.items():
        for rank, (node_id, raw) in enumerate(items):
            entry = fused.setdefault(node_id, {"rrf": 0.0, "legs": [], "raw": {}})
            entry["rrf"] += 1.0 / (RRF_K + rank + 1)
            entry["legs"].append(leg)
            entry["raw"][leg] = raw
    return fused


def _load_nodes(conn, node_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for start in range(0, len(node_ids), 400):
        chunk = node_ids[start:start + 400]
        for r in conn.execute(
                f"SELECT n.*, d.title, d.filename, d.document_type, d.status, d.authority, d.confidentiality, "
                f"d.origin, d.source_hash, d.doc_date, d.department, d.location, d.department_restricted, d.vault_linked "
                f"FROM kf_nodes n JOIN kf_documents d ON d.document_id = n.document_id "
                f"WHERE n.node_id IN ({store.placeholders(chunk)})", chunk):
            out[r["node_id"]] = dict(r)
    return out


def _parent_context(conn, node: Dict[str, Any], chars: int = 360) -> str:
    bits = []
    if node.get("section_path"):
        bits.append(f"[section] {node['section_path']}")
    if node.get("parent_id") and node.get("kind") in ("paragraph", "row_range", "figure"):
        previous = conn.execute("SELECT text FROM kf_nodes WHERE parent_id = ? AND ordinal < ? "
                                "AND kind = 'paragraph' ORDER BY ordinal DESC LIMIT 1",
                                (node["parent_id"], node["ordinal"])).fetchone()
        if previous and previous["text"]:
            bits.append(f"[before] {previous['text'][-chars:]}")
    return "\n".join(bits)


def retrieve(question: str, ctx: AccessContext, plan: Optional[Dict[str, Any]] = None, top_k: int = 12,
             filters: Optional[Dict[str, Any]] = None, use_legacy: bool = True,
             use_vectors: bool = True) -> RetrievalResult:
    started = time.time()
    plan = plan or {}
    scope = plan.get("scope") or {}
    window = tuple(scope["window"]) if scope.get("window") else None
    sources = set(plan.get("sources") or ["fulltext", "vector", "entities", "legacy"])
    diagnostics: Dict[str, Any] = {"legs": {}, "filters": {"window": window, **(filters or {})},
                                   "access": {"role": ctx.role, "classifications": list(ctx.classifications)}}
    if not ctx.authenticated:
        diagnostics["refused"] = "no authenticated session"
        return RetrievalResult([], [], [], [], diagnostics)

    with store.connection() as conn:
        where, params = _base_where(ctx, window, filters)
        lists: Dict[str, List[Tuple[str, float]]] = {}

        lists["fulltext"] = _fts_leg(conn, question, where, params)
        diagnostics["legs"]["fulltext"] = len(lists["fulltext"])

        if use_vectors and "vector" in sources:
            embedding_info: Dict[str, Any] = {}
            vector_hits, note = _vector_leg(conn, question, where, params, info=embedding_info)
            lists["vector"] = vector_hits
            diagnostics["legs"]["vector"] = len(vector_hits)
            diagnostics["embedding"] = embedding_info
            if note:
                diagnostics["vector_note"] = note

        entities = question_entities(conn, question)
        entity_ids = [e["entity_id"] for e in entities]
        if plan.get("entities"):
            entity_ids = list(dict.fromkeys(entity_ids + list(plan["entities"])))
        lists["entities"] = _entity_leg(conn, entity_ids, where, params)
        diagnostics["legs"]["entities"] = len(lists["entities"])
        if window:
            # What HAPPENED in the period: dated event rows (a milestone register) are
            # candidates for any question with a time window, whatever their words.
            lists["events"] = [(r["node_id"], 1.0) for r in conn.execute(
                f"SELECT n.node_id FROM kf_nodes n JOIN kf_documents d ON d.document_id = n.document_id "
                f"WHERE {where} AND n.kind = 'event' AND n.year BETWEEN ? AND ? ORDER BY n.year LIMIT 12",
                params + [window[0], window[1]])]
            diagnostics["legs"]["events"] = len(lists["events"])
        ambiguous = [e for e in entities if e.get("status") == "ambiguous"]
        if ambiguous:
            diagnostics["ambiguous_entities"] = [e["canonical_name"] for e in ambiguous]

        edges: List[Dict[str, Any]] = []
        if "graph" in sources and entity_ids:
            from knowledge_fabric.access import allowed_document_ids
            allowed = allowed_document_ids(conn, ctx)
            graph_hits, edges = _graph_leg(conn, entity_ids, allowed)
            lists["graph"] = graph_hits
            diagnostics["legs"]["graph"] = len(graph_hits)

        facts: List[Dict[str, Any]] = []
        if "structured" in sources and plan.get("metrics"):
            subjects = subject_entities(entities, plan["metrics"])
            subjects += [e + "@%" for e in subjects if "@" not in e]
            facts = structured_facts(conn, ctx, plan["metrics"], window, subjects or None)
            fact_nodes: Dict[str, float] = {}
            for fact in facts:
                if fact.get("node_id"):
                    fact_nodes[fact["node_id"]] = fact_nodes.get(fact["node_id"], 0.0) + 1.0
            lists["structured"] = sorted(fact_nodes.items(), key=lambda kv: -kv[1])
            diagnostics["legs"]["structured"] = len(facts)

        fused = _rrf(lists)
        nodes = _load_nodes(conn, list(fused))
        candidates: List[Dict[str, Any]] = []
        for node_id, entry in fused.items():
            node = nodes.get(node_id)
            if not node or node.get("kind") == "document":
                continue
            if node.get("kind") in ("section", "subsection") and len(node.get("text") or "") < 120:
                continue                       # a bare heading - its paragraphs carry the content
            if _FRONT_MATTER.match(node.get("text") or ""):
                continue                       # '--- title: ... ---' is file metadata, not content
            if not ctx.can_see(node):          # defence in depth - the SQL already filtered
                continue
            candidates.append({**node, "rrf": entry["rrf"], "legs": entry["legs"], "raw": entry["raw"],
                               "chunk_text": node.get("text") or "", "retrieval_score": entry["rrf"],
                               "element_type": node.get("kind"), "equipment": None,
                               "page_number": node.get("page")})

        legacy_notes = []
        if use_legacy and "legacy" in sources:
            for fetch in ("industrial", "original"):
                try:
                    from knowledge_fabric import adapters
                    rows, note = (adapters.industrial_kb(question) if fetch == "industrial"
                                  else adapters.original_store(question))
                except Exception as error:
                    rows, note = [], f"{fetch} store: {error}"
                legacy_notes.append(note)
                for rank, row in enumerate(rows):
                    if not ctx.can_see({"confidentiality": row["confidentiality"]}):
                        continue
                    if window and not _mentions_period(row.get("text") or "", window):
                        continue               # the older stores carry no dates - a dated question needs the year in the text
                    candidates.append({**row, "rrf": 1.0 / (RRF_K + rank + 1), "legs": [row["source"]],
                                       "raw": {row["source"]: row["score"]}, "chunk_text": row["text"],
                                       "retrieval_score": 1.0 / (RRF_K + rank + 1), "element_type": row["kind"],
                                       "authority": 4 if row["source"] == "legacy_industrial" else 5,
                                       "status": "issued"})
            diagnostics["legacy"] = legacy_notes

        # ---- rerank, then weigh by authority and origin ------------------------
        candidates.sort(key=lambda c: -c["rrf"])
        pool = candidates[:max(top_k * 4, 40)]
        rerank_info: Dict[str, Any] = {}
        # The reranker judges a passage WITH its document title and section - a
        # step list means nothing without "Hydrogen Generation Unit start-up
        # procedure" above it. The evidence text itself is restored afterwards.
        for c in pool:
            c["_body"] = c["chunk_text"]
            header = " > ".join(x for x in (c.get("title"), c.get("section_path")) if x)
            c["chunk_text"] = (header + "\n" + c["_body"]) if header else c["_body"]
        try:
            from industrial.reranker import rerank
            pool, rerank_info = rerank(question, pool, dense_scores=[c["rrf"] for c in pool])
        except Exception as error:
            rerank_info = {"backend": "none", "detail": str(error)}
        for c in pool:
            c["chunk_text"] = c.pop("_body", c["chunk_text"])
        diagnostics["rerank"] = {k: rerank_info.get(k) for k in ("backend", "detail", "scored")}

        if pool:
            # min-max scaling: a negative cross-encoder logit must not turn a
            # boost into a penalty
            scores = [float(c.get("rerank_score", 0.0)) for c in pool]
            low, span = min(scores), (max(scores) - min(scores)) or 1.0
            max_rrf = max(c["rrf"] for c in pool) or 1.0
            identifiers = _identifiers(question)
            for c in pool:
                scaled = (float(c.get("rerank_score", 0.0)) - low) / span
                relevance = 0.6 * scaled + 0.4 * (c["rrf"] / max_rrf)
                weight = authority.weight(c.get("authority") or 5)
                origin = ORIGIN_WEIGHT.get(c.get("origin") or "company", 0.8)
                c["final"] = relevance * (0.55 + 0.45 * weight) * origin
                if c.get("document_type") in INTENT_TYPES.get(plan.get("primary") or "", ()):
                    c["final"] *= 1.8
                if _only_questions(c.get("chunk_text") or ""):
                    c["final"] *= 0.4            # a list of questions matches the question, but answers nothing
                if identifiers and any(p.search(c.get("chunk_text") or "") for p in identifiers):
                    c["final"] *= 1.35           # 'L3', 'U5', 'C-501' named in both - the specific passage
            pool.sort(key=lambda c: -c["final"])

        evidence: List[EvidenceItem] = []
        from security.injection import scan
        for c in pool[:top_k]:
            flags = []
            try:
                result = scan(c.get("chunk_text") or "")
                if result.flagged:
                    flags.append("instruction-like text - treated as data: " + ", ".join(result.kinds))
            except Exception:
                pass
            if c.get("origin") in ("sample", "demo"):
                flags.append(f"{c['origin'].upper()} data - not company records")
            if c.get("is_inference"):
                flags.append("AI description of an image - not document text")
            if c.get("status") == "draft":
                flags.append("DRAFT document")
            item = EvidenceItem(
                evidence_id=f"E{len(evidence) + 1}", node_id=c["node_id"], document_id=c["document_id"],
                title=c.get("title") or "", text=c.get("chunk_text") or "", source=c.get("source") or "fabric",
                filename=c.get("filename") or "", page=c.get("page"), section_path=c.get("section_path") or "",
                heading=c.get("heading"), date=c.get("date") or c.get("doc_date"), year=c.get("year"),
                document_type=c.get("document_type") or "", status=c.get("status") or "",
                authority=int(c.get("authority") or 5), authority_label=authority.describe(c.get("authority") or 5),
                classification=c.get("confidentiality") or "internal", origin=c.get("origin") or "company",
                kind=c.get("kind") or "paragraph", source_hash=c.get("source_hash") or "",
                parent_context=_parent_context(conn, c) if c.get("source", "fabric") == "fabric" else "",
                scores={"rrf": round(c["rrf"], 5), "rerank": round(float(c.get("rerank_score", 0.0)), 4),
                        "final": round(c.get("final", 0.0), 4), **{k: round(float(v), 4) for k, v in c["raw"].items()}},
                score=c.get("final", 0.0), legs=c["legs"], flags=flags)
            evidence.append(item)

    diagnostics["elapsed_s"] = round(time.time() - started, 3)
    diagnostics["candidates"] = len(candidates)
    return RetrievalResult(evidence, facts, edges, entities, diagnostics)


# --------------------------------------------------------- explorer / viewer ---

def documents(ctx: AccessContext, filters: Optional[Dict[str, Any]] = None, query: str = "",
              limit: int = 200, offset: int = 0) -> Dict[str, Any]:
    """Document Explorer: only what the caller may see; filters by year, type,
    department, origin, status and free text."""
    filters = dict(filters or {})
    with store.connection() as conn:
        access_sql, params = ctx.clause("d")
        sql = f"SELECT d.* FROM kf_documents d WHERE ({access_sql})"
        if filters.get("year_from"):
            sql += " AND d.year >= ?"
            params.append(int(filters["year_from"]))
        if filters.get("year_to"):
            sql += " AND d.year <= ?"
            params.append(int(filters["year_to"]))
        filter_sql, filter_params = _filter_clause(filters)
        sql += f" AND ({filter_sql})"
        params += filter_params
        if query:
            match = store.fts_query(query)
            if match:
                sql += " AND d.document_id IN (SELECT document_id FROM kf_fts WHERE kf_fts MATCH ?)"
                params.append(match)
        total = conn.execute(f"SELECT COUNT(*) FROM ({sql})", params).fetchone()[0]
        sql += " ORDER BY d.year DESC, d.title LIMIT ? OFFSET ?"
        rows = [dict(r) for r in conn.execute(sql, params + [limit, offset])]
    for row in rows:
        row["metadata"] = store.loads(row.get("metadata"), {})
        row["authority_label"] = authority.describe(row.get("authority") or 5)
    return {"total": total, "documents": rows}


def evidence_detail(ctx: AccessContext, node_id: str) -> Dict[str, Any]:
    """Evidence Viewer: the node, its document, its parent chain, its neighbours,
    its table (cells) if any, the entities it mentions - or a refusal that reveals
    nothing about a document the caller may not see."""
    with store.connection() as conn:
        node = conn.execute("SELECT n.*, d.* FROM kf_nodes n JOIN kf_documents d ON d.document_id = n.document_id "
                            "WHERE n.node_id = ?", (node_id,)).fetchone()
        if not node or not ctx.can_see(dict(node)):
            return {"error": "no such evidence, or not visible to your role"}
        node = dict(node)
        chain, parent_id = [], node.get("parent_id")
        while parent_id:
            parent = conn.execute("SELECT node_id, parent_id, kind, heading, text FROM kf_nodes WHERE node_id = ?",
                                  (parent_id,)).fetchone()
            if not parent:
                break
            chain.insert(0, {"node_id": parent["node_id"], "kind": parent["kind"],
                             "heading": parent["heading"] or (parent["text"] or "")[:80]})
            parent_id = parent["parent_id"]
        siblings = [dict(r) for r in conn.execute(
            "SELECT node_id, kind, ordinal, text FROM kf_nodes WHERE parent_id = ? AND ordinal BETWEEN ? AND ? ORDER BY ordinal",
            (node.get("parent_id"), node["ordinal"] - 2, node["ordinal"] + 2))]
        table = conn.execute("SELECT * FROM kf_tables WHERE node_id = ?", (node_id,)).fetchone()
        cells = []
        if table:
            cells = [dict(r) for r in conn.execute("SELECT row_index, col_index, header, value_text, value_num, unit "
                                                   "FROM kf_cells WHERE table_id = ? ORDER BY row_index, col_index LIMIT 4000",
                                                   (table["table_id"],))]
        mentions = [dict(r) for r in conn.execute(
            "SELECT m.entity_id, m.surface, e.type, e.canonical_name FROM kf_mentions m "
            "JOIN kf_entities e ON e.entity_id = m.entity_id WHERE m.node_id = ?", (node_id,))]
    node["metadata"] = store.loads(node.get("metadata"), {})
    node["authority_label"] = authority.describe(node.get("authority") or 5)
    return {"node": node, "chain": chain, "siblings": siblings,
            "table": {**dict(table), "headers": store.loads(table["headers"], []), "units": store.loads(table["units"], [])}
            if table else None, "cells": cells, "entities": mentions}
