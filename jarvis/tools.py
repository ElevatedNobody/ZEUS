"""
ZEUS registered tools.

Everything the interface and the orchestrator can do with ZEUS goes through
these, and so through `core.tool_registry.ToolRegistry.execute` - ZEUS's single
policy choke point (session, lockdown, role, classification, protected paths,
approval, audit chain). Names are chosen so the existing policy classifies them
correctly without a single change to security/policy.py:

    jarvis_*                              READ   (viewer and above)
    create_ / record_ / ingest_ / set_ /
    approve_ / resolve_ / register_ /
    request_ / export_ / seed_ / index_   WRITE  (operator and above)

Approvals, conflict resolution, link corrections, contract clauses, history
import and the DEMO scenario additionally require an engineer inside
jarvis/service.py - a stricter rule, never a looser one.
"""

from __future__ import annotations

import functools
from typing import Any, Callable, Dict, List, Optional

from core.logger import get_logger
from core.security_manager import RiskLevel
from core.tool_registry import registry

logger = get_logger("jarvis.tools")

SAFE, LOW = RiskLevel.SAFE, RiskLevel.LOW_RISK


def _guard(func: Callable) -> Callable:
    """Refusals and bad input come back as {"ok": False, "error": ...} - shown
    to the operator as the reason, never hidden."""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        from jarvis.service import Refused
        try:
            return func(*args, **kwargs)
        except (Refused, PermissionError) as error:
            return {"ok": False, "error": str(error)}
        except (ValueError, FileNotFoundError, KeyError) as error:
            return {"ok": False, "error": str(error)}
    return wrapper


def _flag(value) -> bool:
    return value is True or str(value).strip().lower() in ("1", "true", "yes", "on")


def _list(value) -> Optional[List[str]]:
    if value in (None, "", []):
        return None
    if isinstance(value, list):
        return [str(v) for v in value if str(v).strip()]
    return [v.strip() for v in str(value).split(",") if v.strip()]


def tool(name: str, description: str, risk=SAFE, params: Optional[Dict[str, str]] = None, timeout: float = 60.0):
    def decorator(func):
        registry.register(name, description, risk, params=params or {}, timeout=timeout)(_guard(func))
        return func
    return decorator


# ================================================================== READ =====

@tool("jarvis_projects", "ZEUS: list projects with their health (forecast vs baseline, blockers, approvals)")
def jarvis_projects():
    from jarvis import service
    return {"ok": True, "projects": service.list_projects()}


@tool("jarvis_overview", "ZEUS: one project's health - forecast and baseline finish, variance, progress, "
      "critical path, blockers, approvals, conflicts, notice deadlines", params={"project": "project id or code"})
def jarvis_overview(project: str = ""):
    from jarvis import service
    return {"ok": True, **service.overview(project or None)}


@tool("jarvis_home", "ZEUS: the command-centre cards for the current project", params={"project": "optional"})
def jarvis_home(project: str = ""):
    from jarvis import service, store
    projects = store.query("SELECT project_id, code, name, origin FROM jv_projects ORDER BY created_at DESC")
    if not projects:
        return {"ok": True, "projects": [], "overview": None}
    try:
        pid = service.resolve_project(project or None)
    except ValueError:
        pid = projects[0]["project_id"]
    return {"ok": True, "projects": projects, "overview": service.overview(pid, light=True)}


@tool("jarvis_schedule", "ZEUS: the schedule with CPM dates, float, critical path and drivers, optionally as it "
      "was known at a past date", params={"project": "project", "as_of": "optional date",
                                           "include_pending": "also apply unapproved updates (preview)"})
def jarvis_schedule(project: str = "", as_of: str = "", include_pending: bool = False):
    from jarvis import service
    return service.schedule_table(project or None, as_of or None, _flag(include_pending))


@tool("jarvis_versions", "ZEUS: schedule versions of a project", params={"project": "project"})
def jarvis_versions(project: str = ""):
    from jarvis import service
    return {"ok": True, "versions": service.versions(project or None)}


@tool("jarvis_compare_versions", "ZEUS: what changed between two schedule versions (dates, durations, logic, finish)",
      params={"version_a": "version id or label", "version_b": "version id or label"})
def jarvis_compare_versions(version_a: str, version_b: str):
    from jarvis import service
    return service.compare_versions(version_a, version_b)


@tool("jarvis_activity", "ZEUS: one activity - forecast, versions, events, evidence, correspondence, blockers, "
      "logic, aliases", params={"activity": "activity id or code", "project": "optional"})
def jarvis_activity(activity: str, project: str = ""):
    from jarvis import service
    return service.activity_detail(activity, project or None)


@tool("jarvis_pending", "ZEUS: field updates awaiting approval and field records whose activity must be confirmed",
      params={"project": "project"})
def jarvis_pending(project: str = ""):
    from jarvis import service
    return {"ok": True, **service.pending_updates(project or None)}


@tool("jarvis_evidence", "ZEUS: the evidence register of a project", params={
    "project": "project", "kind": "optional", "activity": "optional code", "query": "optional text"})
def jarvis_evidence(project: str = "", kind: str = "", activity: str = "", query: str = ""):
    from jarvis import service
    return {"ok": True, "evidence": service.evidence_list(project or None, kind, activity, query)}


@tool("jarvis_evidence_trail", "ZEUS: the full trail behind one evidence item or event - file hash, extractions, "
      "links, events, approvals, schedule effect", params={"evidence_id": "evidence id", "event_id": "or an event id"})
def jarvis_evidence_trail(evidence_id: str = "", event_id: str = ""):
    from jarvis import service
    return service.evidence_trail(evidence_id, event_id)


@tool("jarvis_why_moved", "ZEUS: why did the finish date move - the causal chain from the approved update, "
      "through the dependencies, to the finish, with evidence", params={
          "project": "project", "change_id": "optional forecast change", "use_model": "phrase with the reasoning model"},
      timeout=240.0)
def jarvis_why_moved(project: str = "", change_id: str = "", use_model: bool = False):
    from jarvis import explain, service, store
    pid = service.resolve_project(project or None)
    with store.connection() as conn:
        return explain.why_moved(conn, pid, change_id or None, use_model=_flag(use_model))


@tool("jarvis_forecast_changes", "ZEUS: every forecast change with its trigger and chain", params={"project": "project"})
def jarvis_forecast_changes(project: str = ""):
    from jarvis import explain, service, store
    pid = service.resolve_project(project or None)
    with store.connection() as conn:
        return {"ok": True, "changes": explain.list_changes(conn, pid)}


@tool("jarvis_chronology", "ZEUS: the dated, evidence-linked delay chronology with filters", params={
    "project": "project", "date_from": "optional", "date_to": "optional", "activity": "optional code",
    "contractor": "optional", "kinds": "optional comma list of event kinds", "source_type": "optional",
    "delay_type": "optional", "evidence_only": "only entries with evidence", "include_approvals": "optional"})
def jarvis_chronology(project: str = "", date_from: str = "", date_to: str = "", activity: str = "",
                      contractor: str = "", kinds: str = "", source_type: str = "", delay_type: str = "",
                      evidence_only: bool = False, include_approvals: bool = False):
    from jarvis import chronology, service, store
    pid = service.resolve_project(project or None)
    with store.connection() as conn:
        activity_id = None
        if activity:
            row = conn.execute("SELECT activity_id FROM jv_activities WHERE project_id = ? AND (code = ? OR activity_id = ?)",
                               (pid, activity, activity)).fetchone()
            if not row:
                return {"ok": False, "error": f"no activity '{activity}' in this project"}
            activity_id = row["activity_id"]
        rows = chronology.timeline(conn, pid, date_from or None, date_to or None, activity_id, contractor or None,
                                   _list(kinds), source_type or None, delay_type or None, _flag(evidence_only),
                                   _flag(include_approvals))
        contractors = [r[0] for r in conn.execute("SELECT DISTINCT contractor FROM jv_chronology WHERE project_id = ? "
                                                  "AND contractor IS NOT NULL", (pid,))]
    return {"ok": True, "entries": rows, "contractors": contractors, "delay_types": list(chronology.DELAY_TYPES)}


@tool("jarvis_notice_deadlines", "ZEUS: contract notice deadlines (open / met / late / missed) and reminders",
      params={"project": "project"})
def jarvis_notice_deadlines(project: str = ""):
    from jarvis import chronology, service, store
    pid = service.resolve_project(project or None)
    with store.connection() as conn:
        deadlines = chronology.deadlines(conn, pid, push=True)
        refs = [dict(r) for r in conn.execute("SELECT * FROM jv_contract_refs WHERE project_id = ?", (pid,))]
    return {"ok": True, "deadlines": deadlines, "contract_refs": refs}


@tool("jarvis_delay_windows", "ZEUS: delay windows between snapshots (the structure for a windows analysis)",
      params={"project": "project"})
def jarvis_delay_windows(project: str = ""):
    from jarvis import chronology, service, store
    pid = service.resolve_project(project or None)
    with store.connection() as conn:
        return {"ok": True, "windows": chronology.delay_windows(conn, pid),
                "note": "structured for a windows analysis; responsibility is not apportioned automatically"}


@tool("jarvis_learning_cards", "ZEUS: project-learning card - comparable completed activities, actual durations "
      "and productivity, as planning references", params={"activity": "activity code or id", "project": "optional"})
def jarvis_learning_cards(activity: str, project: str = ""):
    from jarvis import learning
    return learning.cards(activity, project or None)


@tool("jarvis_search", "ZEUS: hybrid, temporal project search with filters", params={
    "query": "text", "project": "project", "kinds": "optional comma list", "activity": "optional activity id",
    "date_from": "optional", "date_to": "optional", "known_before": "optional ISO time: only records captured before"},
      timeout=120.0)
def jarvis_search(query: str = "", project: str = "", kinds: str = "", activity: str = "", date_from: str = "",
                  date_to: str = "", known_before: str = ""):
    from jarvis import search, service
    pid = service.resolve_project(project or None)
    return search.search(query, pid, _list(kinds), activity or None, date_from or None, date_to or None,
                         known_before or None)


@tool("jarvis_ask", "ZEUS: answer a project question from the record (why the finish moved, what changed between "
      "updates, what was known on a date, when a slip was first known, evidence before a delay, chronology, "
      "comparables)", params={"question": "the question", "project": "optional", "use_model": "optional"},
      timeout=240.0)
def jarvis_ask(question: str, project: str = "", use_model: bool = False):
    from jarvis import ask
    return ask.ask(question, project or None, use_model=_flag(use_model))


@tool("jarvis_state_as_of", "ZEUS: what was known about an activity at the end of a past date (bitemporal)",
      params={"activity": "activity code or id", "as_of": "date", "project": "optional"})
def jarvis_state_as_of(activity: str, as_of: str, project: str = ""):
    from jarvis import service
    return service.state_as_of(activity, as_of, project or None)


@tool("jarvis_first_slip", "ZEUS: when the record first showed an activity slipping", params={
    "activity": "activity code or id", "project": "optional"})
def jarvis_first_slip(activity: str, project: str = ""):
    from jarvis import service
    return service.first_known_slip(activity, project or None)


@tool("jarvis_sync_status", "ZEUS: offline sync state - device, connectivity (probed), pending, conflicts, history")
def jarvis_sync_status():
    from jarvis import sync
    return sync.status()


@tool("jarvis_sync_conflicts", "ZEUS: offline sync conflicts with both versions", params={
    "project": "optional", "status": "open (default), resolved or empty for all"})
def jarvis_sync_conflicts(project: str = "", status: str = "open"):
    from jarvis import service, sync
    pid = service.resolve_project(project) if project else None
    return {"ok": True, "conflicts": sync.conflicts(pid, status)}


@tool("jarvis_model_routing", "ZEUS: which local model serves each task, verified against what is installed",
      params={"fresh": "re-check now"}, timeout=90.0)
def jarvis_model_routing(fresh: bool = False):
    from jarvis import models
    return models.routing_report(_flag(fresh))


@tool("jarvis_verify_events", "ZEUS: verify the event store's hash chain and the database integrity")
def jarvis_verify_events():
    from jarvis import events, store
    return {"ok": True, "events": events.verify_chain(), "database": store.integrity()}


@tool("jarvis_contract_refs", "ZEUS: configured contract notice clauses", params={"project": "project"})
def jarvis_contract_refs(project: str = ""):
    from jarvis import service, store
    pid = service.resolve_project(project or None)
    return {"ok": True, "contract_refs": store.query("SELECT * FROM jv_contract_refs WHERE project_id = ?", (pid,))}


# ================================================================= WRITE =====

@tool("create_jarvis_project", "ZEUS: create a project", LOW, params={
    "code": "short code", "name": "name", "client": "optional", "contractor": "optional", "location": "optional",
    "start_date": "optional", "workdays": "optional working weekdays, Monday=0 (e.g. 012345)"})
def create_jarvis_project(code: str, name: str, client: str = "", contractor: str = "", location: str = "",
                          start_date: str = "", workdays: str = "0123456"):
    from jarvis import service
    return {"ok": True, "project": service.create_project(code, name, client, contractor, location, start_date,
                                                          workdays=workdays or "0123456")}


@tool("set_current_project", "ZEUS: make a project the current one for spoken and typed field updates", LOW,
      params={"project": "project id or code"})
def set_current_project(project: str):
    from jarvis import service
    return service.set_current_project(project)


@tool("ingest_schedule_file", "ZEUS: import a schedule version (CSV, XLSX, MS Project XML, Primavera XER)", LOW,
      params={"project": "project", "path": "schedule file", "kind": "baseline or update", "label": "optional",
              "data_date": "optional", "approve": "approve now (engineer)"}, timeout=300.0)
def ingest_schedule_file(path: str, project: str = "", kind: str = "update", label: str = "", data_date: str = "",
                         approve: bool = False):
    from jarvis import service
    return service.import_schedule(project or None, path, kind or "update", label, data_date, _flag(approve))


@tool("approve_schedule_version", "ZEUS: approve or reject a schedule version (engineer)", LOW, params={
    "version_id": "version", "approve": "true / false", "reason": "optional"}, timeout=180.0)
def approve_schedule_version(version_id: str, approve: bool = True, reason: str = ""):
    from jarvis import service
    return service.approve_version(version_id, _flag(approve), reason)


@tool("record_field_update", "ZEUS: capture a typed or spoken field update (any language), extract, link to the "
      "schedule and submit it for approval", LOW, params={
          "text": "the update", "project": "optional", "source_type": "field / contractor / office",
          "occurred_on": "optional date", "use_model": "also use the local extraction / multilingual models"},
      timeout=900.0)
def record_field_update(text: str, project: str = "", source_type: str = "field", occurred_on: str = "",
                        use_model: bool = False):
    from jarvis import service
    if not (text or "").strip():
        return {"ok": False, "error": "the update is empty"}
    return service.capture_update(project or None, text=text, source_type=source_type or "field",
                                  occurred_at=occurred_on or None, use_model=_flag(use_model))


@tool("ingest_field_evidence", "ZEUS: capture a file as evidence - site photo, scanned or typed document, DPR, "
      "spreadsheet, voice memo or letter - then OCR / transcribe, extract, link and submit", LOW, params={
          "path": "the file", "project": "optional", "caption": "optional note", "source_type": "field / contractor / office",
          "correspondence": "treat as a letter", "use_model": "use the local extraction / multilingual models",
          "use_vision": "describe a photo with the local vision model (stored as inference)"}, timeout=900.0)
def ingest_field_evidence(path: str, project: str = "", caption: str = "", source_type: str = "field",
                          correspondence: bool = False, use_model: bool = False, use_vision: bool = False):
    from jarvis import service
    return service.capture_update(project or None, path=path, text=caption, source_type=source_type or "field",
                                  correspondence=True if _flag(correspondence) else None, use_model=_flag(use_model),
                                  use_vision=_flag(use_vision))


@tool("approve_field_update", "ZEUS: approve or reject a submitted field update (engineer) - the forecast is "
      "recomputed and any movement explained", LOW, params={"event_id": "event", "approve": "true / false",
                                                           "reason": "optional"}, timeout=180.0)
def approve_field_update(event_id: str, approve: bool = True, reason: str = ""):
    from jarvis import service
    return service.approve_update(event_id, _flag(approve), reason)


@tool("resolve_activity_link", "ZEUS: confirm or correct which activity a field record belongs to", LOW, params={
    "observation_id": "observation", "activity_id": "activity"})
def resolve_activity_link(observation_id: str, activity_id: str):
    from jarvis import service
    return service.confirm_link(observation_id, activity_id)


@tool("register_contract_notice", "ZEUS: configure a contract notice clause (engineer)", LOW, params={
    "project": "project", "clause": "e.g. GCC 19.1", "title": "title", "notice_type": "delay / claim_particulars / ...",
    "notice_period_days": "days", "trigger_types": "comma list: blocker_reported, delay_reported"})
def register_contract_notice(clause: str, notice_type: str, notice_period_days: int, project: str = "",
                             title: str = "", trigger_types: str = "blocker_reported,delay_reported"):
    from jarvis import service, store
    service.require_role("engineer", "configure contract notice clauses")
    pid = service.resolve_project(project or None)
    ref_id = store.new_id("REF")
    with store.connection() as conn:
        store.insert(conn, "jv_contract_refs", {
            "ref_id": ref_id, "project_id": pid, "clause": clause, "title": title, "notice_type": notice_type,
            "notice_period_days": int(notice_period_days), "trigger_types": store.dumps(_list(trigger_types) or []),
            "recipient": None, "description": title, "origin": service.get_project(pid)["origin"],
            "created_by": store.current_user(), "created_at": store.now()})
        store.audit("register_contract_notice", "contract_ref", ref_id, pid, {"clause": clause}, conn=conn)
    return {"ok": True, "ref_id": ref_id}


@tool("record_notice_issued", "ZEUS: record that a contract notice was issued (closes matching deadlines)", LOW,
      params={"project": "project", "activity": "activity code", "reference": "letter reference",
              "issued_on": "date", "notice_type": "e.g. delay", "to_party": "optional"})
def record_notice_issued(reference: str, issued_on: str, project: str = "", activity: str = "",
                         notice_type: str = "delay", to_party: str = ""):
    from jarvis import dates, events, service, store
    pid = service.resolve_project(project or None)
    with store.connection() as conn:
        activity_id = None
        if activity:
            row = conn.execute("SELECT activity_id FROM jv_activities WHERE project_id = ? AND code = ?",
                               (pid, activity)).fetchone()
            activity_id = row["activity_id"] if row else None
        event = events.append(conn, pid, "notice_issued", activity_id=activity_id, occurred_at=dates.iso(issued_on),
                              source="office", base_head=None,
                              payload={"reference": reference, "notice_type": notice_type, "to_party": to_party,
                                       "issued_on": dates.iso(issued_on)})
    return {"ok": True, "event_id": event["event_id"]}


@tool("resolve_sync_conflict", "ZEUS: resolve an offline sync conflict (engineer) - both versions stay in the history",
      LOW, params={"conflict_id": "conflict", "choice": "a, b or value", "value": "when choice is value",
                   "reason": "why"}, timeout=180.0)
def resolve_sync_conflict(conflict_id: str, choice: str, value: str = "", reason: str = ""):
    from jarvis import sync
    return sync.resolve(conflict_id, choice, value or None, reason)


@tool("set_sync_hub", "ZEUS: set the sync hub folder (a LAN share or a USB drive)", LOW, params={"folder": "folder"})
def set_sync_hub(folder: str):
    from jarvis import sync
    return sync.set_hub(folder)


@tool("export_sync_bundle", "ZEUS: write this device's pending field events to a sync bundle", LOW, params={
    "folder": "optional destination (default: the hub)"}, timeout=300.0)
def export_sync_bundle(folder: str = ""):
    from jarvis import sync
    return sync.export_bundle(folder or None)


@tool("ingest_sync_bundle", "ZEUS: import another device's sync bundle - conflicts are detected, never overwritten",
      LOW, params={"path": "bundle file"}, timeout=600.0)
def ingest_sync_bundle(path: str):
    from jarvis import sync
    return sync.import_bundle(path)


@tool("request_sync", "ZEUS: synchronise with the hub now (only if the hub is actually reachable)", LOW, timeout=600.0)
def request_sync():
    from jarvis import sync
    return sync.run()


@tool("ingest_historical_activities", "ZEUS: import a register of completed activities from past projects "
      "(engineer)", LOW, params={"path": "CSV / XLSX", "origin": "company, sample or demo"}, timeout=300.0)
def ingest_historical_activities(path: str, origin: str = "company"):
    from jarvis import learning, service
    service.require_role("engineer", "import historical project records")
    if origin not in ("company", "sample", "demo"):
        return {"ok": False, "error": "origin is company, sample or demo"}
    return learning.import_history(path, origin)


@tool("seed_jarvis_demo", "ZEUS: load the clearly-labelled DEMO scenario 'Highway Package A' through the real "
      "pipeline (engineer)", LOW, params={"use_model": "also run the local models on the DEMO evidence"}, timeout=1800.0)
def seed_jarvis_demo(use_model: bool = False):
    from jarvis import demo
    return demo.load(use_model=_flag(use_model))


@tool("index_jarvis_projects", "ZEUS: rebuild the project search index (and embeddings when the model is installed)",
      LOW, params={"embed": "also embed with bge-m3"}, timeout=1800.0)
def index_jarvis_projects(embed: bool = False):
    from jarvis import search, service
    service.require_role("engineer", "rebuild the project index")
    return search.reindex(embed=_flag(embed))


@tool("export_progress_report", "ZEUS: progress report (.docx) built from the project record", LOW,
      params={"project": "project"}, timeout=300.0)
def export_progress_report(project: str = ""):
    from jarvis import reports
    return reports.progress_report(project or None)


@tool("export_delay_chronology", "ZEUS: formal delay chronology (.docx, .xlsx or .pdf) with evidence references",
      LOW, params={"project": "project", "fmt": "docx, xlsx or pdf", "date_from": "optional", "date_to": "optional",
                   "activity": "optional code"}, timeout=300.0)
def export_delay_chronology(project: str = "", fmt: str = "docx", date_from: str = "", date_to: str = "",
                            activity: str = ""):
    from jarvis import reports
    if fmt not in ("docx", "xlsx", "pdf"):
        return {"ok": False, "error": "fmt is docx, xlsx or pdf"}
    return reports.chronology_report(project or None, fmt, date_from, date_to, activity)


@tool("export_evidence_pack", "ZEUS: evidence pack (.zip) - every evidence file, SHA-256 manifest, event chain, "
      "chronology", LOW, params={"project": "project", "activity": "optional code"}, timeout=600.0)
def export_evidence_pack(project: str = "", activity: str = ""):
    from jarvis import reports
    return reports.evidence_pack(project or None, activity)


@tool("export_schedule_analysis", "ZEUS: schedule analysis workbook (.xlsx)", LOW, params={"project": "project"},
      timeout=300.0)
def export_schedule_analysis(project: str = ""):
    from jarvis import reports
    return reports.schedule_analysis(project or None)


@tool("export_learning_report", "ZEUS: project-learning card as a document (.docx)", LOW,
      params={"activity": "activity code or id"}, timeout=300.0)
def export_learning_report(activity: str):
    from jarvis import reports
    return reports.learning_report(activity)


# ------------------------------------------------------- chat presentation ----

def _present_ask(out: Dict[str, Any]) -> str:
    if out.get("intent") == "why_moved" and (out.get("data") or {}).get("steps"):
        return _present_why(out["data"]) + "\n" + "\n".join(
            f"- [INFERENCE] {i}" for i in out.get("INFERENCE") or [])
    lines = [out.get("answer") or ""]
    for section in ("FACT", "INFERENCE", "PREDICTION", "RECOMMENDATION"):
        items = [i for i in out.get(section) or [] if i and i not in lines[0]]
        if items:
            lines.append(f"**{section}**")
            lines.extend(f"- {i}" for i in items[:10])
    return "\n".join(l for l in lines if l)


def _present_why(out: Dict[str, Any]) -> str:
    lines = [out.get("answer", "")]
    for s in out.get("steps") or []:
        lines.append(f"- **{s['step']}** {s['title']} - {s['detail']}")
    return "\n".join(lines)


def _present_capture(out: Dict[str, Any]) -> str:
    lines = [f"Captured as evidence {out.get('evidence_id')} ({out.get('kind')}, language {out.get('language')})."]
    if out.get("vernacular"):
        lines.append(f"Read as: {out.get('translation') or out.get('normalized_text')}")
    for o in out.get("observations") or []:
        link = o["link"]
        where = (link["candidates"][0]["code"] if link.get("candidates") else "no activity")
        state = {"linked": f"linked to {where} ({link['confidence']:.2f})",
                 "needs_confirmation": f"needs confirmation (best: {where}, {link['confidence']:.2f})",
                 "unlinked": "not linked"}[link["status"]]
        lines.append(f"- {o['kind']}: \"{o['reading'][:120]}\" - {state}"
                     + (f"; submitted for approval" if o.get("events") else ""))
    for w in out.get("warnings") or []:
        lines.append(f"Note: {w}")
    return "\n".join(lines)


def _present_sync(out: Dict[str, Any]) -> str:
    if "connectivity" in out and "device_id" in out:
        return (f"Device {out['device_id']} - {out['connectivity']} ({out['connectivity_detail']}). "
                f"{out['pending_sync']} event(s) waiting to sync, {out['open_conflicts']} open conflict(s). "
                f"Last successful sync: {out.get('last_successful_sync') or 'never'}.")
    if out.get("offline"):
        return f"OFFLINE - {out.get('reason')}. {out.get('pending_sync', 0)} event(s) stay queued on this device."
    return f"Synchronised: exported {((out.get('exported') or {}).get('events') or 0)} event(s); " \
           f"{out.get('conflicts', 0)} conflict(s) found."


def _register_presenters() -> None:
    try:
        from core import agni_present
        agni_present._PRESENTERS.update({"jarvis_ask": _present_ask, "jarvis_why_moved": _present_why,
                                         "record_field_update": _present_capture,
                                         "ingest_field_evidence": _present_capture,
                                         "jarvis_sync_status": _present_sync, "request_sync": _present_sync})
        agni_present._TITLES.update({"jarvis_overview": "Project health", "jarvis_notice_deadlines": "Notice deadlines",
                                     "jarvis_model_routing": "ZEUS model routing",
                                     "seed_jarvis_demo": "DEMO scenario loaded"})
    except Exception as error:                                  # pragma: no cover
        logger.debug(f"presenters not registered: {error}")


_register_presenters()
