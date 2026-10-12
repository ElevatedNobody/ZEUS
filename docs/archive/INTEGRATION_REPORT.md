# Integration Report

Current ZEUS + recovered functionality + new industrial capability, with the
current project as the authoritative base.

**348 tests pass. 133 tools registered, none removed. No network service, no
second database, no second document generator, no UI copied from the reference
image.**

---

## 1. What the comparison actually found

`MEDHA_13.zip` was compared file by file against the current project:

```
files present only in MEDHA_13 ............ 0
tools present only in MEDHA_13 ............ 0
files where MEDHA_13 held more code ....... 0
tools: MEDHA_13 65   |   current (before) 92
```

Nothing had been deleted. `write_document`, `create_presentation`,
`create_spreadsheet` and `convert_file` were all present and byte-for-byte
sound in the current project. What was missing was the path to them:

| Symptom | Root cause | Fix |
|---|---|---|
| "Create a Word document about X" did nothing | no deterministic route existed in either version; the command depended on the model choosing the tool, and **Ollama is not running on your machine** (your own startup check: `Ollama runtime BLOCK`, `Model: reasoning BLOCK not pulled`) | routes added for Word/PPTX/XLSX phrasings |
| "Make a 10-slide presentation" ignored the count | the digits were captured into the topic | slide count parsed into `slide_count` |
| "Create an Excel sheet for inspection data" made a sheet about "sheet for inspection data" | the noun after the format word was captured as the topic | pattern corrected |
| A failed write still said "saved" | no verification after `save()` | `core/office.py` verifies and refuses |
| Every report needed the LLM | content always came from the model | industrial reports are built from the database and need no model |

## 2. New files

| File | Role |
|---|---|
| `core/office.py` | shared document output: where files go, unique names, open-after, and **verification** (exists, non-empty, correct extension, reopens in its own library) |
| `industrial/plant_data.py` | equipment, maintenance, inspections, alerts, inventory, permits, LOTO, readings - all in the **existing** SQLite database |
| `industrial/plant_ops.py` | the capabilities: operations, monitoring, maintenance, inventory, alerts, equipment dossier, safety, permits, LOTO, system information |
| `industrial/plant_docs.py` | plant data -> real DOCX / XLSX / PPTX / CSV, through `core/office.py` |
| `industrial/plant_tools.py` | 41 tools registered into the existing registry |
| `tests/test_industrial_plant.py` | 39 tests |
| `tests/test_end_to_end_industrial.py` | 17 tests (Phase 17 TEST A-I) |
| `FEATURE_RECOVERY_MATRIX.md`, `INTEGRATION_REPORT.md` | this documentation |

## 3. Modified files

| File | Change |
|---|---|
| `core/builtin_tools.py` | `write_document`, `create_presentation`, `create_spreadsheet` and `convert_file` now finish through `core/office.py` and verify their output. Generation logic itself untouched. |
| `core/decision_engine.py` | ~45 new deterministic routes; office phrasings corrected; specific industrial routes placed ahead of the generic ones |
| `core/tool_registry.py` | imports `industrial.plant_tools`; `Tool.validate` now treats a documented parameter that has a default in the signature as optional |
| `industrial/environment.py` | audit reports the real tool count (plugins discovered) and the current voice stack |
| `scripts/offline_test.py` | new **Industrial Plant Layer** check: seeds the plant tables and builds DOCX/XLSX/PPTX with no model, offline |
| `scripts/startup_check.py` | crash-proofed (a missing library is now a WARN row, not a traceback) |

Nothing was deleted.

## 4. Data model

All of it in `database/medha.db` - the same file as conversation history,
facts, industrial documents and accounts.

```sql
plant_equipment    (tag, name, type, location, unit, status, criticality,
                    manufacturer, model, installed_on, source)
plant_maintenance  (equipment_tag, performed_on, type, description, technician,
                    duration_hours, parts_used, findings, status, next_due, work_order)
plant_inspections  (equipment_tag, inspected_on, type, inspector, result,
                    findings, recommendation, document_path, next_due)
plant_alerts       (alert_id, raised_at, equipment_tag, category, severity,
                    description, status, acknowledged_by/at, resolved_at, source)
plant_inventory    (part_number, description, category, quantity, minimum_stock,
                    unit, storage_location, supplier, equipment_tag)
plant_permits      (permit_id, permit_type, equipment_tag, work_location, hazards,
                    precautions, requested_by, approved_by, status, valid_from/to)
plant_loto         (loto_id, equipment_tag, isolation_points, isolation_status,
                    applied_by/at, verified_by/at, released_by/at, permit_id)
plant_readings     (equipment_tag, measured_at, parameter, value, unit, quality, source)
```

Every table carries `source`. Every reader reports it.

## 5. Honesty about data

This workstation has no plant historian attached, and the code says so rather
than implying otherwise:

- `seed_demo_data()` writes every row with `source='DEMO'`, and every summary,
  report, deck and spreadsheet carries a banner naming it as generated sample
  data.
- `monitoring_snapshot()` returns `live_feed_connected: false` and names the
  integration point in the same breath.
- `record_reading()` **refuses** a reading that does not name its source, so a
  real measurement can always be traced to whatever produced it. Point it at an
  OPC-UA bridge, a historian export or a round sheet and the readings become
  real; nothing else in the stack has to change.
- `find_sop()` reports an empty knowledge base instead of inventing a procedure.

## 6. Safety

| Action | Risk | What happens |
|---|---|---|
| Reading any plant record | SAFE | runs |
| Logging maintenance, raising an alert, updating stock | LOW | runs, attributed to the logged-in operator |
| Requesting a permit, recording an isolation, resolving an alert | MEDIUM | runs, recorded |
| **Approving a permit** | HIGH | registry refuses without an explicit human confirmation, and the approval must name a person |
| **Verifying or releasing an isolation** | HIGH | same gate, and the record names who did it |

ZEUS does not operate plant equipment. The LOTO module is a record of what a
competent person physically did. The approval note generator produces an
**unsigned** signature block and prints "This note is not approved until a
competent person signs it."

Tested: with the confirmation callback returning False, `approve_permit` is
refused and the permit stays `requested`.

## 7. Memory integration

No new memory store. The last equipment discussed is kept in the existing
`facts` table, so:

```
"Tell me everything about Pump P-204"   -> equipment_details, remembers P-204
"Generate the maintenance report for that pump"  -> maintenance_report(P-204)
```

A bug found and fixed during testing: an *empty* filter was also resolving to
the remembered equipment, so "show active alerts" answered about one pump. An
omitted filter now means everything; only a pronoun resolves to the last item.

## 8. Documents produced from data

Seven generators, all verified, all working with Ollama down:

| Command | Output |
|---|---|
| generate a maintenance report [for X] | DOCX: specification, summary, full history, inspections, open alerts, work due |
| generate a safety report | DOCX: critical alerts, safety/compliance alerts, overdue inspections, permits, isolations |
| create a Word approval note for X | DOCX with an unsigned approval block |
| create a management presentation of the maintenance situation | PPTX: status, by unit, alerts, maintenance, spares, safety, sources |
| create a presentation for equipment X | PPTX equipment review |
| export \<dataset\> to Excel | XLSX with header styling, frozen pane, autofilter, sized columns and an **About** sheet carrying provenance |
| export \<dataset\> to CSV | CSV |

When Ollama *is* running, `narrative=True` adds an analysis section on top of
the factual tables. When it is not, the report is still produced and says the
narrative was skipped - it never fails and never fabricates.

## 9. Dependencies

**No new dependencies.** `python-docx`, `openpyxl`, `python-pptx` and `PyMuPDF`
were already in `requirements.txt` / `requirements-industrial.txt`. Nothing was
upgraded.

## 10. Bugs found and fixed during this work

1. **Empty equipment filter narrowed to the remembered item** - "show active
   alerts" returned one alert instead of five. Fixed; two tests guard it.
2. **Documented optional parameters were mandatory** - `Tool.validate` required
   every declared parameter, so `maintenance_history` (no arguments) failed with
   "Missing required parameters". Now the function signature decides; a
   parameter with no default is still required (`read_file` still refuses).
3. **Specific routes were shadowed by generic ones** - "create a presentation
   for equipment P-204" hit the generic deck generator, and "open document X"
   hit the application launcher. Specific patterns moved ahead.
4. **"the crude feed pump" did not resolve** - substring matching failed on a
   natural phrase. Added significant-word scoring with a single-winner rule.
5. **`startup_check.py` crashed on a machine without PyMuPDF** (your traceback)
   - a `.get(key, "")` default does not apply when the stored value is `None`.
   Fixed, and every probe is now crash-proofed.

## 11. Offline verification

```
python scripts/offline_test.py
  Document Generation ........ PASS
  Industrial Plant Layer ..... PASS   (seeds plant data, builds DOCX+XLSX+PPTX, no model)
  External Dependency Check .. PASS   (no external host in any runtime module)
python -m pytest tests/ -q ... 348 passed
python scripts/startup_check.py  133 tools, no network services
```

The industrial modules are scanned by the dependency check and by a test that
asserts they contain no `http://`, `https://`, FastAPI, Flask, boto3 or OpenAI
reference at all.

## 12. Remaining issues

1. **Ollama is not running in this build environment**, so the model-driven
   paths (`write_document`, `create_presentation`, `create_spreadsheet` on free
   text) were verified as far as routing and error handling; the data-driven
   industrial reports were verified end to end producing real files. On your
   machine, start Ollama and they will produce prose too.
2. **The seeded refinery is DEMO data.** It exists so the commands have
   something to answer with. Replace it with your own equipment via
   `register_equipment`, or delete those rows - they are all marked `DEMO`.
3. **No live sensor integration.** The framework is there
   (`plant_readings`, `record_reading(source=...)`, trends); connecting a real
   historian is a collector script, not a change to this code.
4. **The industrial capabilities are not yet surfaced in the dashboard UI.** As
   instructed, no UI was copied from the reference image and the existing
   dashboard was left authoritative; every capability is reachable by typed or
   spoken command today, and the tools are ready for panels whenever you want
   them.
5. **PyMuPDF is missing on your machine** - install it (`pip install PyMuPDF`,
   or `python scripts\setup_industrial_env.py`) or the document pipeline stays
   limited.
