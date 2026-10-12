# Feature Recovery Matrix

Current ZEUS vs MEDHA_13 vs the industrial capabilities in the reference
image.

**The headline finding, stated plainly: nothing had been lost.** A file-by-file
comparison of MEDHA_13 against the current project found *zero* files present
only in MEDHA_13, *zero* tools present only in MEDHA_13, and *no* file where
MEDHA_13 held more code than the current version. MEDHA_13 is a strict subset
(65 tools; the current build had 92 before this work).

Document generation had not been deleted. It was **unreachable and
unverified**:

1. **No deterministic route.** "Create a Word document about X" matched nothing
   in the router, so it fell through to model-based tool selection - which
   needs Ollama. On a machine where Ollama is not running (yours reported
   `Ollama runtime BLOCK not reachable` and `Model: reasoning BLOCK not
   pulled`), *every* document command silently failed. That is what "the
   feature is gone" actually was.
2. **No verification.** The generators returned `{"saved": ...}` as soon as
   `save()` returned, so a zero-byte or corrupt file was reported as a success.
3. **Every generator required the model.** Even a report whose content is
   entirely in the database went through the LLM and died with it.

All three are fixed below.

Legend: **C** = in current ZEUS before this work, **13** = in MEDHA_13.

## Document generation

| Feature | C | 13 | Recovered? | Implementation | Test status | Notes |
|---|---|---|---|---|---|---|
| Word (DOCX) generation | yes | yes | Not lost - **routed and verified** | `core/builtin_tools.py::write_document` + `core/office.py` | pass | new route: "create a Word document about X"; output now verified by reopening |
| PowerPoint (PPTX) generation | yes | yes | Not lost - **routed and verified** | `create_presentation` | pass | slide count now parsed ("make a 10-slide presentation on X") instead of being swallowed into the topic |
| Excel (XLSX) generation | yes | yes | Not lost - **routed and verified** | `create_spreadsheet` | pass | "create an Excel **sheet for** X" used to capture "sheet for X" as the topic |
| CSV output | yes | yes | Extended | `plant_docs.export_dataset_csv` | pass | real CSV of any plant dataset |
| PDF generation | yes | yes | Unchanged | schedule planner, doc pipeline | pass | |
| File conversion | yes | yes | Not lost - **verified** | `convert_file` | pass | now proves the output reopens before reporting success |
| Office file safety (exists / non-zero / extension / reopens) | no | no | **New** | `core/office.py::verify_office_file`, `finish` | pass | a corrupt or empty file is now an error, never a "saved" |
| Shared output path, unique names, open-after | duplicated per tool | duplicated | **Consolidated** | `core/office.py` | pass | one implementation instead of one per generator |

## Industrial capabilities (from the reference image - functions only, no UI copied)

| Feature | C | 13 | Recovered/Added? | Implementation | Test status | Notes |
|---|---|---|---|---|---|---|
| Operations | partial | no | **New** | `plant_ops.operations_summary/status_text`, tool `plant_status` | pass | availability, per-unit breakdown, open alerts, work due |
| Monitoring | no | no | **New** | `plant_ops.monitoring_snapshot/trend`, tools `monitoring_status`, `equipment_trend` | pass | framework only - reports `live_feed_connected: false` and names the integration point |
| Maintenance | no | no | **New** | `plant_data` + `plant_ops.maintenance_overview`, tools `maintenance_history`, `log_maintenance`, `schedule_maintenance`, `maintenance_due` | pass | scheduling writes to the **existing** ZEUS calendar |
| Inventory | no | no | **New** | `inventory_summary`, `find_part`, `update_stock` | pass | flags lines at or below minimum stock |
| Reports | partial | partial | **New (data-driven)** | `plant_docs`, tools `maintenance_report`, `safety_report`, `approval_note` | pass | built from records, no model required |
| Documents | RAG only | RAG only | **Extended** | `list_generated_documents`, `find_generated_document`, `open_document` | pass | browses the existing output folder; no second document store |
| Schedule | calendar | calendar | **Reused** | `schedule_maintenance` -> existing calendar | pass | no second task manager |
| Alerts | no | no | **New** | `plant_alerts` table, `list_plant_alerts`, `raise_plant_alert`, `acknowledge_alert`, `resolve_alert` | pass | full structure: id, timestamp, equipment, category, severity, description, status, source |
| Equipment | no | no | **New** | `plant_equipment`, `equipment_details` (dossier across every table + documents) | pass | "tell me everything stored about Pump P-204" |
| Safety | partial | no | **New** | `safety_status`, `find_sop`, `safety_report` | pass | declares its own limits; SOP search reports an empty KB honestly |
| Permit to Work | no | no | **New** | `plant_permits`, `request_permit`, `approve_permit` | pass | created as *requested*; approval is HIGH_RISK and refused without human confirmation |
| Lockout / Tagout | no | no | **New** | `plant_loto`, `record_isolation`, `list_isolations`, `set_isolation_state` | pass | information/workflow only - ZEUS never operates isolation devices |
| Logs | yes | yes | **Exposed** | `view_logs` over the existing log files | pass | no second logging system |
| Export data | no | no | **New** | `export_plant_data` (xlsx/csv), 9 datasets | pass | every dataset exports and is verified |
| System information | partial | partial | **Extended** | `system_information` | pass | model, Ollama, STT, TTS, memory, database, CPU, RAM; no secrets |
| Memory integration | yes | yes | **Extended** | `plant_tools._resolve` + existing `facts` table | pass | "the report for that pump" resolves to the last equipment discussed |

## Everything that was already there

| Feature | Status after this work |
|---|---|
| Authentication (Argon2id, lock, logout, per-user memory) | unchanged, 41 tests pass |
| Login screen | unchanged, 12 tests pass |
| Voice (Whisper STT, Chatterbox TTS, offline enforcement) | unchanged, 41 tests pass |
| Ollama runtime, model router | unchanged |
| Original RAG (`index_folder`, `ask_knowledge_base`) | unchanged |
| Industrial document pipeline (OCR, layout, schemas, evidence) | unchanged |
| Dashboard, HUD, panels, settings | unchanged - **no UI was copied from the reference image** |
| Automation, plugins, security manager, audit log | unchanged |
| Tool registry | 92 -> 133 tools; **none removed** |
| Test suite | 292 -> 348 tests, all passing |

## Document quality (TEAM_HYDRA / AeroVista references - structure only)

| Feature | Before | Now | Implementation | Test status | Notes |
|---|---|---|---|---|---|
| Shared analysis model | absent | `IndustrialAnalysis` | `industrial/analysis.py` | pass | one object, three renderers |
| Executive deck | bullets | 9 slide types, storyline, native charts | `industrial/deck_builder.py` | pass | dark HUD palette, ours not theirs |
| Multi-sheet workbook | flat table | Summary / data / Analysis / Sources | `industrial/workbook_builder.py` | pass | formulas, filters, freeze, conditional formatting, chart |
| Word report | paragraphs | headings, tables, unsigned approval block | `industrial/report_builder.py` | pass | |
| Spreadsheet analysis | absent | header detection, profiling, outliers, concentration | `analysis.build_workbook_analysis` | pass | verified on the reference workbook |
| XLSX → PPTX workflow | absent | analysed, not pasted | `spreadsheet_to_presentation` | pass | |
| Briefing pack (all three) | absent | one analysis, three consistent files | `executive_briefing_pack` | pass | |
| Voice setup path | models missing, no installer | `SETUP_VOICE.bat` + `scripts/setup_voice.py` | | pass | Python 3.14 pins handled |
