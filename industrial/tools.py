"""
Industrial tools registered into the EXISTING ZEUS tool registry.

Nothing here replaces an existing tool. The original RAG tools
(index_folder / ask_knowledge_base / list_knowledge_sources / forget_document /
clear_knowledge_base) keep working over the original document_chunks table.
These new tools work over the industrial knowledge base, which additionally
preserves document, page, section, revision, equipment, extraction method and
OCR confidence for every answer.

Every tool goes through the same registry, so security confirmation, timeouts
and the audit log apply exactly as they do everywhere else in ZEUS.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from core.logger import get_logger
from core.security_manager import RiskLevel, security_manager
from core.tool_registry import registry

logger = get_logger("industrial.tools")


def _safe(path_str: str) -> Optional[Path]:
    p = Path(path_str).expanduser()
    if not security_manager.validate_path(str(p)):
        return None
    return p


# ------------------------------------------------------------------- status

@registry.register("industrial_status",
                   "Report the industrial workbench status: OCR engines, document "
                   "processors, models, knowledge base size and readiness",
                   RiskLevel.SAFE, timeout=60.0)
def industrial_status():
    from industrial import doc_pipeline, reranker
    from industrial.corpus import corpus_stats
    from industrial.knowledge_base import knowledge_base
    from industrial.schemas import list_schemas
    from industrial.taxonomy import get_taxonomy

    kb = knowledge_base.stats()
    pipeline = doc_pipeline.pipeline_status()
    return {
        "pdf_engine": pipeline["pdf_engine"],
        "ocr_engines": ", ".join(pipeline["ocr"]["available"]) or "none",
        "layout_analyser": pipeline["ocr"]["layout_analyser"],
        "vision_ready": pipeline["vision_model_ready"],
        "embedding_model": kb.get("embedding_model"),
        "embedding_ready": kb.get("embedding_ready"),
        "reranker": reranker.status()["backend"],
        "knowledge_base": f"{kb.get('documents', 0)} documents / {kb.get('chunks', 0)} chunks "
                          f"({kb.get('embedded_chunks', 0)} embedded)",
        "corpus_domains": corpus_stats().get("domains_on_disk", 0),
        "document_schemas": len(list_schemas()),
        "taxonomy": get_taxonomy().stats(),
    }


# ---------------------------------------------------------------- ingestion

@registry.register("ingest_industrial_document",
                   "Ingest one industrial document (PDF/DOCX/XLSX/PPTX/CSV/TXT/image, "
                   "including scanned) into the industrial knowledge base with full "
                   "page/section provenance",
                   RiskLevel.LOW_RISK, params={"path": "file to ingest"}, timeout=900.0)
def ingest_industrial_document(path: str, use_vision: bool = False):
    from industrial.doc_pipeline import ExtractionOptions
    from industrial.knowledge_base import knowledge_base

    target = _safe(path)
    if target is None:
        return {"error": f"Access to '{path}' is blocked by security policy"}
    if not target.is_file():
        return {"error": f"'{path}' is not a file"}

    result = knowledge_base.ingest_document(
        target, options=ExtractionOptions(vision=bool(use_vision)))
    return {k: v for k, v in result.items() if k not in ("schema_match",)}


@registry.register("ingest_industrial_folder",
                   "Ingest every supported document in a folder into the industrial "
                   "knowledge base (recursive)",
                   RiskLevel.LOW_RISK, params={"path": "folder to ingest"}, timeout=3600.0)
def ingest_industrial_folder(path: str, use_vision: bool = False, max_files: int = 300):
    from industrial.doc_pipeline import (SUPPORTED_EXTENSIONS, UNSTRUCTURED_EXTENSIONS,
                                         ExtractionOptions)
    from industrial.knowledge_base import knowledge_base

    folder = _safe(path)
    if folder is None:
        return {"error": f"Access to '{path}' is blocked by security policy"}
    if not folder.is_dir():
        return {"error": f"'{path}' is not a directory"}

    allowed = SUPPORTED_EXTENSIONS | UNSTRUCTURED_EXTENSIONS
    options = ExtractionOptions(vision=bool(use_vision))
    ok = failed = skipped = chunks = 0
    problems: List[str] = []

    for f in sorted(folder.rglob("*")):
        if ok + failed + skipped >= max_files:
            break
        if not f.is_file() or f.suffix.lower() not in allowed:
            continue
        result = knowledge_base.ingest_document(f, options=options)
        if result["status"] == "ok":
            ok += 1
            chunks += result.get("chunks", 0)
        elif result["status"] == "skipped":
            skipped += 1
        else:
            failed += 1
            problems.append(f"{f.name}: {result.get('detail', 'failed')}")

    return {"ingested": ok, "skipped": skipped, "failed": failed, "chunks": chunks,
            "problems": problems[:10]}


@registry.register("ingest_industrial_knowledge_corpus",
                   "Ingest the built-in local industrial history/knowledge corpus "
                   "(knowledge/industrial_history) into the knowledge base",
                   RiskLevel.LOW_RISK, timeout=3600.0)
def ingest_industrial_knowledge_corpus():
    from industrial.corpus import ingest_corpus
    result = ingest_corpus()
    return {"files": result["files"], "ok": result["ok"], "chunks": result["chunks"],
            "embedded": result["embedded"], "duration_s": result["duration_s"]}


# ---------------------------------------------------------------- retrieval

@registry.register("ask_industrial_kb",
                   "Answer a question from the industrial knowledge base with enforced "
                   "citations - names the document, page, section, revision and equipment "
                   "behind every statement",
                   RiskLevel.SAFE, params={"query": "the question to answer"}, timeout=180.0)
def ask_industrial_kb(query: str, top_k: int = 6, only_my_documents: bool = False):
    from industrial.extraction import CLASS_USER_DOCUMENT
    from industrial.knowledge_base import knowledge_base

    result = knowledge_base.answer(
        query, top_k=top_k,
        document_class=CLASS_USER_DOCUMENT if only_my_documents else None)
    if result.get("error"):
        return {"error": result["error"]}

    sources = result.get("sources", {})
    out = {
        "answer": result["answer"],
        "grounded": result["grounded"],
        "documents": ", ".join(sources.get("documents", [])) or "none",
        "pages": ", ".join(sources.get("pages", [])) or "n/a",
        "sections": ", ".join(sources.get("sections", [])[:6]) or "n/a",
        "revisions": ", ".join(sources.get("revisions", [])) or "n/a",
        "equipment": ", ".join(sources.get("equipment", [])) or "n/a",
        "evidence_used": result["citation_report"]["valid_citations"],
        "reranked_by": (result.get("retrieval", {}).get("reranker") or {}).get("backend"),
    }
    report = result["citation_report"]
    if report["invalid_citations"] or report["invented_filenames"]:
        out["citation_warning"] = (
            f"removed invented references: {report['invalid_citations']} "
            f"{report['invented_filenames']}")
    if report["uncited_claims"]:
        out["uncited_statements"] = report["uncited_claims"]
    return out


@registry.register("search_industrial_evidence",
                   "Retrieve and rank raw evidence from the industrial knowledge base "
                   "without generating an answer - shows exactly which chunks would be used",
                   RiskLevel.SAFE, params={"query": "what to search for"}, timeout=120.0)
def search_industrial_evidence(query: str, top_k: int = 6, equipment: str = ""):
    from industrial.knowledge_base import knowledge_base
    evidences, info = knowledge_base.search(query, top_k=top_k,
                                            equipment=equipment or None)
    if not evidences:
        return {"error": info.get("error") or info.get("note") or "no matching evidence"}
    return {
        "matches": [
            f"[{e.evidence_id}] {e.citation()} | {e.claim_type} | score {e.score:.3f} | "
            f"{e.chunk_text[:180].strip()}..." for e in evidences],
        "reranked_by": (info.get("reranker") or {}).get("backend"),
        "candidates_considered": info.get("candidates"),
    }


@registry.register("list_industrial_documents",
                   "List documents held in the industrial knowledge base",
                   RiskLevel.SAFE, timeout=30.0)
def list_industrial_documents_tool():
    from industrial.knowledge_base import knowledge_base
    docs = knowledge_base.list_documents()
    if not docs:
        return {"documents": "The industrial knowledge base is empty."}
    return {"documents": [
        f"{d['filename']} [{d['document_type']}/{d['document_class']}] "
        f"{d['chunk_count']} chunks"
        + (f" rev {d['revision']}" if d.get("revision") else "")
        + (f" equipment {d['equipment']}" if d.get("equipment") else "")
        for d in docs[:60]], "total": len(docs)}


@registry.register("forget_industrial_document",
                   "Remove one document (and its chunks) from the industrial knowledge base",
                   RiskLevel.LOW_RISK, params={"document_id": "document id to remove"})
def forget_industrial_document(document_id: str):
    from industrial.knowledge_base import knowledge_base
    removed = knowledge_base.forget(document_id)
    return {"removed_chunks": removed} if removed else {"error": "no such document id"}


@registry.register("clear_industrial_knowledge_base",
                   "Clear the entire industrial knowledge base (documents, chunks, assets). "
                   "Does not touch the original ZEUS knowledge base.",
                   RiskLevel.MEDIUM_RISK, timeout=60.0)
def clear_industrial_knowledge_base_tool():
    from industrial.knowledge_base import knowledge_base
    return {"cleared_chunks": knowledge_base.clear()}


# ------------------------------------------------------- document inspection

@registry.register("classify_industrial_document",
                   "Identify which industrial document type a file is (inspection report, "
                   "SOP, P&ID, shift log, work order, ...) without ingesting it",
                   RiskLevel.SAFE, params={"path": "file to classify"}, timeout=300.0)
def classify_industrial_document(path: str):
    from industrial.doc_pipeline import ExtractionOptions, extract_document
    from industrial.schemas import classify_document

    target = _safe(path)
    if target is None or not target.is_file():
        return {"error": f"'{path}' is not an accessible file"}
    doc = extract_document(target, ExtractionOptions(images=False, tables=False, vision=False,
                                                     max_pages=6))
    if doc.error and not doc.elements:
        return {"error": doc.error}
    match = classify_document(doc)
    return {"document_type": match.schema_id, "name": match.name,
            "confidence": match.confidence, "score": match.score,
            "matched_keywords": ", ".join(match.matched_keywords[:8]),
            "pages_examined": doc.page_count, "scanned": doc.is_scanned,
            "read_by": ", ".join(doc.extraction_methods)}


@registry.register("extract_industrial_document",
                   "Extract the structured fields of an industrial document against its "
                   "schema, with the page and method behind every value - does not store it",
                   RiskLevel.SAFE, params={"path": "file to extract"}, timeout=900.0)
def extract_industrial_document(path: str, use_vision: bool = False):
    from industrial.doc_pipeline import ExtractionOptions
    from industrial.extraction import extract_and_chunk

    target = _safe(path)
    if target is None or not target.is_file():
        return {"error": f"'{path}' is not an accessible file"}

    norm, chunks, raw = extract_and_chunk(
        target, options=ExtractionOptions(vision=bool(use_vision)))
    if norm is None:
        return {"error": raw.error if raw else "extraction failed"}

    fields = norm.metadata.get("schema_fields", {})
    validation = norm.metadata.get("schema_validation", [])
    return {
        "document_type": norm.document_type,
        "title": norm.title, "revision": norm.revision, "date": norm.doc_date,
        "author": norm.author, "equipment": norm.equipment, "plant_unit": norm.plant_unit,
        "pages": norm.page_count, "chunks": len(chunks),
        "read_by": norm.extraction_method,
        "ocr_confidence": norm.metadata.get("mean_ocr_confidence"),
        "fields": {k: (v.get("value") if isinstance(v, dict) else v) for k, v in fields.items()},
        "field_evidence": {k: f"page {v.get('page_number')} via {v.get('method')}"
                           for k, v in fields.items() if isinstance(v, dict)},
        "validation_failures": [f"{r['field']}: {r['rule']} - {r['detail']}"
                                for r in validation if r["status"] == "fail"],
    }


# ------------------------------------------------------------------ taxonomy

@registry.register("industrial_term",
                   "Look up an industrial term or abbreviation in the local taxonomy "
                   "(P&ID, FCC, DCS, HAZOP, CML, RBI, ...)",
                   RiskLevel.SAFE, params={"term": "term or abbreviation"})
def industrial_term(term: str):
    from industrial.taxonomy import get_taxonomy
    tax = get_taxonomy()
    expansion = tax.expand_abbreviation(term.strip())
    spec = tax.describe(term)
    if not spec and not expansion:
        hits = tax.detect_terms(term)
        if not hits:
            return {"error": f"'{term}' is not in the industrial taxonomy"}
        spec = tax.describe(hits[0]["term"])
    out: Dict[str, Any] = {}
    if expansion:
        out["abbreviation"] = f"{term.strip()} = {expansion}"
    if spec:
        out.update({"term": spec["term"], "category": spec.get("category"),
                    "definition": spec.get("definition"),
                    "also_called": ", ".join(spec.get("aliases", [])),
                    "related": ", ".join(spec.get("related", []))})
    return out


# -------------------------------------------------------------------- agents

@registry.register("list_industrial_agents",
                   "List the industrial agents and whether each one's environment is ready",
                   RiskLevel.SAFE, timeout=90.0)
def list_industrial_agents_tool():
    from industrial.agent_interfaces import preflight_all
    result = preflight_all()
    return {"agents": [
        f"{r['name']} [{'implemented' if r['implemented'] else 'prepared'}] - "
        f"environment {'READY' if r['environment_ready'] else 'BLOCKED'}"
        + (f" ({'; '.join(r['missing'][:2])})" if r["missing"] else "")
        for r in result["agents"].values()],
        "first_agent_to_implement": result["first_agent"]}


@registry.register("industrial_agent_status",
                   "Show one industrial agent's workflow, required models/tools and what "
                   "is missing before it can run",
                   RiskLevel.SAFE, params={"agent_id": "inspection, industrial_document, "
                                                       "engineering, industrial_coding, "
                                                       "calculation or report_approval"},
                   timeout=90.0)
def industrial_agent_status(agent_id: str):
    from industrial.agent_interfaces import get_agent
    agent = get_agent(agent_id.strip().lower())
    if agent is None:
        return {"error": f"unknown industrial agent '{agent_id}'"}
    pre = agent.preflight()
    return {"agent": pre["name"], "implemented": pre["implemented"],
            "environment_ready": pre["environment_ready"],
            "workflow": " -> ".join(pre["workflow"]),
            "missing": "; ".join(pre["missing"]) or "nothing",
            "model_slots": {k: f"{v['model']} ({'ok' if v['available'] else 'missing'})"
                            for k, v in pre["detail"].get("model_slots", {}).items()}}


@registry.register("industrial_model_health",
                   "Check every registered local model against the Ollama library and "
                   "report capability, size, quantisation and health",
                   RiskLevel.SAFE, timeout=120.0)
def industrial_model_health(probe: bool = False):
    from industrial.model_registry import get_registry
    reg = get_registry()
    health = reg.health_check(probe=bool(probe))
    return {
        "ollama": health["ollama_daemon"],
        "present": f"{health['present']} / {len(reg.models)}",
        "missing": [m["model"] for m in health["models"] if m["health"] == "absent"][:12],
        "slots": {k: f"{v['status']} -> {v['resolved'] or 'none'}"
                  for k, v in health["slots"].items()},
        "unregistered_local_models": health["unregistered_models"][:10],
    }


def register_all() -> List[str]:
    """Called on import by core/tool_registry.py. Also wires the industrial
    agents into the existing agent registry."""
    try:
        from industrial.agent_interfaces import register_with_core_registry
        register_with_core_registry()
    except Exception as e:  # never break tool loading
        logger.warning(f"industrial agent registration skipped: {e}")
    return [t.name for t in registry.list_tools() if t.name.startswith("industrial_")
            or "industrial" in t.name]


register_all()
