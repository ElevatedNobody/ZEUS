# Regression report - ZEUS upgrade (2026-09-27)

Machine: the owner's ROG Strix (Windows 11, 15.4 GB RAM, RTX 5060 Laptop), Ollama running
with the local models. Every number below is from a run on this machine; the logs are the
pytest output of each run.

## Full suite (`pytest tests`, per-test timeout 180 s)

| Run | Result |
|---|---|
| Pre-upgrade baseline, 2026-09-25 | 796 passed, 10 failed (all environmental - list below) |
| 2026-09-27, run 4 | 1049 passed, 30 failed, 1 error |
| 2026-09-27, run 5 | **1059 passed, 13 failed, 38 skipped** |
| Run 5's 5 new failures, fixed and re-run | 82 passed (the affected files in full) |
| 2026-09-27 evening, after changelog section 10 | **1154 passed, 8 failed** - exactly the 8 environmental failures listed below |
| same, end-to-end in the real window (`AGNI_RUN_E2E=1`) | **40 passed** |
| same, `RAG_EVALUATE` / `HERMES_CHECK` / model probe | 19/19, 24/24, 64/64 checks |
| 2026-09-28, after changelog section 11 (Whisper fallback, RAG routing and context) | **1202 passed, 8 failed, 41 skipped** (32 min) - the same 8 environmental failures |
| same, `RAG_EVALUATE` | 19/19 |
| same, end-to-end in the real window (`AGNI_RUN_E2E=1`) | **40 passed** |
| same, live voice -> Whisper -> router -> RAG -> deepseek-r1 -> answer + sources (`AGNI_RUN_LIVE=1 tests/e2e/test_voice_rag_live.py`) | **1 passed** (37 s) |

A first 2026-09-28 run WITHOUT the per-test timeout stalled at about 38 % (pytest idle, no child
process, no model generating). It was stopped; the files around that point passed on their own
(39 passed + the known Ollama-down failure), and the full re-run with `--timeout 180` - as the
baselines use - completed with no timeout triggered. The cause of the stall was not identified.

Section 10 changed three test contracts on purpose: the chat status line now states the
verification state (VERIFIED only on company records), the MIC indicator shows the recogniser's
real stage (the page no longer claims LISTENING), and a complex question needs the model's facts
separated from its inferences. The e2e firewall check now waits for the loaded rule list - it
used to match its own subtitle.

**Run 4 -> 5.** 21 failures in run 4 were test pollution. The new knowledge-fabric tests
monkeypatched `chat` on the shared `ollama_client` instance, and pytest's undo leaves a bound
method on the instance that shadows every later `patch("...OllamaClient.chat")`. The new tests
now patch the classes. The other new failures were tests of the removed windows (migrated
below).

**Run 5's new failures, and what was done:**

| Test | Cause | Resolution |
|---|---|---|
| `test_authentication::test_login_screen_exists_and_offers_no_bypass` | read `ui/access.py`, removed with the previous interface | now checks the ZEUS sign-in (`ui/medha/js/auth.js`, `auth/web_login.py`) for the same banned bypasses |
| `test_authentication::test_there_is_exactly_one_authentication_screen` | expected `access.py` as the one login screen | now: no Qt login screen at all, and exactly one sign-in page script |
| `test_chatterbox_voice::test_the_brand_lockup_survives_a_rename` | scanned the removed window files for "J A R V I S" | scans `ui/agni_shell.py` and every file of `ui/medha` |
| `test_decision_engine::test_write_document_generates_and_saves_docx`, `test_write_document::test_stale_ollama_cache_...` | the new file summary said "Saved to"; the tests (the existing contract) expect "saved" | the summary now says "File saved to <path>" |

**Remaining 8 failures - environmental, all present in the pre-upgrade baseline:**

| Test | Why it cannot pass on this machine as configured |
|---|---|
| `test_end_to_end_industrial::test_i2_industrial_answers_work_with_ollama_down` | asserts Ollama is NOT running; it is |
| `test_industrial_plant::test_report_works_without_a_language_model` | same - with Ollama up the narrative is included |
| `test_rag_and_vision::test_index_folder_reports_ollama_offline_clearly` | the legacy indexer now degrades to keyword search instead of erroring (baseline) |
| `test_install_everything::test_check_never_downloads_whisper` | the Whisper model is installed on this machine, so the check reports OK |
| `test_voice_integration` x3 (model paths, Whisper / Chatterbox local load) | `/tmp/...` test paths resolve to `C:\tmp\...` on Windows |
| `test_chatterbox_voice::test_nothing_is_still_called_jarvis` | the scan also reaches files generated on this machine (`AGNI_DIAGNOSTIC_REPORT.json/.txt`, `AGNI_VERIFICATION_REPORT.md`, which record the absolute `...\ZEUS\...` folder path), `README_FIRST.txt`, and third-party files inside `.venv` (networkx, vba_extract) |

## Targeted suites (all passing)

| Suite | Result |
|---|---|
| ZEUS end-to-end through the real window (`AGNI_RUN_E2E=1 tests/e2e/test_medha_e2e.py`) | **38 / 38** - sign-in, every screen, commands, pipeline, approvals, lock, 0 idle animation frames |
| RAG evaluation (`RAG_EVALUATE.bat`) | **19 / 19** |
| Knowledge fabric (`tests/rag`, `reasoning`, `artifacts`, `temporal`, `knowledge_graph`, `entity_resolution`, company orchestration) | all passing, including the 27 register tests |
| Interface contract (`test_ui_capability_coverage`, `test_no_previous_ui`, `test_medha_interface_behaviour`, `test_zero_regression_contract`) | all passing |

## Found by testing this round (each fixed, each with a test)

* The voice wave ran on an idle home screen (e2e): panel reads set the assistant state.
* `Monthly_Rent_INR` rendered as "MonthlyRentINR": underscores were read as italics.
* Register rows were stored as dated facts; an HR register was readable by every role;
  record rows crowded text answers; a list of sample questions outranked answers; YAML
  front matter surfaced as a passage (`docs/RAG_EVALUATION.md`).
* A second command could run alongside the first; file results were dumped raw; text under
  10 px; the camera was not released when the window closed.
* A test pollution pattern, above.

Not done: the audit-chain quarantine (`python -m agni_ops audit-repair --apply`) waits for the
owner's approval - it moves 46 records signed by a test's temporary key out of the main chain
into a quarantine file (a backup is kept). Until then `SECURITY_CHECK.bat` reports the chain
as WARN/FAIL.
