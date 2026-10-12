"""
ZEUS knowledge fabric - company intelligence on top of the existing RAG.

Additive by design (docs/MEDHA_RAG_UPGRADE_PLAN.md): the original
`document_chunks` store and the industrial knowledge base stay exactly as they
were and are read through `knowledge_fabric.adapters`. This package owns its
own database (`database/knowledge_fabric.db`) so that nothing in `medha.db`
changes shape.

    store          schema, connections, the access predicate
    temporal       time expressions -> year / quarter / month scopes
    entities       entity extraction, canonical ids, aliases, corrections
    tables         table grids -> cells and numeric facts
    graph          typed relations with evidence
    authority      source authority ladder
    ingest         document -> hierarchy, tables, facts, entities, relations
    access         who may see which document (RBAC x classification x department)
    planner        question -> intents, time scope, entities, retrieval + reasoning plan
    retrieval      security-first hybrid retrieval (FTS5, vectors, entities, graph, facts)
    analytics      trusted calculations with their inputs attached
    contradictions conflicting facts and statements
    context        final model context, provenance kept per source
    reasoning      decision support and operational recommendations
    analysis       the single persisted AnalysisObject
    renderers      chat / DOCX / XLSX / PPTX / PDF / brief / action plan
    coverage       1989-2026 availability report
    evaluation     retrieval / citation / temporal / calculation / entity metrics
    service        the pipeline used by tools, the orchestrator and the interface
    tools          registered tools

Everything is local: SQLite, numpy, the Office libraries already installed, and
the Ollama daemon on loopback for embeddings and (optionally) narrative text.
"""

from __future__ import annotations

FIRST_YEAR = 1989
LAST_YEAR = 2026

# Where a document came from. Only "company" counts as company history; the
# others are always labelled as what they are.
ORIGIN_COMPANY = "company"
ORIGIN_SAMPLE = "sample"
ORIGIN_DEMO = "demo"
ORIGIN_GENERAL = "general_knowledge"
ORIGIN_FIXTURE = "test_fixture"
ORIGINS = (ORIGIN_COMPANY, ORIGIN_SAMPLE, ORIGIN_DEMO, ORIGIN_GENERAL, ORIGIN_FIXTURE)
