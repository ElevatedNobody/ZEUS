# Provenance - ZEUS Unicorn RAG 1990-2026

| | |
|---|---|
| Supplied by | the ZEUS owner, 2026-09-27, as `AGNI_UNICORN_RAG_1990_2026.zip` |
| Archive SHA-256 | `dc00ed044b17cd74234ebb7ca07109347679b270d9a6e399d4086ff267594be3` |
| Copied to | `company_records/agni_unicorn_1990_2026/` (139 files: `.md`, `.xlsx`, `.csv`) |
| Not copied | `metadata/chunks.jsonl`, `metadata/rag_manifest.json` (pre-chunked text - ZEUS chunks the originals itself), `ingest_to_text.py`, the empty `raw_text/`, `README.md` (quoted below) |
| Ingested as | origin **company**, dataset **"ZEUS Unicorn RAG 1990-2026"** - the owner's instruction: this corpus is the source of truth for Company Intelligence |
| Command | `RAG_INGEST.bat company_records\agni_unicorn_1990_2026 --dataset "ZEUS Unicorn RAG 1990-2026" --dataset-note "..."` |

## What the corpus says about itself

The archive's README ends with: *"Fictional for RAG/SIH only."* That sentence is stored
with every document of the dataset (`dataset_note`) and shown wherever Company
Intelligence shows the dataset, so no one reads these figures as audited history.

README summary: cities Perimerian (1990-04-15), Mumbai (1998-09-01), Gharwar
(2007-06-20), Meridian (2016-11-12); unicorn from FY2022; annual P&L and balance
sheet 1990-2026; 13,514 daily P&L rows; monthly roll-up and a 2024-2026 daily
sample; a company records pack for every year; 13 departments with SOPs.

## Known inconsistencies inside the dataset (found on ingest, not corrected)

* The daily P&L rows do not add up to the stated annual revenue (over 1990-2026 the
  site sums are about 29% above the annual statements), and the monthly roll-up
  differs from the summed daily rows in almost every month. ZEUS prefers the stated
  figure and reports the gap as a reconciliation.
* The milestone "Crosses INR 50 Cr annual revenue" is dated 2001-03-31, while the
  stated FY2001 revenue is 20.84 INR Cr. ZEUS reports this as a disagreement.
* 37 of the files are byte-identical copies under different folders (`by_doc_type`,
  `word_md`, `pptx_md`); each is stored once, the other paths are recorded on it.

## Removing it

Every document carries `dataset = "ZEUS Unicorn RAG 1990-2026"`. A database backup
taken just before the ingest is in `backups/knowledge_fabric/`.
