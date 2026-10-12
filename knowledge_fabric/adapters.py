"""
Read-only adapters over the two RAG stores that existed before the fabric.

    industrial knowledge base   (industrial_documents / industrial_chunks)
    original document store     (document_chunks - index_folder / ask_knowledge_base)

Neither store is modified, migrated away or replaced (docs/RAG_MIGRATION.md).
Their passages join fabric retrieval as extra candidate lists, carry their own
provenance ("legacy_industrial" / "legacy_chunks"), and pass the same access
check as everything else: an industrial chunk keeps its confidentiality label;
the original store has none, so its passages are treated as "internal".
"""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.logger import get_logger

logger = get_logger("knowledge_fabric.adapters")


def industrial_kb(question: str, top_k: int = 6) -> Tuple[List[Dict[str, Any]], str]:
    """Top passages from the industrial knowledge base, or ([], reason)."""
    try:
        from industrial.knowledge_base import IndustrialKnowledgeBase
        kb = IndustrialKnowledgeBase()
        available, detail = kb.embedding_available()
        if not available:
            return [], f"industrial knowledge base not searched: {detail}"
        evidences, info = kb.search(question, top_k=top_k)
    except Exception as error:
        return [], f"industrial knowledge base unavailable: {error}"
    out = []
    for ev in evidences:
        out.append({
            "node_id": f"legacy_industrial:{ev.document_id}:{ev.page_number or 0}:{abs(hash(ev.chunk_text)) % 10**8}",
            "document_id": f"legacy:{ev.document_id}", "title": ev.title or ev.filename,
            "filename": ev.filename, "text": ev.chunk_text, "page": ev.page_number,
            "section_path": ev.section or "", "heading": ev.heading, "date": ev.chunk_date,
            "year": int(ev.chunk_date[:4]) if ev.chunk_date and ev.chunk_date[:4].isdigit() else None,
            "document_type": ev.document_type or "user_document", "confidentiality": ev.confidentiality or "internal",
            "origin": "general_knowledge" if ev.document_class == "knowledge_corpus" else "company",
            "source": "legacy_industrial", "score": float(ev.score or 0.0), "kind": ev.element_type or "paragraph",
            "claim_type": ev.claim_type})
    return out, f"industrial knowledge base: {len(out)} passage(s) ({info.get('reranker', info.get('rerank_backend', ''))})"


def _tokens(text: str) -> List[str]:
    return [t for t in re.findall(r"[a-z0-9]{3,}", (text or "").lower())]


def original_store(question: str, top_k: int = 4) -> Tuple[List[Dict[str, Any]], str]:
    """The original `document_chunks` store. Cosine over its stored embeddings when
    an embedding model answers; otherwise a lexical score - never nothing silently."""
    try:
        from memory.database import get_all_chunks, init_db
        init_db()
        chunks = get_all_chunks()
    except Exception as error:
        return [], f"original document store unavailable: {error}"
    if not chunks:
        return [], "original document store is empty"
    scored: List[Tuple[float, Dict[str, Any]]] = []
    query_vector = None
    try:
        from core.capabilities import embedding_provider
        query_vector = embedding_provider.embed(question)
    except Exception:
        query_vector = None
    if query_vector:
        import json
        import numpy as np
        q = np.asarray(query_vector, dtype="float32")
        qn = float(np.linalg.norm(q)) or 1.0
        for chunk in chunks:
            try:
                v = np.asarray(json.loads(chunk["embedding"]), dtype="float32")
            except Exception:
                continue
            if v.size != q.size:
                continue
            scored.append((float(v @ q) / ((float(np.linalg.norm(v)) or 1.0) * qn), chunk))
        how = "semantic"
    else:
        terms = set(_tokens(question))
        if not terms:
            return [], "original document store: no searchable words in the question"
        for chunk in chunks:
            words = _tokens(chunk["chunk_text"])
            if not words:
                continue
            overlap = sum(1 for w in words if w in terms)
            if overlap:
                scored.append((overlap / math.sqrt(len(words)), chunk))
        how = "lexical (no embedding model reachable)"
    scored.sort(key=lambda item: -item[0])
    out = []
    for score, chunk in scored[:top_k]:
        path = chunk.get("source_path") or ""
        out.append({"node_id": f"legacy_chunks:{chunk.get('id')}", "document_id": f"legacy:{path}",
                    "title": Path(path).name, "filename": Path(path).name, "text": chunk["chunk_text"],
                    "page": None, "section_path": "", "heading": None, "date": None, "year": None,
                    "document_type": "user_document", "confidentiality": "internal", "origin": "company",
                    "source": "legacy_chunks", "score": score, "kind": "paragraph"})
    return out, f"original document store: {len(out)} passage(s), {how}"
