# ZEUS interface architecture

The previous interface is discarded as a VISUAL system - its colours, layouts, cards,
navigation, login, dashboard, assets and copy. It is not restyled: the new interface
lives in a new directory (`ui/medha/`), is written from zero on a new design system
(`docs/UI_DESIGN_SYSTEM.md`), and the old visual files are deleted once every capability
they exposed is reachable from the new one (enforced by a test, section 7).

What is kept is not visual: the Qt host window, the typed bridge
(`ui/bridge_contract.py`), the `agni://` scheme handler that serves only local files,
and the request interceptor that blocks every external request. Those are the UI's
backend, and security depends on them.

## 1. Runtime

| Layer | Choice | Why |
|---|---|---|
| Host | PySide6 `QMainWindow` + Qt WebEngine (`ui/agni_shell.py`) | already verified offline; no browser, no port, no server |
| Page | `ui/medha/index.html`, classic scripts sharing `window.ZEUS` (no ES modules - module loading over the custom `agni://` scheme is a CORS risk), hand-written CSS | no build step, no framework download, no CDN; everything ships in the folder |
| Transport | QWebChannel object `agni`, typed request/reply + pushed events | the page can call nothing that is not in the contract |
| Fonts | Plus Jakarta Sans / JetBrains Mono when installed locally, else Segoe UI Variable / Cascadia Mono | offline, zero download |
| Graphics | one 2D canvas (the voice wave, `js/voicewave.js`), CSS transforms/opacity, inline SVG charts | the wave draws only while something happens; no WebGL, no video |
| Design | "Voice Core" - the owner's HTML mock of 2026-09-27, rebuilt offline (`docs/UI_DESIGN_SYSTEM.md`) | |

## 2. Screen map (every screen talks to the real backend)

Navigation is grouped into the areas requested: COMMAND, INTELLIGENCE, KNOWLEDGE,
WORKBENCH, INDUSTRIAL, AUTOMATION, SYSTEM, SECURITY, VAULT.

| # | Screen | Group | Backend it drives |
|---|---|---|---|
| 1 | Login | - | bridge `auth_state`, `auth_login`, `auth_setup`, `auth_unlock`, `auth_face*` |
| 2 | Authentication (face / liveness / second factor / lockout / lockdown / session expiry) | - | same, plus events `auth`, `auth_face` |
| 3 | Command Center | COMMAND | `submit`, `listen`, `set_speak`, `stop_speaking`, `history`, `status`, activity events |
| 4 | Company Intelligence (1989-2026 timeline) | INTELLIGENCE | `rag_coverage`, `rag_timeline`, `rag_documents` |
| 5 | Knowledge / RAG (ask with live pipeline) | KNOWLEDGE | `company_intelligence`, `ask_industrial_kb`, `ask_knowledge_base` |
| 6 | Document Explorer (Company records / **Registers** / Industrial & personal) | KNOWLEDGE | `search_company_documents`, `company_registers` (every register with its data-quality check), `list_industrial_documents`, `list_knowledge_sources`, ingest tools |
| 7 | Evidence Viewer | KNOWLEDGE | `rag_evidence`, `rag_show_provenance` |
| 8 | AI Reasoning Workspace | WORKBENCH | `analyze_question`, `rag_analysis` |
| 9 | Industrial Workspace (overview, plant, field map, analytics, process flow, alerts) | INDUSTRIAL | `plant_snapshot`, `plant_telemetry`, `equipment_details`, alert tools, `run_industrial_agent` |
| 10 | Artifact Workspace | WORKBENCH | `list_artifacts`, `verify_artifact`, `open_path` |
| 11 | Excel Analysis | WORKBENCH | `create_analysis_artifact(format=xlsx)`, workbook tools |
| 12 | Presentation Builder | WORKBENCH | `create_analysis_artifact(format=pptx)`, deck tools |
| 13 | Word Report Builder | WORKBENCH | `create_analysis_artifact(format=docx/pdf)`, report tools |
| 14 | Hermes Memory | SYSTEM | memory tools (list / search / forget) |
| 15 | Model Center | SYSTEM | `hardware_profile`, `model_routing`, `capability_report`, `model_integrity_report`, `pin_trusted_models`, `release_models` |
| 16 | Agent Center | SYSTEM | `agent_roster`, `run_industrial_agent`, activity events |
| 17 | Automation | AUTOMATION | automation, reminder and watcher tools |
| 18 | Vault | VAULT | vault tools, employee register, bridge `unlock_vault`, `pick_file` |
| 19 | Security Center | SECURITY | `security_status`, `sovereignty_report`, `network_status`, `set_network_mode`, events, users, face login, lockdown |
| 20 | Firewall | SECURITY | `firewall_status`, WFP tools |
| 21 | Audit | SECURITY | `show_audit_log`, `verify_audit_chain` |
| 22 | System Settings | SYSTEM | voice, session, password, network probe, diagnostics |
| 23 | Productivity (tasks, calendar, notes, meetings) | WORKBENCH | productivity tools - **kept although not in the requested list: nothing may be lost** |

## 3. Capability coverage (from the pre-upgrade interface)

All 77 capabilities the old interface reached (`ui_capabilities` in the baseline: 58
tools and 14 bridge methods it called, plus bridge methods it declared) must be reached
from `ui/medha/`. `tests/test_ui_capability_coverage.py` parses the new interface and
fails for any capability that is no longer reachable - including the ones only an
old tab exposed (for example `approval_note`, `seed_industrial_demo_data`,
`process_meeting_audio`, `wfp_blocked_connections`).

## 4. State and events

* `js/shell.js` holds the session's state (hello, last status, assistant state, the
  conversation); screens read it and re-render on events. Status is polled every 5 s,
  never overlapping and never while the window is hidden.
* Bridge events drive state: `state`, `level`, `approval`, `approval_done`, `security`,
  `notification`, `session`, `lockdown`, `refresh`, plus `auth_face`, `workspace` and
  `analysis` (the knowledge fabric's real stages). The assistant state also drives the
  voice wave and the words over it.
* **No invented status.** Every indicator renders a value the backend returned; a
  value that could not be measured is shown as UNKNOWN / UNAVAILABLE with the reason.
  A button whose capability is missing says so and why instead of pretending.

## 5. Layout

See `docs/UI_DESIGN_SYSTEM.md` - the voice home (wave, voice line, Field Telemetry and
Operations cards, prompt chips, conversation) and glass panel screens, a glass rail, the
top bar with MIC / AUDIO / CINEMATIC VIEW, and the command banner. The evidence sheet
slides in from the right.

## 6. Performance budget (target: the 16 GB laptop)

* Idle: nothing animates and nothing repaints - measured 0 animation frames in 3 s on the
  voice home (`tests/e2e/test_medha_e2e.py::test_idle_costs_no_animation_frames`).
* Motion: transform and opacity only; one `requestAnimationFrame` loop at most (the
  wave), advanced by elapsed time, stopped when idle, hidden or reduced-motion.
* One request through the engine at a time: a second is refused out loud by the page
  and, if the page failed to, by the bridge (`BUSY_REPLY`).
* Checked at 1536x864 (a 1920x1080 panel at 125 %) and 2000x1211.

## 7. Removal of the previous interface - DONE 2026-09-27

1. `ui/medha/` built; the host serves only `ui/medha` (`WEB_ROOT`), no fallback.
2. `tests/test_ui_capability_coverage.py`: all 77 capabilities of the previous interface
   are reached (three native-dialog calls replaced by in-page forms; the old bridge
   methods stay in the contract - `RETIRED` in `tests/test_zero_regression_contract.py`).
3. Removed after a byte-verified backup (`../retired_previous_ui_2026-09-27.zip`, 45 files,
   outside the application folder): `ui/web/`, `ui/access.py`, `ui/access_motion.py`,
   `ui/command_center.py`, `ui/dashboard.py`, `ui/panels.py`, `ui/widgets.py`,
   `ui/hud_widgets.py`, `ui/backdrop.py`, `ui/void_wave.py`, `ui/theme.py`,
   `ui/approval_dialog.py`, `ui/resources/backdrop.jpg`, and their window tests.
4. `tests/test_no_previous_ui.py` fails if any of them, an import of them, or their asset
   names come back.
5. The behaviours the old window tests protected are tested against ZEUS in
   `tests/test_medha_interface_behaviour.py` (one request at a time, nothing slow on the
   GUI thread, file results summarised, no text under 10 px, the sign-in claims nothing
   the backend did not say, the camera released on close) and in
   `tests/test_no_previous_ui.py` (rendering rules, voice badge honesty, speech state
   through a queued signal).
