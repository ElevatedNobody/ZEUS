# ZEUS next-generation RAG - architecture

Package: `knowledge_fabric/` (its own database, `database/knowledge_fabric.db`). The original
RAG (`document_chunks`, the industrial knowledge base) is untouched and still searched through
`knowledge_fabric/adapters.py` - nothing was replaced, a layer was added.

## One question, end to end

```
question
  -> planner          intents (18), time scope, metrics, formats, which legs, simple | complex path
  -> retrieval        ACCESS FILTER IN SQL (role x classification x department x vault)
                        full-text (FTS5/BM25) | vectors | entity mentions | graph | table facts | older stores
                        -> reciprocal-rank fusion -> rerank (with title + section) -> authority x origin
                        -> injection scan, provenance, parent context            = evidence [E1..]
  -> registers        when the question names a kind of record ('which leases expire in the next
                        90 days', 'total rent of active leases', 'unpaid property tax', 'LS-2022-1002'):
                        the matching rows are selected BY CODE (category values, status, date window
                        against today, amount thresholds, a record's code), counted / summed /
                        averaged / ranked / grouped with every input row listed        = [C1..]
                        the rows become the first evidence [E1..], passages about the same records
                        follow; every row is checked against the calendar (Active but ended, Paid
                        with no date, filed in the future ...) and findings are reported as conflicts
  -> Hermes memory    its own section and ids [M1..] - information, never authority
  -> analytics        every number: series, totals, averages, change, CAGR, YoY, trend, correlation,
                        ranking, extrapolation, scenarios - each with its input facts   = [C1..]
  -> validation       conflicts between sources (SOURCE A / SOURCE B / CONFLICT), coverage gaps,
                        sample / demo origin warning
  -> reasoning        patterns; drivers labelled evidenced | correlated | hypothesis;
                        recommendations (11 operational fields), options, plan, KPIs, risks
  -> answer           deterministic answer from the calculations and evidence;
                        optional model summary kept ONLY if every citation exists and every
                        number is in the evidence or the calculations
  -> AnalysisObject   saved (kf_analyses) with its id
  -> renderers        XLSX / DOCX / PPTX / PDF / brief / action plan / Markdown from the SAME object,
                        each file validated, checked for consistency, registered as an artifact
```

Live stages reported to the interface, each only when it has happened:
`QUERY -> UNDERSTANDING -> SEARCHING -> EVIDENCE FOUND -> ANALYZING -> VALIDATING -> GENERATING -> READY`.
The model's hidden reasoning is never streamed.

## Modules

| Module | Responsibility |
|---|---|
| `store` | schema, connections, context-local database switch (evaluation) |
| `ingest` | document -> hierarchy (document / section / subsection / paragraph / table / row range), tables -> cells -> facts, entities, relations, dates with their source |
| `temporal` | years, ranges, fiscal years, quarters, months, decades, relative periods against the records' latest year, context windows |
| `tables` | numbers, units, wide / long / entity-per-row tables, metric names |
| `registers` | registers (tables of records): detection, typed columns and roles, one evidence node per row, the record query engine, data-quality checks, personal-data protection |
| `entities` | extraction, canonical ids, evidence-backed aliases, ambiguity, human corrections |
| `graph` | typed relations (FAILED_DUE_TO, CAUSED, ...) with their evidence node |
| `authority` | document types and the 1-7 authority ladder |
| `access` | who may see which document |
| `planner` | intents and the retrieval plan |
| `retrieval` | hybrid retrieval, Document Explorer, Evidence Viewer |
| `analytics` | the calculation engine |
| `contradictions` | numeric conflicts, differing stated causes, revision comparison |
| `context` | the labelled model context (SYSTEM / SECURITY / REQUEST / HERMES / EVIDENCE / DATA / GRAPH / CALCULATED) |
| `reasoning` | decision support over `config/knowledge_fabric/playbook.json` |
| `analysis` | the AnalysisObject and answer validation |
| `renderers` | the output formats |
| `coverage` | 1989-2026 coverage and timeline |
| `evaluation` | the RAG evaluation on the synthetic fixture |
| `service` | the pipeline |
| `tools` | registered tools (roles in `security/policy.py` `_ROLE_OVERRIDES`) |

## Security properties (tested)

* Unauthorised documents never enter retrieval: the access predicate is part of the SQL of
  every leg; results are re-checked; the graph leg only follows edges from visible documents.
* No leakage: no count of hidden documents, the Evidence Viewer refuses a forbidden node with the
  same message as a missing one, coverage says whose view it is.
* Documents are data: instruction-like text is flagged, fenced in the model context and quoted
  "as data, not followed" in answers.
* No session, no search.

## Hardware and models

No model is required for retrieval, calculation, reasoning or file generation. Optional: the
local embedding model (vectors), a local reranker (cross-encoder), and the chat model for the
summary - all local through Ollama / `models/`, never downloaded by this layer.
