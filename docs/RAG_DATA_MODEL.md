# RAG data model - `database/knowledge_fabric.db`

SQLite (WAL), schema in `knowledge_fabric/store.py`. Separate from `medha.db` so no existing
table changed shape.

| Table | One row per | Key columns |
|---|---|---|
| `kf_documents` | document | `document_id` (hash of path + content), title, type, `doc_date`, `year`, `time_start`/`time_end`, `fiscal_year`, department, location, `confidentiality` (public / internal / confidential / restricted), `department_restricted`, `vault_linked`, `status`, `authority` (1-7), `origin` (company / sample / demo / general_knowledge / test_fixture), `source_hash`, `metadata` (date source, OCR confidence, entity site, ...) |
| `kf_nodes` | hierarchy node | `kind` (document / section / subsection / paragraph / table / row_range / **record** / figure), `parent_id`, `ordinal`, `depth`, `page`, `section_path`, `year` (the paragraph's own year; a later year in the text - a deadline - does not count; a record carries no year of its own) |
| `kf_registers` | register (a table whose rows are records) | `register_id` (= its table id), `document_id`, `table_node`, `name` (sheet), `key_column`, `columns` (JSON: name, type date / number / code / category / text, role status / end_date / start_date / due_date / paid_date / filed_date / review_date / name, `sensitive`, `period`, category values), `n_records` |
| `kf_records` | register row | `register_id`, `row_index`, `record_key` (the key column, or a composite), `node_id` (its evidence node), `codes` (every code in the row, for lookup), `data` (JSON column -> value) |
| `kf_fts` | indexed passage | FTS5 (porter, unicode61) over text, heading, title |
| `kf_vectors` | embedded passage | float32 BLOB + dimension (mixed dimensions are reported, never compared) |
| `kf_tables` / `kf_cells` | table / cell | headers, units, orientation, every cell with its number |
| `kf_facts` | numeric fact | metric (as written) + `metric_norm`, value, unit, year / quarter / month, `entity_id`, `cell_ref`, `node_id` |
| `kf_entities` | real-world thing | `entity_id` (`UNIT:5`, `UNIT:5@site`, `EQUIPMENT:C-501`, ...), type, status (active / ambiguous / merged), attributes |
| `kf_aliases` | name of an entity | source: pattern / gazetteer / evidence (a document said so) / descriptor / human |
| `kf_mentions` | entity in a passage | confidence, surface text |
| `kf_relations` | typed edge | src, relation, dst, confidence, `evidence_node`, `document_id`, validity |
| `kf_corrections` | human correction | merge / alias / unalias / rename / retype / resolve - re-applied after every graph rebuild |
| `kf_analyses` | saved analysis | the full AnalysisObject (JSON), owner, verified flag |
| `kf_user_departments` | user x department | who may see department-restricted documents |
| `kf_ingestion_log` | ingestion attempt | status, detail, run id |

## Registers (added 2026-09-27)

A table is a register when it has at least 4 columns and 3 rows, a unique key column
(`*_ID`, `*_Code`, `*_No` or code-shaped values such as `LS-2021-1001`) - or a code
column that forms a composite key - and at least two typed columns (dates, amounts, a
status). Periods across the columns make it a time table instead.

* **Rows are records, not dated figures.** A register yields no `kf_facts` unless it has
  a real period column (Period / Month / FY / Quarter); an event date (DOJ, End_Date) is
  not a period. Its document takes no "most frequent year" - each record carries its own
  dates.
* **Each row is an evidence node** (`kind = 'record'`, text "Column: value · ..."),
  indexed for full text, cited as `[E#]`. Passage search leaves record nodes out (the
  register engine answers register questions); they are not embedded.
* **Personal data:** a register with two or more personal columns (PAN, Aadhaar, UAN,
  CTC / salary, basic, HRA, PF, ESI, passport, date of birth) makes its document
  `confidential` and department-restricted to Human Resources - only HR members and
  administrators can retrieve it. Personal columns are never shown in an answer unless
  the question names them.
* **Data quality** is checked on every query against the system date: status Active but
  the end date passed; flagged "Expiring_Nd" but the end date is not within N days; Paid /
  Filed with no payment / filing date; a payment or filing dated in the future; start
  after end; stated tenure that does not match the dates; duplicate keys. Findings are
  conflicts of type `record` - reported, never corrected.

## Provenance chain

answer sentence -> `[E#]` / `[F#]` / `[C#]` / `[G#]` -> node (a passage or a register row) / fact / calculation / edge ->
document (`source_hash`, path, date, authority, origin) -> the file on disk. Calculations list
their input facts; artifacts carry the analysis id and are checked to state no other number.

## Retention

Removing a document (`rag remove <id>`) deletes its rows only - never the file. Human entity
corrections survive re-ingestion and graph rebuilds. The database is included in
`RESTORE_MY_DATA.py`.
