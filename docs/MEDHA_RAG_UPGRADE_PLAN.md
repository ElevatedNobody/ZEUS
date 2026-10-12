# ZEUS RAG upgrade plan - from document search to company intelligence

Status of this plan is kept current in the checklist at the end. Architecture
details: `docs/NEXT_GEN_RAG_ARCHITECTURE.md`; schema: `docs/RAG_DATA_MODEL.md`;
evaluation: `docs/RAG_EVALUATION.md`; migration: `docs/RAG_MIGRATION.md`.

## 1. Where we start (measured, not assumed)

ZEUS already has more than a vector store. Two RAG stores exist and both stay:

| Store | What it does well | What it cannot do |
|---|---|---|
| `document_chunks` (original) | index a folder, answer from it | no metadata, no provenance beyond the file |
| Industrial KB (`industrial_documents` / `industrial_chunks`) | 25 metadata fields per chunk, BGE-M3 vectors, taxonomy query expansion, cross-encoder/hybrid rerank, grounded answers, **citation validation** | no time model, no entities or graph, tables flattened into chunk text, no structured numbers, no document hierarchy above "section", no source authority, no document-level access control, no contradiction handling, answers are one-shot text |

Also present and reused: the document pipeline (PDF / DOCX / XLSX / PPTX / CSV / text /
images, OCR, vision), metadata harvesting, 14 document schemas, the industrial
taxonomy, the reranker, Evidence/CitationValidator, Hermes memory, the Office
generators, validators and the audit chain.

**Data reality.** The repository contains no real company records for 1989-2026.
It contains a general industrial-history corpus (31 files, general knowledge), a
company-history pack that labels itself **SAMPLE / FICTIONAL** (Nexus Precision) and a
**DEMO** refinery dataset. The upgrade builds the machinery for a real corpus and
reports coverage honestly: every year without real documents is MISSING, sample data
is shown as SAMPLE, never as history.

## 2. Target pipeline

```
USER -> INTENT -> QUERY PLANNER -> MULTI-SOURCE RETRIEVAL -> TEMPORAL ANALYSIS
     -> ENTITY RESOLUTION -> KNOWLEDGE GRAPH -> STRUCTURED ANALYSIS -> EVIDENCE RANKING
     -> REASONING -> VALIDATION -> RECOMMENDATION -> ARTIFACT GENERATION
     -> PROVENANCE -> AUDIT -> ANSWER
```

Simple questions take the short path (classify -> retrieve -> answer); the planner
decides, per question, which stages run.

## 3. Components (new package `knowledge_fabric/`)

| Component | Responsibility | Builds on |
|---|---|---|
| `store` | own SQLite database `database/knowledge_fabric.db`: documents, hierarchical nodes, FTS5 index, float32 vectors, tables + cells, numeric facts, entities + aliases + mentions, relations, analyses, corrections | SQLite FTS5 |
| `ingest` | document -> hierarchy (document > section > subsection > paragraph / table / cell range / figure / page > atomic evidence), dates/years/quarters, department, type, authority, classification, entities, relations, table facts; directory / year / archive ingestion, update, remove, reindex, re-embed | `doc_pipeline`, `extraction`, `schemas`, `taxonomy`, embeddings |
| `temporal` | before / after / during / between / since / until / year / quarter / month / fiscal year / YoY / CAGR / trend / change; context windows ("after 2017" also reads 2015-2017) | - |
| `entities` | canonical ids, aliases, confidence, evidence-backed merges only, human correction | taxonomy tag patterns |
| `graph` | typed entities and relations (OWNS, OPERATES, LOCATED_AT, PART_OF, DEPENDS_ON, MAINTAINED_BY, FAILED_DUE_TO, INSPECTED_BY, USED_IN, SUPPLIED_BY, AFFECTED, CAUSED, FOLLOWED_BY, APPROVED_BY, CHANGED_TO, RELATED_TO), each with its evidence node | entities |
| `planner` | 18 intents (FACT_LOOKUP ... MULTIMODAL), retrieval + reasoning path per question | temporal, entities |
| `retrieval` | security filter FIRST, then metadata / temporal / entity filters, FTS5 + vector + graph + table facts, reciprocal-rank fusion, authority weighting, rerank, parent-context expansion; legacy stores through the adapter | `reranker`, `provenance` |
| `analytics` | trusted numbers: sum, average, median, min, max, std dev, growth, YoY, CAGR, variance, correlation, trend, rolling average, time series, scenarios - every result lists its input evidence | numpy / statistics |
| `contradictions` + `authority` | conflicting facts shown as SOURCE A / SOURCE B with date, revision, scope, definition, unit, authority; authority ladder (approved SOP > audited report > engineering document > internal report > correspondence > draft > unverified) | - |
| `security` | RBAC x document classification x department; unauthorised documents never enter retrieval, counts, titles, citations or coverage | `security/policy.py` |
| `context` | final context in labelled sections: SYSTEM POLICY, SECURITY POLICY, USER REQUEST, HERMES, ENTERPRISE RAG, STRUCTURED DATA, GRAPH EVIDENCE, CALCULATED RESULTS; documents fenced as data | Hermes recall, injection scan |
| `reasoning` | facts -> evidence -> patterns -> drivers -> options -> trade-offs -> risks -> implementation -> KPIs -> monitoring; assumptions never shown as facts; recommendations in operational form (action, owner, timeline, dependencies, cost category, impact, risk, KPI, data required, success condition, monitoring frequency) | analytics, local LLM (optional) |
| `analysis` | ONE persisted, verified `AnalysisObject` per question; follow-ups reuse it | - |
| `renderers` | chat, DOCX, XLSX, PPTX, PDF, executive brief, action plan - from the same object | existing Office generators |
| `coverage` | 1989-2026 AVAILABLE / SAMPLE / MISSING per year, missing departments / periods / types / datasets | store |
| `evaluation` | recall@k, precision, reranking, citation accuracy, grounding, calculation, temporal, entity resolution, artifact correctness | labelled test corpus |
| `adapters` | read-only access to `document_chunks` and the industrial KB; optional import | legacy stores |
| `tools` | registered tools for all of the above (RBAC-profiled like every tool) | tool registry |

Orchestrator, router, agents and UI integration are described in their own documents.

## 4. Principles that are tested, not promised

1. **No fabrication** - an answer states the gap when evidence is insufficient; numbers
   come from the analytics engine or are labelled as assumptions; sample data is labelled.
2. **Provenance never mixes** - Hermes memory, enterprise documents, structured data,
   graph evidence and calculations each keep their own section and ids.
3. **Security before relevance** - the access filter runs inside the SQL that finds
   candidates; nothing unauthorised is scored, counted, named or cited.
4. **Documents are data** - instruction-like text in a document is quoted and marked,
   never obeyed.
5. **One analysis, many formats** - DOCX, XLSX and PPTX of the same question carry the
   same numbers because they render the same object.
6. **Nothing is lost** - the legacy stores stay, the contract test enforces the rest.

## 5. Checklist

- [x] Audit, feature inventory, zero-regression baseline + contract test
- [x] Store + schema + FTS5 (`knowledge_fabric/store.py`)
- [x] Ingestion (hierarchy, tables, facts, metadata, entities, relations, authority, classification)
- [x] Temporal parser + context windows
- [x] Entity resolution + human corrections
- [x] Knowledge graph + queries
- [x] Query planner (18 intents)
- [x] Security-aware hybrid retrieval + legacy adapter + rerank + parent context
- [x] Numerical analysis engine
- [x] Contradiction detection + source authority
- [x] Context assembly with Hermes kept separate
- [x] Reasoning / decision support + operational recommendations
- [x] Analysis object + renderers (chat, DOCX, XLSX, PPTX, PDF, brief, action plan)
- [x] Coverage report 1989-2026
- [x] Tools + CLI (`python -m agni_ops rag ...`, in agni_ops/rag.py rather than scripts/rag.py so it shares the
      BAT files' logging and exit codes) + BAT files (RAG_INGEST, RAG_REINDEX, RAG_EVALUATE, SECURITY_CHECK)
- [x] Evaluation harness + tests/rag, reasoning, artifacts, temporal, knowledge_graph, entity_resolution
- [ ] Model router roles
- [ ] Orchestrator company-intelligence path + new agents
- [ ] New UI + new login connected to the real backend
- [ ] Regression, security, RAG evaluation, UI, BAT runs; documentation
      (RAG evaluation 19/19 PASS and SECURITY_CHECK run on 2026-09-26 - the security check FAILS on the
      audit chain: 46 records written by test runs with a temporary key; see REGRESSION_REPORT.md.
      RAG docs: NEXT_GEN_RAG_ARCHITECTURE, RAG_DATA_MODEL, RAG_EVALUATION, RAG_MIGRATION, DATA_COVERAGE)
