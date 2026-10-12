# ZEUS Feature Inventory — baseline before the Vault/Productivity work

Taken before any code was changed. This is the parity contract: everything
below must still work afterwards.

**Baseline, counted from the live registries rather than from the source:**
147 unique tools, 19 specialist agents, 12 validators, 14 auto-fix strategies,
8 model slots, 164 routes, 543 passing tests, 16 database tables.

*(An earlier draft of this document said 20 agents, 15 strategies and 9 slots.
Those were miscounts in the summary line — the tables below it listed 19, 14
and 8. The figures here are what the registries actually report, which is why
the parity test asserts against the registries and not against this document.)*

---

## 1. What the new features would have duplicated

The single most important finding. **Three of the seven "new" features already
exist and work.**

| Asked for | Already exists | Where |
|---|---|---|
| Persistent Tasks | `todos` table + 5 tools + full CRUD | `memory/database.py:424-482`, `core/builtin_tools.py:1577-1620` |
| Calendar | `calendar_events` table + 3 tools | `memory/database.py:485-535`, `core/builtin_tools.py:1641-1673` |
| Notes (implied) | `notes` table + 4 tools | `memory/database.py:538-590`, `core/builtin_tools.py:1675-1714` |
| Productivity navigation | `productivity` **specialist agent with 14 tools** | `core/orchestration/agents.py:260` |

Building a "new task system" would have produced a second to-do list beside the
existing one, with its own table, and split the user's data across both. The
brief's own warning — *do not replace the existing agentic task system with a
simple to-do list* — applies in the other direction too: **do not replace the
existing personal to-do list with a second one.**

**Therefore: Tasks, Calendar and Notes are EXTENDED and EXPOSED, never rebuilt.**

Genuinely greenfield (zero matches for `vault|employee|staff|personnel|encrypt`
anywhere in the repository):

- Secure File Locker
- Employee Data
- Vault authentication and its audit trail
- Meeting-specific fields (attendees, location, agenda, duration)

---

## 2. Existing capability map

### Orchestration — `core/orchestration/`
The pipeline is `classify → memory → plan → route → execute → validate →
autofix → deliver`, in `orchestrator.py:66-93`. **Not to be bypassed by new
features.**

- 18 `TaskKind` values, 4 `Complexity` levels, 9 `Stage` values
- 19 specialists (`agents.py:65-278`), each declaring `tools`, `model_slot`,
  `validator_kind`, `deterministic`
- 12 validators behind one signature `validate(kind, payload) -> ValidationReport`
- 14 auto-fix strategies, `MAX_ATTEMPTS = 2`
- **`OrchestrationResult.to_engine_response()` (`contracts.py:239-266`) is the
  backward-compatibility seam.** Adding keys is safe; changing `response`,
  `intent` or `refresh` is not.

### Tools — 147 unique, 4 sources
`core/builtin_tools.py` 77 · `industrial/plant_tools.py` 48 ·
`industrial/tools.py` 15 · `core/orchestration/tools.py` 7 · plugin 1

Relevant existing groups: files & folders 9 · documents 4 + 16 industrial
deliverables · **todos 5 · calendar 3 · notes 4 · schedules 2** · memory/facts 3
· RAG 5 + 12 industrial · vision/OCR 3 · plant 38 · automation 25.

### Model routing — two layers, both preserved
`core/model_router.py` maps slot → model (8 chat slots). `core/orchestration/router.py`
adds RAM-aware step-down. `embedding`/`reranker` **raise `ValueError`** if routed
as chat slots — the architectural fix from the previous work.

### Capabilities — `core/capabilities.py`
`Health` has 7 states. Nothing reports READY without a real call.

### Auth — `auth/`
Argon2id → bcrypt → scrypt, self-describing hash format, never falls back to a
bare hash. `AuthResult.__bool__` returns `.ok`. Lockout is per-account and
process-local for unknown usernames (no enumeration oracle).

### Per-user data scoping — `memory/database.py:283-305`
`_USER_SCOPED_TABLES = ("messages", "facts", "todos", "calendar_events",
"notes", "schedules")`. A module-global `_ACTIVE_USER_ID`, set on login/unlock,
cleared on logout. When `None`, every query behaves as it did before login
existed — this is what keeps the CLI and tests working.

**Any new personal table added to that tuple is automatically migrated,
counted by `orphan_row_counts()`, and adopted by `adopt_orphan_rows()`.**

### Security — `core/security_manager.py`
5 risk levels, `CONFIRM_THRESHOLD = HIGH_RISK`, protected paths, blocked command
patterns, JSON-line audit log at `logs/audit.log`.

**`validate_path` is NOT called by `registry.execute()`** — it is called
manually by 12 individual tool bodies. Any new file-touching tool must call it
explicitly.

---

## 3. Defects found during the inventory

Recorded because they affect the work, not to be silently absorbed.

| # | Defect | Where | Consequence |
|---|---|---|---|
| 1 | `create_presentation` registered **twice** — first on a private helper | `builtin_tools.py:1146` and `:1243` | Last-write-wins made it correct only because of the order the two appear in the file. **Fixed:** the decorator is off the helper. |
| 2 | Command Center defines **none** of `refresh_todos`, `refresh_notes_panel`, `refresh_tasks`, `set_user` | `main.py:123-131` calls all four | After a user switch the Command Center still shows the **previous** operator's name and avatar. Fails silently. |
| 3 | **5 dead nav items** | `command_center.py:43-47` | "New task", "Voice", "Ask", "Workflows", "Scheduled" are in `NAV` but not `NAV_COMMAND`; clicking only moves focus. The docstring claims no item leads to a dead end. |
| 4 | Command Center **drops `ui_action`** | no `_apply_ui_action` | `match_ui_action` returns `{"type": "switch_tab"}`; the dashboard honours it, the Command Center ignores it. |
| 5 | **No logout** in the Command Center | `command_center.py:534-537` | `_lock` calls `on_lock()` with no argument, so it is always a lock. The dashboard has both. |
| 6 | `delete_user` orphans scoped rows | `database.py:1077` | Rows keep a stale `user_id`. |
| 7 | `MemoryManager.forget` does not delete | `memory_manager.py:57` | Writes an empty string. |
| 8 | `is_admin` is written and never read | schema `:206`, no branch anywhere | There is no role system, only `require_authenticated()` — which is never called. |
| 9 | `ArtifactCard` `verified` hardcoded to `None` | `command_center.py:725` | The verified chip never renders. |

Fixed in this work: 1, 2, 3, 4, 5. Left alone with a note: 6, 7, 8, 9 — each is
pre-existing, none blocks the new features, and fixing 8 properly means
designing a role system nobody asked for.

---

## 4. What the old dashboard exposes that the Command Center does not

This is the parity gap the new navigation must close. `ui/dashboard.py` is still
reachable with `--classic-ui`.

| Capability | Dashboard affordance | Command Center |
|---|---|---|
| Calendar month grid | `QCalendarWidget`, click a day | absent |
| Add event | title + `HH:MM` fields + Add button | absent |
| Delete event | double-click a row | absent |
| To-do list | `QListWidget` with per-item checkboxes | absent |
| Complete / uncomplete | checkbox → `complete_todo`/`uncomplete_todo` | absent |
| Add to-do | line edit + Enter | absent |
| Delete to-do | double-click | absent |
| Notes master/detail | list + editor + Save | absent |
| New / delete note | buttons, double-click | absent |
| Active task list | `QListWidget`, double-click to cancel | absent |
| Log out | dedicated button → `on_lock(True)` | absent (lock only) |
| `ui_action` tab switching | `_apply_ui_action` | absent |

Every one of these is preserved by being **built into the Command Center**, not
by keeping the dashboard around.

---

## 5. Design decisions that follow from the inventory

**1. Extend, do not duplicate.** `todos` gains `due_date`, `priority`,
`assignee`; `calendar_events` gains `kind`, `attendees`, `location`, `agenda`,
`duration_minutes`. Additive nullable columns through the existing
`_migrate_user_scoping` pattern, so every existing row and every existing
caller keeps working.

**2. A meeting is a calendar event with a kind.** Not a separate table. One
timeline, one set of queries, and "what's on Friday" returns both.

**3. The Vault is a new table plus encrypted blobs on disk.** It cannot reuse
`document_chunks` or the industrial store: those are deliberately *not*
user-scoped (shared workbench data), and vault contents must never enter RAG.

**4. Vault authentication is a separate passphrase, not the login password.**
The brief asks for "Vault authentication" as a distinct item. A separate
passphrase also means the encryption key is not re-derived on every password
change, and a shoulder-surfed login does not open the vault.

**5. Employee records live behind the vault.** They are PII. They get the same
audit trail and the same locked/unlocked gate.

**6. Both new domains become specialist agents with real tools**, so they run
through the same classify → plan → route → execute → validate pipeline as
everything else, rather than being UI-only features.

**7. The five dead nav items get real commands**, and the navigation grows two
sections (Productivity, Vault) rather than replacing the existing six.

---

## 6. Regression checklist

Verified after the work, by test, not by inspection:

- 543 pre-existing tests still pass (598 total after this work)
- 147 pre-existing tools still registered, none shadowed (166 total)
- 19 pre-existing specialists intact, 2 added, no tool owned by two agents
- The `_ROUTES` table still resolves every pre-existing pattern (new routes are
  appended and anchored, and a test asserts no new route shadows an old one)
- `to_engine_response()` still returns `response`/`intent`/`refresh`
- Embedding and reranking still refuse to be routed as chat slots
- Voice still reports its real state, still optional, still text-mode-safe
- The startup sequence still runs in order and still gates on authentication
- Every one of the 20 existing specialists still owns its tools
- Documents, spreadsheets, presentations and PDFs still generate and validate

---

## 7. Result

**604 tests passing** (543 before this work, 61 added). Verified from the live
registries, not from this document:

| | Before | After |
|---|---|---|
| Tools | 147 | 166 |
| Specialist agents | 19 | 21 |
| Validators | 12 | 12 |
| Auto-fix strategies | 14 | 14 |
| Model slots | 8 | 8 |
| Natural-language routes | 164 | 192 |
| Dead navigation items | 5 | **0** |
| Database tables | 16 | 19 |

Nothing shrank. `test_the_baseline_capability_counts_are_intact` fails the
build if any of those numbers ever drops below the "before" column.

### What was added

**Secure locker** — AES-256-GCM (Fernet fallback), key derived by scrypt
(n=2¹⁵) from a passphrase that is never stored and never passed as a tool
argument. Files are opaque blobs named by a random id; the plaintext SHA-256 is
checked on every read; `lock()` overwrites the key buffer. 8 tools, its own
agent, its own audit trail that records refusals as well as successes.

**Employee register** — 6 tools, its own agent, user-scoped, audited.

**Tasks and calendar — extended, not replaced.** `todos` gained `due_date`,
`priority`, `assignee`, `source`; `calendar_events` gained `kind`, `attendees`,
`location`, `agenda`, `duration_minutes`. All nullable, added by the existing
idempotent migration, so `add_todo(text)` and `add_event(title, date)` behave
exactly as before. A meeting is a calendar event with `kind="meeting"` — one
timeline, so `agenda_for` answers with events, meetings and the tasks due that
day together.

**Five working panels in the Command Center** — Tasks, Calendar & meetings,
Notes, Secure locker, Employee register — closing every gap in the table in
section 4. They go through the tool registry, never straight to the database,
so risk confirmation, the audit log and the timeout all still apply.

### Defects fixed along the way

1. `create_presentation` was registered twice, the first time on a private
   helper with a different signature. Only file order kept it correct.
2. `set_user`, `refresh_todos`, `refresh_notes_panel`, `refresh_tasks` did not
   exist on the Command Center, so all four failed silently and a user switch
   left the previous operator's name on screen.
3. Five navigation items did nothing.
4. `ui_action` was dropped, so "show my calendar" did nothing.
5. There was no way to sign out — only to lock.

### Caught by tests before shipping

- An employee-lookup route matching `who is (.+)` swallowed "who is the prime
  minister" and every question starting with "find".
- Four navigation commands pointed at phrases the engine does not route
  ("explain the pipeline", "show background tasks").
- Panels were connected to the status bar before it was constructed.
