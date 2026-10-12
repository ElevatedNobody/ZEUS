# RAG evaluation

`RAG_EVALUATE.bat` (= `python -m agni_ops rag evaluate`, also part of `TEST_AGNI.bat rag` and
`SECURITY_CHECK.bat`).

## What it measures - and what it does not

It runs the whole pipeline on a labelled **synthetic** corpus, `tests/fixtures/knowledge_fabric/corpus`
("Test Fixture Refinery" - it does not exist), in a **temporary database**, with the older stores,
memory and the chat model switched off, so every answer is known in advance and every check is
a real pass or fail. It measures the METHOD. It says nothing about how complete the company's
own records are - `docs/DATA_COVERAGE.md` does that. The real knowledge base is never read or
changed (the database switch is context-local).

## Checks (19)

| Category | Check | Expected |
|---|---|---|
| entity | U5 in 2019 | resolved to Unit 5; 120 from the maintenance report, not the company total |
| calculation | highest maintenance cost | Unit 5, 80 + 120 + 135 = 335 |
| reasoning | losses 2019-2021 and what to do | 2018 baseline added; stated causes found; the 2021 restatement (-85 -> -80) flagged and the later report preferred; recommendations |
| temporal | revenue 2016 vs 2023 | both points, 4,350 and 4,900 |
| temporal | last five years | 2019-2023, against the records' latest year |
| calculation | average net profit 2015-2019 | 255 |
| calculation | CAGR of revenue 2015-2019 | ((4300/4200)^(1/4) - 1) x 100 |
| calculation | CAGR ending in a loss | refused, not computed |
| retrieval | start-up procedure | the SOP first |
| knowledge_graph | why did C-501 fail | FAILED_DUE_TO bearing wear, cited |
| abstention | revenue in 1995 | "no document you can access", nothing invented |
| access_control | viewer asks about board decisions | no confidential passage, title, citation or figure |
| access_control | engineer asks the same | the board minutes, first |
| injection | vendor note with "ignore all previous instructions" | flagged, quoted as data |
| access_control | no session | nothing searched |
| artifacts | XLSX / DOCX / PPTX / PDF of the losses analysis | each opens, passes its validator, carries the analysis id, states no number the analysis does not hold |

Every answer must also pass citation and number validation.

## Last result

2026-09-26: **PASS - 19/19** (`RAG_EVALUATE.bat`, exit code 0). Found and fixed on the way:
legacy passages ignored the period of a dated question (the abstention check failed until they
were filtered by the years in their text); a generic "maintenance" department match narrowed
table facts; a negative reranker score could invert the procedure boost.

## Unit and integration tests

`tests/rag`, `tests/temporal`, `tests/entity_resolution`, `tests/knowledge_graph`,
`tests/reasoning`, `tests/artifacts` - 112 tests with `tests/test_audit_chain_integrity.py`,
all passing on 2026-09-26.

2026-09-27: **PASS - 19/19** again after the register engine, the classification rules and
the passage-ranking changes below.

### Registers (2026-09-27) - `tests/rag/test_kf_registers.py`, 27 tests

Synthetic workbooks built per run, dates relative to today (nothing from the owner's data):
register detection and keys; one evidence node per row; no dated facts from register rows
(a real FY column still dates them); a personal-data register is confidential and
HR-restricted (an engineer outside HR does not even see it exists); personal columns hidden
unless asked; "the next 90 days" uses the end date; counts, totals, highest, group-by;
unpaid, before a month; lookup by code with the asked field; the calendar checks (Active but
ended, flagged expiring but not, Paid with no date, paid in the future); the service answer
cites rows and validates; the Excel "Records" sheet recomputes the total with `=SUM` and
matches the engine; the Word report carries the rows; passage search skips rows and lists of questions; document types (SOP
ids, slide decks, contracts, title records, safety briefs) and departments (only on a clear
lead).

### Passage ranking fixes found on the owner's knowledge base

* a slide listing test prompts ("What is notice period for L3?") outranked the answer - a
  passage made of questions or quoted prompts is weighted 0.4;
* a specific identifier in the question and the passage (L3, U5, C-501 - not years) weighs
  1.35;
* YAML front matter (`--- title: ... ---`) is file metadata, never a passage;
* a text answer quotes only passages within half the best score - no filler third quote.
