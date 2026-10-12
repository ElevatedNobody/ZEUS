# Data coverage - company records 1989-2026

Generated from the live knowledge base on 2026-09-27 (`RAG_INGEST.bat` / `python -m agni_ops rag coverage`).
Re-run that command any time; this file is a snapshot, the command is the truth.

## Verdict

**There are no company records in the knowledge base yet.** Every year from 1989 to 2026 is
either MISSING or covered only by sample / general material. ZEUS reports this gap; it does
not fill it. A question about the company's history, figures or performance will be answered
with "no document you can access answers this question" and the list of missing years - never
with an estimate.

| Status | Years | Meaning |
|---|---|---|
| AVAILABLE | 0 | company documents for the year exist |
| SAMPLE | 5 | only sample / demo material mentions the year - not company history |
| GENERAL | 5 | only general industrial knowledge mentions the year |
| MISSING | 28 | nothing at all |

(Updated 2026-09-27 after the owner's knowledge base was added - it is labelled SAMPLE,
see below, so it does not change the verdict.)

## What is in the knowledge base

| Source | Origin label | Documents | Notes |
|---|---|---|---|
| `knowledge/industrial_history/` | `general_knowledge` | 34 | history and practice of industrial equipment and methods - background, not company records |
| `knowledge/JARVIS_PACK/markdown/` | `sample` | 16 | "Nexus Precision" (a fictional company) and the assistant project's own planning documents |
| `samples/agni_rag_knowledge_base/data/` | `sample` | 16 | the owner's "ZEUS RAG knowledge base" (2026-09-27): 5 registers - leases (40), rent schedule (48), properties (25), compliance checklist (150), employees (60, HR-restricted), policy index (8), GST returns (24), TDS challans (30), property tax (20), income-tax filings (3), vendor contracts (20) - and 11 SOP / policy / contract / slide documents. Its own README says every name, GSTIN and amount is **fictional**; provenance in `samples/agni_rag_knowledge_base/PROVENANCE.md` |
| **company documents** | `company` | **0** | none provided yet |

Totals (2026-09-27): 66 documents, 1,542 passages (including one record node per register
row), 373 table facts, 574 vector embeddings (bge-m3, local - vector search is on). 35
documents carry no year and appear as undated - the registers among them on purpose: each
record carries its own dates.

The owner's registers are answered row by row (`knowledge_fabric/registers.py`); their data
quality is reported on every question - on 2026-09-27: 22 of 40 leases contradict their own
dates (e.g. Active with an end date in 2022), 7 property-tax items are marked Paid with no
payment date, 12 vendor contracts are Active past their end date. These are findings about
the sample data, shown to the user - not corrected.

The older stores (`document_chunks` in medha.db and the industrial knowledge base with its
DEMO refinery) are still searched through `knowledge_fabric/adapters.py`; their passages are
labelled with their source and only count for a dated question when the year appears in the text.

## Missing, in detail

* **Document types with no company document:** annual report, financial record, production
  record, maintenance report, inspection report, safety record, incident report, SOP, project
  record, audit report.
* **Departments with no company document:** Operations, Maintenance, Inspection, HSE, Finance,
  Projects, Procurement, Human Resources, Quality Control, Process Engineering,
  Instrumentation, Electrical, Information Technology.
* **Key datasets with no yearly figures:** revenue, net profit, production, crude throughput,
  maintenance cost, downtime.

## How to close the gaps

1. Put the company's documents in year folders (`2019\`, `FY2019-20\`, ...) - the year is read
   from the folder name, then the file name, then the document itself.
2. `RAG_INGEST.bat <folder> --origin company` (add `--confidentiality confidential`,
   `--department Finance --restricted`, `--location "X Refinery"` where they apply).
3. `RAG_REINDEX.bat` to add vector embeddings when the embedding model is available.
4. `python -m agni_ops rag coverage` - years move from MISSING to AVAILABLE only when company
   documents for them exist.

Nothing in this repository was invented to make the timeline look complete. The synthetic
"Test Fixture Refinery" in `tests/fixtures/knowledge_fabric/` is used only by the tests and the
RAG evaluation, in temporary databases.
