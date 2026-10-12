# Architecture Audit and Orchestration Upgrade

What was already here, what was added, and how the two are joined.

**Result: 418 tests pass (375 before this work, 43 new). Nothing was removed,
replaced or disabled. Tool count went 139 → 145; agent roster 11 → 27.**

---

## 1. The audit, before anything was changed

A file-by-file inventory of the project as it stood.

| Layer | Modules | Lines | What it owns |
|---|---|---|---|
| `core/` | 14 | ~4,500 | tool registry, decision engine, model router, planner, task manager, security manager, Ollama client, office helper, agents |
| `memory/` | 2 | ~1,300 | one SQLite database (conversation, facts, todos, calendar, notes, RAG chunks, industrial KB, users, sessions) |
| `industrial/` | 22 | ~10,000 | plant data, plant ops, plant tools, analysis model, three renderers, doc pipeline, OCR engines, knowledge base, reranker, schemas, taxonomy |
| `voice/` | 5 | ~1,500 | Whisper STT, Chatterbox TTS, offline enforcement |
| `auth/` | 4 | ~780 | Argon2id/bcrypt/scrypt hashing, sessions, lockout |
| `ui/` | 4 | ~2,200 | dashboard, HUD widgets, login window |
| `automation/`, `plugins/` | 5 | ~240 | desktop control, plugin loader |
| `scripts/` | 8 | ~2,500 | setup, diagnostics, offline test, startup check |

### Interfaces found (and preserved verbatim)

| Interface | Contract | Status |
|---|---|---|
| `DecisionEngine.process(text, context)` | returns `{response, intent, refresh, agent?, ui_action?}` | **unchanged** — extra `pipeline` key added alongside |
| `registry.execute(name, **kwargs)` | returns `ToolResult(success, output, error)` | unchanged |
| `model_router.route(slot)` | returns a model name | unchanged — now also wrapped |
| `memory.database.*` | one SQLite file, user-scoped | unchanged |
| `security_manager.check_and_confirm` / `_audit` | risk gating and audit log | unchanged — pipeline now writes one line per run |
| `main.handle_text` / `listen_and_process` | voice and text share one path | unchanged |
| `core.office.verify_office_file` / `finish` | reopen-before-claiming | unchanged — validators build on it |

### What the audit concluded

Three genuine gaps, and one thing that was **already right**:

1. Already right: **voice and text already shared one pipeline.** `listen_and_process` → `handle_text` → `engine.process`. No second assistant existed and none was created.
2. Gap: **model selection was task-type only.** No RAM awareness, no "smallest capable", no concept of "this needs no model at all".
3. Gap: **no validation layer.** `core/office.py` verified office files well, but nothing checked code, OCR, RAG grounding, or data — and nothing repaired a failure.
4. Gap: **multi-step planning needed the LLM.** `planner.decompose` required Ollama, so "make a report and a deck and a workbook" failed entirely when Ollama was down.

---

## 2. What was added

One new package, `core/orchestration/` — 9 modules, additive only.

```
                        USER REQUEST  (typed or spoken - same path)
                                 |
                    +------------v-------------+
                    |  DecisionEngine.process  |   help / UI actions first
                    +------------+-------------+
                                 |
                    +------------v-------------+
                    |      ORCHESTRATOR        |
                    +------------+-------------+
                                 |
   CLASSIFY  --> MEMORY --> PLAN --> ROUTE --> EXECUTE --> VALIDATE --> AUTO-FIX --> DELIVER
   classifier.py  database  task_    router.py  registry   validators   autofix.py   contracts
   (regex, no     (bounded  graph.py (smallest  (existing  .py (12      (14 bounded  (structured
    model)         facts)   (DAG)     capable)   139 tools) kinds)       strategies)  result)
                                 |
                          activity.py  ->  dashboard PIPELINE card
                                 |
                       security_manager._audit  ->  logs/audit.log
```

| Module | Lines | Responsibility |
|---|---|---|
| `contracts.py` | 250 | `TaskKind`, `Complexity`, `PlanStep`, `ValidationReport`, `MedhaError`, `OrchestrationResult`. No imports of models, tools or the database. |
| `classifier.py` | 230 | Deterministic task classification. The model is consulted only if nothing matched **and** Ollama is already up. |
| `router.py` | 260 | `CapabilityRouter`: smallest capable model, RAM-aware step-down, `release()`. Wraps the existing `model_router`. |
| `task_graph.py` | 250 | Deterministic decomposition into a dependency graph; LLM planner as fallback, every tool checked against the registry. |
| `agents.py` | 330 | 19 specialists. Each declares its tools, model slot and validator. Publishes into the original `AGENT_REGISTRY`. |
| `validators.py` | 420 | 12 validator kinds. Built on `core.office.verify_office_file`. |
| `autofix.py` | 200 | 14 repair strategies, each changing something specific. Hard cap of 2 attempts. |
| `orchestrator.py` | 400 | The pipeline. Injected with the engine's route matcher and chat handler. |
| `activity.py` | 100 | Thread-safe TASK / AGENT / MODEL / TOOLS / STATUS board for the UI. |
| `tools.py` | 145 | Six tools exposing the pipeline to the user. |

---

## 3. The joining decision: wrap, never replace

The orchestrator does **not** own the command table or the chat behaviour. Both
are injected from `DecisionEngine.__init__`:

```python
self.orchestrator = get_orchestrator(
    direct_matcher=self._match_direct_command,   # the existing 200+ routes
    chat_handler=self._chat_response,            # the existing chat prompt
    formatter=self._format_tool_output)          # the existing output format
```

Consequences that matter:

- Every command that resolved before resolves to **the same tool, through the same regex**.
- Every conversational reply uses **the same system prompt** as before.
- If the orchestration layer fails to import, `process()` falls through to the
  original code path. The pipeline is an upgrade, not a dependency.

---

## 4. Model routing on a 16 GB machine

**Deterministic first.** Seven task kinds are declared to need no model at all:
`ocr`, `pdf`, `conversion`, `file_ops`, `industrial_data`, `automation`,
`memory`. A plant query loads nothing:

```
show the plant status  ->  kind=industrial_data  agent=operations  model=none
```

**Smallest capable.** Each (kind, complexity) pair has a ladder of slots. The
first slot with a pulled model wins. The agent that will do the work can name
the slot it needs — the Report Agent asks for `json_structured` (3B), so a long
sentence does not escalate it to a 7B reasoning model.

**RAM aware.** Every candidate's resident size is estimated from its own tag
(parameters × bytes-per-parameter for the quantisation, plus overhead). If it
does not fit in free RAM minus headroom, the router steps **down** the ladder
rather than swapping. Tested by forcing 2 GB free and asserting no 7B model is
chosen.

**Honest when a model is missing.** A slot with nothing pulled reports
`try: ollama pull <model>` rather than silently substituting a text model for a
vision task.

| Slot | Model | Used for |
|---|---|---|
| `classify` | granite3.1-dense:2b / smollm2:1.7b / qwen2.5:0.5b | one-word decisions |
| `fast` | phi3.5:3.8b / gemma2:2b | short chat |
| `default` | qwen2.5:3b | general |
| `json_structured` | qwen2.5:3b | document/deck/workbook structure |
| `reasoning` | mistral:7b / deepseek-r1:7b | analysis, RAG answers |
| `code` | qwen2.5-coder:7b | code |
| `vision` | qwen2.5vl:3b / moondream:1.8b | images |
| `embedding` | bge-m3 | RAG chunks |
| `reranker` | bge-reranker-v2-m3 | RAG ranking |

---

## 5. Validation and repair

Twelve validators: `docx`, `xlsx`, `pptx`, `pdf`, `csv`, `bundle`, `file`,
`code`, `rag`, `ocr`, `data`, `text`.

The office validators go beyond "the file opens":

- **pptx** — walks every shape on every slide and fails on anything crossing the
  slide edge, or type below 8.5 pt. This catches the defect a format check
  cannot see.
- **xlsx** — checks formulas are present and that cross-sheet references point
  at sheets that exist, so no one receives a workbook of `#REF!`.
- **docx** — headings, body text, tables.
- **bundle** — a briefing pack is checked file by file. One bad file fails it.
- **code** — parsed with `ast.parse`. ZEUS does not say code works because it
  looks like code.

**The rule learned the hard way:** a validator may only fail on *positive
evidence* of a problem. Three tests failed during development because the
validators were vetoing tools that had genuinely succeeded but reported their
result in a shape the validator did not recognise. The `file` validator now
records "not checked" instead of failing; the `rag` validator accepts a tool's
own answer shape; and a tool's own error message is never replaced by a
validator's more generic one. `test_a_validator_never_vetoes_a_tool_it_simply_does_not_understand`
guards this.

Fourteen repair strategies, each changing something specific — fewer items per
slide for an overflow, a fresh path for a corrupt write, the parser's own
message fed back for a syntax error. **Maximum two attempts.** A retry that
changes nothing is not a repair.

---

## 6. Multi-step decomposition without a model

The brief's flagship case now works with Ollama down:

```
"create an executive report and an excel workbook and a powerpoint for P-204"

  -> MULTI_STEP_TASK, 3 steps, one subject
  s1 executive_report      -> analysis_report_P-204.docx    verified
  s2 executive_workbook    -> analysis_workbook_P-204.xlsx  verified
  s3 executive_presentation-> executive_deck_P-204.pptx     verified
```

Three files, one analysis, numbers guaranteed to agree. When the request names
a source file, it is read **once** and the deliverables depend on that step —
so a spreadsheet is profiled a single time and the deck, report and workbook
all render the same analysis.

---

## 7. UI

A new **PIPELINE** card sits beside the AI MODEL card, showing:

```
PIPELINE
create an executive presentation
Report · qwen2.5:3b-instruct-q4_K_M
executive_presentation
● validating
```

Task, agent, model, tools, status. Concise by design — no prompts, no model
reasoning, no chain-of-thought.

Six tools make the same information answerable by asking: `pipeline_status`,
`list_specialists`, `model_routing`, `validation_report`, `release_models`,
`explain_last_run`. Reachable by voice too, since voice and text share the path.

---

## 8. What was verified

| Check | Result |
|---|---|
| `pytest tests/` | **418 passed** (375 pre-existing + 43 new) |
| `scripts/offline_test.py` | Orchestration Pipeline **PASS** — 7 stages, plant query ran with no model, corrupt file refused, repair proposed |
| `scripts/startup_check.py` | exit 0; new rows for the pipeline and the router |
| External dependency scan | 102 files, no network dependency, no web framework |
| Route regression | every documented command still resolves to the same tool |

---

## 9. Honest limitations

1. **Ollama was not running in the build environment.** Every deterministic
   path — routing, classification, planning, validation, repair, and all
   document generation from plant records — was exercised fully. The paths that
   genuinely need a model (chat, narrative, LLM-assisted planning, vision) were
   exercised through their *failure* branches only.
2. **The RAM figure is an estimate**, parsed from the model tag. It orders
   candidates and refuses an obvious misfit; it is not a measurement.
3. **`release()` asks Ollama to evict** via `keep_alive: 0`. Ollama decides
   when memory is actually returned.
4. **The Inspection Agent's flagship chain** (OCR → findings → SOP retrieval →
   reranking → approval note) has every component built and registered, but is
   not yet wired as one single command.
5. **Roles and confidential-data categories** from the wider brief are not
   built. Authentication, per-user scoping and the audit log are in place; the
   permission matrix and restricted formula store are not.
