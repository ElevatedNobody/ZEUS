"""
The company knowledge graph: typed entities, typed relations, and for every
relation the sentence that states it.

Relations are extracted by explicit phrasing only ("failed due to",
"supplied by", "located at", ...). Two entities that merely share a sentence
get a weak RELATED_TO (confidence 0.3) - never a causal edge. Nothing is
inferred from similarity.
"""

from __future__ import annotations

import re
from collections import deque
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from knowledge_fabric import store
from knowledge_fabric.entities import Extractor, Mention

RELATIONS = ("OWNS", "OPERATES", "LOCATED_AT", "PART_OF", "DEPENDS_ON", "MAINTAINED_BY",
             "FAILED_DUE_TO", "INSPECTED_BY", "USED_IN", "SUPPLIED_BY", "AFFECTED", "CAUSED",
             "FOLLOWED_BY", "APPROVED_BY", "CHANGED_TO", "RELATED_TO")

# (trigger regex, relation, direction) - direction "forward": left -> right,
# "reverse": right -> left (e.g. "A was caused by B" -> B CAUSED A).
_PATTERNS: List[Tuple[re.Pattern, str, str]] = [
    (re.compile(r"\b(?:is|are|was|were)?\s*(?:located|situated|installed)\s+(?:at|in)\b", re.I), "LOCATED_AT", "forward"),
    (re.compile(r"\b(?:is|are|was|were)?\s*(?:a\s+)?part\s+of\b", re.I), "PART_OF", "forward"),
    (re.compile(r"\b(?:owns|owned)\b(?!\s+by)", re.I), "OWNS", "forward"),
    (re.compile(r"\b(?:is|are|was|were)\s+owned\s+by\b", re.I), "OWNS", "reverse"),
    (re.compile(r"\b(?:operates|operated|runs)\b(?!\s+by)", re.I), "OPERATES", "forward"),
    (re.compile(r"\b(?:is|are|was|were)\s+operated\s+by\b", re.I), "OPERATES", "reverse"),
    (re.compile(r"\bdepends?\s+on\b|\bdependent\s+on\b|\brelies\s+on\b", re.I), "DEPENDS_ON", "forward"),
    (re.compile(r"\b(?:is|are|was|were)?\s*maintained\s+by\b", re.I), "MAINTAINED_BY", "forward"),
    (re.compile(r"\bfail(?:ed|ure|s)?\s+(?:due\s+to|because\s+of|owing\s+to|as\s+a\s+result\s+of|caused\s+by)\b", re.I), "FAILED_DUE_TO", "forward"),
    (re.compile(r"\b(?:is|are|was|were)?\s*inspected\s+by\b", re.I), "INSPECTED_BY", "forward"),
    (re.compile(r"\b(?:is|are|was|were)?\s*used\s+in\b", re.I), "USED_IN", "forward"),
    (re.compile(r"\b(?:is|are|was|were)?\s*(?:supplied|provided|manufactured|procured)\s+(?:by|from)\b", re.I), "SUPPLIED_BY", "forward"),
    (re.compile(r"\baffected\b|\bimpacted\b", re.I), "AFFECTED", "forward"),
    (re.compile(r"\b(?:caused|led\s+to|resulted\s+in|triggered)\b(?!\s+by)", re.I), "CAUSED", "forward"),
    (re.compile(r"\b(?:was|were|is|are)\s+(?:caused|triggered)\s+by\b|\bdue\s+to\b|\bbecause\s+of\b|\bowing\s+to\b", re.I), "CAUSED", "reverse"),
    (re.compile(r"\b(?:was|were)?\s*followed\s+by\b", re.I), "FOLLOWED_BY", "forward"),
    (re.compile(r"\b(?:is|are|was|were)?\s*approved\s+by\b", re.I), "APPROVED_BY", "forward"),
    (re.compile(r"\b(?:was|were)?\s*(?:changed|renamed|converted|upgraded|replaced)\s+(?:to|with|into|by)\b", re.I), "CHANGED_TO", "forward"),
]

_SENTENCE = re.compile(r"(?<=[.!?;])\s+(?=[A-Z0-9\"(])|\n+")


def sentences(text: str) -> List[Tuple[int, str]]:
    out, position = [], 0
    for part in _SENTENCE.split(text or ""):
        start = (text or "").find(part, position)
        if part.strip():
            out.append((max(start, 0), part))
        position = max(start, 0) + len(part)
    return out


_CAUSE_PHRASE = re.compile(r"^\s*(?:the\s+|a\s+|an\s+|excessive\s+|severe\s+)?"
                           r"([a-z][a-z\-]*(?:\s+[a-z][a-z\-]*){0,4}?)"
                           r"(?=\s*(?:[,.;:]|$|\s+(?:and|which|that|in|at|on|during|after|before|leading|resulting|causing|of the)\b))",
                           re.IGNORECASE)
_CAUSAL = {"FAILED_DUE_TO", "CAUSED"}
# "compressor C-501 in Unit 5 failed ..." - the equipment is what failed, not the unit
_SUBJECT_PREFERS_ASSET = {"FAILED_DUE_TO", "MAINTAINED_BY", "INSPECTED_BY", "SUPPLIED_BY"}
_ASSET_ORDER = {"EQUIPMENT": 0, "UNIT": 1, "PLANT": 2, "REFINERY": 3, "PROJECT": 4}


def _cause_mention(sentence: str, after: int) -> Optional[Mention]:
    """'... failed due to bearing wear, which ...' -> CAUSE 'bearing wear'.
    A cause does not have to be a named entity to be worth recording, but it
    must be a short noun phrase directly after the causal phrase."""
    m = _CAUSE_PHRASE.match(sentence[after:])
    if not m:
        return None
    phrase = m.group(1).strip().lower()
    words = phrase.split()
    if not words or len(words) > 5 or words[0] in ("the", "it", "this", "that", "which", "a", "an"):
        return None
    start = after + m.start(1)
    from knowledge_fabric.entities import slug
    return Mention(f"CAUSE:{slug(phrase)}", "CAUSE", phrase, sentence[start:start + len(m.group(1))],
                   start, start + len(m.group(1)), 0.7, "cause_phrase")


def extract_relations(text: str, extractor: Extractor, location: Optional[str] = None
                      ) -> List[Dict[str, Any]]:
    """Relations stated in `text`, each with the sentence that states it."""
    found: List[Dict[str, Any]] = []
    for offset, sentence in sentences(text):
        mentions = [m for m in extractor.mentions(sentence, location) if m.type != "YEAR"]
        if not mentions:
            continue
        explicit = False
        used_spans: List[Tuple[int, int]] = []
        for pattern, relation, direction in _PATTERNS:
            for trigger in pattern.finditer(sentence):
                span = (trigger.start(), trigger.end())
                # "failed due to" must not ALSO count as a bare "due to"
                if any(s <= span[0] < e or s < span[1] <= e for s, e in used_spans):
                    continue
                clause_start = max(sentence.rfind(",", 0, trigger.start()), sentence.rfind(";", 0, trigger.start())) + 1
                left = [m for m in mentions if m.end <= trigger.start() and m.start >= clause_start] or \
                       [m for m in mentions if m.end <= trigger.start()]
                right = [m for m in mentions if m.start >= trigger.end()]
                if relation in _CAUSAL and left and (not right or right[0].start > trigger.end() + 3):
                    cause = _cause_mention(sentence, trigger.end())
                    if cause is not None:
                        right = [cause]
                if not left or not right:
                    continue
                if relation in _SUBJECT_PREFERS_ASSET:
                    ranked = sorted(left, key=lambda m: (_ASSET_ORDER.get(m.type, 9), -m.start))
                    a = ranked[0]
                else:
                    a = left[-1]
                b = right[0]
                used_spans.append(span)
                if a.entity_id == b.entity_id:
                    continue
                src, dst = (a, b) if direction == "forward" else (b, a)
                found.append({"src": src, "relation": relation, "dst": dst, "confidence": 0.75,
                              "sentence": sentence.strip()[:400]})
                explicit = True
        if not explicit:
            unique = {}
            for m in mentions:
                unique.setdefault(m.entity_id, m)
            ordered = list(unique.values())
            for i in range(len(ordered)):
                for j in range(i + 1, min(len(ordered), i + 3)):
                    found.append({"src": ordered[i], "relation": "RELATED_TO", "dst": ordered[j],
                                  "confidence": 0.3, "sentence": sentence.strip()[:400]})
    return found


def store_relations(conn, relations: Iterable[Dict[str, Any]], node_id: str, document_id: str,
                    valid_from: Optional[str] = None) -> int:
    count = 0
    for rel in relations:
        src: Mention = rel["src"]
        dst: Mention = rel["dst"]
        for mention in (src, dst):
            conn.execute("INSERT OR IGNORE INTO kf_entities(entity_id, type, canonical_name, attributes, status, created_at) "
                         "VALUES (?, ?, ?, '{}', 'active', ?)",
                         (mention.entity_id, mention.type, mention.canonical, store.now()))
        conn.execute("INSERT INTO kf_relations(src, relation, dst, evidence_node, document_id, confidence, valid_from, source) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?, 'pattern')",
                     (src.entity_id, rel["relation"], dst.entity_id, node_id, document_id,
                      rel["confidence"], valid_from))
        count += 1
    return count


# --------------------------------------------------------------- queries ----

def neighbours(conn, entity_ids: Iterable[str], relations: Optional[Iterable[str]] = None,
               min_confidence: float = 0.5, allowed_documents: Optional[Set[str]] = None,
               limit: int = 200) -> List[Dict[str, Any]]:
    """Edges touching the given entities. `allowed_documents` is the access
    filter: an edge whose evidence lives in a document the caller may not see
    is not returned (no metadata leak through the graph)."""
    ids = list(entity_ids)
    if not ids:
        return []
    sql = (f"SELECT r.*, se.canonical_name AS src_name, se.type AS src_type, "
           f"de.canonical_name AS dst_name, de.type AS dst_type FROM kf_relations r "
           f"JOIN kf_entities se ON se.entity_id = r.src JOIN kf_entities de ON de.entity_id = r.dst "
           f"WHERE (r.src IN ({store.placeholders(ids)}) OR r.dst IN ({store.placeholders(ids)})) "
           f"AND r.confidence >= ?")
    params: List[Any] = ids + ids + [min_confidence]
    rels = list(relations or [])
    if rels:
        sql += f" AND r.relation IN ({store.placeholders(rels)})"
        params += rels
    sql += " ORDER BY r.confidence DESC LIMIT ?"
    params.append(limit * 3)
    out = []
    for row in conn.execute(sql, params):
        if allowed_documents is not None and row["document_id"] not in allowed_documents:
            continue
        out.append(dict(row))
        if len(out) >= limit:
            break
    return out


def paths(conn, start: str, goal_type: Optional[str] = None, max_hops: int = 3,
          min_confidence: float = 0.5, allowed_documents: Optional[Set[str]] = None
          ) -> List[List[Dict[str, Any]]]:
    """Multi-hop paths from `start` to entities of `goal_type` (breadth first)."""
    results: List[List[Dict[str, Any]]] = []
    queue = deque([(start, [])])
    seen = {start}
    while queue:
        current, path = queue.popleft()
        if len(path) >= max_hops:
            continue
        for edge in neighbours(conn, [current], min_confidence=min_confidence,
                               allowed_documents=allowed_documents, limit=60):
            nxt = edge["dst"] if edge["src"] == current else edge["src"]
            if nxt in seen:
                continue
            seen.add(nxt)
            new_path = path + [edge]
            nxt_type = edge["dst_type"] if edge["src"] == current else edge["src_type"]
            if goal_type is None or nxt_type == goal_type:
                results.append(new_path)
            queue.append((nxt, new_path))
        if len(results) >= 50:
            break
    return results


def describe_edge(edge: Dict[str, Any]) -> str:
    return f"{edge['src_name']} -{edge['relation']}-> {edge['dst_name']}"
