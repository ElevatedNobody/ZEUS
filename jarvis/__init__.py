"""
ZEUS - Intelligent Data Capture & Schedule-Linking Layer for Infrastructure
Project Management (Real-Time Actual Progress Tracking: the planning-to-execution
bridge). The package keeps the name it was written under; see docs/CRUDEX_PROJECTS_ARCHITECTURE.md.

    store        the project database (relational + append-only event store)
    dates        day-level dates, ranges and Indian / Hindi month names
    vernacular   regional-language, transliterated and site-shorthand normalisation
    capture      evidence intake: files, images, voice memos, typed notes (provenance kept)
    extraction   structured facts from captured text (rules + the local EXTRACTION model)
    linking      field reference -> schedule activity resolution with confidence
    importers    schedule providers: CSV / XLSX, MS Project XML, Primavera XER
    cpm          deterministic critical-path engine with actuals and blockers
    events       the event store: append, approve, project state as of any time
    forecast     recompute after an approved update, snapshots, forecast changes
    explain      "why did the finish date move?" - the causal chain with evidence
    chronology   dated delay chronology, contract notice deadlines, delay windows
    sync         offline conflict-safe synchronisation between devices
    learning     project-learning cards from completed, evidence-linked activities
    search       hybrid, temporal project search; state reconstruction questions
    models       task -> local model routing, verified against what is installed
    reports      progress report, chronology, evidence pack, schedule analysis
    service      the pipeline the tools, the orchestrator and the interface call
    tools        registered tools (every call passes ZEUS's policy engine)

Everything is local: SQLite, the Office libraries already installed, the OCR
engines ZEUS ships, and Ollama on loopback for models.
"""

from __future__ import annotations

PRODUCT = "ZEUS"

# Where a record came from. Only "company" is the organisation's own history;
# everything else is labelled as what it is, on screen and in every report.
ORIGIN_COMPANY = "company"
ORIGIN_DEMO = "demo"
ORIGIN_SAMPLE = "sample"
ORIGIN_FIXTURE = "test_fixture"
ORIGINS = (ORIGIN_COMPANY, ORIGIN_DEMO, ORIGIN_SAMPLE, ORIGIN_FIXTURE)


def origin_label(origin: str) -> str:
    return {"company": "RECORDED", "demo": "DEMO", "sample": "SAMPLE",
            "test_fixture": "TEST FIXTURE"}.get(origin or "", (origin or "UNKNOWN").upper())
