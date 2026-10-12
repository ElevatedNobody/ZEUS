"""
Project questions in plain words, answered from the record.

    "Why did the finish date move?"                              -> explain.why_moved
    "What changed between the 1 Sept and 10 Sept schedule updates?" -> version comparison
    "What did we know about P17 concreting on 8 September?"      -> bitemporal state
    "When did we first know that P17 concreting was slipping?"   -> first slip / first warning
    "What evidence existed before the delay was reported?"       -> evidence captured before it
    "Delay chronology for P17" / "notice deadlines"              -> chronology
    "How long did comparable pier concreting take?"              -> learning cards
    anything else                                                -> hybrid project search

Intent is recognised deterministically (no model in the loop), and every
answer separates FACT, INFERENCE, PREDICTION and RECOMMENDATION.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from jarvis import chronology, dates, explain, extraction, linking, search, service, store

_INTENTS = [
    ("why_moved", re.compile(r"\bwhy\b.*\b(finish|completion|end date|date|forecast|milestone|handover)\b.*"
                             r"\b(move|moved|slip|slipped|change|changed|shift|shifted|delay|delayed|extend)", re.I)),
    ("compare_versions", re.compile(r"\bwhat (?:has )?changed\b.*\b(schedule|update|version|programme|program)s?\b", re.I)),
    ("state_as_of", re.compile(r"\bwhat did we know\b|\b(?:status|state|progress) of .+ (?:as of|as on|on)\b", re.I)),
    ("first_slip", re.compile(r"\bwhen did we first (?:know|learn|see|hear)\b", re.I)),
    ("evidence_before", re.compile(r"\b(?:what|which) evidence\b.*\bbefore\b", re.I)),
    ("deadlines", re.compile(r"\bnotice (?:deadline|period)s?\b|\bdeadlines?\b", re.I)),
    ("chronology", re.compile(r"\b(chronology|timeline|sequence of events)\b", re.I)),
    ("learning", re.compile(r"\b(comparable|similar|past|historical|previous projects?|learning|how long did)\b", re.I)),
    ("progress", re.compile(r"\b(progress|status|how far|percent complete)\b", re.I)),
]


def intent_of(question: str, use_model: bool = False) -> str:
    for name, pattern in _INTENTS:
        if pattern.search(question or ""):
            return name
    if use_model:
        label = _model_intent(question)
        if label:
            return label
    return "search"


_LABELS = [name for name, _p in _INTENTS] + ["search"]


def _model_intent(question: str) -> str:
    """Simple classification goes to the small local model - and its answer is
    used only if it is exactly one of the known labels."""
    from jarvis import models
    route = models.route("classification")
    if not route.get("usable"):
        return ""
    from core.ollama_client import ollama_client
    reply = ollama_client.chat(route["model"], [
        {"role": "system", "content": "Classify a construction project question. Reply with ONE label only."},
        {"role": "user", "content": f"Labels: {', '.join(_LABELS)}\nQuestion: {question[:400]}\nLabel:"}],
        retries=0, timeout=60, task_type="classify", purpose="ZEUS question intent")
    label = (reply or "").strip().strip(".").lower().split()[0] if reply and not reply.startswith("[") else ""
    return label if label in _LABELS else ""


def activity_in(question: str, project_id: str) -> Optional[Dict[str, Any]]:
    """The activity a question names, if it names one clearly enough."""
    obs = extraction.Observation(text=question, reading=question)
    obs.element_refs = extraction.element_refs(question)
    obs.work_types = extraction._work_types(question)
    obs.refs = [r["surface"] for r in obs.element_refs] + re.findall(r"\b[A-Z]{1,5}(?:-[A-Z0-9]{1,6}){1,4}\b", question)
    if not obs.refs and not obs.work_types:
        return None
    with store.connection() as conn:
        result = linking.resolve(conn, project_id, obs, dates.today())
    top = (result.get("candidates") or [None])[0]
    if top and top["score"] >= linking.SUGGEST:
        return top
    return None


def ask(question: str, project: Optional[str] = None, use_model: bool = False) -> Dict[str, Any]:
    project_id = service.resolve_project(project)
    intent = intent_of(question, use_model=use_model)
    activity = activity_in(question, project_id)
    out: Dict[str, Any] = {"ok": True, "question": question, "intent": intent, "project_id": project_id,
                           "activity": activity, "FACT": [], "INFERENCE": [], "PREDICTION": [], "RECOMMENDATION": []}
    if intent == "why_moved":
        with store.connection() as conn:
            data = explain.why_moved(conn, project_id, use_model=use_model)
        out.update(answer=data["answer"], data=data, FACT=data.get("classification", {}).get("FACT", []),
                   INFERENCE=data.get("classification", {}).get("INFERENCE", []))
    elif intent == "compare_versions":
        out.update(_compare(question, project_id))
    elif intent == "state_as_of":
        day = dates.parse_date(question)
        if not activity or not day:
            out.update(answer="Name the activity and the date, e.g. 'What did we know about P17 concreting on 8 "
                              "September?'", data=None)
        else:
            data = service.state_as_of(activity["activity_id"], day.isoformat())
            f = data.get("forecast_then") or {}
            st = data.get("state") or {}
            blockers = [b for b in (st.get("blockers") or {}).values() if b["status"] == "open"]
            out["FACT"] = [f"By the end of {dates.fmt(day)}, {len(data['known_events'])} record(s) about "
                           f"{activity['code']} had been captured."]
            for e in [x for x in data["known_events"] if x["event_type"] not in ("approval", "link_confirmed")]:
                out["FACT"].append(f"{dates.fmt(e['occurred_at'])}: {e['event_type'].replace('_', ' ')} "
                                   f"({e['status_then']}) - {(e.get('payload') or {}).get('note', '')[:120]}")
            if f:
                out["FACT"].append(f"Forecast then: {f['status'].replace('_', ' ')}, finish {dates.fmt(f['ef'])}"
                                   + (f", {f['total_float']} d float" if f.get("total_float") is not None else ""))
            if blockers:
                out["FACT"].append(f"Open blockers then: " + "; ".join(b["description"][:80] for b in blockers))
            out.update(answer=" ".join(out["FACT"][:3]), data=data)
    elif intent == "first_slip":
        if not activity:
            out.update(answer="Name the activity (for example 'P17 concreting').", data=None)
        else:
            data = service.first_known_slip(activity["activity_id"])
            slip, warn = data.get("first_forecast_slip"), data.get("first_field_warning")
            if warn:
                out["FACT"].append(f"First field warning: {warn['event_type'].replace('_', ' ')} captured "
                                   f"{warn['captured_at']} by {warn.get('actor') or 'unknown'} on {warn['device_id']}.")
            if slip:
                out["FACT"].append(f"First forecast past the baseline: captured {slip['captured_at']} - finish "
                                   f"{dates.fmt(slip['forecast_finish'])} vs baseline {dates.fmt(slip['baseline_finish'])} "
                                   f"({slip['slip_days']:+d} d).")
            if not slip and not warn:
                out["FACT"].append(f"The record has never shown {activity['code']} finishing after its baseline.")
            out.update(answer=" ".join(out["FACT"]), data=data)
    elif intent == "evidence_before":
        data = service.evidence_before(project_id, activity["activity_id"] if activity else "")
        if data.get("found"):
            first = data["first_report"]
            out["FACT"] = [f"The first delay/blocker report was captured {first['captured_at']} "
                           f"({first['event_type'].replace('_', ' ')} on {first.get('activity_code') or 'the project'})."] + [
                f"Before it: {e['kind']} '{(e['title'] or '')[:60]}' captured {e['captured_at']} by "
                f"{e['captured_by'] or 'unknown'}" for e in data["evidence_before"]]
        out.update(answer=" ".join(out["FACT"][:4]) if out["FACT"] else data.get("answer", ""), data=data)
    elif intent == "deadlines":
        with store.connection() as conn:
            rows = chronology.deadlines(conn, project_id)
        out["FACT"] = [f"{r['clause']} {r['notice_type']} notice for {r['activity_code'] or 'project'}: due "
                       f"{dates.fmt(r['due_date'])} - {r['status'].upper()}"
                       + (f" ({r['days_left']} d left)" if r["status"] == "open" else "") for r in rows]
        out.update(answer="; ".join(out["FACT"]) or "No contract notice deadline has been opened.", data=rows)
    elif intent == "chronology":
        with store.connection() as conn:
            rows = chronology.timeline(conn, project_id, activity_id=activity["activity_id"] if activity else None)
        out["FACT"] = [f"{dates.fmt(r['occurred_at'])}: {r['title']} - {r['detail'] or ''}"[:200] for r in rows]
        out.update(answer=f"{len(rows)} dated entries" + (f" for {activity['code']}" if activity else "") + ".",
                   data=rows)
    elif intent == "learning" and activity:
        from jarvis import learning
        data = learning.cards(activity["activity_id"])
        out.update(answer=data.get("message") or " ".join(data["INFERENCE"] + data["PREDICTION"]) or
                   f"{len(data['comparables'])} comparable activities.", data=data,
                   FACT=data.get("FACT", []), INFERENCE=data.get("INFERENCE", []),
                   PREDICTION=data.get("PREDICTION", []), RECOMMENDATION=data.get("RECOMMENDATION", []))
    elif intent == "progress" and activity:
        data = service.activity_detail(activity["activity_id"])
        f = data.get("forecast") or {}
        out["FACT"] = [f"{activity['code']} {activity['name']}: {str(f.get('status', '—')).replace('_', ' ')}, "
                       f"{f.get('percent_complete') if f.get('percent_complete') is not None else '—'}% complete, "
                       f"forecast {dates.fmt(f.get('es'))} -> {dates.fmt(f.get('ef'))}, baseline finish "
                       f"{dates.fmt(f.get('baseline_finish'))}."]
        out.update(answer=out["FACT"][0], data={"forecast": f, "events": len(data["events"]),
                                                "evidence": len(data["evidence"])})
    else:
        out["intent"] = "search"
        data = search.search(question, project_id=project_id)
        out["FACT"] = [f"[{r['kind']}] {r['title']}: {r['excerpt'][:160]}" for r in data["results"][:6]]
        out.update(answer=f"{data['count']} matching record(s) ({data['backend']}).", data=data)
    return out


def _compare(question: str, project_id: str) -> Dict[str, Any]:
    found = dates.find_dates(question, relative=False)
    versions = service.versions(project_id)
    approved = [v for v in versions if v["status"] == "approved"]
    if len(approved) < 2:
        return {"answer": "Fewer than two approved schedule versions exist - there is nothing to compare.", "data": None}

    def nearest(day):
        return min(approved, key=lambda v: abs((dates.from_iso(v["data_date"] or v["captured_at"]) - day).days))

    if len(found) >= 2:
        a, b = nearest(found[0][0]), nearest(found[1][0])
    else:
        a, b = approved[-2], approved[-1]
    if a["version_id"] == b["version_id"]:
        return {"answer": "Both dates point to the same schedule version.", "data": None}
    data = service.compare_versions(a["version_id"], b["version_id"])
    facts = [f"Comparing '{data['from']['label']}' (data date {dates.fmt(data['from']['data_date'])}) with "
             f"'{data['to']['label']}' (data date {dates.fmt(data['to']['data_date'])})."]
    if data.get("finish_shift_days") is not None:
        facts.append(f"Forecast finish {dates.fmt(data['from']['forecast_finish'])} -> "
                     f"{dates.fmt(data['to']['forecast_finish'])} ({data['finish_shift_days']:+d} d).")
    for c in data["activity_changes"][:12]:
        if c["change"] != "changed":
            facts.append(f"{c['code']} {c['change']}.")
            continue
        fields = ", ".join(f"{k.replace('_', ' ')} {v['from'] or '—'} -> {v['to'] or '—'}" for k, v in c["fields"].items())
        facts.append(f"{c['code']}: {fields}.")
    for l in data["logic_changes"][:8]:
        facts.append(f"Logic {l['change']}: {l['pred']} -> {l['succ']} ({l['from']} -> {l['to']}).")
    return {"answer": " ".join(facts[:4]), "data": data, "FACT": facts}
