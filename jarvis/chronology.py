"""
Delay notice & chronology assistant.

Every event appended to the store is materialised here as a dated chronology
entry that keeps its source (the event, the evidence ids, the activity, the
contractor), so a chronology is BUILT from the record rather than written from
memory. Claims tools are usually document-focused; here a letter sits in the
same timeline as the field update and the schedule change it relates to:

    blocker reported -> contractor letter -> schedule update -> forecast moved
    -> notice deadline approaching

Contract notice clauses are configured per project (clause, notice period,
which event types start the clock). A reported blocker or delay opens a
deadline; a recorded notice for the same activity closes it (met, or late);
an open deadline past its date is MISSED. Reminders are pushed through ZEUS's
background notifications.

Delay windows (the data structure a windows analysis - AACE RP 29R-03 - needs):
consecutive snapshots, the finish movement inside each window, and the
activities and evidence that drove it.
"""

from __future__ import annotations

import datetime as _dt
import sqlite3
from typing import Any, Dict, List, Optional

from jarvis import dates, store

_TITLES = {
    "progress_update": "Progress reported", "activity_started": "Activity started",
    "activity_completed": "Activity completed", "remaining_update": "Remaining duration reported",
    "blocker_reported": "Blocker reported", "blocker_cleared": "Blocker cleared", "delay_reported": "Delay reported",
    "correspondence_received": "Correspondence", "notice_issued": "Notice issued",
    "evidence_captured": "Evidence captured", "inspection_completed": "Inspection completed",
    "schedule_imported": "Schedule version imported", "schedule_changed": "Schedule version approved",
    "forecast_changed": "Forecast changed", "approval": "Approval", "conflict_detected": "Sync conflict detected",
    "conflict_resolved": "Sync conflict resolved", "link_confirmed": "Activity link confirmed",
}
DELAY_TYPES = ("material", "weather", "labour", "equipment", "design", "access", "permit", "power", "utility",
               "client", "other")


def _activity(conn: sqlite3.Connection, activity_id: Optional[str]) -> Dict[str, Any]:
    if not activity_id:
        return {}
    row = conn.execute("SELECT code, name, contractor FROM jv_activities WHERE activity_id = ?",
                       (activity_id,)).fetchone()
    return dict(row) if row else {}


def _detail(event: Dict[str, Any]) -> str:
    p = event.get("payload") or {}
    kind = event["event_type"]
    if kind == "progress_update":
        parts = []
        if p.get("percent_complete") is not None:
            parts.append(f"{p['percent_complete']:g}% complete")
        if p.get("quantity_done") is not None:
            parts.append(f"{p['quantity_done']:g} {p.get('unit') or ''}".strip())
        return ", ".join(parts) + (f" - {p['note']}" if p.get("note") else "")
    if kind in ("blocker_reported", "delay_reported"):
        text = p.get("description") or ""
        if p.get("category"):
            text += f" [{p['category']}]"
        if p.get("expected_clear"):
            text += f"; expected to clear {dates.fmt(p['expected_clear'])}"
        return text
    if kind == "forecast_changed":
        return (f"finish {dates.fmt(p.get('previous_finish'))} -> {dates.fmt(p.get('new_finish'))} "
                f"({(p.get('delta_days') or 0):+d} d); {p.get('moved_activities', 0)} activities moved")
    if kind == "approval":
        return f"{p.get('decision', '')} {p.get('target_type', '').replace('_', ' ')}" + (
            f" - {p['reason']}" if p.get("reason") else "")
    if kind in ("correspondence_received", "notice_issued"):
        return " - ".join(x for x in (p.get("reference"), p.get("subject"), p.get("from_party")) if x)
    if kind in ("schedule_imported", "schedule_changed"):
        return f"{p.get('label', '')} ({p.get('kind', '')}, data date {p.get('data_date') or '—'})"
    if kind in ("conflict_detected", "conflict_resolved"):
        return p.get("summary") or p.get("reason") or ""
    return p.get("note") or p.get("description") or ""


def record_event(conn: sqlite3.Connection, event: Dict[str, Any]) -> None:
    """Materialise one event in the chronology (and the entities it implies)."""
    activity = _activity(conn, event.get("activity_id"))
    p = event.get("payload") or {}
    title = _TITLES.get(event["event_type"], event["event_type"].replace("_", " ").capitalize())
    if activity:
        title = f"{title} - {activity['code']}"
    origin = event.get("origin") or "company"
    store.insert(conn, "jv_chronology", {
        "chron_id": store.new_id("CH"), "project_id": event["project_id"],
        "occurred_at": event.get("occurred_at"), "captured_at": event.get("captured_at"),
        "kind": event["event_type"], "title": title, "detail": _detail(event)[:1000],
        "activity_id": event.get("activity_id"), "source_kind": "event", "source_id": event["event_id"],
        "evidence_ids": store.dumps(event.get("evidence_ids") or []),
        "contractor": p.get("contractor") or activity.get("contractor"),
        "delay_type": p.get("category") if event["event_type"] in ("blocker_reported", "delay_reported") else None,
        "source_type": event.get("source"), "origin": origin, "created_at": store.now()}, or_ignore=True)
    kind = event["event_type"]
    if kind == "blocker_reported":
        blocker_id = p.get("blocker_id") or event["event_id"]
        store.insert(conn, "jv_blockers", {
            "blocker_id": blocker_id, "project_id": event["project_id"], "activity_id": event.get("activity_id"),
            "raised_event_id": event["event_id"], "description": p.get("description"),
            "original_text": p.get("original_text"), "category": p.get("category"),
            "raised_on": dates.iso(event.get("occurred_at")), "expected_clear": dates.iso(p.get("expected_clear")),
            "responsible_party": p.get("responsible_party"), "status": "reported"}, or_ignore=True)
    if kind in ("blocker_reported", "delay_reported"):
        start = dates.iso(event.get("occurred_at"))
        end = dates.iso(p.get("expected_clear")) if p.get("expected_clear") else None
        store.insert(conn, "jv_delay_events", {
            "delay_id": store.new_id("DL"), "project_id": event["project_id"], "activity_id": event.get("activity_id"),
            "blocker_id": p.get("blocker_id") or (event["event_id"] if kind == "blocker_reported" else None),
            "event_id": event["event_id"], "start_date": start, "end_date": end,
            "days": dates.days_between(start, end) if end else None, "cause_category": p.get("category"),
            "responsible_party": p.get("responsible_party") or "not yet determined",
            "description": p.get("description"), "created_at": store.now()})
        open_deadlines(conn, event)
    if kind == "blocker_cleared":
        conn.execute("UPDATE jv_blockers SET cleared_on = ?, cleared_event_id = ? WHERE project_id = ? AND "
                     "activity_id = ? AND status IN ('reported', 'open') AND (? IS NULL OR blocker_id = ?)",
                     (dates.iso(event.get("occurred_at")), event["event_id"], event["project_id"],
                      event.get("activity_id"), p.get("blocker_id"), p.get("blocker_id")))
    if kind == "notice_issued":
        close_deadlines(conn, event)


# ------------------------------------------------------------- deadlines --

def open_deadlines(conn: sqlite3.Connection, event: Dict[str, Any]) -> List[str]:
    created = []
    trigger_day = dates.from_iso(event.get("occurred_at"))
    if not trigger_day:
        return created
    for ref in conn.execute("SELECT * FROM jv_contract_refs WHERE project_id = ?", (event["project_id"],)):
        types = store.loads(ref["trigger_types"], [])
        if types and event["event_type"] not in types:
            continue
        due = trigger_day + _dt.timedelta(days=int(ref["notice_period_days"]))
        deadline_id = store.new_id("ND")
        cur = conn.execute("INSERT OR IGNORE INTO jv_notice_deadlines (deadline_id, project_id, ref_id, trigger_kind, "
                           "trigger_id, trigger_date, due_date, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?)",
                           (deadline_id, event["project_id"], ref["ref_id"], event["event_type"], event["event_id"],
                            trigger_day.isoformat(), due.isoformat(), store.now()))
        if cur.rowcount:
            created.append(deadline_id)
            # an earlier notice for the same activity already satisfies a new deadline
            _match_existing_notice(conn, deadline_id, event, ref)
    return created


def _match_existing_notice(conn, deadline_id, event, ref):
    row = conn.execute("SELECT n.* FROM jv_notices n JOIN jv_events e ON e.event_id = n.event_id WHERE n.project_id = ? "
                       "AND e.activity_id IS ? AND n.issued_on >= ? AND (n.notice_type = ? OR n.ref_id = ? OR "
                       "n.ref_id IS NULL) ORDER BY n.issued_on LIMIT 1",
                       (event["project_id"], event.get("activity_id"), dates.iso(event.get("occurred_at")),
                        ref["notice_type"], ref["ref_id"])).fetchone()
    if row:
        _settle(conn, deadline_id, row["notice_id"], row["issued_on"])


def _settle(conn, deadline_id: str, notice_id: str, issued_on: str) -> None:
    deadline = conn.execute("SELECT due_date FROM jv_notice_deadlines WHERE deadline_id = ?", (deadline_id,)).fetchone()
    status = "met" if issued_on and issued_on <= deadline["due_date"] else "late"
    conn.execute("UPDATE jv_notice_deadlines SET status = ?, notice_id = ? WHERE deadline_id = ?",
                 (status, notice_id, deadline_id))


def close_deadlines(conn: sqlite3.Connection, event: Dict[str, Any]) -> None:
    p = event.get("payload") or {}
    notice_id = p.get("notice_id") or store.new_id("NT")
    issued = dates.iso(p.get("issued_on") or event.get("occurred_at"))
    store.insert(conn, "jv_notices", {
        "notice_id": notice_id, "project_id": event["project_id"], "ref_id": p.get("ref_id"),
        "trigger_kind": p.get("trigger_kind"), "trigger_id": p.get("trigger_id"),
        "notice_type": p.get("notice_type") or "delay", "issued_on": issued, "issued_by": p.get("from_party"),
        "to_party": p.get("to_party"), "reference": p.get("reference"),
        "evidence_id": (event.get("evidence_ids") or [None])[0], "event_id": event["event_id"],
        "created_at": store.now()}, or_ignore=True)
    rows = conn.execute("SELECT d.deadline_id, d.trigger_date, r.notice_type, r.ref_id FROM jv_notice_deadlines d "
                        "JOIN jv_contract_refs r ON r.ref_id = d.ref_id JOIN jv_events e ON e.event_id = d.trigger_id "
                        "WHERE d.project_id = ? AND d.status = 'open' AND e.activity_id IS ?",
                        (event["project_id"], event.get("activity_id"))).fetchall()
    for row in rows:
        if p.get("ref_id") and p["ref_id"] != row["ref_id"]:
            continue
        if not p.get("ref_id") and p.get("notice_type") and p["notice_type"] != row["notice_type"]:
            continue
        if issued and issued >= row["trigger_date"]:
            _settle(conn, row["deadline_id"], notice_id, issued)


def deadlines(conn: sqlite3.Connection, project_id: str, today: Optional[_dt.date] = None,
              remind_within: int = 7, push: bool = False) -> List[Dict[str, Any]]:
    today = today or dates.today()
    conn.execute("UPDATE jv_notice_deadlines SET status = 'missed' WHERE project_id = ? AND status = 'open' "
                 "AND due_date < ?", (project_id, today.isoformat()))
    rows = [dict(r) for r in conn.execute(
        "SELECT d.*, r.clause, r.title AS clause_title, r.notice_type, r.notice_period_days, r.recipient, "
        "e.activity_id, a.code AS activity_code, a.name AS activity_name, e.payload AS trigger_payload, "
        "n.reference AS notice_reference, n.issued_on AS notice_issued_on "
        "FROM jv_notice_deadlines d JOIN jv_contract_refs r ON r.ref_id = d.ref_id "
        "LEFT JOIN jv_events e ON e.event_id = d.trigger_id LEFT JOIN jv_activities a ON a.activity_id = e.activity_id "
        "LEFT JOIN jv_notices n ON n.notice_id = d.notice_id WHERE d.project_id = ? ORDER BY d.due_date",
        (project_id,))]
    for row in rows:
        row["days_left"] = (dates.from_iso(row["due_date"]) - today).days
        payload = store.loads(row.pop("trigger_payload"), {})
        row["trigger_description"] = payload.get("description", "")
        row["reminder"] = row["status"] == "open" and 0 <= row["days_left"] <= remind_within
        if push and row["reminder"]:
            key = f"reminded:{row['deadline_id']}:{today.isoformat()}"
            if not conn.execute("SELECT 1 FROM jv_meta WHERE key = ?", (key,)).fetchone():
                conn.execute("INSERT INTO jv_meta (key, value) VALUES (?, ?)", (key, "1"))
                try:
                    from core.background_events import push_event
                    push_event(f"ZEUS: {row['clause']} {row['notice_type']} notice for {row['activity_code'] or 'the project'} "
                               f"due {dates.fmt(row['due_date'])} ({row['days_left']} day(s) left)")
                except Exception:
                    pass
    return rows


# ------------------------------------------------------------- the timeline --

def timeline(conn: sqlite3.Connection, project_id: str, date_from: Optional[str] = None,
             date_to: Optional[str] = None, activity_id: Optional[str] = None, contractor: Optional[str] = None,
             kinds: Optional[List[str]] = None, source_type: Optional[str] = None,
             delay_type: Optional[str] = None, with_evidence_only: bool = False,
             include_approvals: bool = False, limit: int = 1000) -> List[Dict[str, Any]]:
    sql = ("SELECT c.*, a.code AS activity_code, a.name AS activity_name FROM jv_chronology c "
           "LEFT JOIN jv_activities a ON a.activity_id = c.activity_id WHERE c.project_id = ?")
    params: List[Any] = [project_id]
    if date_from:
        sql += " AND substr(c.occurred_at, 1, 10) >= ?"
        params.append(dates.iso(date_from))
    if date_to:
        sql += " AND substr(c.occurred_at, 1, 10) <= ?"
        params.append(dates.iso(date_to))
    if activity_id:
        sql += " AND c.activity_id = ?"
        params.append(activity_id)
    if contractor:
        sql += " AND lower(COALESCE(c.contractor, '')) LIKE ?"
        params.append(f"%{contractor.lower()}%")
    if kinds:
        sql += f" AND c.kind IN ({','.join('?' for _ in kinds)})"
        params.extend(kinds)
    elif not include_approvals:
        sql += " AND c.kind NOT IN ('approval', 'link_confirmed')"
    if source_type:
        sql += " AND c.source_type = ?"
        params.append(source_type)
    if delay_type:
        sql += " AND c.delay_type = ?"
        params.append(delay_type)
    if with_evidence_only:
        sql += " AND c.evidence_ids != '[]'"
    sql += " ORDER BY c.occurred_at, c.captured_at LIMIT ?"
    params.append(int(limit))
    rows = [dict(r) for r in conn.execute(sql, params)]
    status = {r[0]: r[1] for r in conn.execute(
        "SELECT s.event_id, s.status FROM jv_event_status s JOIN jv_events e ON e.event_id = s.event_id "
        "WHERE e.project_id = ?", (project_id,))}
    payloads = {r[0]: store.loads(r[1], {}) for r in conn.execute(
        "SELECT event_id, payload FROM jv_events WHERE project_id = ?", (project_id,))}
    by_event = {r["source_id"]: r for r in rows}
    for row in rows:
        row["evidence_ids"] = store.loads(row["evidence_ids"], [])
        row["status"] = status.get(row["source_id"])
        payload = payloads.get(row["source_id"], {})
        related = []
        for key in ("trigger_event_id", "target", "event_a", "event_b", "chosen_event", "trigger_id"):
            if payload.get(key):
                related.append({"relation": key.replace("_", " "), "event_id": payload[key],
                                "title": (by_event.get(payload[key]) or {}).get("title")})
        row["related"] = related
    return rows


def delay_windows(conn: sqlite3.Connection, project_id: str) -> List[Dict[str, Any]]:
    """Windows between consecutive snapshots: how far the finish moved inside
    each, and what drove it. Structured for a windows analysis; it does not
    apportion responsibility - that remains an expert's judgement."""
    snaps = [dict(r) for r in conn.execute(
        "SELECT snapshot_id, seq, captured_at, data_date, project_finish, version_id, trigger_event_id, reason "
        "FROM jv_snapshots WHERE project_id = ? ORDER BY seq", (project_id,))]
    changes = {r["to_snapshot"]: dict(r) for r in conn.execute(
        "SELECT * FROM jv_forecast_changes WHERE project_id = ?", (project_id,))}
    out = []
    for before, after in zip(snaps, snaps[1:]):
        change = changes.get(after["snapshot_id"]) or {}
        chain = store.loads(change.get("chain"), [])
        start, end = before["data_date"], after["data_date"]
        count = conn.execute("SELECT COUNT(*) FROM jv_events WHERE project_id = ? AND captured_at > ? AND "
                             "captured_at <= ? AND event_type IN ('progress_update','activity_started',"
                             "'activity_completed','blocker_reported','blocker_cleared','delay_reported',"
                             "'correspondence_received','notice_issued')",
                             (project_id, before["captured_at"], after["captured_at"])).fetchone()[0]
        out.append({"window": f"{dates.fmt(before['captured_at'])} -> {dates.fmt(after['captured_at'])}",
                    "from_snapshot": before["snapshot_id"], "to_snapshot": after["snapshot_id"],
                    "data_date_from": start, "data_date_to": end,
                    "finish_before": before["project_finish"], "finish_after": after["project_finish"],
                    "movement_days": dates.days_between(before["project_finish"], after["project_finish"]) or 0,
                    "driving_activity": chain[0]["code"] if chain else None,
                    "driving_path": [s["code"] for s in chain], "trigger_event_id": after.get("trigger_event_id"),
                    "reason": after.get("reason"), "field_events_in_window": count,
                    "schedule_version_changed": before["version_id"] != after["version_id"]})
    return out
