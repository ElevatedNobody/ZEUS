"""
The event store - ZEUS's durable, chronological project history.

Every change of state is an EVENT, appended and never edited:

    progress_update  activity_started  activity_completed  remaining_update
    blocker_reported  blocker_cleared  delay_reported  correspondence_received
    notice_issued  evidence_captured  inspection_completed  schedule_imported
    schedule_changed  forecast_changed  approval  conflict_detected  conflict_resolved

Three times are kept on every event (bitemporal history):
    occurred_at   when it happened on site
    captured_at   when a device recorded it - when the organisation first KNEW
    received_at   when this store received it (after an offline sync, later)

Approvals are events too; the current status table (`jv_event_status`) is only
a cache of them. So "what did we know about this activity on 12 September?"
is answered by replaying the events and the approvals that had been captured by
the end of 12 September - nothing later leaks into the answer.

Each event carries a content hash (stable across devices) and a chain hash
over the local log, so a record that was edited or removed is detected.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import socket
import sqlite3
import uuid
from typing import Any, Dict, Iterable, List, Optional, Sequence

from jarvis import dates, store

EVENT_TYPES = ("progress_update", "activity_started", "activity_completed", "remaining_update",
               "blocker_reported", "blocker_cleared", "delay_reported", "correspondence_received",
               "notice_issued", "evidence_captured", "inspection_completed", "schedule_imported",
               "schedule_changed", "forecast_changed", "approval", "conflict_detected", "conflict_resolved",
               "link_confirmed")

# Events that change an activity's state - these need an approval before the
# forecast uses them, and these are the ones offline devices can conflict on.
STATE_EVENTS = ("progress_update", "activity_started", "activity_completed", "remaining_update",
                "blocker_reported", "blocker_cleared")
STATE_FIELDS = {"progress_update": ("percent_complete", "quantity_done"),
                "activity_started": ("actual_start",), "activity_completed": ("actual_finish",),
                "remaining_update": ("remaining_days",)}
# Recorded facts that need no approval to exist (the approval of a state change is
# what the forecast waits for; a letter received is simply a fact with evidence).
RECORD_EVENTS = ("delay_reported", "correspondence_received", "notice_issued", "evidence_captured",
                 "inspection_completed", "schedule_imported", "schedule_changed", "forecast_changed",
                 "approval", "conflict_detected", "conflict_resolved", "link_confirmed")

GENESIS = "0" * 64


# ------------------------------------------------------------ device identity --

def _state(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM jv_sync_state WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def _set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO jv_sync_state (key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


def device_id(conn: Optional[sqlite3.Connection] = None) -> str:
    """This installation's device id - created once, kept in the project store."""
    if conn is None:
        with store.connection() as own:
            return device_id(own)
    value = _state(conn, "device_id")
    if not value:
        host = "".join(ch for ch in socket.gethostname().upper() if ch.isalnum())[:12] or "DEVICE"
        value = f"{host}-{uuid.uuid4().hex[:6].upper()}"
        _set_state(conn, "device_id", value)
    return value


def _next_lamport(conn: sqlite3.Connection, seen: Optional[int] = None) -> int:
    current = int(_state(conn, "lamport") or 0)
    value = max(current, int(seen or 0)) + 1
    _set_state(conn, "lamport", str(value))
    return value


def observe_lamport(conn: sqlite3.Connection, seen: int) -> None:
    current = int(_state(conn, "lamport") or 0)
    if seen > current:
        _set_state(conn, "lamport", str(seen))


# ------------------------------------------------------------------ hashing --

_HASHED = ("event_id", "project_id", "event_type", "activity_id", "occurred_at", "captured_at", "device_id",
           "actor", "source", "payload", "evidence_ids", "observation_id", "confidence", "base_head",
           "lamport", "origin")


def content_hash(event: Dict[str, Any]) -> str:
    body = {k: event.get(k) for k in _HASHED}
    if isinstance(body.get("payload"), str):
        body["payload"] = store.loads(body["payload"], body["payload"])
    if isinstance(body.get("evidence_ids"), str):
        body["evidence_ids"] = store.loads(body["evidence_ids"], [])
    text = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def chain_hash(prev: str, content: str) -> str:
    return hashlib.sha256(f"{prev}:{content}".encode("ascii")).hexdigest()


# ------------------------------------------------------------------- append --

def head_of(conn: sqlite3.Connection, activity_id: Optional[str]) -> Optional[str]:
    """The latest event this store knows for an activity - recorded on each new
    event as its `base_head`, which is how concurrent offline edits are told
    apart from sequential ones."""
    if not activity_id:
        return None
    row = conn.execute("SELECT event_id FROM jv_events WHERE activity_id = ? AND event_type IN "
                       f"({','.join('?' for _ in STATE_EVENTS + ('conflict_resolved',))}) ORDER BY seq DESC LIMIT 1",
                       (activity_id, *STATE_EVENTS, "conflict_resolved")).fetchone()
    return row[0] if row else None


def append(conn: sqlite3.Connection, project_id: str, event_type: str, *, activity_id: Optional[str] = None,
           occurred_at: Optional[str] = None, captured_at: Optional[str] = None, device: Optional[str] = None,
           actor: Optional[str] = None, source: str = "field", payload: Optional[Dict[str, Any]] = None,
           evidence_ids: Sequence[str] = (), observation_id: Optional[str] = None,
           confidence: Optional[float] = None, origin: str = "company", status: Optional[str] = None,
           event_id: Optional[str] = None, base_head: Optional[str] = "__auto__", lamport: Optional[int] = None,
           sync_state: str = "local", expected_content_hash: Optional[str] = None,
           chronology: bool = True) -> Dict[str, Any]:
    if event_type not in EVENT_TYPES:
        raise ValueError(f"unknown event type '{event_type}'")
    captured = dates.iso_dt(captured_at) if captured_at else store.now_iso()
    event = {
        "event_id": event_id or str(uuid.uuid4()),
        "project_id": project_id, "event_type": event_type, "activity_id": activity_id,
        "occurred_at": dates.iso_dt(occurred_at) if occurred_at else captured,
        "captured_at": captured,
        "device_id": device or device_id(conn),
        "actor": actor if actor is not None else store.current_user(),
        "source": source,
        "payload": payload or {},
        "evidence_ids": list(evidence_ids or []),
        "observation_id": observation_id,
        "confidence": None if confidence is None else round(float(confidence), 3),
        "base_head": head_of(conn, activity_id) if base_head == "__auto__" else base_head,
        "lamport": _next_lamport(conn) if lamport is None else int(lamport),
        "origin": origin,
    }
    if lamport is not None:
        observe_lamport(conn, int(lamport))
    content = content_hash(event)
    if expected_content_hash and expected_content_hash != content:
        raise ValueError(f"event {event['event_id']} does not match its content hash - refused")
    last = conn.execute("SELECT seq, chain_hash FROM jv_events ORDER BY seq DESC LIMIT 1").fetchone()
    prev = last["chain_hash"] if last else GENESIS
    seq = (last["seq"] + 1) if last else 1
    row = dict(event, seq=seq, payload=store.dumps(event["payload"]), evidence_ids=store.dumps(event["evidence_ids"]),
               received_at=store.now(), sync_state=sync_state, content_hash=content, prev_hash=prev,
               chain_hash=chain_hash(prev, content))
    store.insert(conn, "jv_events", row)
    initial = status or ("submitted" if event_type in STATE_EVENTS else "recorded")
    conn.execute("INSERT OR REPLACE INTO jv_event_status (event_id, status) VALUES (?, ?)",
                 (event["event_id"], initial))
    event.update(seq=seq, content_hash=content, chain_hash=row["chain_hash"], status=initial,
                 received_at=row["received_at"])
    if chronology:
        from jarvis import chronology as _chronology
        _chronology.record_event(conn, event)
    return event


def get(conn: sqlite3.Connection, event_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT e.*, s.status, s.decided_by, s.decided_at, s.decision_event_id, s.reason "
                       "FROM jv_events e LEFT JOIN jv_event_status s ON s.event_id = e.event_id "
                       "WHERE e.event_id = ?", (event_id,)).fetchone()
    return _decode(dict(row)) if row else None


def _decode(row: Dict[str, Any]) -> Dict[str, Any]:
    row["payload"] = store.loads(row.get("payload"), {})
    row["evidence_ids"] = store.loads(row.get("evidence_ids"), [])
    return row


def list_events(conn: sqlite3.Connection, project_id: str, activity_id: Optional[str] = None,
                types: Optional[Iterable[str]] = None, known_by: Optional[str] = None,
                limit: int = 5000) -> List[Dict[str, Any]]:
    sql = ("SELECT e.*, s.status, s.decided_by, s.decided_at, s.decision_event_id, s.reason FROM jv_events e "
           "LEFT JOIN jv_event_status s ON s.event_id = e.event_id WHERE e.project_id = ?")
    params: List[Any] = [project_id]
    if activity_id:
        sql += " AND e.activity_id = ?"
        params.append(activity_id)
    if types:
        types = list(types)
        sql += f" AND e.event_type IN ({','.join('?' for _ in types)})"
        params.extend(types)
    if known_by:
        sql += " AND e.captured_at <= ?"
        params.append(known_by)
    sql += " ORDER BY e.occurred_at, e.captured_at, e.seq LIMIT ?"
    params.append(int(limit))
    return [_decode(dict(r)) for r in conn.execute(sql, params).fetchall()]


# ----------------------------------------------------------------- approval --

def decide(conn: sqlite3.Connection, event_id: str, approved: bool, reason: str = "",
           captured_at: Optional[str] = None, device: Optional[str] = None,
           actor: Optional[str] = None) -> Dict[str, Any]:
    """Approve or reject a submitted state event. The decision is itself an
    event (who, when, why), and only then is the status cache updated."""
    target = get(conn, event_id)
    if target is None:
        raise ValueError(f"no event {event_id}")
    if target["status"] not in ("submitted",):
        raise ValueError(f"event {event_id} is {target['status']} - only a submitted update can be decided"
                         + (" (resolve the sync conflict instead)" if target["status"] == "conflicted" else ""))
    decision = append(conn, target["project_id"], "approval", activity_id=target["activity_id"],
                      captured_at=captured_at, occurred_at=captured_at, device=device, actor=actor,
                      source="review",
                      payload={"target": event_id, "decision": "approved" if approved else "rejected",
                               "reason": reason, "target_type": target["event_type"]},
                      evidence_ids=target["evidence_ids"], status="recorded", base_head=None)
    conn.execute("UPDATE jv_event_status SET status = ?, decided_by = ?, decided_at = ?, decision_event_id = ?, "
                 "reason = ? WHERE event_id = ?",
                 ("approved" if approved else "rejected", decision["actor"], decision["captured_at"],
                  decision["event_id"], reason, event_id))
    return decision


def set_status(conn: sqlite3.Connection, event_id: str, status: str, reason: str = "",
               decision_event_id: Optional[str] = None) -> None:
    conn.execute("UPDATE jv_event_status SET status = ?, reason = COALESCE(NULLIF(?, ''), reason), "
                 "decision_event_id = COALESCE(?, decision_event_id) WHERE event_id = ?",
                 (status, reason, decision_event_id, event_id))


def statuses_as_of(conn: sqlite3.Connection, project_id: str, known_by: Optional[str]) -> Dict[str, str]:
    """Each event's status as it stood at `known_by` (captured time), rebuilt
    from the approval / resolution events captured by then."""
    if not known_by:
        return {r[0]: r[1] for r in conn.execute(
            "SELECT s.event_id, s.status FROM jv_event_status s JOIN jv_events e ON e.event_id = s.event_id "
            "WHERE e.project_id = ?", (project_id,))}
    status: Dict[str, str] = {}
    rows = conn.execute("SELECT event_id, event_type, payload FROM jv_events WHERE project_id = ? AND captured_at <= ? "
                        "ORDER BY captured_at, seq", (project_id, known_by)).fetchall()
    for event_id, event_type, payload in rows:
        if event_type in STATE_EVENTS:
            status.setdefault(event_id, "submitted")
        elif event_type == "approval":
            body = store.loads(payload, {})
            if body.get("target"):
                status[body["target"]] = body.get("decision", "approved")
        elif event_type == "conflict_detected":
            body = store.loads(payload, {})
            for target in (body.get("event_a"), body.get("event_b")):
                if target and status.get(target) in (None, "submitted", "approved"):
                    status[target] = "conflicted"
        elif event_type == "conflict_resolved":
            body = store.loads(payload, {})
            if body.get("chosen_event"):
                status[body["chosen_event"]] = "approved"
            for discarded in body.get("discarded_events") or []:
                status[discarded] = "superseded"
    return status


# --------------------------------------------------------- state projection --

def activity_states(conn: sqlite3.Connection, project_id: str, known_by: Optional[str] = None,
                    include_pending: bool = False, occurred_by: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """Each activity's actual state from APPROVED events, as known at `known_by`.

    `occurred_by` additionally ignores anything that happened on site after that
    moment. With `include_pending`, submitted-but-unapproved updates are applied
    too and the state says so."""
    status = statuses_as_of(conn, project_id, known_by)
    rows = list_events(conn, project_id, types=STATE_EVENTS + ("conflict_resolved",), known_by=known_by)
    states: Dict[str, Dict[str, Any]] = {}

    def state_for(activity_id: str) -> Dict[str, Any]:
        return states.setdefault(activity_id, {
            "actual_start": None, "actual_finish": None, "percent_complete": None, "quantity_done": None,
            "remaining_days": None, "blockers": {}, "events": [], "evidence_ids": [], "pending": [],
            "last_update": None, "start_evidenced": False, "sources": {}})

    for event in rows:
        if not event.get("activity_id"):
            continue
        if occurred_by and (event.get("occurred_at") or "") > occurred_by:
            continue
        st = status.get(event["event_id"], "submitted")
        effective = st == "approved" or (include_pending and st == "submitted")
        s = state_for(event["activity_id"])
        if st == "submitted":
            s["pending"].append(event["event_id"])
        if event["event_type"] == "conflict_resolved":
            _apply_resolution(s, event)
            continue
        if not effective:
            continue
        _apply(s, event)
    return states


def _day(event: Dict[str, Any], key: str) -> Optional[str]:
    value = (event.get("payload") or {}).get(key)
    return dates.iso(value) if value else dates.iso(event.get("occurred_at"))


def _apply(s: Dict[str, Any], event: Dict[str, Any]) -> None:
    p = event.get("payload") or {}
    kind = event["event_type"]
    s["events"].append(event["event_id"])
    for evidence in event.get("evidence_ids") or []:
        if evidence not in s["evidence_ids"]:
            s["evidence_ids"].append(evidence)
    s["last_update"] = event.get("occurred_at")
    if kind == "progress_update":
        if p.get("percent_complete") is not None:
            s["percent_complete"] = float(p["percent_complete"])
            s["sources"]["percent_complete"] = event["event_id"]
        if p.get("quantity_done") is not None:
            s["quantity_done"] = float(p["quantity_done"])
        if (s["percent_complete"] or 0) > 0 and not s["actual_start"]:
            s["actual_start"] = dates.iso(event.get("occurred_at"))
            s["sources"]["actual_start"] = event["event_id"]
    elif kind == "activity_started":
        s["actual_start"] = _day(event, "actual_start")
        s["start_evidenced"] = True
        s["sources"]["actual_start"] = event["event_id"]
    elif kind == "activity_completed":
        s["actual_finish"] = _day(event, "actual_finish")
        s["percent_complete"] = 100.0
        s["remaining_days"] = 0
        s["sources"]["actual_finish"] = event["event_id"]
        if p.get("actual_start"):
            s["actual_start"] = dates.iso(p["actual_start"])
            s["start_evidenced"] = True
        for blocker in s["blockers"].values():
            if blocker["status"] == "open":
                blocker.update(status="cleared", cleared_on=s["actual_finish"])
    elif kind == "remaining_update":
        if p.get("remaining_days") is not None:
            s["remaining_days"] = int(p["remaining_days"])
            s["sources"]["remaining_days"] = event["event_id"]
    elif kind == "blocker_reported":
        blocker_id = p.get("blocker_id") or event["event_id"]
        s["blockers"][blocker_id] = {"blocker_id": blocker_id, "status": "open",
                                     "description": p.get("description", ""), "category": p.get("category", ""),
                                     "expected_clear": dates.iso(p.get("expected_clear")) if p.get("expected_clear") else None,
                                     "raised_on": dates.iso(event.get("occurred_at")), "event_id": event["event_id"]}
    elif kind == "blocker_cleared":
        target = p.get("blocker_id")
        for blocker_id, blocker in s["blockers"].items():
            if blocker["status"] == "open" and (not target or target == blocker_id):
                blocker.update(status="cleared", cleared_on=dates.iso(event.get("occurred_at")),
                               cleared_event=event["event_id"])


def _apply_resolution(s: Dict[str, Any], event: Dict[str, Any]) -> None:
    p = event.get("payload") or {}
    field_name, value = p.get("field"), p.get("value")
    s["events"].append(event["event_id"])
    if field_name == "percent_complete" and value is not None:
        s["percent_complete"] = float(value)
        s["sources"]["percent_complete"] = event["event_id"]
        if float(value) > 0 and not s["actual_start"]:
            s["actual_start"] = dates.iso(p.get("occurred_at") or event.get("occurred_at"))
    elif field_name in ("actual_start", "actual_finish") and value:
        s[field_name] = dates.iso(value)
        s["sources"][field_name] = event["event_id"]
        if field_name == "actual_finish":
            s["percent_complete"] = 100.0
    elif field_name == "remaining_days" and value is not None:
        s["remaining_days"] = int(value)


# ------------------------------------------------------------- verification --

def verify_chain(conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """Recompute every content hash and the chain. Any edit, deletion or
    reordering of the log shows up as the first sequence number that breaks."""
    if conn is None:
        with store.connection() as own:
            return verify_chain(own)
    prev, checked, problems = GENESIS, 0, []
    for row in conn.execute("SELECT * FROM jv_events ORDER BY seq"):
        event = dict(row)
        checked += 1
        expected_content = content_hash(event)
        if expected_content != event["content_hash"]:
            problems.append({"seq": event["seq"], "event_id": event["event_id"], "problem": "content changed"})
        if event["prev_hash"] != prev:
            problems.append({"seq": event["seq"], "event_id": event["event_id"], "problem": "chain broken before this event"})
        if chain_hash(event["prev_hash"], event["content_hash"]) != event["chain_hash"]:
            problems.append({"seq": event["seq"], "event_id": event["event_id"], "problem": "chain hash wrong"})
        prev = event["chain_hash"]
    return {"ok": not problems, "events": checked, "problems": problems[:20], "head": prev}


def as_of_cutoff(value: Optional[str]) -> Optional[str]:
    """'12 September' / '2026-09-12' -> '2026-09-12T23:59:59' (end of that day);
    a full timestamp is kept as it is."""
    if not value:
        return None
    text = str(value)
    if "T" in text and dates.parse_datetime(text):
        return dates.iso_dt(text)
    return dates.end_of_day(dates.parse_date(text) or text)


def day_after(value: Optional[str]) -> Optional[_dt.date]:
    day = dates.from_iso(value)
    return day + _dt.timedelta(days=1) if day else None
