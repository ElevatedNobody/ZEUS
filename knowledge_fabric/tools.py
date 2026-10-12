"""
Registered tools for ZEUS company intelligence.

Every read tool works on the signed-in user's AccessContext - it can only
return what that user may see, and says nothing about what it withheld.
Who may CHANGE the corpus is set in security/policy.py (_ROLE_OVERRIDES):
ingesting and correcting entities need an engineer; removing documents,
reindexing, rebuilding the graph and department access need an administrator.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from core.logger import get_logger
from core.security_manager import RiskLevel, security_manager
from core.tool_registry import registry

logger = get_logger("knowledge_fabric.tools")


def _ctx():
    from knowledge_fabric.access import current
    return current()


def _refuse_unsigned(ctx) -> Optional[Dict[str, Any]]:
    if not ctx.authenticated:
        return {"ok": False, "error": "sign in first - company records are not searched without a session"}
    return None


def _checked_path(path: str) -> str:
    if not security_manager.validate_path(str(path)):
        raise PermissionError(f"access to '{path}' is blocked by security policy")
    return str(path)


def analysis_summary(obj) -> Dict[str, Any]:
    """What a caller (chat, UI, orchestrator) needs from an AnalysisObject."""
    return {"ok": True, "analysis_id": obj.analysis_id, "answer": obj.answer, "summary": obj.summary,
            "verified": obj.verified, "verification_state": obj.verification_state,
            "cited_origins": obj.cited_origins(), "reasoning": obj.reasoning, "validation": obj.validation, "narrative_source": obj.narrative_source,
            "intents": obj.plan.get("intents", []), "period": obj.plan.get("scope", {}).get("window"),
            "sources": [e["citation"] for e in obj.evidence],
            "evidence": [{"id": e["evidence_id"], "citation": e["citation"], "node_id": e["node_id"],
                          "authority": e["authority_label"], "origin": e["origin"], "flags": e.get("flags", [])}
                         for e in obj.evidence],
            "calculations": [{"id": c["id"], "name": c["name"], "value": c.get("value"), "unit": c.get("unit"),
                              "ok": c.get("ok", True)} for c in obj.calculations],
            "recommendations": [{"id": r["id"], "action": r["action"], "owner": r["owner"],
                                 "timeline": r["timeline"], "confidence": r["confidence"]}
                                for r in obj.recommendations],
            "gaps": obj.gaps, "contradictions": len(obj.contradictions), "stages": obj.stages,
            "artifacts": obj.artifacts, "memory_used": len(obj.memory_used)}


# ---------------------------------------------------------------- read ------

@registry.register("company_intelligence",
                   "Answer a question about the company's records (history, figures, trends, causes, "
                   "recommendations) with cited evidence and verified calculations",
                   RiskLevel.SAFE, params={"question": "the question"}, timeout=330.0)
def company_intelligence(question: str):
    from knowledge_fabric import service
    ctx = _ctx()
    refused = _refuse_unsigned(ctx)
    if refused:
        return refused
    obj = service.analyze(question, ctx=ctx, make_artifacts=False)
    result = analysis_summary(obj)
    if obj.plan.get("formats"):
        result["note"] = ("files were requested - create them with create_analysis_artifact "
                          f"(analysis {obj.analysis_id}, formats {', '.join(obj.plan['formats'])})")
    return result


@registry.register("knowledge_overview",
                   "What the knowledge base holds that you may see - documents by origin and type, "
                   "datasets and procedures - when a request names the knowledge base but asks nothing yet",
                   RiskLevel.SAFE, timeout=30.0)
def knowledge_overview():
    from knowledge_fabric import store
    ctx = _ctx()
    refused = _refuse_unsigned(ctx)
    if refused:
        return refused
    with store.connection() as conn:
        sql, params = ctx.clause("d")
        rows = [dict(r) for r in conn.execute(
            f"SELECT d.origin, d.document_type, COUNT(*) AS n FROM kf_documents d WHERE {sql} "
            f"GROUP BY d.origin, d.document_type ORDER BY n DESC", params)]
        procedures = [r["title"] for r in conn.execute(
            f"SELECT d.title FROM kf_documents d WHERE {sql} AND (d.document_type IN ('sop', 'procedure') "
            f"OR d.title LIKE '%SOP%' OR d.title LIKE '%Procedure%') ORDER BY d.title LIMIT 25", params)]
        datasets = [r[0] for r in conn.execute(
            f"SELECT DISTINCT json_extract(d.metadata, '$.dataset') FROM kf_documents d WHERE {sql} "
            f"AND json_extract(d.metadata, '$.dataset') IS NOT NULL", params)]
    by_origin: Dict[str, int] = {}
    by_type: Dict[str, int] = {}
    for r in rows:
        by_origin[r["origin"]] = by_origin.get(r["origin"], 0) + r["n"]
        by_type[r["document_type"] or "other"] = by_type.get(r["document_type"] or "other", 0) + r["n"]
    return {"ok": True, "documents": sum(by_origin.values()), "by_origin": by_origin,
            "by_type": dict(sorted(by_type.items(), key=lambda kv: -kv[1])[:12]),
            "procedures": procedures, "datasets": datasets,
            "next": "Ask what to read, e.g. 'Read the HSE SOP from the knowledge base' or "
                    "'According to the maintenance SOP, what is the procedure?'"}


@registry.register("knowledge_coverage",
                   "Which years, document types and departments the company records cover (1989-2026) and what is missing",
                   RiskLevel.SAFE, params={"first": "first year (optional)", "last": "last year (optional)"},
                   timeout=60.0)
def knowledge_coverage(first: str = "", last: str = ""):
    from knowledge_fabric import FIRST_YEAR, LAST_YEAR, coverage
    ctx = _ctx()
    refused = _refuse_unsigned(ctx)
    if refused:
        return refused
    return coverage.report(ctx, int(first or FIRST_YEAR), int(last or LAST_YEAR))


@registry.register("company_timeline", "Year-by-year timeline of the company records: documents, key figures, "
                   "incidents, projects and decisions", RiskLevel.SAFE,
                   params={"first": "first year (optional)", "last": "last year (optional)"}, timeout=60.0)
def company_timeline(first: str = "", last: str = ""):
    from knowledge_fabric import FIRST_YEAR, LAST_YEAR, coverage
    ctx = _ctx()
    refused = _refuse_unsigned(ctx)
    if refused:
        return refused
    return coverage.timeline(ctx, int(first or FIRST_YEAR), int(last or LAST_YEAR))


@registry.register("company_year_detail", "What is behind one year of the company timeline: documents with provenance, "
                   "typed sections, figures and dated events", RiskLevel.SAFE, params={"year": "the year"}, timeout=60.0)
def company_year_detail(year: str):
    from knowledge_fabric import coverage
    ctx = _ctx()
    refused = _refuse_unsigned(ctx)
    if refused:
        return refused
    try:
        wanted = int(str(year).strip())
    except ValueError:
        return {"ok": False, "error": f"'{year}' is not a year"}
    return {"ok": True, **coverage.year_detail(ctx, wanted)}


@registry.register("search_company_documents", "List company documents by text, year, type or department",
                   RiskLevel.SAFE, params={"query": "text (optional)", "year_from": "optional", "year_to": "optional",
                                           "document_type": "optional", "department": "optional"}, timeout=30.0)
def search_company_documents(query: str = "", year_from: str = "", year_to: str = "", document_type: str = "",
                             department: str = ""):
    from knowledge_fabric import retrieval
    ctx = _ctx()
    refused = _refuse_unsigned(ctx)
    if refused:
        return refused
    filters = {k: v for k, v in (("year_from", year_from), ("year_to", year_to), ("document_type", document_type),
                                 ("department", department)) if v}
    return retrieval.documents(ctx, filters, query=query, limit=200)


@registry.register("show_evidence", "Show one evidence passage with its document, section, table and entities",
                   RiskLevel.SAFE, params={"node_id": "evidence node id"}, timeout=20.0)
def show_evidence(node_id: str):
    from knowledge_fabric import retrieval
    ctx = _ctx()
    refused = _refuse_unsigned(ctx)
    if refused:
        return refused
    return retrieval.evidence_detail(ctx, node_id)


@registry.register("document_provenance", "Where a company document came from: file, hash, dates, authority, "
                   "ingestion history", RiskLevel.SAFE, params={"document_id": "document id"}, timeout=20.0)
def document_provenance(document_id: str):
    from knowledge_fabric import authority, store
    ctx = _ctx()
    refused = _refuse_unsigned(ctx)
    if refused:
        return refused
    with store.connection() as conn:
        row = conn.execute("SELECT * FROM kf_documents WHERE document_id = ?", (document_id,)).fetchone()
        if not row or not ctx.can_see(dict(row)):
            return {"ok": False, "error": "no such document, or not visible to your role"}
        log = [dict(r) for r in conn.execute("SELECT * FROM kf_ingestion_log WHERE document_id = ? "
                                             "ORDER BY at DESC LIMIT 20", (document_id,))]
        counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t} WHERE document_id = ?", (document_id,)).fetchone()[0]
                  for t in ("kf_nodes", "kf_tables", "kf_facts")}
    doc = dict(row)
    doc["metadata"] = store.loads(doc.get("metadata"), {})
    doc["authority_label"] = authority.describe(doc.get("authority") or 5)
    return {"ok": True, "document": doc, "counts": counts, "ingestion_log": log}


@registry.register("compare_document_revisions", "Compare two versions of a company document: changed figures "
                   "and text", RiskLevel.SAFE, params={"document_a": "document id", "document_b": "document id"},
                   timeout=30.0)
def compare_document_revisions(document_a: str, document_b: str):
    from knowledge_fabric import contradictions
    ctx = _ctx()
    refused = _refuse_unsigned(ctx)
    if refused:
        return refused
    return contradictions.compare_documents(ctx, document_a, document_b)


@registry.register("list_analyses", "The company-intelligence analyses you have run", RiskLevel.SAFE, timeout=10.0)
def list_analyses():
    from knowledge_fabric import analysis
    ctx = _ctx()
    refused = _refuse_unsigned(ctx)
    if refused:
        return refused
    return {"ok": True, "analyses": analysis.history(ctx.username or "")}


@registry.register("get_analysis", "The full saved company-intelligence analysis: series, calculations, drivers, "
                   "recommendations, plan, KPIs, risks, conflicts, gaps (the latest when no id is given)",
                   RiskLevel.SAFE, params={"analysis_id": "optional"}, timeout=20.0)
def get_analysis(analysis_id: str = ""):
    from knowledge_fabric import analysis
    ctx = _ctx()
    refused = _refuse_unsigned(ctx)
    if refused:
        return refused
    obj = analysis.load(analysis_id, ctx.username or "") if analysis_id else analysis.latest(ctx.username or "")
    if obj is None:
        return {"ok": False, "error": "no such analysis for your account"}
    return {"ok": True, **obj.to_dict(), "verification_state": obj.verification_state,
            "cited_origins": obj.cited_origins()}


@registry.register("company_registers",
                   "The registers you can see (lease, employee, tax, contract lists...) with their columns and a "
                   "data-quality check of every record against today's date",
                   RiskLevel.SAFE, timeout=60.0)
def company_registers():
    import datetime as dt
    from knowledge_fabric import registers, store
    ctx = _ctx()
    refused = _refuse_unsigned(ctx)
    if refused:
        return refused
    today = dt.date.today()
    out = []
    with store.connection() as conn:
        for reg in registers.accessible_registers(conn, ctx):
            records = registers._load_records(conn, reg["register_id"])
            findings = registers.quality(registers._reg_meta(reg), records, today)
            by_check: Dict[str, int] = {}
            for f in findings:
                by_check[f["check"]] = by_check.get(f["check"], 0) + 1
            out.append({"register": reg["name"], "document": reg.get("title"), "document_id": reg["document_id"],
                        "origin": reg.get("origin"), "confidentiality": reg.get("confidentiality"),
                        "records": len(records), "key": reg.get("key_column"),
                        "columns": [{"name": c["name"], "type": c["type"], "role": c.get("role"),
                                     "personal": bool(c.get("sensitive"))} for c in reg["columns"]],
                        "quality": {"checked_against": today.isoformat(), "findings": len(findings),
                                    "by_check": by_check, "examples": [f"{f['record_key']}: {f['detail']}" for f in findings[:5]]}})
    return {"ok": True, "registers": out,
            "note": "Registers are answered by the engine row by row - ask e.g. 'which leases expire in the next 90 days'."}


@registry.register("knowledge_fabric_status","Size and health of the company knowledge base you can see",
                   RiskLevel.SAFE, timeout=20.0)
def knowledge_fabric_status():
    from knowledge_fabric import store
    ctx = _ctx()
    refused = _refuse_unsigned(ctx)
    if refused:
        return refused
    with store.connection() as conn:
        sql, params = ctx.clause("d")
        docs = conn.execute(f"SELECT d.origin, COUNT(*) AS n FROM kf_documents d WHERE {sql} GROUP BY d.origin",
                            params).fetchall()
        nodes = conn.execute(f"SELECT COUNT(*) FROM kf_nodes n JOIN kf_documents d ON d.document_id = n.document_id "
                             f"WHERE {sql}", params).fetchone()[0]
        facts = conn.execute(f"SELECT COUNT(*) FROM kf_facts f JOIN kf_documents d ON d.document_id = f.document_id "
                             f"WHERE {sql}", params).fetchone()[0]
        vectors = conn.execute(f"SELECT COUNT(*) FROM kf_vectors v JOIN kf_nodes n ON n.node_id = v.node_id "
                               f"JOIN kf_documents d ON d.document_id = n.document_id WHERE {sql}", params).fetchone()[0]
    return {"ok": True, "database": str(store.db_path()), "documents_by_origin": {r["origin"]: r["n"] for r in docs},
            "passages": nodes, "table_facts": facts, "passages_with_vectors": vectors,
            "vector_search": "on" if vectors else "off - no embeddings stored (run RAG_REINDEX.bat / rag reembed)"}


@registry.register("ambiguous_entities", "Entities the records name inconsistently, waiting for a person to resolve",
                   RiskLevel.SAFE, timeout=20.0)
def ambiguous_entities():
    from knowledge_fabric import entities, store
    ctx = _ctx()
    refused = _refuse_unsigned(ctx)
    if refused:
        return refused
    with store.connection() as conn:
        return {"ok": True, "entities": entities.ambiguous_entities(conn)}


# ---------------------------------------------------------------- write -----

@registry.register("create_analysis_artifact", "Create a verified Excel / Word / PowerPoint / PDF / brief / action "
                   "plan from a saved company-intelligence analysis (the latest when no id is given)",
                   RiskLevel.LOW_RISK, params={"format": "xlsx|docx|pptx|pdf|brief|action_plan|md",
                                               "analysis_id": "optional"}, timeout=180.0)
def create_analysis_artifact(format: str, analysis_id: str = ""):
    from knowledge_fabric import service
    ctx = _ctx()
    refused = _refuse_unsigned(ctx)
    if refused:
        return refused
    return service.artifact_from_analysis(analysis_id or None, format, ctx.username or "")


@registry.register("ingest_company_document", "Add one company document to the knowledge base (with its year, "
                   "classification and department)", RiskLevel.MEDIUM_RISK,
                   params={"path": "file path", "origin": "company|sample|demo|general_knowledge",
                           "confidentiality": "public|internal|confidential|restricted (optional)",
                           "department": "optional", "department_restricted": "true/false (optional)",
                           "location": "site name (optional)", "year": "optional"}, timeout=600.0)
def ingest_company_document(path: str, origin: str = "company", confidentiality: str = "", department: str = "",
                            department_restricted: str = "", location: str = "", year: str = ""):
    from knowledge_fabric import ingest
    overrides = _overrides(confidentiality, department, department_restricted, location, year)
    return ingest.ingest_document(_checked_path(path), origin=origin, overrides=overrides).to_dict()


@registry.register("ingest_company_directory", "Add a folder of company documents (years taken from folder names "
                   "like 2019/ when present)", RiskLevel.MEDIUM_RISK,
                   params={"path": "folder", "origin": "company|sample|demo|general_knowledge",
                           "confidentiality": "optional", "department": "optional", "location": "optional",
                           "year": "optional - every file belongs to this year"}, timeout=3600.0)
def ingest_company_directory(path: str, origin: str = "company", confidentiality: str = "", department: str = "",
                             location: str = "", year: str = ""):
    from knowledge_fabric import ingest
    overrides = _overrides(confidentiality, department, "", location, "")
    result = ingest.ingest_directory(_checked_path(path), origin=origin, overrides=overrides,
                                     year=int(year) if year else None)
    result["results"] = result.get("results", [])[:200]
    return result


def _overrides(confidentiality: str, department: str, department_restricted: str, location: str,
               year: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if confidentiality:
        out["confidentiality"] = confidentiality
    if department:
        out["department"] = department
    if str(department_restricted).lower() in ("1", "true", "yes"):
        out["department_restricted"] = True
    if location:
        out["location"] = location
    if year:
        out["year"] = int(year)
    return out


@registry.register("update_entity", "Correct the entity register: merge, alias, unalias, rename, retype or resolve "
                   "(logged, and kept across graph rebuilds)", RiskLevel.MEDIUM_RISK,
                   params={"action": "merge|alias|unalias|rename|retype|resolve", "payload": "JSON object"},
                   timeout=30.0)
def update_entity(action: str, payload: str):
    from knowledge_fabric import entities
    ctx = _ctx()
    data = json.loads(payload) if isinstance(payload, str) else dict(payload or {})
    return entities.correct(action, data, ctx.username or "")


@registry.register("remove_company_document", "Remove one document from the company knowledge base "
                   "(the file itself is not touched)", RiskLevel.HIGH_RISK,
                   params={"document_id": "document id"}, timeout=60.0)
def remove_company_document(document_id: str):
    from knowledge_fabric import ingest
    return ingest.remove_document(document_id)


@registry.register("reindex_company_knowledge", "Rebuild the full-text index of the company knowledge base",
                   RiskLevel.MEDIUM_RISK, timeout=1800.0)
def reindex_company_knowledge():
    from knowledge_fabric import ingest
    return ingest.reindex()


@registry.register("reembed_company_knowledge", "Compute missing vector embeddings with the local embedding model",
                   RiskLevel.MEDIUM_RISK, params={"limit": "max passages (default 5000)"}, timeout=3600.0)
def reembed_company_knowledge(limit: str = ""):
    from knowledge_fabric import ingest
    return ingest.reembed(int(limit or 5000))


@registry.register("rebuild_knowledge_graph", "Re-extract entities and relations from every passage "
                   "(human corrections are re-applied)", RiskLevel.MEDIUM_RISK, timeout=3600.0)
def rebuild_knowledge_graph():
    from knowledge_fabric import ingest
    return ingest.rebuild_graph()


@registry.register("set_user_departments", "Set which departments' restricted documents a user may see",
                   RiskLevel.HIGH_RISK, params={"username": "user", "departments": "comma-separated"}, timeout=10.0)
def set_user_departments(username: str, departments: str):
    from knowledge_fabric.access import set_user_departments as _set
    names: List[str] = [d.strip() for d in (departments or "").split(",") if d.strip()]
    return _set(username, names)


@registry.register("evaluate_company_rag", "Run the RAG evaluation on the labelled test-fixture corpus "
                   "(retrieval, citations, temporal, calculations, entities, access control)",
                   RiskLevel.SAFE, timeout=900.0)
def evaluate_company_rag():
    from knowledge_fabric import evaluation
    return evaluation.run()
