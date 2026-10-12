"""
The ZEUS pipeline as one set of calls - used by the registered tools (and so
by the interface and the orchestrator), the ops CLI and the tests.

Role rules layered ON TOP of ZEUS's policy engine (which already requires a
signed-in session and at least 'operator' for every write tool):

    approving a field update, a schedule version, resolving a sync conflict,
    correcting a link to an activity that was not suggested, configuring
    contract notices, importing history, loading the DEMO scenario   -> engineer+

These checks only ever raise the bar; nothing here grants what the policy
engine refused.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
from pathlib import Path
from typing import Any, Dict, List, Optional

from jarvis import (ORIGINS, capture, chronology, dates, events, explain, forecast, importers, linking, origin_label,
                    search, store)
from core.logger import get_logger

logger = get_logger("jarvis.service")


class Refused(PermissionError):
    pass


def require_role(minimum: str, action: str) -> None:
    from security.policy import RANK, current_identity
    identity = current_identity()
    if not identity.authenticated:
        raise Refused("No one is signed in - authentication is required")
    role = identity.role if identity.role in RANK else "viewer"
    if RANK[role] < RANK[minimum]:
        raise Refused(f"Your role ({role}) cannot {action} - it needs {minimum} or above")


# ---------------------------------------------------------------- projects --

def create_project(code: str, name: str, client: str = "", contractor: str = "", location: str = "",
                   start_date: str = "", description: str = "", origin: str = "company",
                   workdays: str = "0123456", holidays: Optional[List[str]] = None,
                   near_critical_days: int = 5) -> Dict[str, Any]:
    code = (code or "").strip().upper()
    if not code or not (name or "").strip():
        raise ValueError("a project needs a code and a name")
    if origin not in ORIGINS:
        raise ValueError(f"origin must be one of {', '.join(ORIGINS)}")
    project_id = store.new_id("PRJ")
    with store.connection() as conn:
        if conn.execute("SELECT 1 FROM jv_projects WHERE code = ?", (code,)).fetchone():
            raise ValueError(f"a project with code {code} already exists")
        store.insert(conn, "jv_projects", {
            "project_id": project_id, "code": code, "name": name.strip(), "client": client, "contractor": contractor,
            "location": location, "description": description, "origin": origin, "status": "active",
            "start_date": dates.iso(start_date) if start_date else None, "near_critical_days": int(near_critical_days),
            "created_by": store.current_user(), "created_at": store.now(), "metadata": store.dumps({})})
        store.insert(conn, "jv_calendars", {"calendar_id": store.new_id("CAL"), "project_id": project_id,
                                            "name": "Project calendar", "workdays": workdays or "0123456",
                                            "holidays": store.dumps(holidays or [])})
        store.insert(conn, "jv_schedules", {"schedule_id": store.new_id("SCH"), "project_id": project_id,
                                            "name": f"{code} schedule", "created_at": store.now()})
        store.audit("create_project", "project", project_id, project_id, {"code": code, "origin": origin}, conn=conn)
    return get_project(project_id)


def get_project(project_id: str) -> Dict[str, Any]:
    row = store.one("SELECT * FROM jv_projects WHERE project_id = ? OR code = ?", (project_id, project_id))
    if not row:
        raise ValueError(f"unknown project '{project_id}'")
    row["origin_label"] = origin_label(row["origin"])
    return row


def resolve_project(project: Optional[str]) -> str:
    """A project id from an id, a code, the operator's current project, or the only one."""
    if project:
        return get_project(project)["project_id"]
    current = store.get_setting("current_project")
    if current and store.one("SELECT 1 FROM jv_projects WHERE project_id = ?", (current,)):
        return current
    rows = store.query("SELECT project_id FROM jv_projects WHERE status = 'active' ORDER BY created_at DESC")
    if len(rows) == 1:
        return rows[0]["project_id"]
    if not rows:
        raise ValueError("there is no ZEUS project yet - create one or load the DEMO scenario")
    raise ValueError("several projects exist - choose one (Projects screen) or name it")


def set_current_project(project: str) -> Dict[str, Any]:
    project_id = get_project(project)["project_id"]
    store.set_setting("current_project", project_id)
    return {"ok": True, "project_id": project_id}


def list_projects() -> List[Dict[str, Any]]:
    out = []
    for p in store.query("SELECT * FROM jv_projects ORDER BY created_at DESC"):
        try:
            health = overview(p["project_id"], light=True)
        except Exception as error:
            health = {"error": str(error)}
        out.append(dict(p, origin_label=origin_label(p["origin"]), health=health))
    return out


# ---------------------------------------------------------------- schedules --

def import_schedule(project: str, path: str, kind: str = "update", label: str = "", data_date: str = "",
                    approve: bool = False, captured_at: Optional[str] = None, device: Optional[str] = None,
                    actor: Optional[str] = None) -> Dict[str, Any]:
    if kind not in ("baseline", "update"):
        raise ValueError("kind is 'baseline' or 'update'")
    project_id = resolve_project(project)
    if kind == "baseline":
        require_role("engineer", "import a baseline schedule")
    schedule = importers.read_schedule(path)
    source = Path(path)
    captured = dates.iso_dt(captured_at) if captured_at else store.now_iso()
    sha = hashlib.sha256(source.read_bytes()).hexdigest()
    with store.connection() as conn:
        project_row = conn.execute("SELECT * FROM jv_projects WHERE project_id = ?", (project_id,)).fetchone()
        schedule_id = conn.execute("SELECT schedule_id FROM jv_schedules WHERE project_id = ?",
                                   (project_id,)).fetchone()["schedule_id"]
        version_id = store.new_id("VER")
        evidence_id = store.new_id("EVD")
        stored = capture._keep_copy(project_id, source, sha)
        store.insert(conn, "jv_evidence", {
            "evidence_id": evidence_id, "project_id": project_id, "kind": "schedule_file",
            "title": label or source.name, "filename": source.name, "stored_path": str(stored), "sha256": sha,
            "bytes": source.stat().st_size, "mime": None, "occurred_at": captured, "captured_at": captured,
            "received_at": store.now(), "captured_by": actor if actor is not None else store.current_user(),
            "device_id": device or events.device_id(conn), "source_type": "import", "original_text": None,
            "language": None, "scripts": None, "origin": project_row["origin"],
            "metadata": store.dumps({"provider": schedule.provider, "warnings": schedule.warnings})})
        version_data_date = dates.iso(data_date) if data_date else schedule.data_date
        store.insert(conn, "jv_schedule_versions", {
            "version_id": version_id, "schedule_id": schedule_id, "project_id": project_id,
            "label": label or f"{kind} {version_data_date or dates.iso(captured)}", "kind": kind,
            "data_date": version_data_date, "provider": schedule.provider, "source_file": source.name,
            "source_hash": sha, "evidence_id": evidence_id, "status": "pending",
            "created_by": actor if actor is not None else store.current_user(), "created_at": store.now(),
            "captured_at": captured, "notes": "; ".join(schedule.warnings)[:2000]})
        code_to_id: Dict[str, str] = {}
        for act in schedule.activities:
            row = conn.execute("SELECT activity_id FROM jv_activities WHERE project_id = ? AND code = ?",
                               (project_id, act.code)).fetchone()
            category = ",".join(capture.extraction._work_types(act.name)) or None
            if row:
                activity_id = row["activity_id"]
                conn.execute("UPDATE jv_activities SET name = ?, wbs_code = ?, activity_type = ?, location = "
                             "COALESCE(NULLIF(?, ''), location), contractor = COALESCE(NULLIF(?, ''), contractor), "
                             "unit = COALESCE(NULLIF(?, ''), unit), planned_qty = COALESCE(?, planned_qty), "
                             "work_category = COALESCE(?, work_category) WHERE activity_id = ?",
                             (act.name, act.wbs_code, act.type, act.location, act.contractor, act.unit, act.quantity,
                              category, activity_id))
            else:
                activity_id = store.new_id("ACT")
                store.insert(conn, "jv_activities", {
                    "activity_id": activity_id, "project_id": project_id, "code": act.code, "name": act.name,
                    "wbs_code": act.wbs_code, "activity_type": act.type, "location": act.location,
                    "contractor": act.contractor or project_row["contractor"], "unit": act.unit,
                    "planned_qty": act.quantity, "work_category": category, "created_at": store.now()})
            code_to_id[act.code] = activity_id
            store.insert(conn, "jv_activity_versions", {
                "version_id": version_id, "activity_id": activity_id, "planned_start": act.planned_start,
                "planned_finish": act.planned_finish, "duration_days": act.duration if act.duration is not None else 0,
                "baseline_start": act.baseline_start, "baseline_finish": act.baseline_finish,
                "actual_start": act.actual_start, "actual_finish": act.actual_finish,
                "percent_complete": act.percent_complete, "remaining_days": act.remaining,
                "constraint_type": "SNET" if act.constraint_date else None, "constraint_date": act.constraint_date,
                "planned_qty": act.quantity})
            for alias in act.aliases + [act.code]:
                conn.execute("INSERT OR IGNORE INTO jv_entity_mappings (mapping_id, project_id, alias_norm, alias, "
                             "activity_id, confidence, source, confirmed_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                             (store.new_id("MAP"), project_id, linking.norm(alias), alias, activity_id, 0.9, "import",
                              None, store.now()))
            search.index_item(conn, f"activity:{activity_id}", "activity", activity_id, project_id, activity_id, None,
                              None, "schedule", 9, project_row["origin"], f"{act.code} {act.name}",
                              f"{act.code} {act.name} {act.wbs_name} {act.location} {act.contractor} "
                              f"{' '.join(act.aliases)}")
        for dep in schedule.dependencies:
            if dep.pred_code in code_to_id and dep.succ_code in code_to_id:
                conn.execute("INSERT OR REPLACE INTO jv_dependencies (version_id, pred_id, succ_id, dep_type, lag_days) "
                             "VALUES (?, ?, ?, ?, ?)", (version_id, code_to_id[dep.pred_code],
                                                        code_to_id[dep.succ_code], dep.type, dep.lag))
        for w in schedule.wbs:
            if w.get("code"):
                conn.execute("INSERT OR IGNORE INTO jv_wbs (wbs_id, project_id, code, name) VALUES (?, ?, ?, ?)",
                             (f"{project_id}:{w['code']}", project_id, w["code"], w.get("name")))
        if not project_row["start_date"]:
            starts = [a.planned_start for a in schedule.activities if a.planned_start]
            if starts:
                conn.execute("UPDATE jv_projects SET start_date = ? WHERE project_id = ?", (min(starts), project_id))
        events.append(conn, project_id, "schedule_imported", occurred_at=captured, captured_at=captured, device=device,
                      actor=actor, source="import", evidence_ids=[evidence_id], origin=project_row["origin"],
                      base_head=None, payload={"version_id": version_id, "label": label, "kind": kind,
                                               "data_date": version_data_date, "provider": schedule.provider,
                                               "activities": len(schedule.activities),
                                               "dependencies": len(schedule.dependencies)})
        store.audit("import_schedule", "schedule_version", version_id, project_id,
                    {"file": source.name, "sha256": sha, "provider": schedule.provider}, conn=conn)
    result = {"ok": True, "version_id": version_id, "provider": schedule.provider,
              "activities": len(schedule.activities), "dependencies": len(schedule.dependencies),
              "data_date": version_data_date, "warnings": schedule.warnings, "status": "pending"}
    if approve:
        result["approval"] = approve_version(version_id, True, captured_at=captured_at, device=device, actor=actor)
        result["status"] = "approved"
    return result


def approve_version(version_id: str, approve: bool = True, reason: str = "", captured_at: Optional[str] = None,
                    device: Optional[str] = None, actor: Optional[str] = None) -> Dict[str, Any]:
    require_role("engineer", "approve a schedule version")
    captured = dates.iso_dt(captured_at) if captured_at else store.now_iso()
    with store.connection() as conn:
        version = conn.execute("SELECT * FROM jv_schedule_versions WHERE version_id = ?", (version_id,)).fetchone()
        if not version:
            raise ValueError(f"unknown schedule version {version_id}")
        if version["status"] != "pending":
            raise ValueError(f"version {version['label']} is already {version['status']}")
        who = actor if actor is not None else store.current_user()
        conn.execute("UPDATE jv_schedule_versions SET status = ?, approved_by = ?, approved_at = ? WHERE version_id = ?",
                     ("approved" if approve else "rejected", who, captured, version_id))
        event = events.append(conn, version["project_id"], "schedule_changed", occurred_at=captured,
                              captured_at=captured, device=device, actor=actor, source="review",
                              evidence_ids=[version["evidence_id"]] if version["evidence_id"] else [],
                              base_head=None, payload={"version_id": version_id, "label": version["label"],
                                                       "kind": version["kind"], "data_date": version["data_date"],
                                                       "decision": "approved" if approve else "rejected",
                                                       "reason": reason})
        outcome = None
        if approve:
            outcome = forecast.recompute(conn, version["project_id"], event["event_id"],
                                         f"schedule version {version['label']} approved", captured)
        store.audit("approve_version" if approve else "reject_version", "schedule_version", version_id,
                    version["project_id"], {"reason": reason}, conn=conn)
    if approve:
        _derive_history(version["project_id"])
    return {"ok": True, "version_id": version_id, "status": "approved" if approve else "rejected",
            "forecast": outcome}


def versions(project: str) -> List[Dict[str, Any]]:
    project_id = resolve_project(project)
    return store.query("SELECT version_id, label, kind, data_date, provider, source_file, source_hash, status, "
                       "created_by, captured_at, approved_by, approved_at, notes FROM jv_schedule_versions "
                       "WHERE project_id = ? ORDER BY captured_at", (project_id,))


# ------------------------------------------------------------------ capture --

def capture_update(project: Optional[str], **kwargs) -> Dict[str, Any]:
    project_id = resolve_project(project)
    return capture.capture(project_id, **kwargs)


def approve_update(event_id: str, approve: bool = True, reason: str = "", captured_at: Optional[str] = None,
                   device: Optional[str] = None, actor: Optional[str] = None) -> Dict[str, Any]:
    require_role("engineer", "approve a field update")
    with store.connection() as conn:
        target = events.get(conn, event_id)
        if target is None:
            raise ValueError(f"unknown event {event_id}")
        decision = events.decide(conn, event_id, approve, reason, captured_at=captured_at, device=device, actor=actor)
        outcome = None
        if approve:
            if target["event_type"] == "blocker_reported":
                conn.execute("UPDATE jv_blockers SET status = 'open' WHERE raised_event_id = ?", (event_id,))
            if target["event_type"] == "blocker_cleared":
                conn.execute("UPDATE jv_blockers SET status = 'cleared' WHERE cleared_event_id = ?", (event_id,))
            try:
                outcome = forecast.recompute(conn, target["project_id"], event_id,
                                             f"approved {target['event_type'].replace('_', ' ')}",
                                             decision["captured_at"])
            except forecast.NoSchedule as error:
                outcome = {"note": str(error)}
        else:
            if target["event_type"] == "blocker_reported":
                conn.execute("UPDATE jv_blockers SET status = 'rejected' WHERE raised_event_id = ?", (event_id,))
        store.audit("approve_update" if approve else "reject_update", "event", event_id, target["project_id"],
                    {"reason": reason, "event_type": target["event_type"]}, conn=conn)
    if approve and target["event_type"] == "activity_completed":
        _derive_history(target["project_id"])
    result = {"ok": True, "event_id": event_id, "decision": "approved" if approve else "rejected",
              "decision_event_id": decision["event_id"], "forecast": outcome}
    if outcome and outcome.get("change"):
        with store.connection() as conn:
            result["explanation"] = explain.why_moved(conn, target["project_id"], outcome["change"]["change_id"])
    return result


def pending_updates(project: str) -> List[Dict[str, Any]]:
    project_id = resolve_project(project)
    with store.connection() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT e.event_id, e.event_type, e.activity_id, a.code AS activity_code, a.name AS activity_name, "
            "e.occurred_at, e.captured_at, e.device_id, e.actor, e.source, e.payload, e.evidence_ids, e.confidence, "
            "s.status FROM jv_events e JOIN jv_event_status s ON s.event_id = e.event_id "
            "LEFT JOIN jv_activities a ON a.activity_id = e.activity_id "
            "WHERE e.project_id = ? AND s.status IN ('submitted', 'conflicted') ORDER BY e.occurred_at, e.seq",
            (project_id,))]
        for r in rows:
            r["payload"] = store.loads(r["payload"], {})
            r["evidence_ids"] = store.loads(r["evidence_ids"], [])
        unlinked = [dict(r) for r in conn.execute(
            "SELECT o.observation_id, o.evidence_id, o.observed_at, o.original_text, o.normalized_text, o.kind, "
            "o.activity_ref, o.link_status, o.link_confidence, o.link_reasons, o.percent_complete, o.expected_date "
            "FROM jv_field_observations o JOIN jv_evidence ev ON ev.evidence_id = o.evidence_id WHERE o.project_id = ? "
            "AND o.link_status IN ('needs_confirmation', 'unlinked') AND o.kind != 'note' AND ev.kind != 'correspondence' "
            "ORDER BY o.observed_at", (project_id,))]
        for u in unlinked:
            u["link_reasons"] = store.loads(u["link_reasons"], {})
    return {"updates": rows, "links_to_confirm": unlinked}


def confirm_link(observation_id: str, activity_id: str) -> Dict[str, Any]:
    """Link an observation to an activity: confirming a suggestion needs an
    operator; linking it to anything that was not suggested (a correction)
    needs an engineer. The decision is remembered as an alias."""
    with store.connection() as conn:
        row = conn.execute("SELECT * FROM jv_field_observations WHERE observation_id = ?", (observation_id,)).fetchone()
        if not row:
            raise ValueError(f"unknown observation {observation_id}")
        row = dict(row)
        suggested = [c["activity_id"] for c in (store.loads(row["link_reasons"], {}) or {}).get("candidates", [])]
        if activity_id not in suggested or row["link_status"] == "linked":
            require_role("engineer", "link a field record to an activity that was not suggested")
        else:
            require_role("operator", "confirm an activity link")
        activity = conn.execute("SELECT * FROM jv_activities WHERE activity_id = ? AND project_id = ?",
                                (activity_id, row["project_id"])).fetchone()
        if not activity:
            raise ValueError("that activity is not in this project")
        evidence = conn.execute("SELECT * FROM jv_evidence WHERE evidence_id = ?", (row["evidence_id"],)).fetchone()
        obs = capture.extraction.Observation(
            text=row["original_text"] or "", reading=row["normalized_text"] or "", kind=row["kind"] or "note",
            refs=[r.strip() for r in (row["activity_ref"] or "").split(",") if r.strip()],
            element_refs=capture.extraction.element_refs(row["normalized_text"] or ""),
            work_types=capture.extraction._work_types(row["normalized_text"] or ""),
            percent_complete=row["percent_complete"], quantity=row["quantity"], unit=row["unit"],
            occurred_on=dates.iso(row["observed_at"]), expected_date=row["expected_date"],
            remaining_days=row["remaining_days"], blocker_category=row["blocker_category"])
        learned = linking.remember(conn, row["project_id"], obs, activity_id)
        previous = row["activity_id"]
        conn.execute("UPDATE jv_field_observations SET activity_id = ?, link_status = 'linked', link_confidence = 1.0 "
                     "WHERE observation_id = ?", (activity_id, observation_id))
        events.append(conn, row["project_id"], "link_confirmed", activity_id=activity_id, source="review",
                      evidence_ids=[row["evidence_id"]], observation_id=observation_id, base_head=None,
                      payload={"observation_id": observation_id, "previous_activity": previous,
                               "aliases_learned": learned})
        made = []
        if row["event_id"] and previous and previous != activity_id:
            # A correction: the earlier event stays in the history, superseded.
            events.set_status(conn, row["event_id"], "superseded", "linked to a different activity on review")
        if evidence["kind"] == "correspondence":
            # a letter is linked to the activity, but changes no state
            for c in conn.execute("SELECT correspondence_id FROM jv_correspondence WHERE evidence_id = ?",
                                  (row["evidence_id"],)):
                store.insert(conn, "jv_links", {"project_id": row["project_id"], "from_kind": "correspondence",
                                                "from_id": c[0], "to_kind": "activity", "to_id": activity_id,
                                                "relation": "relates_to", "confidence": 1.0, "method": "confirmed",
                                                "created_by": store.current_user(), "created_at": store.now()},
                             or_ignore=True)
        elif not row["event_id"] or previous != activity_id:
            made = capture.observation_events(conn, row["project_id"], observation_id, obs, activity_id,
                                              row["evidence_id"], row["observed_at"] or evidence["captured_at"],
                                              evidence["captured_at"], evidence["device_id"], evidence["captured_by"],
                                              evidence["source_type"], evidence["origin"], 1.0)
        store.insert(conn, "jv_links", {"project_id": row["project_id"], "from_kind": "evidence",
                                        "from_id": row["evidence_id"], "to_kind": "activity", "to_id": activity_id,
                                        "relation": "evidences", "confidence": 1.0, "method": "confirmed",
                                        "created_by": store.current_user(), "created_at": store.now()}, or_ignore=True)
        store.audit("confirm_link", "observation", observation_id, row["project_id"],
                    {"activity": activity["code"], "previous": previous}, conn=conn)
    return {"ok": True, "observation_id": observation_id, "activity": activity["code"], "events": made,
            "aliases_learned": learned}


# ---------------------------------------------------------------- overview --

def overview(project: str, light: bool = False) -> Dict[str, Any]:
    project_id = resolve_project(project)
    with store.connection() as conn:
        p = dict(conn.execute("SELECT * FROM jv_projects WHERE project_id = ?", (project_id,)).fetchone())
        snap = forecast.latest_snapshot(conn, project_id)
        counts = {
            "activities": conn.execute("SELECT COUNT(*) FROM jv_activities WHERE project_id = ?", (project_id,)).fetchone()[0],
            "evidence": conn.execute("SELECT COUNT(*) FROM jv_evidence WHERE project_id = ?", (project_id,)).fetchone()[0],
            "pending_approvals": conn.execute(
                "SELECT COUNT(*) FROM jv_events e JOIN jv_event_status s ON s.event_id = e.event_id WHERE "
                "e.project_id = ? AND s.status = 'submitted'", (project_id,)).fetchone()[0],
            "links_to_confirm": conn.execute(
                "SELECT COUNT(*) FROM jv_field_observations o JOIN jv_evidence ev ON ev.evidence_id = o.evidence_id "
                "WHERE o.project_id = ? AND o.link_status IN ('needs_confirmation', 'unlinked') AND o.kind != 'note' "
                "AND ev.kind != 'correspondence'", (project_id,)).fetchone()[0],
            "open_blockers": conn.execute("SELECT COUNT(*) FROM jv_blockers WHERE project_id = ? AND status = 'open'",
                                          (project_id,)).fetchone()[0],
            "open_conflicts": conn.execute("SELECT COUNT(*) FROM jv_sync_conflicts WHERE project_id = ? AND "
                                           "status = 'open'", (project_id,)).fetchone()[0],
            "forecast_changes": conn.execute("SELECT COUNT(*) FROM jv_forecast_changes WHERE project_id = ? AND "
                                             "delta_days != 0", (project_id,)).fetchone()[0],
        }
        due = [d for d in chronology.deadlines(conn, project_id, push=not light) if d["status"] in ("open", "missed")]
        result: Dict[str, Any] = {"project": dict(p, origin_label=origin_label(p["origin"])), "counts": counts,
                                  "deadlines_open": [d for d in due if d["status"] == "open"][:5],
                                  "deadlines_missed": sum(1 for d in due if d["status"] == "missed")}
        if snap is None:
            result.update(schedule="none", note="no approved schedule yet")
            return result
        acts = (snap["activities"] or {}).get("activities", {})
        baseline_finish = (snap["activities"] or {}).get("baseline_finish")
        tasks = [a for a in acts.values() if a.get("type") == "task"]
        weight = sum(a.get("duration") or 0 for a in tasks) or 1
        actual = sum((a.get("duration") or 0) * (a.get("percent_complete") or 0) / 100 for a in tasks) / weight * 100
        data_date = dates.from_iso(snap["data_date"])
        planned = 0.0
        for a in tasks:
            bs, bf = dates.from_iso(a.get("baseline_start")), dates.from_iso(a.get("baseline_finish"))
            if not bs or not bf or not data_date:
                continue
            span = max(1, (bf - bs).days + 1)
            done = min(span, max(0, (data_date - bs).days))
            planned += (a.get("duration") or 0) * done / span
        planned = planned / weight * 100
        history = [{"seq": r["seq"], "captured_at": r["captured_at"], "project_finish": r["project_finish"],
                    "reason": r["reason"]} for r in conn.execute(
            "SELECT seq, captured_at, project_finish, reason FROM jv_snapshots WHERE project_id = ? ORDER BY seq",
            (project_id,))]
        result.update({
            "schedule": "approved", "version": (snap["activities"] or {}).get("version_label"),
            "data_date": snap["data_date"], "forecast_finish": snap["project_finish"],
            "baseline_finish": baseline_finish,
            "finish_variance_days": dates.days_between(baseline_finish, snap["project_finish"]) if baseline_finish else None,
            "actual_progress_pct": round(actual, 1), "planned_progress_pct": round(planned, 1),
            "progress_basis": "duration-weighted: reported % complete vs baseline dates at the data date",
            "critical": sum(1 for a in acts.values() if a.get("critical")),
            "near_critical": sum(1 for a in acts.values() if a.get("near_critical")),
            "critical_path": [acts[a]["code"] for a in (snap["activities"] or {}).get("critical_path", []) if a in acts],
            "snapshot_captured_at": snap["captured_at"], "finish_history": history})
        if not light:
            result["latest_change"] = explain.list_changes(conn, project_id, only_finish=True)[:1]
    return result


def schedule_table(project: str, as_of: Optional[str] = None, include_pending: bool = False) -> Dict[str, Any]:
    project_id = resolve_project(project)
    cutoff = events.as_of_cutoff(as_of) if as_of else None
    with store.connection() as conn:
        data = forecast.compute(conn, project_id, known_by=cutoff, include_pending=include_pending)
        evidence_counts = {r[0]: r[1] for r in conn.execute(
            "SELECT to_id, COUNT(DISTINCT from_id) FROM jv_links WHERE project_id = ? AND to_kind = 'activity' AND "
            "from_kind = 'evidence' GROUP BY to_id", (project_id,))}
        extra = {r["activity_id"]: dict(r) for r in conn.execute(
            "SELECT activity_id, wbs_code, location, contractor, unit, planned_qty FROM jv_activities WHERE project_id = ?",
            (project_id,))}
    rows = []
    for aid, a in data["activities"].items():
        rows.append(dict(a, **(extra.get(aid) or {}), evidence_count=evidence_counts.get(aid, 0)))
    rows.sort(key=lambda r: (r.get("es") or "", r.get("code") or ""))
    code_of = {aid: a["code"] for aid, a in data["activities"].items()}
    deps = [dict(d, pred_code=code_of.get(d["pred"]), succ_code=code_of.get(d["succ"])) for d in data["dependencies"]]
    return {"ok": True, "project_id": project_id, "as_of": cutoff, "version": data["version_label"],
            "data_date": data["data_date"], "project_finish": data["project_finish"],
            "baseline_finish": data["baseline_finish"], "critical_path": [code_of.get(i) for i in data["critical_path"]],
            "activities": rows, "dependencies": deps, "include_pending": include_pending}


def activity_detail(activity: str, project: Optional[str] = None) -> Dict[str, Any]:
    with store.connection() as conn:
        row = conn.execute("SELECT * FROM jv_activities WHERE activity_id = ? OR (code = ? AND project_id = COALESCE(?, "
                           "project_id))", (activity, activity, project)).fetchone()
        if not row:
            raise ValueError(f"unknown activity '{activity}'")
        a = dict(row)
        project_id = a["project_id"]
        versions_rows = [dict(r) for r in conn.execute(
            "SELECT sv.label, sv.kind, sv.data_date, sv.status, av.planned_start, av.planned_finish, av.duration_days, "
            "av.actual_start, av.actual_finish, av.percent_complete FROM jv_activity_versions av JOIN "
            "jv_schedule_versions sv ON sv.version_id = av.version_id WHERE av.activity_id = ? ORDER BY sv.captured_at",
            (a["activity_id"],))]
        history = [{"seq": r["seq"], "captured_at": r["captured_at"], "reason": r["reason"],
                    "ef": (store.loads(r["activities"], {}).get("activities", {}).get(a["activity_id"]) or {}).get("ef"),
                    "total_float": (store.loads(r["activities"], {}).get("activities", {}).get(a["activity_id"]) or {}).get("total_float")}
                   for r in conn.execute("SELECT seq, captured_at, reason, activities FROM jv_snapshots WHERE "
                                         "project_id = ? ORDER BY seq", (project_id,))]
        evs = events.list_events(conn, project_id, activity_id=a["activity_id"])
        evidence_ids = sorted({e for ev in evs for e in ev.get("evidence_ids") or []} | {
            r[0] for r in conn.execute("SELECT from_id FROM jv_links WHERE to_kind = 'activity' AND to_id = ? AND "
                                       "from_kind = 'evidence'", (a["activity_id"],))})
        evidence = explain._evidence(conn, evidence_ids)
        correspondence = [dict(r) for r in conn.execute(
            "SELECT c.* FROM jv_correspondence c JOIN jv_links l ON l.from_kind = 'correspondence' AND "
            "l.from_id = c.correspondence_id WHERE l.to_kind = 'activity' AND l.to_id = ?", (a["activity_id"],))]
        blockers = [dict(r) for r in conn.execute("SELECT * FROM jv_blockers WHERE activity_id = ? ORDER BY raised_on",
                                                  (a["activity_id"],))]
        preds = [dict(r) for r in conn.execute(
            "SELECT d.dep_type, d.lag_days, p.code, p.name FROM jv_dependencies d JOIN jv_activities p ON "
            "p.activity_id = d.pred_id WHERE d.succ_id = ? AND d.version_id = (SELECT version_id FROM "
            "jv_schedule_versions WHERE project_id = ? AND status = 'approved' ORDER BY approved_at DESC LIMIT 1)",
            (a["activity_id"], project_id))]
        succs = [dict(r) for r in conn.execute(
            "SELECT d.dep_type, d.lag_days, s.code, s.name FROM jv_dependencies d JOIN jv_activities s ON "
            "s.activity_id = d.succ_id WHERE d.pred_id = ? AND d.version_id = (SELECT version_id FROM "
            "jv_schedule_versions WHERE project_id = ? AND status = 'approved' ORDER BY approved_at DESC LIMIT 1)",
            (a["activity_id"], project_id))]
        try:
            now = forecast.compute(conn, project_id)["activities"].get(a["activity_id"])
        except forecast.NoSchedule:
            now = None
        mappings = [dict(r) for r in conn.execute("SELECT alias, source, confidence FROM jv_entity_mappings WHERE "
                                                  "activity_id = ?", (a["activity_id"],))]
    return {"ok": True, "activity": a, "forecast": now, "versions": versions_rows, "forecast_history": history,
            "events": evs, "evidence": evidence, "correspondence": correspondence, "blockers": blockers,
            "predecessors": preds, "successors": succs, "aliases": mappings}


# ---------------------------------------------------------------- evidence --

def evidence_list(project: str, kind: str = "", activity: str = "", query: str = "", limit: int = 200) -> List[Dict]:
    project_id = resolve_project(project)
    sql = ("SELECT e.evidence_id, e.kind, e.title, e.filename, e.sha256, e.occurred_at, e.captured_at, e.captured_by, "
           "e.device_id, e.source_type, e.language, e.origin, substr(e.original_text, 1, 240) AS excerpt, "
           "(SELECT group_concat(DISTINCT a.code) FROM jv_links l JOIN jv_activities a ON a.activity_id = l.to_id "
           " WHERE l.from_kind = 'evidence' AND l.from_id = e.evidence_id AND l.to_kind = 'activity') AS activities "
           "FROM jv_evidence e WHERE e.project_id = ?")
    params: List[Any] = [project_id]
    if kind:
        sql += " AND e.kind = ?"
        params.append(kind)
    if activity:
        sql += (" AND e.evidence_id IN (SELECT from_id FROM jv_links l JOIN jv_activities a ON a.activity_id = l.to_id "
                "WHERE l.from_kind = 'evidence' AND (a.code = ? OR a.activity_id = ?))")
        params.extend([activity, activity])
    if query:
        sql += " AND (lower(e.original_text) LIKE ? OR lower(e.title) LIKE ?)"
        params.extend([f"%{query.lower()}%", f"%{query.lower()}%"])
    sql += " ORDER BY e.occurred_at DESC LIMIT ?"
    params.append(int(limit))
    rows = store.query(sql, params)
    for r in rows:
        r["origin_label"] = origin_label(r["origin"])
    return rows


def evidence_trail(evidence_id: str = "", event_id: str = "") -> Dict[str, Any]:
    """Everything behind one piece of evidence (or one event): the file and its
    hash (re-verified now), each extraction with its method and model, the
    observations with their link reasons, the events with their approval, the
    schedule state before and after, and what moved downstream."""
    with store.connection() as conn:
        if event_id and not evidence_id:
            ev = events.get(conn, event_id)
            if not ev:
                raise ValueError(f"unknown event {event_id}")
            evidence_id = (ev.get("evidence_ids") or [""])[0]
            if not evidence_id:
                raise ValueError(f"this {ev['event_type'].replace('_', ' ')} has no evidence attached - it was "
                                 f"recorded by {ev.get('actor') or 'the system'} ({ev.get('source')})")
        row = conn.execute("SELECT * FROM jv_evidence WHERE evidence_id = ?", (evidence_id,)).fetchone()
        if not row:
            raise ValueError(f"unknown evidence {evidence_id}")
        item = dict(row)
        item["metadata"] = store.loads(item["metadata"], {})
        item["origin_label"] = origin_label(item["origin"])
        integrity = {"stored_copy": bool(item["stored_path"])}
        if item["stored_path"]:
            path = Path(item["stored_path"])
            integrity["exists"] = path.is_file()
            integrity["sha256_matches"] = path.is_file() and capture._sha256(path) == item["sha256"]
        else:
            integrity["sha256_matches"] = hashlib.sha256((item["original_text"] or "").encode("utf-8")).hexdigest() == item["sha256"]
        extractions = [dict(r) for r in conn.execute("SELECT * FROM jv_extractions WHERE evidence_id = ? ORDER BY created_at",
                                                     (evidence_id,))]
        for x in extractions:
            x["output"] = store.loads(x["output"], {})
        observations = [dict(r) for r in conn.execute(
            "SELECT o.*, a.code AS activity_code, a.name AS activity_name FROM jv_field_observations o LEFT JOIN "
            "jv_activities a ON a.activity_id = o.activity_id WHERE o.evidence_id = ?", (evidence_id,))]
        for o in observations:
            o["link_reasons"] = store.loads(o["link_reasons"], {})
        media = {"image": dict(conn.execute("SELECT * FROM jv_image_evidence WHERE evidence_id = ?", (evidence_id,)).fetchone() or {}),
                 "voice": dict(conn.execute("SELECT * FROM jv_voice_evidence WHERE evidence_id = ?", (evidence_id,)).fetchone() or {}),
                 "document": dict(conn.execute("SELECT * FROM jv_documents WHERE evidence_id = ?", (evidence_id,)).fetchone() or {})}
        related = [events._decode(dict(r)) for r in conn.execute(
            "SELECT e.*, s.status, s.decided_by, s.decided_at FROM jv_events e LEFT JOIN jv_event_status s ON "
            "s.event_id = e.event_id WHERE e.evidence_ids LIKE ? ORDER BY e.seq", (f'%"{evidence_id}"%',))]
        trail = []
        for ev in related:
            entry = explain._event_view(conn, ev)
            entry.pop("evidence", None)
            entry["chain_hash"] = ev.get("chain_hash")
            snap = conn.execute("SELECT snapshot_id, seq, project_finish, activities, captured_at FROM jv_snapshots "
                                "WHERE trigger_event_id = ?", (ev["event_id"],)).fetchone()
            if snap and ev.get("activity_id"):
                before = conn.execute("SELECT project_finish, activities FROM jv_snapshots WHERE project_id = ? AND "
                                      "seq = ?", (ev["project_id"], snap["seq"] - 1)).fetchone()
                after_act = (store.loads(snap["activities"], {}).get("activities", {}) or {}).get(ev["activity_id"], {})
                before_act = ((store.loads(before["activities"], {}).get("activities", {}) or {}).get(ev["activity_id"], {})
                              if before else {})
                entry["schedule_effect"] = {
                    "snapshot": snap["snapshot_id"],
                    "previous_state": {k: before_act.get(k) for k in ("es", "ef", "total_float", "percent_complete", "status")},
                    "new_state": {k: after_act.get(k) for k in ("es", "ef", "total_float", "percent_complete", "status")},
                    "project_finish_before": before["project_finish"] if before else None,
                    "project_finish_after": snap["project_finish"]}
                change = conn.execute("SELECT change_id, delta_days, affected FROM jv_forecast_changes WHERE "
                                      "to_snapshot = ?", (snap["snapshot_id"],)).fetchone()
                if change:
                    entry["schedule_effect"]["forecast_change"] = change["change_id"]
                    entry["schedule_effect"]["downstream"] = [
                        {"code": s["code"], "delta_days": s["delta_days"], "new_finish": s["new_finish"]}
                        for s in store.loads(change["affected"], [])]
            trail.append(entry)
    return {"ok": True, "evidence": item, "integrity": integrity, "media": media, "extractions": extractions,
            "observations": observations, "events": trail}


# ---------------------------------------------------------- temporal history --

def state_as_of(activity: str, as_of: str, project: Optional[str] = None) -> Dict[str, Any]:
    """What ZEUS knew about an activity at the end of `as_of`: only events
    and approvals CAPTURED by then; the forecast as it could be computed then."""
    cutoff = events.as_of_cutoff(as_of)
    if not cutoff:
        raise ValueError(f"'{as_of}' is not a date")
    with store.connection() as conn:
        row = conn.execute("SELECT * FROM jv_activities WHERE activity_id = ? OR (code = ? AND project_id = COALESCE(?, "
                           "project_id))", (activity, activity, project)).fetchone()
        if not row:
            raise ValueError(f"unknown activity '{activity}'")
        a = dict(row)
        known = [e for e in events.list_events(conn, a["project_id"], activity_id=a["activity_id"], known_by=cutoff)]
        statuses = events.statuses_as_of(conn, a["project_id"], cutoff)
        for e in known:
            e["status_then"] = statuses.get(e["event_id"], e.get("status"))
        state = events.activity_states(conn, a["project_id"], known_by=cutoff).get(a["activity_id"], {})
        try:
            then = forecast.compute(conn, a["project_id"], known_by=cutoff)
            forecast_then = then["activities"].get(a["activity_id"])
            project_finish_then = then["project_finish"]
        except forecast.NoSchedule:
            forecast_then, project_finish_then = None, None
        snapshot = forecast.latest_snapshot(conn, a["project_id"], known_by=cutoff)
    evidence_ids = sorted({x for e in known for x in e.get("evidence_ids") or []})
    return {"ok": True, "activity": {"code": a["code"], "name": a["name"], "activity_id": a["activity_id"]},
            "as_of": cutoff, "known_events": known, "state": {k: v for k, v in state.items() if k != "sources"},
            "forecast_then": forecast_then, "project_finish_then": project_finish_then,
            "snapshot_then": {"seq": snapshot["seq"], "captured_at": snapshot["captured_at"],
                              "project_finish": snapshot["project_finish"]} if snapshot else None,
            "evidence_ids": evidence_ids,
            "basis": "events and approvals captured on or before the cut-off; later records are excluded"}


def compare_versions(version_a: str, version_b: str) -> Dict[str, Any]:
    with store.connection() as conn:
        va = conn.execute("SELECT * FROM jv_schedule_versions WHERE version_id = ? OR label = ?", (version_a, version_a)).fetchone()
        vb = conn.execute("SELECT * FROM jv_schedule_versions WHERE version_id = ? OR label = ?", (version_b, version_b)).fetchone()
        if not va or not vb:
            raise ValueError("both versions must exist")
        acts_a, deps_a = forecast.version_rows(conn, va["version_id"])
        acts_b, deps_b = forecast.version_rows(conn, vb["version_id"])
        snaps = {}
        for v in (va, vb):
            s = conn.execute("SELECT project_finish, captured_at FROM jv_snapshots WHERE version_id = ? ORDER BY seq "
                             "LIMIT 1", (v["version_id"],)).fetchone()
            snaps[v["version_id"]] = dict(s) if s else None
    changes = []
    for aid in sorted(set(acts_a) | set(acts_b), key=lambda i: (acts_a.get(i) or acts_b.get(i))["code"]):
        a, b = acts_a.get(aid), acts_b.get(aid)
        if a is None or b is None:
            changes.append({"code": (a or b)["code"], "name": (a or b)["name"],
                            "change": "added" if a is None else "removed"})
            continue
        diff = {}
        for key in ("planned_start", "planned_finish", "duration_days", "actual_start", "actual_finish",
                    "percent_complete", "remaining_days", "constraint_date"):
            if (a.get(key) or None) != (b.get(key) or None):
                diff[key] = {"from": a.get(key), "to": b.get(key)}
        if diff:
            shift = dates.days_between(a.get("planned_finish"), b.get("planned_finish"))
            changes.append({"code": a["code"], "name": a["name"], "change": "changed", "fields": diff,
                            "finish_shift_days": shift})
    code = {**{k: v["code"] for k, v in acts_a.items()}, **{k: v["code"] for k, v in acts_b.items()}}
    la = {(d.pred, d.succ): (d.type, d.lag) for d in deps_a}
    lb = {(d.pred, d.succ): (d.type, d.lag) for d in deps_b}
    logic = [{"pred": code.get(k[0]), "succ": code.get(k[1]),
              "change": "added" if k not in la else "removed" if k not in lb else "changed",
              "from": la.get(k), "to": lb.get(k)} for k in sorted(set(la) | set(lb)) if la.get(k) != lb.get(k)]
    fa, fb = snaps.get(va["version_id"]), snaps.get(vb["version_id"])
    return {"ok": True, "from": {"label": va["label"], "data_date": va["data_date"], "status": va["status"],
                                 "forecast_finish": fa["project_finish"] if fa else None},
            "to": {"label": vb["label"], "data_date": vb["data_date"], "status": vb["status"],
                   "forecast_finish": fb["project_finish"] if fb else None},
            "finish_shift_days": dates.days_between(fa["project_finish"], fb["project_finish"]) if fa and fb else None,
            "activity_changes": changes, "logic_changes": logic}


def first_known_slip(activity: str, project: Optional[str] = None) -> Dict[str, Any]:
    """The first moment the record showed the activity finishing after its
    baseline - the snapshot, the update that caused it, and the first field
    report of trouble on it (which may be earlier than any forecast change)."""
    with store.connection() as conn:
        row = conn.execute("SELECT * FROM jv_activities WHERE activity_id = ? OR (code = ? AND project_id = COALESCE(?, "
                           "project_id))", (activity, activity, project)).fetchone()
        if not row:
            raise ValueError(f"unknown activity '{activity}'")
        a = dict(row)
        first_snapshot = None
        for snap in conn.execute("SELECT * FROM jv_snapshots WHERE project_id = ? ORDER BY seq", (a["project_id"],)):
            act = (store.loads(snap["activities"], {}).get("activities", {}) or {}).get(a["activity_id"])
            if act and act.get("baseline_finish") and act.get("ef") and act["ef"] > act["baseline_finish"]:
                trigger = events.get(conn, snap["trigger_event_id"]) if snap["trigger_event_id"] else None
                first_snapshot = {"captured_at": snap["captured_at"], "seq": snap["seq"], "forecast_finish": act["ef"],
                                  "baseline_finish": act["baseline_finish"],
                                  "slip_days": dates.days_between(act["baseline_finish"], act["ef"]),
                                  "trigger": explain._event_view(conn, trigger)}
                break
        warnings = [e for e in events.list_events(conn, a["project_id"], activity_id=a["activity_id"],
                                                  types=("blocker_reported", "delay_reported"))]
        first_report = min(warnings, key=lambda e: e["captured_at"]) if warnings else None
        first_warning = explain._event_view(conn, first_report) if first_report else None
    return {"ok": True, "activity": {"code": a["code"], "name": a["name"]}, "first_forecast_slip": first_snapshot,
            "first_field_warning": first_warning}


def evidence_before(project: str, activity: str = "", kind: str = "delay") -> Dict[str, Any]:
    """What evidence existed before the first delay / blocker was reported."""
    project_id = resolve_project(project)
    with store.connection() as conn:
        activity_id = None
        if activity:
            row = conn.execute("SELECT activity_id FROM jv_activities WHERE project_id = ? AND (code = ? OR activity_id = ?)",
                               (project_id, activity, activity)).fetchone()
            activity_id = row["activity_id"] if row else None
        first = conn.execute("SELECT * FROM jv_events WHERE project_id = ? AND event_type IN ('blocker_reported', "
                             "'delay_reported') AND (? IS NULL OR activity_id = ?) ORDER BY captured_at LIMIT 1",
                             (project_id, activity_id, activity_id)).fetchone()
        if not first:
            return {"ok": True, "found": False, "answer": "No delay or blocker has been reported" +
                    (f" for {activity}" if activity else "") + "."}
        first = events._decode(dict(first))
        rows = [dict(r) for r in conn.execute(
            "SELECT evidence_id, kind, title, captured_at, occurred_at, captured_by, device_id, source_type, "
            "substr(original_text, 1, 240) AS excerpt FROM jv_evidence WHERE project_id = ? AND captured_at < ? "
            "ORDER BY captured_at", (project_id, first["captured_at"]))]
        view = explain._event_view(conn, first)
    return {"ok": True, "found": True, "first_report": view, "evidence_before": rows,
            "basis": "evidence captured strictly before the first delay/blocker report was captured"}


def _derive_history(project_id: str) -> None:
    try:
        from jarvis import learning
        learning.derive_history(project_id)
    except Exception as error:
        logger.warning(f"learning history not refreshed: {error}")
