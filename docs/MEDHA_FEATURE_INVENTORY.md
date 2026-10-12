# ZEUS feature inventory - the zero-regression contract

Taken on 2026-09-26, **before any code of the ZEUS upgrade was changed**
(code snapshot: `../snapshots/pre_medha_upgrade_20260926_210613.zip`).

Two halves, deliberately:

* **This document** - every functional capability in words: where it lives, what it
  needs, how it is reached, its state, its tests, what the new system builds on it,
  and what the upgrade does to it.
* **`tests/baseline/zero_regression_baseline.json`** - the same system read from the
  LIVE registries by `scripts/feature_inventory.py` (human-readable copy:
  `docs/generated/FEATURE_REGISTRY.md`). `tests/test_zero_regression_contract.py`
  fails if any tool, agent, agent-tool binding, bridge method, model slot, validator,
  auto-fix strategy, task kind, pipeline stage, database table, BAT file, ops command,
  decision route, config key or interface capability in it disappears.
  A registry cannot miscount itself; the previous hand-written inventory did, three times.

**Baseline counts (live, 2026-09-26):** 210 tools · 23 specialist agents · 20 bridge
methods · 8 model slots · 12 validators · 14 auto-fix strategies · 18 task kinds ·
10 pipeline stages · 168 decision-route targets · 18 database tables · 13 BAT files ·
16 ops commands · 121 config keys · 77 interface capabilities · 906 test functions.

**The one rule the upgrade applies to this list:** the VISUAL layer (everything under
`ui/` that draws pixels) is discarded and rebuilt from scratch, as instructed. Every
capability the old visuals exposed is re-exposed by the new interface; none is dropped
because its old screen was. The backend is preserved and extended, never replaced.

Status legend: **WORKING** = exercised by tests that pass on the repair machine ·
**WORKING (env)** = works, but its tests depend on this machine (documented in
`tests/KNOWN_ENVIRONMENT_FAILURES.json`) · **PARTIAL** = works with a stated limit.

---

## A. Interface (visual layer - rebuilt from zero)

| Feature | Module | Depends on | Entry point | Status | Tests | New system builds on | Migration plan |
|---|---|---|---|---|---|---|---|
| Web shell: 10 screens, ~36 tabs (Command, Industrial, Knowledge, Agents, Workbench, Productivity, Vault, Security, Models, Audit) | `ui/agni_shell.py`, `ui/web/**` | PySide6 QtWebEngine + QWebChannel, `agni://` scheme | `main.py run_ui()` | WORKING | `tests/e2e/test_ui_e2e.py` (24), `test_start_and_rendering.py` (20) | the typed bridge `ui/bridge_contract.py`, the scheme handler, request interceptor | **Visuals retired**, replaced by `ui/medha/` (new design system). Shell window, bridge, scheme handler and offline interceptor **kept** (backend of the UI). All 77 capabilities re-mapped (see `docs/UI_ARCHITECTURE.md`). |
| Qt Command Center fallback (when WebEngine is missing) | `ui/command_center.py`, `ui/panels.py`, `ui/widgets.py`, `ui/theme.py`, `ui/hud_widgets.py`, `ui/backdrop.py`, `ui/void_wave.py` | PySide6 widgets | `main.py run_ui()` fallback | WORKING (1 env test) | `test_command_center.py`, `test_command_center_panels.py` | - | **Visuals retired.** Missing WebEngine -> CLI mode with the exact repair command (REPAIR.bat reinstalls PySide6). Behaviours those tests guarded (threading, user switch, panels reach tools) re-tested against the new UI. |
| Classic dashboard (`--classic-ui`) | `ui/dashboard.py` | PySide6 | `main.py --classic-ui` | WORKING | `test_dashboard_panels.py`, `test_dashboard_threading.py` | - | **Visuals retired.** Flag kept, prints that the classic UI was replaced, starts the new UI. |
| Access screen: login / first-run setup / unlock, face sign-in, animations | `ui/access.py`, `ui/access_motion.py` | `auth/session_manager.py`, `security/face_auth.py` | `main.py authenticate()`, `lock_session()` | WORKING | `test_access_window.py` (15), `test_face_sign_in.py` (screen part) | all of auth below | **Visuals retired**, login rebuilt inside the new UI (`docs/AUTH_ARCHITECTURE.md`). Guarded behaviours re-tested: verification off the UI thread, one attempt at a time, generic errors, lockout countdown, Escape never dismisses, no processEvents, first-run setup, unlock only by the locked user. |
| Approval prompt for high-risk actions | `ui/approval_dialog.py` (Qt), web `approval` event | `security/approvals.py` | bridge event `approval` / method `resolve_approval` | WORKING | `test_security.py` | approvals gateway | Backend kept; new UI renders an approval sheet. |
| Voice controls (listen once, speak replies, stop) | web controls | bridge `listen`, `set_speak`, `stop_speaking` | new UI | WORKING | `test_voice_mode.py`, e2e | voice manager | Re-exposed in the new command bar. |
| Request history, response rendering, command chips, cinema mode | web | bridge `submit`, `history` | new UI | WORKING | e2e | orchestrator | Re-exposed in Command Center. |

**Done 2026-09-27.** The visual files above were removed after a byte-verified backup
(`../retired_previous_ui_2026-09-27.zip`, 45 files). The new interface uses the owner's
"Voice Core" design. Its tests: `tests/e2e/test_medha_e2e.py` (38, all passing),
`tests/test_ui_capability_coverage.py` (all 77 previous capabilities reached),
`tests/test_medha_interface_behaviour.py` and `tests/test_no_previous_ui.py` (the
behaviours the removed window tests guarded), `tests/test_web_login.py` (sign-in). The
three native-dialog bridge calls the old page used (`change_password`, `create_user`,
`unlock_vault`) stay in the bridge contract and are reached through in-page forms
(`account_password`, `account_create`, `vault_open`) - `RETIRED` in
`tests/test_zero_regression_contract.py` names each replacement.

## B. Authentication, session, RBAC

| Feature | Module | Depends on | Entry point | Status | Tests | New system builds on | Migration plan |
|---|---|---|---|---|---|---|---|
| Local accounts, Argon2id, first account = administrator, generic errors, unknown-user timing and wording equalised | `auth/auth_manager.py`, `auth/password_manager.py` | argon2-cffi | session manager | WORKING | `test_authentication.py` | new login | Preserved unchanged. |
| Lockout after N failures, countdown | `auth/auth_manager.py` ([auth] max_failed_attempts, lockout_seconds) | database | login | WORKING | `test_authentication.py`, `test_access_window.py` | new login LOCKED state | Preserved; new UI shows the real countdown. |
| Sessions: login, lock, unlock, logout, idle timeout, per-user memory scope, vault locked + approvals refused on lock/logout | `auth/session_manager.py` | database, vault, approvals, assistant_state | `session_manager` | WORKING | `test_authentication.py`, `test_security.py` | new login, SESSION EXPIRED state | Preserved; new bridge auth methods call it. |
| Face authentication: enrol / verify / identify / remove; DPAPI-encrypted 512-d template; liveness required; modes off / second_factor / face_or_password; 3-attempt pause persisted; same-face-on-two-accounts detection | `security/face_auth.py`, `auth/session_manager.py` | InsightFace (vendored), onnxruntime, OpenCV, models/insightface | tools `register_face`, `verify_my_face`, `remove_face`, `face_status`; login | WORKING | `test_face_and_firewall.py`, `test_face_sign_in.py` | new login face flow | Preserved; new login drives it through the bridge (camera frames streamed from Python). |
| Offline password reset | `SET_PASSWORD.py`, `RESET_PASSWORD.py` | database | command line | WORKING | `test_authentication.py` | - | Preserved. |
| RBAC: viewer < operator < engineer < admin; per-tool access / classification / min-role; refuses the test-mode bypass | `security/policy.py` | session | every tool call | WORKING | `test_security.py`, `test_ops_and_security.py` | **security-aware RAG** reuses the role ladder | Preserved; extended to document classification (`docs/AUTH_ARCHITECTURE.md`). |
| Human approval for high-risk tools | `security/approvals.py`, `[security] confirm_threshold` | session | tool registry | WORKING | `test_security.py` | high-risk operations | Preserved. |
| Emergency lockdown (network loopback-only, vault locked, approvals refused, tasks cancelled, models released) - persisted, admin-only release | `security/lockdown.py` | egress, vault, approvals | tools `engage_lockdown`, `disengage_lockdown` | WORKING | `test_security.py` | new LOCKDOWN state | Preserved. |

## C. Security centre, firewall, audit, vault

| Feature | Module | Depends on | Entry point | Status | Tests | New system builds on | Migration plan |
|---|---|---|---|---|---|---|---|
| In-process egress guard: monitor / sovereign (loopback + LAN) / lockdown, live counters | `security/egress.py` | - | installed at start; tools `network_status`, `set_network_mode` | WORKING | `test_security.py`, `test_ops_and_security.py` | Security Center, Firewall screen | Preserved. |
| Windows Firewall rules (sovereign / lockdown), verified field by field, reversible, backups | `security/firewall.py`, `agni_ops/firewall.py`, `FIREWALL_AGNI.bat`, `APPLY_FIREWALL.bat`, `REMOVE_FIREWALL.bat` | PowerShell NetSecurity, admin | BAT; tool `firewall_status` | WORKING (Ollama rules need admin run) | `test_ops_and_security.py` | Firewall screen | Preserved. |
| WFP filter layer | `security/wfp.py`, `bin/agni-wfp.exe`, `tools/agni-wfp/` (Go source) | admin | tools `wfp_firewall_status`, `apply_wfp_firewall`, `remove_wfp_firewall`, `wfp_blocked_connections` | WORKING (reading needs admin) | `test_face_and_firewall.py` | Firewall screen | Preserved. |
| Tamper-evident audit chain (HMAC, DPAPI-wrapped key) + audit log | `security/audit_chain.py`, `core/security_manager.py` | DPAPI | tools `verify_audit_chain`, `show_audit_log` | WORKING | `test_security.py` | **every RAG answer and artifact is audited** | Preserved; RAG operations add audit entries. |
| Security events | `security/events.py` | - | tool `security_events` | WORKING | `test_security.py` | Security Center | Preserved. |
| Trusted-model registry (HMAC-signed digests) | `security/integrity.py`, `config/trusted_models.json` | audit key | tools `model_integrity_report`, `pin_trusted_models` | WORKING | `test_ops_and_security.py` | router refuses untrusted models | Preserved. |
| Sovereignty report | `security/sovereignty.py` | egress, firewall | tool `sovereignty_report` | WORKING | `test_security.py` | Security Center | Preserved. |
| Prompt-injection detection | `security/injection.py` | - | memory recall, RAG | WORKING | `test_memory_hermes.py` | **RAG treats documents as data** | Reused by the knowledge fabric. |
| Path guard, Python sandbox | `security/pathguard.py`, `security/sandbox.py` | - | file tools, `run_python_sandboxed` | WORKING | `test_security.py`, `test_tools.py` | - | Preserved. |
| Vault: encrypted locker (add / open / export / remove / list / audit), lock/unlock, snapshot | `core/vault.py`, `core/vault_tools.py` | DPAPI / cryptography | 14 vault tools, bridge `unlock_vault` | WORKING | `test_vault_and_productivity.py` | Vault screen; vault-linked documents never enter RAG while locked | Preserved. |
| Employee register | `core/vault_tools.py` | vault | tools `add_employee`, `find_employee`, ... | WORKING | `test_vault_and_productivity.py` | Vault screen | Preserved. |

## D. Models

| Feature | Module | Depends on | Entry point | Status | Tests | New system builds on | Migration plan |
|---|---|---|---|---|---|---|---|
| Ollama client: chat, embeddings (validated vectors), vision with smaller-model fallback, think control, explicit failure replies | `core/ollama_client.py` | Ollama on loopback | everywhere | WORKING | `test_model_verification.py` | embeddings + reasoning for RAG | Preserved. |
| Model router: 8 slots (fast, classify, reasoning, json_structured, code, vision, multilingual, default), trusted-only, leaky models moved off latency slots | `core/model_router.py` | registry, calibration | `route(slot)` | WORKING | `test_model_verification.py`, `test_orchestration.py` | **role-based selection** (GENERAL_REASONING, FAST, CODING, VISION, EMBEDDING, RERANKER, EXTRACTION, DOCUMENT, SMALL_LOCAL) | **Extended**: `select(role, requirements)` added beside `route()`; every slot keeps working. |
| Hardware detection, hardware-aware model plan, model registry | `core/hardware.py`, `industrial/model_registry.py`, `config/industrial/model_registry.json` | nvidia-smi, psutil | tools `hardware_profile`, `model_routing`, `capability_report` | WORKING | `test_agni_setup_and_replies.py` | router constraints (RAM/VRAM) | Preserved; mandatory before any model choice. |
| Model lifecycle: residency, unload, commit-headroom check, idle releasers | `core/model_lifecycle.py`, `core/sysmem.py` | Ollama API | internal; tool `release_models` | WORKING | `test_ops_and_security.py` | router availability | Preserved. |
| Think-false calibration | `core/model_calibration.py` | - | first use | WORKING | `test_model_verification.py` | - | Preserved. |
| Typed model verification (embedding / chat / vision tests) | `agni_ops/models.py`, `VERIFY_MODELS.bat` | Ollama | `TEST_AGNI.bat models` | WORKING | `test_model_verification.py` | Model Center | Preserved. |

## E. Orchestration and agents

| Feature | Module | Depends on | Entry point | Status | Tests | New system builds on | Migration plan |
|---|---|---|---|---|---|---|---|
| Orchestrator pipeline: intent -> memory -> classify -> plan -> authorize -> route -> execute -> validate -> autofix -> deliver; live activity events | `core/orchestration/orchestrator.py`, `activity.py`, `contracts.py` | everything | bridge `submit` | WORKING | `test_orchestration.py` | complex-question path | **Extended**: a company-intelligence path (plan -> retrieve -> rerank -> analyse -> reason -> validate -> recommend -> artifact -> verify -> deliver) is added; existing paths untouched. |
| Classifier (18 task kinds, complexity) | `core/orchestration/classifier.py` | - | orchestrator | WORKING | `test_orchestration.py` | Query planner | Extended with analysis intents. |
| Deterministic task graphs | `core/orchestration/task_graph.py` | - | orchestrator | WORKING | `test_orchestration.py` | - | Preserved. |
| Capability router (agent + model per step) | `core/orchestration/router.py` | model router | orchestrator | WORKING | `test_orchestration.py` | - | Preserved. |
| Validators (12) and auto-fix strategies (14) | `validators.py`, `autofix.py` | - | orchestrator | WORKING | `test_orchestration.py` | artifact verification | Preserved; analysis artifacts validated by the same validators. |
| Decision engine: 168 routed phrasings | `core/decision_engine.py` | tools | orchestrator / CLI | WORKING | `test_decision_engine.py` | - | Preserved; new phrasings added. |
| 23 specialist agents with tool bindings (operations, maintenance, inspection, safety, inventory, alerts, report, document, data_analysis, knowledge, vision, engineering, coding, file, automation, memory, system, productivity, vault, people, security, industrial_agents, conversation) | `core/orchestration/agents.py`, `core/agents.py` | tools | orchestrator | WORKING | `test_orchestration.py` | **new agents** (Retrieval, Temporal, Entity, Graph, Analysis, FinancialAnalysis, Recommendation, DecisionSupport, Artifact, Validation, Citation) | **Added beside**; no existing agent or binding removed (enforced by the contract). |
| Industrial agent runner (inspection / maintenance / safety / report workflows) | `industrial/agent_interfaces.py`, `agent_workflows.py` | KB, plant data | tool `run_industrial_agent`, `agent_roster` | WORKING | `test_end_to_end_industrial.py` | Agent Center | Preserved. |

## F. Tools, automation, plugins

| Feature | Module | Status | Tests | Migration plan |
|---|---|---|---|---|
| 210 registered tools (86 built-in, 48 plant, 40 ZEUS, 15 industrial, 14 vault, 7 orchestration) - full list in `docs/generated/FEATURE_REGISTRY.md` | `core/tool_registry.py` + modules | WORKING | `test_tools.py` and per area | Preserved; the knowledge fabric ADDS tools. |
| Desktop automation: open / close / focus apps, type, hotkeys, mouse, clipboard, screenshots, read screen (OCR), media, lock screen, shutdown (approval) | `automation/*.py`, `core/builtin_tools.py` | WORKING | `test_automation.py` | Automation screen. |
| Background automation: reminders, folder watchers, notifications | `core/background_events.py`, `core/builtin_tools.py` | WORKING | `test_background_automation.py` | Automation screen + notifications. |
| Plugins (calculator) | `plugins/plugin_loader.py` | WORKING | `test_tools.py` | Preserved. |

## G. Voice

| Feature | Module | Status | Tests | Migration plan |
|---|---|---|---|---|
| Speech-to-text: faster-whisper in a crash-isolated worker, backend chain (CUDA fp16 -> CUDA int8 -> CPU int8), session blacklist, end-of-speech capture | `voice/speech_to_text.py`, `stt_manager.py`, `stt_worker.py` | WORKING | `test_stt_isolation.py`, `test_listen_end_of_speech.py`, `test_voice_integration.py` (3 env) | Preserved; mic control in new UI. |
| Text-to-speech: Chatterbox on GPU kept loaded (`chatterbox_server.py`), Windows SAPI, Piper; streaming by sentence; interruptible | `voice/providers.py`, `chatterbox_bridge.py`, `text_to_speech.py`, `speech_prep.py` | WORKING | `test_chatterbox_server.py`, `test_chatterbox_voice.py` (1 env), `test_voice_providers.py` | Preserved. |
| Voice mode, meetings from audio | `main.py`, `industrial/meetings.py` | WORKING | `test_voice_mode.py`, `test_productivity.py` | Preserved. |

## H. Vision, OCR, documents, artifacts

| Feature | Module | Status | Tests | New system builds on | Migration plan |
|---|---|---|---|---|---|
| Document pipeline: PDF (text + tables + images, scanned detection), DOCX, XLSX, PPTX, CSV, text, images; OCR PaddleOCR -> Tesseract -> EasyOCR; vision descriptions | `industrial/doc_pipeline.py`, `ocr_engines.py` | WORKING (OCR engines optional) | `test_industrial_pipeline.py`, `test_rag_and_vision.py` | **the fabric's ingestion front end** | Reused as is. |
| Metadata harvesting + provenance-preserving chunking (document, page, section, revision, date, author, equipment, unit, department, confidentiality, OCR confidence) | `industrial/extraction.py` | WORKING | `test_industrial_pipeline.py` | hierarchical nodes | Reused. |
| 14 industrial document schemas, field extraction with evidence, validation | `industrial/schemas.py`, `config/industrial/document_schemas.json` | WORKING | `test_industrial_pipeline.py` | document typing + authority | Reused. |
| Industrial taxonomy: abbreviations, synonyms, query expansion, equipment-tag patterns | `industrial/taxonomy.py`, `config/industrial/taxonomy.json` | WORKING | `test_industrial_pipeline.py` | entity extraction + query expansion | Reused. |
| Word / Excel / PowerPoint / PDF generation (reports, workbooks, decks, rich docs, knowledge-pack documents), conversion | `core/office.py`, `core/rich_docs.py`, `industrial/report_builder.py`, `workbook_builder.py`, `deck_builder.py`, `plant_docs.py`, `core/knowledge_docs.py` | WORKING | `test_office_and_conversion.py`, `test_document_quality.py`, `test_write_document.py` | **renderers of the single analysis object** | Reused; new renderers call them. |
| Artifact registry + verification | `core/artifacts.py`, tools `list_artifacts`, `verify_artifact` | WORKING | `test_office_and_conversion.py` | Artifact workspace | Preserved. |

## I. RAG, knowledge, memory

| Feature | Module | Depends on | Status | Tests | New system builds on | Migration plan |
|---|---|---|---|---|---|---|
| Original RAG store (`document_chunks`): index a folder, ask the knowledge base | `memory/database.py`, `core/builtin_tools.py` (`index_folder`, `ask_knowledge_base`) | embeddings | WORKING (1 env test) | `test_capabilities_rag.py`, `test_rag_and_vision.py` | adapter source | **Kept and queried through an adapter** - never deleted. |
| Industrial knowledge base: BGE-M3 vectors in SQLite, taxonomy expansion, cosine shortlist, cross-encoder or hybrid rerank, grounded answers, citation validation (no invented sources) | `industrial/knowledge_base.py`, `reranker.py`, `provenance.py` | Ollama embeddings, optional cross-encoder | WORKING | `test_industrial_pipeline.py`, `test_end_to_end_industrial.py` | Evidence + CitationValidator + reranker | **Kept; its documents are read by the fabric through an adapter.** |
| Industrial-history corpus (31 domain files, claim types) and knowledge sources | `industrial/corpus.py`, `knowledge/industrial_history/`, `knowledge_sources/` | KB | WORKING | `test_industrial_pipeline.py` | general-knowledge source (not company data) | Preserved, labelled as general knowledge. |
| Knowledge pack (Nexus Precision) - **explicitly SAMPLE / FICTIONAL** | `knowledge/JARVIS_PACK/`, `core/knowledge_docs.py` | - | WORKING | `test_write_document.py` | coverage reports it as SAMPLE, never as company history | Preserved, labelled. |
| Hermes memory (vendored, pinned) + native FTS5 backend; write policy (no secrets), recall fence, memory is information not authority | `memory/hermes_store.py`, `write_policy.py`, `recall.py`, `memory_manager.py`, `third_party/hermes` | SQLite | WORKING | `test_memory.py`, `test_memory_hermes.py` | **separate context section with its own provenance** | Preserved; kept apart from enterprise evidence. |
| Conversation history, facts | `memory/database.py` | SQLite | WORKING | `test_memory.py` | - | Preserved. |

## J. Industrial workbench

| Feature | Module | Status | Tests | Migration plan |
|---|---|---|---|---|
| Plant data model: equipment, readings, alerts, work orders, LOTO isolation, inventory, shifts (source-labelled; DEMO rows marked) | `industrial/plant_data.py`, `plant_ops.py` | WORKING (1 env test) | `test_industrial_plant.py` | Industrial workspace. |
| Plant analytics: trends, KPIs, anomaly checks, process flow, field map | `industrial/plant_analytics.py`, `analysis.py` | WORKING | `test_plant_analytics.py` | Industrial workspace; also feeds the numerical engine. |
| Engineering calculations (pump power, unit conversion, ...) | `industrial/engineering.py`, calculator plugin | WORKING | `test_tools.py` | Workbench. |
| Telemetry, alerts, equipment dossier | `industrial/telemetry.py`, `plant_tools.py` | WORKING | `test_industrial_plant.py` | Industrial workspace. |
| Demo refinery seed (labelled DEMO, admin only) | `industrial/demo_refinery.py`, `scripts/seed_demo_refinery.py` | WORKING | `test_end_to_end_industrial.py` | Preserved, labelled. |

## K. Productivity

| Feature | Module | Status | Tests | Migration plan |
|---|---|---|---|---|
| To-dos, calendar, notes, meeting minutes (audio / transcript), schedule planner, reminders | `memory/database.py`, `core/builtin_tools.py`, `industrial/meetings.py` | WORKING | `test_productivity.py`, `test_schedule_planner.py`, `test_vault_and_productivity.py` | Workbench -> Productivity screen (not in the requested screen list, kept because nothing may be lost). |

## L. Operations, offline guarantees, LAN

| Feature | Module | Status | Tests | Migration plan |
|---|---|---|---|---|
| BAT entry points (13): INSTALL_AGNI, START_AGNI, REPAIR_AGNI, FIREWALL_AGNI, TEST_AGNI, SETUP_AGNI, SETUP_VOICE, VERIFY_MODELS, APPLY/REMOVE_FIREWALL, INSTALL, INSTALL_ALL, START | root `*.bat` | WORKING | `test_ops_and_security.py` (CRLF, exit codes) | Preserved; **added:** RAG_INGEST, RAG_REINDEX, RAG_EVALUATE, SECURITY_CHECK. |
| Ops CLI (16 commands: system, preflight, repair, install-verify, firewall *, test *) with states PASS / FAIL / WARN / NOT_TESTED / INPUT_REQUIRED / DEGRADED / UNAVAILABLE, timestamped run logs, diagnostic report | `agni_ops/` | WORKING | `test_ops_and_security.py` | Preserved; RAG suites added. |
| Installer / voice setup / repair / restore / release builders | `scripts/install_everything.py`, `setup_voice_engine.py`, `agni_ops/repair.py`, `RESTORE_MY_DATA.py`, `make_release.py`, `make_anonymous_release.py` | WORKING | `test_install_everything.py`, `test_ops_and_security.py` | Preserved; restore list gains the knowledge-fabric store. |
| Offline / sovereign runtime: no downloads at runtime, HF offline, Chromium background networking off, `agni://` interceptor blocks external requests, offline self-test | `voice/offline.py`, `ui/agni_shell.py`, `scripts/offline_test.py` | WORKING | `test_start_and_rendering.py`, offline_test | Preserved; the new UI loads nothing from the network (fonts are Windows system fonts). |
| LAN / multi-node: sovereign mode allows configured LAN ranges; Ollama on another LAN host (`[ollama] allow_remote`); `probe_network` | `security/egress.py`, `core/ollama_client.py` | WORKING | `test_security.py` | Preserved. |
| Logging (`logs/medha.log`), run logs, audit log | `core/logger.py`, `agni_ops/runlog.py` | WORKING | several | Preserved. |

---

## What the upgrade adds on top (never instead)

See `docs/MEDHA_RAG_UPGRADE_PLAN.md` (knowledge fabric, planner, temporal, entities,
graph, numerical engine, decision support, single analysis object with DOCX / XLSX /
PPTX / PDF renderers, coverage, evaluation), `docs/UI_ARCHITECTURE.md` and
`docs/UI_DESIGN_SYSTEM.md` (new interface), `docs/AUTH_ARCHITECTURE.md` (new login on
the same security core).

Added 2026-09-27 (on top, nothing replaced): **registers** - spreadsheets whose rows are
records, answered row by row by the engine with a data-quality check against today's
date (`knowledge_fabric/registers.py`, tool `company_registers`, Document Explorer >
Registers); personal-data registers restricted to HR; the owner's sample knowledge base in
`samples/agni_rag_knowledge_base/` (labelled SAMPLE); vector embeddings built (574
passages, bge-m3 local).
