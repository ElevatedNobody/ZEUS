"""
"Why did the finish date move?" - answered from the schedule logic, with the
field evidence attached, not with "schedule risk increased".

    OLD FORECAST -> CHANGED ACTIVITY -> TRIGGERING EVENT -> DEPENDENCY chain
    -> DOWNSTREAM ACTIVITIES -> NEW FORECAST -> EVIDENCE

`build_chain()` compares two snapshots deterministically. Starting at the
activity that sets the project finish, it follows each activity's DRIVER (the
constraint the CPM engine recorded as binding) back through the predecessors
that moved, until it reaches the activity whose own dates changed for a reason
of its own - a blocker, reported progress, a late actual, the data date. That
is the root; the path from it to the finish is the causal chain.

`why_moved()` turns a stored forecast change into the answer: which approved
update triggered it, who captured it, on which device, when it was approved,
the original evidence, the dependency that carried the delay, what else moved
and how the float changed. A narrative sentence is generated from those facts
only; a model may phrase it, but a model sentence naming a date that is not in
the facts is discarded.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any, Dict, List, Optional

from jarvis import dates, events, store

_ROOT_REASON = {
    "blocker": "an open blocker holds the remaining work until it clears",
    "data_date": "the work was not done by the data date, so what remains is rescheduled from then",
    "actual": "an actual date was reported",
    "constraint": "a start-no-earlier-than constraint",
    "project_start": "the project start",
    "predecessor": "its predecessor's dates",
}


def build_chain(prev: Dict[str, Dict], new: Dict[str, Dict], finish_id: str,
                prev_finish: Optional[str], new_finish: Optional[str]) -> Dict[str, Any]:
    moved: Dict[str, int] = {}
    for aid, after in new.items():
        before = prev.get(aid)
        if before and before.get("ef") != after.get("ef"):
            moved[aid] = dates.days_between(before.get("ef"), after.get("ef")) or 0
    finish_delta = dates.days_between(prev_finish, new_finish) or 0

    def step(aid: str) -> Dict[str, Any]:
        before, after = prev.get(aid, {}), new.get(aid, {})
        drv = after.get("driver") or {}
        return {"activity_id": aid, "code": after.get("code"), "name": after.get("name"),
                "type": after.get("type"), "previous_start": before.get("es"), "new_start": after.get("es"),
                "previous_finish": before.get("ef"), "new_finish": after.get("ef"), "delta_days": moved.get(aid, 0),
                "driver": drv, "via": ({"pred": drv.get("pred"), "pred_code": drv.get("pred_code"),
                                        "type": drv.get("type"), "lag": drv.get("lag")}
                                       if drv.get("kind") == "predecessor" else None),
                "float_before": before.get("total_float"), "float_after": after.get("total_float"),
                "critical_before": bool(before.get("critical")), "critical_after": bool(after.get("critical")),
                "near_critical_before": bool(before.get("near_critical"))}

    path: List[str] = []
    cursor = finish_id if (finish_id in moved or finish_delta) else None
    if cursor is None and moved:
        # The finish held (float absorbed it): explain the largest movement instead.
        cursor = max(moved, key=lambda a: (moved[a], a))
    seen = set()
    while cursor and cursor not in seen:
        seen.add(cursor)
        path.append(cursor)
        drv = (new.get(cursor) or {}).get("driver") or {}
        pred = drv.get("pred") if drv.get("kind") == "predecessor" else None
        cursor = pred if pred in moved else None
    path.reverse()
    chain = [step(aid) for aid in path]
    root = chain[0] if chain else None
    if root:
        kind = (root["driver"] or {}).get("kind")
        root["root_reason"] = _ROOT_REASON.get(kind, kind or "")
    affected = sorted((step(aid) for aid in moved), key=lambda s: (-abs(s["delta_days"]), s["code"] or ""))
    milestones = [s for s in affected if "milestone" in (s.get("type") or "")]
    return {"moved": bool(moved) or bool(finish_delta), "finish_delta_days": finish_delta,
            "root_activity_id": root["activity_id"] if root else None,
            "chain": chain, "affected": affected, "milestones": milestones,
            "absorbed_by_float": bool(moved) and not finish_delta}


# --------------------------------------------------------------- the answer --

def _evidence(conn: sqlite3.Connection, evidence_ids: List[str]) -> List[Dict[str, Any]]:
    if not evidence_ids:
        return []
    rows = conn.execute(f"SELECT evidence_id, kind, title, filename, sha256, captured_at, occurred_at, captured_by, "
                        f"device_id, source_type, original_text, language, origin FROM jv_evidence WHERE evidence_id IN "
                        f"({','.join('?' for _ in evidence_ids)})", list(evidence_ids)).fetchall()
    out = []
    for r in rows:
        item = dict(r)
        item["excerpt"] = (item.pop("original_text") or "")[:400]
        out.append(item)
    order = {e: i for i, e in enumerate(evidence_ids)}
    return sorted(out, key=lambda e: order.get(e["evidence_id"], 0))


def _event_view(conn: sqlite3.Connection, event: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not event:
        return None
    activity = conn.execute("SELECT code, name FROM jv_activities WHERE activity_id = ?",
                            (event.get("activity_id"),)).fetchone()
    return {"event_id": event["event_id"], "event_type": event["event_type"],
            "activity_id": event.get("activity_id"),
            "activity_code": activity["code"] if activity else None,
            "activity_name": activity["name"] if activity else None,
            "occurred_at": event.get("occurred_at"), "captured_at": event.get("captured_at"),
            "device_id": event.get("device_id"), "actor": event.get("actor"), "source": event.get("source"),
            "confidence": event.get("confidence"), "payload": event.get("payload"),
            "status": event.get("status"), "approved_by": event.get("decided_by"),
            "approved_at": event.get("decided_at"), "content_hash": event.get("content_hash"),
            "evidence": _evidence(conn, event.get("evidence_ids") or [])}


def list_changes(conn: sqlite3.Connection, project_id: str, only_finish: bool = False) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM jv_forecast_changes WHERE project_id = ?"
    if only_finish:
        sql += " AND delta_days != 0"
    rows = [dict(r) for r in conn.execute(sql + " ORDER BY created_at DESC", (project_id,))]
    for r in rows:
        chain = store.loads(r.pop("chain"), [])
        r["chain_codes"] = [s.get("code") for s in chain]
        r["root_code"] = chain[0]["code"] if chain else None
        r.pop("affected", None)
        r.pop("milestones", None)
    return rows


def why_moved(conn: sqlite3.Connection, project_id: str, change_id: Optional[str] = None,
              use_model: bool = False) -> Dict[str, Any]:
    if change_id:
        row = conn.execute("SELECT * FROM jv_forecast_changes WHERE change_id = ? AND project_id = ?",
                           (change_id, project_id)).fetchone()
    else:
        row = conn.execute("SELECT * FROM jv_forecast_changes WHERE project_id = ? AND delta_days != 0 "
                           "ORDER BY created_at DESC LIMIT 1", (project_id,)).fetchone()
        row = row or conn.execute("SELECT * FROM jv_forecast_changes WHERE project_id = ? ORDER BY created_at DESC "
                                  "LIMIT 1", (project_id,)).fetchone()
    if not row:
        return {"ok": True, "found": False,
                "answer": "The forecast finish has not moved: no approved update has changed it since the schedule "
                          "was approved.", "facts": []}
    change = dict(row)
    chain = store.loads(change["chain"], [])
    affected = store.loads(change["affected"], [])
    milestones = store.loads(change["milestones"], [])
    trigger = events.get(conn, change["trigger_event_id"]) if change.get("trigger_event_id") else None
    if trigger and not trigger.get("decided_by") and trigger["event_type"] in ("schedule_changed", "conflict_resolved"):
        # these ARE the decision: whoever recorded them approved them
        trigger["decided_by"], trigger["decided_at"] = trigger.get("actor"), trigger.get("captured_at")
    root = chain[0] if chain else None
    root_events = []
    if root:
        root_events = [e for e in events.list_events(conn, project_id, activity_id=root["activity_id"])
                       if e["event_type"] in events.STATE_EVENTS + ("delay_reported", "correspondence_received",
                                                                    "notice_issued")
                       and e.get("captured_at", "") <= (change.get("captured_at") or "9999")]
    correspondence = []
    if root:
        correspondence = [dict(r) for r in conn.execute(
            "SELECT c.* FROM jv_correspondence c JOIN jv_links l ON l.from_kind = 'correspondence' AND "
            "l.from_id = c.correspondence_id WHERE l.to_kind = 'activity' AND l.to_id = ? ORDER BY c.letter_date",
            (root["activity_id"],))]
    evidence_ids: List[str] = []
    for e in ([trigger] if trigger else []) + root_events:
        for ev in e.get("evidence_ids") or []:
            if ev not in evidence_ids:
                evidence_ids.append(ev)
    for c in correspondence:
        if c.get("evidence_id") and c["evidence_id"] not in evidence_ids:
            evidence_ids.append(c["evidence_id"])
    facts = _facts(change, chain, trigger, milestones)
    steps = _steps(change, chain, trigger, affected, conn)
    answer = narrative(change, chain, trigger, milestones)
    model_used = ""
    if use_model:
        phrased, model_used = _model_narrative(answer, facts)
        if phrased:
            answer = phrased
    return {"ok": True, "found": True, "change_id": change["change_id"],
            "previous_finish": change["previous_finish"], "new_finish": change["new_finish"],
            "delta_days": change["delta_days"], "captured_at": change["captured_at"],
            "answer": answer, "narrative_source": model_used or "deterministic template",
            "steps": steps, "chain": chain, "affected": affected, "milestones": milestones,
            "trigger": _event_view(conn, trigger),
            "root_activity_events": [_event_view(conn, e) for e in root_events],
            "correspondence": correspondence, "evidence": _evidence(conn, evidence_ids), "facts": facts,
            "classification": {"FACT": [f["text"] for f in facts if f["kind"] == "FACT"],
                               "INFERENCE": [f["text"] for f in facts if f["kind"] == "INFERENCE"]}}


def _steps(change, chain, trigger, affected, conn) -> List[Dict[str, Any]]:
    """The interactive chain the interface draws, every step clickable."""
    steps = [{"step": "OLD FORECAST", "title": dates.fmt(change["previous_finish"]),
              "detail": "project finish before this update", "ref": {"kind": "snapshot", "id": change["from_snapshot"]}}]
    if chain:
        root = chain[0]
        steps.append({"step": "CHANGED ACTIVITY", "title": f"{root['code']} {root['name']}",
                      "detail": f"finish {dates.fmt(root['previous_finish'])} -> {dates.fmt(root['new_finish'])} "
                                f"({root['delta_days']:+d} d): {root.get('root_reason', '')}",
                      "ref": {"kind": "activity", "id": root["activity_id"]}})
    if trigger:
        steps.append({"step": "TRIGGERING EVENT", "title": trigger["event_type"].replace("_", " "),
                      "detail": f"captured {trigger.get('captured_at')} by {trigger.get('actor') or '—'} on "
                                f"{trigger.get('device_id')}; approved by {trigger.get('decided_by') or '—'} "
                                f"at {trigger.get('decided_at') or '—'}",
                      "ref": {"kind": "event", "id": trigger["event_id"]}})
    for s in chain[1:]:
        via = s.get("via") or {}
        steps.append({"step": "DEPENDENCY", "title": f"{via.get('pred_code')} -{via.get('type', 'FS')}"
                                                     f"{'+' + str(via['lag']) if via.get('lag') else ''}-> {s['code']}",
                      "detail": f"{s['name']}: finish {dates.fmt(s['previous_finish'])} -> {dates.fmt(s['new_finish'])} "
                                f"({s['delta_days']:+d} d); float {s.get('float_before')} -> {s.get('float_after')}",
                      "ref": {"kind": "activity", "id": s["activity_id"]}})
    others = [a for a in affected if a["activity_id"] not in {c["activity_id"] for c in chain}]
    if others:
        steps.append({"step": "DOWNSTREAM ACTIVITIES",
                      "title": f"{len(others)} more activit{'y' if len(others) == 1 else 'ies'} moved",
                      "detail": ", ".join(f"{a['code']} ({a['delta_days']:+d} d)" for a in others[:12]),
                      "ref": {"kind": "activities", "id": [a["activity_id"] for a in others]}})
    steps.append({"step": "NEW FORECAST", "title": dates.fmt(change["new_finish"]),
                  "detail": f"project finish {change['delta_days']:+d} calendar day(s)",
                  "ref": {"kind": "snapshot", "id": change["to_snapshot"]}})
    evidence_ids = (trigger or {}).get("evidence_ids") or []
    steps.append({"step": "EVIDENCE", "title": f"{len(evidence_ids)} item(s) behind the triggering update",
                  "detail": "open to see the original field record, its extraction and hash",
                  "ref": {"kind": "evidence", "id": evidence_ids}})
    return steps


def _facts(change, chain, trigger, milestones) -> List[Dict[str, str]]:
    facts = [{"kind": "FACT", "text": f"Forecast project finish moved from {dates.fmt(change['previous_finish'])} to "
                                      f"{dates.fmt(change['new_finish'])} ({change['delta_days']:+d} days)."}]
    if trigger:
        p = trigger.get("payload") or {}
        what = p.get("description") or p.get("note") or trigger["event_type"].replace("_", " ")
        facts.append({"kind": "FACT", "text": f"Triggering update: {trigger['event_type'].replace('_', ' ')} - {what}; "
                                              f"captured {trigger.get('captured_at')} by {trigger.get('actor') or 'unknown'} "
                                              f"on device {trigger.get('device_id')}; approved by "
                                              f"{trigger.get('decided_by') or 'unknown'}."})
        if p.get("expected_clear"):
            facts.append({"kind": "FACT", "text": f"The blocker's expected clearance is {dates.fmt(p['expected_clear'])}."})
    for s in chain:
        facts.append({"kind": "FACT", "text": f"{s['code']} finish {dates.fmt(s['previous_finish'])} -> "
                                              f"{dates.fmt(s['new_finish'])} ({s['delta_days']:+d} d)."})
        if s.get("critical_before") and s is chain[0]:
            facts.append({"kind": "INFERENCE", "text": f"{s['code']} was on the critical path, so its delay passed "
                                                       f"straight to the finish."})
        elif s is chain[0] and s.get("float_before") is not None:
            facts.append({"kind": "INFERENCE", "text": f"{s['code']} had {s['float_before']} day(s) of float before "
                                                       f"the update; the delay exceeded it."})
    for m in milestones:
        facts.append({"kind": "FACT", "text": f"Milestone {m['code']} moved {m['delta_days']:+d} days to "
                                              f"{dates.fmt(m['new_finish'])}."})
    return facts


def narrative(change, chain, trigger, milestones) -> str:
    if not chain:
        return (f"The forecast finish moved from {dates.fmt(change['previous_finish'])} to "
                f"{dates.fmt(change['new_finish'])} ({change['delta_days']:+d} days).")
    root = chain[0]
    lines = []
    if change["delta_days"]:
        lines.append(f"The forecast finish moved {change['delta_days']:+d} days, from "
                     f"{dates.fmt(change['previous_finish'])} to {dates.fmt(change['new_finish'])}.")
    else:
        lines.append(f"The project finish held at {dates.fmt(change['new_finish'])}; float absorbed the movement.")
    cause = ""
    if trigger:
        p = trigger.get("payload") or {}
        what = (p.get("description") or p.get("note") or "").strip().rstrip("।.")[:160]
        cause = (f" after the approved {trigger['event_type'].replace('_', ' ')} of "
                 f"{dates.fmt(trigger.get('occurred_at'))}" + (f' ("{what}")' if what else ""))
        if p.get("expected_clear"):
            cause += f", expected to clear on {dates.fmt(p['expected_clear'])}"
    lines.append(f"It started at {root['code']} ({root['name']}): its finish moved from "
                 f"{dates.fmt(root['previous_finish'])} to {dates.fmt(root['new_finish'])}{cause}.")
    if len(chain) > 1:
        hops = " -> ".join(f"{s['code']} ({dates.fmt(s['new_finish'])})" for s in chain[1:])
        lines.append(f"Through the {chain[1]['via']['type'] if chain[1].get('via') else 'FS'} dependencies the delay "
                     f"passed along: {hops}.")
    if milestones:
        lines.append("Milestones moved: " + ", ".join(f"{m['code']} {m['delta_days']:+d} d" for m in milestones) + ".")
    if trigger:
        lines.append(f"Evidence: {len(trigger.get('evidence_ids') or [])} item(s) behind the update, captured "
                     f"{trigger.get('captured_at')} on {trigger.get('device_id')} by {trigger.get('actor') or 'unknown'}, "
                     f"approved by {trigger.get('decided_by') or 'unknown'}.")
    return " ".join(lines)


_DATE_TOKEN = re.compile(r"\b\d{1,2} [A-Z][a-z]{2} \d{4}\b|\b\d{4}-\d{2}-\d{2}\b")


def _model_narrative(template: str, facts: List[Dict[str, str]]):
    """Let the reasoning model phrase the answer from the facts - and keep it
    only if every date it names is one of the facts' dates."""
    from jarvis import models
    route = models.route("causal_explanation")
    if not route.get("usable"):
        return "", ""
    from core.ollama_client import ollama_client
    fact_text = "\n".join(f"- [{f['kind']}] {f['text']}" for f in facts)
    # what the phrasing must keep: the old and new finish, and the activity where it started
    required = _DATE_TOKEN.findall(facts[0]["text"]) if facts else []
    codes = re.findall(r"\b[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+\b", fact_text)
    root = next((c for c in codes if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", c)), "")
    reply = ollama_client.chat(route["model"], [
        {"role": "system", "content": "You explain construction schedule changes to a project manager. Use ONLY the "
                                      "facts given. Do not add dates, activities, causes or numbers. Quote dates and "
                                      "activity codes exactly as they are written in the facts (e.g. '27 Sep 2026'). "
                                      "Keep FACT and INFERENCE separate. At most six sentences."},
        {"role": "user", "content": f"Facts:\n{fact_text}\n\nExplain why the finish date moved, naming the previous "
                                    f"and the new finish date and the activity where the delay started."}],
        retries=0, timeout=180, task_type="reasoning", purpose="ZEUS why-did-the-finish-move")
    if not reply or reply.startswith("["):
        return "", ""
    allowed = set(_DATE_TOKEN.findall(template + "\n" + fact_text))
    if any(token not in allowed for token in _DATE_TOKEN.findall(reply)):
        return "", ""                                  # it introduced a date
    if any(token not in reply for token in required) or (root and root not in reply):
        return "", ""                                  # it lost the finish dates or the root activity
    return reply.strip(), route["model"]
