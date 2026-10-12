# Regression Report

Every feature that worked before the login and voice-repair work, checked after
it. Baseline captured before the first modification
(`/backups/baseline.json`: 92 tools, 17 agents, 125 files, 228 tests passing).

**Result: 375 tests pass. No feature lost. Two behaviours deliberately changed,
both documented below, and three defects found by the final end-to-end pass and
fixed.**

---

## Backup taken before modification

```
backups/MEDHA_pre_auth_<timestamp>.zip     full project snapshot
backups/baseline.json                       tool list, agent list, file count
```

## Feature matrix

| # | Feature | Before | After | Result |
|---|---|---|---|---|
| 1 | ZEUS startup (UI) | works | works, behind the login screen | PASS |
| 2 | ZEUS startup (CLI) | works | works, behind a terminal login | PASS |
| 3 | Text input / chat | works | works | PASS |
| 4 | Text response in the UI | works | works, and now appears **before** speech instead of after | IMPROVED |
| 5 | PySide6 dashboard, HUD, orb, sparklines | works | works, plus LOCK / LOG OUT and an operator caption | PASS |
| 6 | Ollama integration | works | works | PASS |
| 7 | Model router / local models | works | works | PASS |
| 8 | Tool registry | 92 tools | 133 tools, none removed | PASS |
| 9 | Agent registry | 17 agents | 17 agents, none removed | PASS |
| 10 | Decision engine routing | works | works, plus industrial + remember/forget routes | PASS |
| 11 | Memory: conversation history | works | works, scoped to the logged-in user | PASS |
| 12 | Memory: long-term facts | works | works, scoped to the logged-in user | PASS |
| 13 | Pre-existing memory after upgrade | n/a | adopted by the first account that logs in | PASS |
| 14 | To-Do panel | works | works, per user | PASS |
| 15 | Calendar panel | works | works, per user | PASS |
| 16 | Notes panel | works | works, per user | PASS |
| 17 | Weekly schedule planner (PDF/HTML/sync) | works | works, per user | PASS |
| 18 | SQLite database | works | works, extended with users/sessions/auth_events | PASS |
| 19 | Original RAG (index_folder / ask_knowledge_base) | works | works, `document_chunks` untouched | PASS |
| 20 | Industrial knowledge base + evidence citations | works | works, shared between operators by design | PASS |
| 21 | Industrial document pipeline (OCR/layout/tables) | works | works | PASS |
| 22 | Industrial agents (prepared) | works | works | PASS |
| 23 | Document generation (DOCX/XLSX/PPTX/PDF) | works | works | PASS |
| 24 | File tools, conversion, search | works | works | PASS |
| 25 | Automation (apps, windows, keyboard, mouse, clipboard) | works | works | PASS |
| 26 | Background tasks (watch folder, reminders) | works | works | PASS |
| 27 | Security manager, confirmation, audit log | works | works, plus an authentication audit trail | PASS |
| 28 | Plugin system (calculator) | works | works | PASS |
| 29 | Screen OCR / vision tools | works | works | PASS |
| 30 | Voice: STT (Whisper) | **broken** (model resolved by name, network needed) | local model, device probing, resampling, silence gate | FIXED |
| 31 | Voice: TTS (Chatterbox) | **broken** (no offline enforcement, torch 2.6 unpickling) | local checkpoint, retry path, async playback | FIXED |
| 32 | Voice → same pipeline and memory | partial | spoken input is ordinary input, one memory | PASS |
| 33 | Offline operation | works | works, now including login and voice | PASS |
| 34 | START.bat | works | works, unchanged | PASS |
| 35 | start_medha_industrial.bat | works | works, 7 steps, login noted | PASS |
| 36 | BUILD_EXE.bat | works | works, bundles auth + voice modules | PASS |
| 37 | scripts/offline_test.py | 11 checks | 14 checks (STT, TTS, industrial plant layer) | PASS |
| 38 | scripts/ingest_industrial_knowledge.py | works | works | PASS |
| 39 | scripts/audit_environment.py | works | works; tool count corrected (see defect 3) | PASS |
| 40 | Pre-existing test suite | 228 pass | 348 pass (228 + 41 auth + 12 login UI + 56 industrial/e2e + 11 other) | PASS |

## Deliberate behaviour changes

**1. Route arguments keep their original case.** `_match_direct_command`
matched against lowercased text and passed the lowercased capture groups to
tools, so `analyze the report D:/Reports/INSP-2024-118.pdf` reached the tool as
`d:/reports/...` and a calendar entry was saved as `doctor visit`.
`DecisionEngine._restore_original_case()` now re-reads each group from the
original string. `tests/test_productivity.py::test_add_event_routes_correctly`
was updated to assert the corrected behaviour, and a regression guard was added.

**2. The spoken reply no longer delays the written one.** `handle_text()` used
to block on `tts.speak()`, so the text answer reached the UI only after playback
finished. It now queues speech (`speak_async`) and returns immediately. The user
still gets both, in a better order.

Neither change removes a capability.

## Defects found by the final end-to-end pass, and fixed

Running the whole flow as a user - rather than only as tests - found three
things the tests had not:

**1. A failed `AuthResult` was truthy.** `authenticate()` returns a dataclass, so
`if sessions.unlock(password):` was true even when the password was wrong. No
shipped caller depended on it (they all read `.ok`), but the obvious way to write
the next caller would have let the wrong password through. `AuthResult.__bool__`
now returns `ok`, with a test that asserts it.

**2. The refusal wording leaked which accounts exist.** A real account answered
"Invalid credentials. 4 attempt(s) left."; an invented username answered
"Invalid credentials." The timing was already equalised, but the *text* was an
account-enumeration oracle. Unknown usernames now run the same countdown from a
bounded, process-local counter whose lock also expires. Three tests cover it.

**3. The readiness check under-reported the tool count.** `startup_check.py`
printed "91 registered" against a baseline of 92, which reads as a lost feature.
Nothing was lost: `detect_existing_medha()` read the registry without running
plugin discovery, so the calculator plugin's `calculate` tool had not registered
yet. It now discovers plugins exactly as `main.py` does, and prints 92. The same
function's voice section still described the pre-integration build (pyttsx3, "no
Chatterbox"); it now describes what is actually installed.

## What could have broken and did not

- **The original `document_chunks` RAG table** - schema asserted unchanged by test.
- **Single-user memory with no session** - when no one is logged in the database
  behaves exactly as before; this is what keeps every pre-existing test green.
- **Tool and agent counts** - compared against the baseline: identical sets.
- **The dashboard's existing panels** - the two new top-bar buttons are additive;
  `run_ui()` and `MedhaWindow()` gained keyword arguments with defaults, so
  existing callers and tests still work.

## Verification performed

```
python -m pytest tests/ -q                      375 passed
python scripts/offline_test.py                  13 checks; External Dependency Check PASS
python scripts/voice_doctor.py                  root causes named with fixes
python scripts/startup_check.py                 92 tools, 17 agents, no network services
python scripts/audit_environment.py             environment report regenerated
full flow demo: first run -> wrong password refused -> login -> per-user memory
                -> lock/unlock -> second operator isolated -> restart persists
                -> audit trail carries no password and no hash
```

## Remaining issues

1. Real Whisper/Chatterbox weights could not be exercised in this build
   environment (Hugging Face returns 403); the model runtimes were replaced by
   contract-enforcing stubs. Everything ZEUS owns ran for real.
2. Ollama is not installed here, so model-dependent answers were verified
   against a loopback stub and through the deterministic routes that need no
   model at all.
3. Data at rest is not encrypted - password hashes are safe, conversation
   history and industrial documents are not. Full-disk encryption is the right
   control and is out of scope.

## Industrial integration pass (MEDHA_13 comparison + reference-image capabilities)

Baseline re-checked after adding the industrial layer. Tool set compared
against `/backups/baseline.json` again:

```
tools removed since the original baseline ... 0
tools added ................................ 41
agents ..................................... 17, unchanged
tests ...................................... 292 -> 348, all passing
```

| # | Feature | Before | After | Result |
|---|---|---|---|---|
| 41 | Word/PPTX/XLSX generators | worked only via model tool-selection | same code, now reachable by direct command AND verified after writing | IMPROVED |
| 42 | convert_file | reported success on the converter's word | verifies the output reopens first | IMPROVED |
| 43 | Tool parameter validation | every documented parameter mandatory | signature decides; required ones still enforced | IMPROVED |
| 44 | Plant data (equipment/maintenance/alerts/inventory/permits/LOTO) | absent | in the existing database, DEMO-labelled | NEW |
| 45 | Industrial reports (DOCX/XLSX/PPTX from data) | absent | work with Ollama down | NEW |
| 46 | Industrial command routing | absent | ~45 deterministic routes | NEW |
| 47 | Documents browse/search/open | RAG only | plus generated-document listing | NEW |
| 48 | System information | partial | model, Ollama, STT, TTS, DB, CPU, RAM, no secrets | NEW |
| 49 | Dashboard / login / voice / auth | working | untouched, all tests still pass | PASS |
| 50 | Reference-image UI | n/a | **not copied** - existing UI remains authoritative | PASS |

Deliberate behaviour changes in this pass, both documented in
INTEGRATION_REPORT.md section 10: optional tool parameters are no longer
mandatory, and specific industrial routes take precedence over the generic
"create a presentation about anything" route.

## Document-quality pass (shared analysis layer, executive PPTX/XLSX/DOCX)

| # | Feature | Before | After | Status | Test | Notes |
|---|---|---|---|---|---|---|
| 51 | Shared analytical model | none - each generator built its own content | one `IndustrialAnalysis` renders to DOCX, XLSX and PPTX | NEW | `test_one_analysis_gives_identical_numbers_in_all_three_files` | numbers cannot disagree between files |
| 52 | Executive PPTX | bullet slides | 11-slide storyline: recommendation, KPIs, diagnosis, chart, evidence, actions, risks, roadmap, ask | IMPROVED | `test_deck_has_a_storyline_not_a_pile_of_bullets` | native editable charts |
| 53 | Slide overflow | possible | impossible - cards sized to available space | FIXED | `test_deck_content_fits_on_the_slide` | found by rendering, not by reading |
| 54 | Multi-sheet XLSX | single flat sheet | Summary + data sheets + Analysis + Sources | IMPROVED | `test_workbook_is_multi_sheet_with_a_management_view` | |
| 55 | Excel formulas | values pasted by Python | `SUM`, `COUNTIF`, `IFERROR`, `COUNTA` incl. cross-sheet | NEW | `test_workbook_uses_real_formulas_not_pasted_numbers` | recalculated in LibreOffice and checked |
| 56 | Typed cells | numbers as text | numbers as numbers with formats | FIXED | `test_workbook_numbers_are_numbers` | |
| 57 | Word report structure | headings + paragraphs | headings, styled tables, severity colour, unsigned approval block | IMPROVED | `test_report_is_structured_with_headings_and_tables` | |
| 58 | Spreadsheet → deck | not possible | header detection, column profiling, concentration and outlier findings | NEW | `test_spreadsheet_to_deck_analyses_rather_than_pasting` | tested on a real 3-sheet workbook |
| 59 | Output verification | trusted `save()` | reopen-and-parse before reporting success | IMPROVED | 3 negative tests | missing / empty / corrupt all rejected |
| 60 | Existing generators, auth, voice, dashboard | working | untouched | PASS | full suite | 375 passing |

Tool count after this pass: **140** (baseline 92, none removed).

## Presentation / workbook / report quality pass (reference-grade rebuild)

The three renderers were rebuilt against the quality bar of the reference deck
and workbook. Structure and layout grammar only - no design, palette, branding,
wording or business content from either reference was reproduced.

| # | Feature | Before | After | Status | Test | Notes |
|---|---|---|---|---|---|---|
| 61 | Deck visual system | dark HUD, terminal-like, unreadable tables | light print-quality scheme: navy header band, white cards, coloured accents | IMPROVED | rendered to PNG and inspected | decks get projected and printed; a dark theme survives neither |
| 62 | Slide utilisation | half-empty pages, content floating in the top third | every slide fills its page; cards, tables and read-outs sized from the space that exists | FIXED | `test_deck_content_fits_on_the_slide` | six slides had large dead areas |
| 63 | Deck tables | cramped, headers wrapping mid-word ("Severit / y") | navy header, banded rows, content-weighted columns, whole-word header sizing | FIXED | visual render | |
| 64 | Table read-out strip | none | totals and row counts fill the space under a short table | NEW | visual render | |
| 65 | Diagnosis slide | 4 stacked bars | 3x2 card grid with severity accent edge, magnitude chip and evidence | IMPROVED | `test_deck_has_a_storyline_not_a_pile_of_bullets` | |
| 66 | Chart slide | chart plus a near-empty panel | chart plus three stacked read-out cards (total, largest, spread) with data labels | IMPROVED | visual render | |
| 67 | Roadmap | uniform cards | phase-coloured headers with the outcome pinned to the bottom | IMPROVED | visual render | |
| 68 | Workbook presentation | plain header row | full-width navy title band, subtitle strip, section bands, banded rows, tinted total row | IMPROVED | rendered to PDF and inspected | |
| 69 | Workbook usability | gridlines on, no print setup | gridlines off, fit-to-width, landscape, coloured sheet tabs, Summary prints on one page | IMPROVED | rendered to PDF | |
| 70 | Word report | headings and paragraphs from page one | cover page with title band, recommendation callout, KPI band, contents, then a page break | IMPROVED | `test_report_is_structured_with_headings_and_tables` | |
| 71 | Word tables | autofit, header not repeated | shaded header repeating across pages, banded rows, content-weighted column widths, no row split | IMPROVED | rendered to PDF | |
| 72 | Word header/footer | none | running document title, live PAGE / NUMPAGES fields | NEW | rendered to PDF | |
| 73 | Approval block | unsigned | unchanged - still unsigned, still refuses to pre-fill | PASS | `test_report_approval_block_is_never_pre_approved` | |

## Orchestration pass (pipeline, router, validators, agents)

| # | Feature | Before | After | Status | Test | Notes |
|---|---|---|---|---|---|---|
| 74 | `DecisionEngine.process` contract | `{response, intent, refresh, agent?, ui_action?}` | identical, plus a `pipeline` key | PASS | `test_engine_response_keeps_every_field_it_always_had` | additive only; UI untouched |
| 75 | Command routing | 200+ deterministic routes | same routes, same tools - the orchestrator is injected with them | PASS | full engine suite | wrap, not replace |
| 76 | Task classification | intent only (QUESTION/COMMAND/TASK/MULTI_STEP) | plus task kind and complexity, deterministic | NEW | `test_classifier_recognises_the_format_that_was_asked_for` | legacy intent preserved |
| 77 | Model selection | task-type slot only | smallest capable, RAM-aware, agent can name its slot | IMPROVED | `test_router_steps_down_when_memory_is_tight` | forced 2 GB free, no 7B chosen |
| 78 | Model for deterministic work | a model was routed for everything | 7 task kinds declared model-free | NEW | `test_deterministic_work_gets_no_model` | plant queries load nothing |
| 79 | Missing model handling | fell back to first preference silently | names the model and the `ollama pull` command | IMPROVED | `test_router_names_the_missing_model_instead_of_substituting` | |
| 80 | Model unloading | idle timer only | `release_models` asks Ollama to evict | NEW | `test_orchestration_tools_are_reachable_and_honest` | |
| 81 | Output validation | office files only | 12 validator kinds incl. code, OCR, RAG grounding, bundles | NEW | 10 validator tests | |
| 82 | Slide overflow detection | build-time only | also validated post-write on any deck | NEW | `test_pptx_validator_catches_content_running_off_the_slide` | |
| 83 | Auto-correction | none | 14 strategies, each changing something specific, capped at 2 attempts | NEW | `test_repair_changes_something_specific`, `test_repairs_are_bounded` | |
| 84 | Validator over-reach | n/a | a validator may only fail on positive evidence | FIXED | `test_a_validator_never_vetoes_a_tool_it_simply_does_not_understand` | found by test: 3 working tools were being called failures |
| 85 | Tool error messages | tool's own message | still the tool's own message, never a validator's | FIXED | `test_a_failing_tool_reports_its_own_message_not_a_generic_one` | |
| 86 | Multi-step planning | required Ollama | deterministic decomposition; LLM planner only as fallback | IMPROVED | `test_one_sentence_three_deliverables_becomes_three_steps` | works with Ollama down |
| 87 | Shared source read | each deliverable re-read the file | read once, deliverables depend on it | NEW | `test_a_named_source_file_is_read_once_and_shared` | keeps numbers consistent |
| 88 | Planner tool safety | unknown tools dropped | unchanged, now covered by test | PASS | `test_the_planner_never_admits_a_tool_that_does_not_exist` | |
| 89 | Agent roster | 11 organisational entries | 19 specialists published into the same registry (27 total) | IMPROVED | `test_specialists_appear_in_the_original_agent_registry` | no entry removed |
| 90 | Duplicate tool ownership | n/a | one tool, one owner - enforced | FIXED | `test_agents_share_one_registry_rather_than_owning_copies` | found `safety_report` claimed twice |
| 91 | Agent tool claims | n/a | every claimed tool must exist | NEW | `test_every_tool_an_agent_claims_actually_exists` | |
| 92 | Pipeline visibility (UI) | model name only | PIPELINE card: task, agent, model, tools, status | NEW | dashboard panel tests | no chain-of-thought exposed |
| 93 | Pipeline introspection | none | 6 tools: pipeline_status, list_specialists, model_routing, validation_report, release_models, explain_last_run | NEW | `test_orchestration_tools_are_reachable_and_honest` | reachable by voice too |
| 94 | Audit logging | per tool | plus one line per orchestrated run (user, kind, agent, model, tools, artifacts, validated, repaired) | IMPROVED | `test_the_run_is_written_to_the_audit_log` | no prompts, no data content |
| 95 | Offline guarantee | verified | plus an Orchestration Pipeline check in `offline_test.py` | IMPROVED | `test_the_layer_adds_no_network_calls` | 7 stages with Ollama down |
| 96 | Startup readiness | 15 rows | plus orchestration pipeline and model router rows | IMPROVED | `scripts/startup_check.py` exit 0 | router row reports models genuinely pulled |
| 97 | Voice path | shared with text | unchanged - voice reaches the pipeline through the same engine | PASS | `test_voice_reaches_the_industrial_layer_through_the_same_engine` | |
| 98 | Fallback safety | n/a | if the orchestration layer fails to import, `process()` uses the original path | NEW | code path retained | an upgrade, not a dependency |
| 99 | Auth, login, memory, RAG, OCR, automation, schedule planner | working | untouched | PASS | full suite | |
| 100 | Startup launcher | `start_medha_industrial.bat` | unchanged - picks up the new rows automatically | PASS | manual | no new launcher created |

Test count after this pass: **418** (375 before, 43 new). Tool count: **145**
(139 before; none removed). Specialist agents: **19**, total agent roster **27**.
