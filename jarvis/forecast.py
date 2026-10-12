"""
Forecast = the approved schedule version + the approved field events, run
through the CPM engine.

`recompute()` is called after every approval (a field update, a schedule
version, a conflict resolution). It writes a SNAPSHOT - every activity's early
and late dates, float, criticality and driver as they stood at that moment of
knowledge - and, when anything moved, a FORECAST CHANGE with the causal chain
from the triggering update to the project finish (jarvis/explain.py).

Precedence of status information, per field and by date:
  * a schedule version's own actuals / % complete / remaining are its planner's
    statement as of its data date;
  * an approved field event that occurred on or after that status date
    overrides it; an older one fills a field the version left empty.
"""

from __future__ import annotations

import datetime as _dt
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

from jarvis import cpm, dates, events, store


class NoSchedule(ValueError):
    pass


def calendar_for(conn: sqlite3.Connection, project_id: str) -> cpm.Calendar:
    row = conn.execute("SELECT workdays, holidays FROM jv_calendars WHERE project_id = ? ORDER BY calendar_id LIMIT 1",
                       (project_id,)).fetchone()
    if not row:
        return cpm.Calendar()
    return cpm.Calendar(row["workdays"], store.loads(row["holidays"], []))


def current_version(conn: sqlite3.Connection, project_id: str, known_by: Optional[str] = None) -> Optional[Dict]:
    sql = "SELECT * FROM jv_schedule_versions WHERE project_id = ? AND status = 'approved'"
    params: List[Any] = [project_id]
    if known_by:
        sql += " AND COALESCE(approved_at, captured_at) <= ?"
        params.append(known_by)
    sql += " ORDER BY COALESCE(approved_at, captured_at) DESC, created_at DESC LIMIT 1"
    row = conn.execute(sql, params).fetchone()
    return dict(row) if row else None


def baseline_version(conn: sqlite3.Connection, project_id: str) -> Optional[Dict]:
    row = conn.execute("SELECT * FROM jv_schedule_versions WHERE project_id = ? AND kind = 'baseline' AND "
                       "status = 'approved' ORDER BY COALESCE(approved_at, captured_at) LIMIT 1",
                       (project_id,)).fetchone()
    return dict(row) if row else None


def version_rows(conn: sqlite3.Connection, version_id: str) -> Tuple[Dict[str, Dict], List[cpm.Dependency]]:
    rows = conn.execute("SELECT a.*, v.planned_start, v.planned_finish, v.duration_days, v.baseline_start, "
                        "v.baseline_finish, v.actual_start, v.actual_finish, v.percent_complete, v.remaining_days, "
                        "v.constraint_type, v.constraint_date, v.planned_qty AS version_qty "
                        "FROM jv_activity_versions v JOIN jv_activities a ON a.activity_id = v.activity_id "
                        "WHERE v.version_id = ? ORDER BY a.code", (version_id,)).fetchall()
    acts = {r["activity_id"]: dict(r) for r in rows}
    deps = [cpm.Dependency(r["pred_id"], r["succ_id"], r["dep_type"], int(r["lag_days"] or 0))
            for r in conn.execute("SELECT * FROM jv_dependencies WHERE version_id = ?", (version_id,))]
    return acts, deps


def build_inputs(conn: sqlite3.Connection, project_id: str, known_by: Optional[str] = None,
                 include_pending: bool = False) -> Dict[str, Any]:
    version = current_version(conn, project_id, known_by)
    if version is None:
        raise NoSchedule("no approved schedule version for this project yet - import and approve one first")
    baseline = baseline_version(conn, project_id)
    acts, deps = version_rows(conn, version["version_id"])
    base_acts = version_rows(conn, baseline["version_id"])[0] if baseline else {}
    states = events.activity_states(conn, project_id, known_by=known_by, include_pending=include_pending)
    field_dates = _field_dates(conn, states)
    status_date = (dates.from_iso(version["data_date"]) - _dt.timedelta(days=1)) if version.get("data_date") else None

    inputs: List[cpm.ActivityIn] = []
    latest_event_day: Optional[_dt.date] = None
    provenance: Dict[str, Dict[str, Any]] = {}
    for aid, row in acts.items():
        state = states.get(aid) or {}
        base = base_acts.get(aid) or {}
        chosen: Dict[str, Any] = {}
        source: Dict[str, str] = {}
        for key, state_key in (("actual_start", "actual_start"), ("actual_finish", "actual_finish"),
                               ("percent_complete", "percent_complete"), ("remaining_days", "remaining_days")):
            version_value = row.get(key)
            event_value = state.get(state_key)
            event_day = field_dates.get((aid, state_key))
            if event_value is not None and (version_value is None or status_date is None or
                                            (event_day and event_day >= status_date)):
                chosen[key] = event_value
                source[key] = (state.get("sources") or {}).get(state_key, "event")
                if event_day and (latest_event_day is None or event_day > latest_event_day):
                    latest_event_day = event_day
            else:
                chosen[key] = version_value
                if version_value is not None:
                    source[key] = f"version:{version['label']}"
        open_blockers = [b for b in (state.get("blockers") or {}).values() if b["status"] == "open"]
        dated = [b for b in open_blockers if b.get("expected_clear")]
        blocked_until = max((dates.from_iso(b["expected_clear"]) for b in dated), default=None)
        blocker_id = next((b["blocker_id"] for b in dated if dates.from_iso(b["expected_clear"]) == blocked_until), None)
        for b in open_blockers:
            raised = dates.from_iso(b.get("raised_on"))
            if raised and (latest_event_day is None or raised > latest_event_day):
                latest_event_day = raised
        constraint = dates.from_iso(row.get("constraint_date")) if (row.get("constraint_type") or "SNET") else None
        inputs.append(cpm.ActivityIn(
            id=aid, code=row["code"], name=row["name"], duration=int(row.get("duration_days") or 0),
            type=row.get("activity_type") or "task",
            planned_start=dates.from_iso(row.get("planned_start")), planned_finish=dates.from_iso(row.get("planned_finish")),
            baseline_start=dates.from_iso(row.get("baseline_start") or base.get("planned_start")),
            baseline_finish=dates.from_iso(row.get("baseline_finish") or base.get("planned_finish")),
            actual_start=dates.from_iso(chosen.get("actual_start")),
            actual_finish=dates.from_iso(chosen.get("actual_finish")),
            percent_complete=chosen.get("percent_complete"), remaining=chosen.get("remaining_days"),
            constraint_start=constraint, blocked_until=blocked_until, blocker_id=blocker_id,
            blocker_open_no_date=bool(open_blockers and not dated)))
        provenance[aid] = {"sources": source, "open_blockers": open_blockers,
                           "pending_events": state.get("pending", []),
                           "evidence_ids": state.get("evidence_ids", []), "events": state.get("events", [])}

    version_dd = dates.from_iso(version.get("data_date"))
    event_dd = latest_event_day + _dt.timedelta(days=1) if latest_event_day else None
    data_date = max([d for d in (version_dd, event_dd) if d] or [dates.today()])
    project = conn.execute("SELECT start_date, near_critical_days FROM jv_projects WHERE project_id = ?",
                           (project_id,)).fetchone()
    return {"version": version, "baseline": baseline, "activities": inputs, "dependencies": deps,
            "data_date": data_date, "calendar": calendar_for(conn, project_id),
            "project_start": dates.from_iso(project["start_date"]) if project else None,
            "near_critical_days": int(project["near_critical_days"] if project else 5),
            "provenance": provenance}


def _field_dates(conn: sqlite3.Connection, states: Dict[str, Dict]) -> Dict[Tuple[str, str], _dt.date]:
    """When the event that set each field occurred (for version-vs-event precedence)."""
    ids = {eid for s in states.values() for eid in (s.get("sources") or {}).values()}
    occurred = {}
    for i in range(0, len(ids), 400):
        chunk = list(ids)[i:i + 400]
        for row in conn.execute(f"SELECT event_id, occurred_at, payload, event_type FROM jv_events WHERE event_id IN "
                                f"({','.join('?' for _ in chunk)})", chunk):
            occurred[row["event_id"]] = dates.from_iso(row["occurred_at"])
    out = {}
    for aid, s in states.items():
        for field_name, eid in (s.get("sources") or {}).items():
            if eid in occurred:
                out[(aid, field_name)] = occurred[eid]
    return out


def compute(conn: sqlite3.Connection, project_id: str, known_by: Optional[str] = None,
            include_pending: bool = False) -> Dict[str, Any]:
    """The forecast as known at `known_by` (not persisted)."""
    inp = build_inputs(conn, project_id, known_by, include_pending)
    result = cpm.compute(inp["activities"], inp["dependencies"], inp["data_date"], inp["calendar"],
                         inp["project_start"], inp["near_critical_days"])
    acts = {aid: o.as_dict() for aid, o in result.activities.items()}
    for aid, info in inp["provenance"].items():
        acts[aid].update(status_sources=info["sources"], open_blockers=info["open_blockers"],
                         pending_events=info["pending_events"], evidence_ids=info["evidence_ids"],
                         applied_events=info["events"])
    return {"version_id": inp["version"]["version_id"], "version_label": inp["version"]["label"],
            "data_date": result.data_date, "project_finish": result.project_finish,
            "finish_activity": result.finish_activity, "critical_path": result.critical_path,
            "activities": acts, "dependencies": [vars(d) for d in inp["dependencies"]],
            "baseline_finish": _baseline_finish(acts)}


def _baseline_finish(acts: Dict[str, Dict]) -> Optional[str]:
    finishes = [a.get("baseline_finish") for a in acts.values() if a.get("baseline_finish")]
    return max(finishes) if finishes else None


def latest_snapshot(conn: sqlite3.Connection, project_id: str, known_by: Optional[str] = None) -> Optional[Dict]:
    sql = "SELECT * FROM jv_snapshots WHERE project_id = ?"
    params: List[Any] = [project_id]
    if known_by:
        sql += " AND captured_at <= ?"
        params.append(known_by)
    row = conn.execute(sql + " ORDER BY seq DESC LIMIT 1", params).fetchone()
    return _snapshot(dict(row)) if row else None


def _snapshot(row: Dict) -> Dict:
    row["activities"] = store.loads(row.get("activities"), {})
    return row


def recompute(conn: sqlite3.Connection, project_id: str, trigger_event_id: Optional[str] = None,
              reason: str = "", captured_at: Optional[str] = None) -> Dict[str, Any]:
    """Recompute, persist a snapshot, and record a forecast change if anything moved."""
    forecast = compute(conn, project_id)
    previous = latest_snapshot(conn, project_id)
    seq = (previous["seq"] + 1) if previous else 1
    captured = captured_at or store.now_iso()
    snapshot_id = store.new_id("SNAP")
    payload = {"activities": forecast["activities"], "critical_path": forecast["critical_path"],
               "dependencies": forecast["dependencies"], "version_label": forecast["version_label"],
               "baseline_finish": forecast["baseline_finish"]}
    store.insert(conn, "jv_snapshots", {
        "snapshot_id": snapshot_id, "project_id": project_id, "version_id": forecast["version_id"], "seq": seq,
        "created_at": store.now(), "captured_at": captured, "data_date": forecast["data_date"],
        "trigger_event_id": trigger_event_id, "reason": reason, "project_finish": forecast["project_finish"],
        "finish_activity_id": forecast["finish_activity"], "activities": store.dumps(payload)})
    change = None
    if previous is not None:
        from jarvis import explain
        prev_acts = (previous["activities"] or {}).get("activities", {})
        analysis = explain.build_chain(prev_acts, forecast["activities"], forecast["finish_activity"],
                                       previous.get("project_finish"), forecast["project_finish"])
        if analysis["moved"]:
            trigger = events.get(conn, trigger_event_id) if trigger_event_id else None
            change = {
                "change_id": store.new_id("FC"), "project_id": project_id, "from_snapshot": previous["snapshot_id"],
                "to_snapshot": snapshot_id, "trigger_event_id": trigger_event_id,
                "trigger_kind": (trigger or {}).get("event_type") or reason,
                "previous_finish": previous.get("project_finish"), "new_finish": forecast["project_finish"],
                "delta_days": dates.days_between(previous.get("project_finish"), forecast["project_finish"]) or 0,
                "root_activity_id": analysis["root_activity_id"], "chain": store.dumps(analysis["chain"]),
                "affected": store.dumps(analysis["affected"]), "milestones": store.dumps(analysis["milestones"]),
                "created_at": store.now(), "captured_at": captured}
            store.insert(conn, "jv_forecast_changes", change)
            events.append(conn, project_id, "forecast_changed", activity_id=analysis["root_activity_id"],
                          occurred_at=forecast["data_date"], captured_at=captured, source="cpm",
                          actor="ZEUS", payload={"change_id": change["change_id"],
                                                   "previous_finish": change["previous_finish"],
                                                   "new_finish": change["new_finish"],
                                                   "delta_days": change["delta_days"],
                                                   "trigger_event_id": trigger_event_id,
                                                   "moved_activities": len(analysis["affected"])},
                          evidence_ids=(trigger or {}).get("evidence_ids") or [], base_head=None, status="recorded")
    return {"snapshot_id": snapshot_id, "seq": seq, "project_finish": forecast["project_finish"],
            "data_date": forecast["data_date"], "change": change}
