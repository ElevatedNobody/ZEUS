"""
Structured facts from captured site text.

Two passes, recorded separately so every fact keeps its method:

1. RULES (always): sentences are split, and each is read for an activity
   reference ("Pier P17", "P-017", "pile 17", a schedule code, a work type),
   what happened (completed / started / progress / blocker / delay / cleared),
   percent complete, quantity + unit, the date it happened, an expected or
   resumption date, the blocker's category, and the contractor. It reads the
   vernacular module's English reading, so Hindi or Hinglish notes work.

2. MODEL (when the local EXTRACTION model is installed and asked for): the
   same fields as JSON. A model value is accepted only if it can be traced to
   the text - a percentage, quantity or date that does not appear in the note
   is dropped, never stored. The model may add an activity guess, which the
   linking stage still scores like any other reference.

Nothing here writes to the database; capture.py stores the result.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from jarvis import dates

WORK_TYPES = {
    "excavation": ("excavation", "excavate", "excavated", "digging", "khudai"),
    "piling": ("piling", "pile boring", "bored pile", "pile"),
    "pcc": ("pcc", "plain cement concrete", "blinding", "lean concrete"),
    "reinforcement": ("reinforcement", "rebar", "steel fixing", "bar bending", "reinf", "rebar tying"),
    "shuttering": ("shuttering", "formwork", "deshuttering"),
    "concreting": ("concreting", "concrete pour", "pour", "poured", "casting", "concreted", "dhalai"),
    "curing": ("curing", "cured"),
    "girder": ("girder", "launching", "erection", "erected"),
    "deck": ("deck slab", "deck"),
    "pier_cap": ("pier cap", "pier-cap", "cap"),
    "earthwork": ("earthwork", "embankment", "filling", "compaction", "subgrade"),
    "pavement": ("gsb", "wmm", "dbm", "bituminous", "pqc", "dlc", "pavement", "granular sub-base", "wet mix macadam"),
    "drainage": ("drain", "drainage", "culvert"),
    "survey": ("survey", "setting out", "layout"),
}
ELEMENTS = ("pier", "pile", "abutment", "pier cap", "girder", "span", "deck", "culvert", "drain", "wall", "foundation",
            "footing", "chainage", "bridge", "retaining wall", "rob", "vup", "minor bridge", "slab", "column", "beam")

_COMPLETE = re.compile(r"\b(completed?|done|finished|over|achieved|poured completely|casted|cast)\b", re.I)
_STARTED = re.compile(r"\b(started|commenced|begun|began|start(?:s|ed)? today|mobili[sz]ed|in progress|underway|"
                      r"under way|ongoing|wip)\b", re.I)
_NOT_DONE = re.compile(r"\b(not (?:done|completed?|started|received|available|possible|arrived|supplied)|no (?:cement|"
                       r"steel|sand|material|labour|power|access)|could not|couldn't|unable)\b", re.I)
_BLOCKER = re.compile(r"\b(stopped|halted|held up|hold|on hold|blocked|hindrance|shortage|not received|not available|"
                      r"non-availability|breakdown|broke down|strike|rain(?:fall)?|flood|waterlogging|dispute|"
                      r"encroachment|pending approval|awaiting|no access|power cut|is stopped)\b", re.I)
_DELAY = re.compile(r"\b(delay(?:ed|s)?|late|behind schedule|slipp(?:ed|ing)|postponed|deferred|rescheduled)\b", re.I)
_CLEARED = re.compile(r"\b(resumed|restarted|received|arrived|cleared|restored|supplied|delivered|available now)\b", re.I)
_PERCENT = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*(?:%|percent|per cent|pc\b)", re.I)
_QTY = re.compile(r"(\d+(?:\.\d+)?)\s*(m3|m³|cum|cu\.?\s?m|mt|tonnes?|tons?|rmt|rm|nos|sqm|m2|m²|km|m)\b", re.I)
_EXPECT = re.compile(r"\b(expected|expect(?:ed)? to|by|till|until|tak|resume[sd]?|resumption|from|w\.e\.f\.?|"
                     r"likely|revised completion|complete (?:the \w+ )?by|completion by)\b", re.I)
_REMAINING = re.compile(r"(\d+)\s*(?:more\s+)?(?:working\s+)?days?\s*(?:remaining|more|balance|to complete|required)", re.I)
_CODE = re.compile(r"\b([A-Z]{1,5}(?:-[A-Z0-9]{1,6}){1,4})\b")
# "Pier 17", "pile no. 17", "P17", "P-017", "A1", "P16-P17". A single letter must be
# attached to its number ("a 12" is English, not abutment 12).
_ELEMENT_REF = re.compile(r"\b(?:(pier|pile|abutment|span|girder|culvert|column|footing|well)\s*(?:no\.?\s*|#\s*|-\s*)?"
                          r"|(p|a|s)-?)(\d{1,4})(?!\d)(?:\s*[-/&]\s*(?:p|pier)?\s*(\d{1,4}))?\b", re.I)
_CHAINAGE = re.compile(r"\b(?:ch(?:ainage)?\.?|km)\s*(\d{1,3}(?:[+.]\d{1,3})?)", re.I)

CATEGORIES = {
    "material": ("cement", "steel", "sand", "aggregate", "material", "supply", "shortage", "rebar", "bitumen",
                 "ready mix", "rmc", "not received", "non-availability"),
    "weather": ("rain", "flood", "storm", "waterlogging", "heat", "fog", "monsoon"),
    "labour": ("labour", "labor", "manpower", "workers", "strike", "mazdoor"),
    "equipment": ("breakdown", "crane", "pump", "batching plant", "transit mixer", "jcb", "excavator", "machine",
                  "equipment"),
    "design": ("drawing", "design", "rfi", "revision", "gfc"),
    "access": ("access", "land", "row", "encroachment", "right of way", "villagers", "obstruction"),
    "permit": ("permission", "permit", "approval", "noc", "clearance", "awaiting approval"),
    "power": ("power cut", "electricity", "power"),
    "utility": ("utility", "shifting", "pipeline", "cable"),
}


@dataclass
class Observation:
    text: str                                   # the sentence as captured (original script)
    reading: str                                # its normalised reading
    kind: str = "note"                          # completed | started | progress | blocker | blocker_cleared | delay | inspection | note
    refs: List[str] = field(default_factory=list)
    element_refs: List[Dict[str, Any]] = field(default_factory=list)
    work_types: List[str] = field(default_factory=list)
    percent_complete: Optional[float] = None
    quantity: Optional[float] = None
    unit: Optional[str] = None
    occurred_on: Optional[str] = None
    expected_date: Optional[str] = None
    remaining_days: Optional[int] = None
    blocker_category: Optional[str] = None
    contractor: Optional[str] = None
    location: Optional[str] = None
    delay: bool = False
    confidence: float = 0.5
    method: str = "rules"
    model_guess: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _sentences(original: str, reading: str) -> List[tuple]:
    """Pair original and reading sentence by sentence when they split alike;
    otherwise keep the whole note as one unit."""
    splitter = re.compile(r"(?<=[.;!?।|\n])\s+|\n+")
    a = [s.strip() for s in splitter.split(original or "") if s and s.strip()]
    b = [s.strip() for s in splitter.split(reading or "") if s and s.strip()]
    if len(a) == len(b) and a:
        return list(zip(a, b))
    return [(original.strip(), (reading or original).strip())]


def _work_types(text: str) -> List[str]:
    low = text.lower()
    found = []
    for work, words in WORK_TYPES.items():
        if any(re.search(rf"(?<![a-z]){re.escape(w)}(?:s|es)?(?![a-z])", low) for w in words):
            found.append(work)
    if "pier_cap" in found and "cap" not in low.replace("pier cap", "").replace("pier-cap", "") and "pier cap" not in low:
        found.remove("pier_cap")
    return found


def _category(text: str) -> Optional[str]:
    low = text.lower()
    best, hits = None, 0
    for category, words in CATEGORIES.items():
        n = sum(1 for w in words if w in low)
        if n > hits:
            best, hits = category, n
    return best


def element_refs(text: str) -> List[Dict[str, Any]]:
    refs = []
    for m in _ELEMENT_REF.finditer(text or ""):
        word = (m.group(1) or m.group(2)).lower()
        element = {"p": "pier", "a": "abutment", "s": "span"}.get(word, word)
        refs.append({"element": element, "number": int(m.group(3)),
                     "number_to": int(m.group(4)) if m.group(4) else None, "surface": m.group(0).strip(),
                     "explicit_word": bool(m.group(1))})
    return refs


def _clauses(text: str, sentence: str) -> List[tuple]:
    """Split a sentence at commas / semicolons / dashes between clauses."""
    splitter = re.compile(r"\s*[,;]\s+|\s+[-–—]\s+")
    a = [c for c in splitter.split(text) if c.strip()]
    b = [c for c in splitter.split(sentence) if c.strip()]
    if len(a) == len(b) and b:
        return list(zip(a, b))
    return [(text, c) for c in b] or [(text, sentence)]


def extract_rules(original: str, reading: str, reference: _dt.date, known_codes: Optional[List[str]] = None,
                  contractors: Optional[List[str]] = None) -> List[Observation]:
    """Clause by clause, with the context a site note relies on carried over:
    a clause with no reference of its own belongs to the element named in its
    sentence (or the previous clause); a second clause of the same kind
    ("cement not received, concreting stopped") joins the first; a clause
    that only gives a date ("supply expected by 11 September") is the
    expected clearance of the blocker before it."""
    merged: List[Observation] = []
    for text, sentence in _sentences(original, reading):
        sentence_refs = element_refs(sentence)
        numbers = {r["number"] for r in sentence_refs}
        for clause_text, clause in _clauses(text, sentence):
            obs = _analyse(clause_text, clause, reference, known_codes, contractors)
            previous = merged[-1] if merged else None
            own_refs = bool(obs.refs)
            # A clause with its own subject ("Side drains ...") is not about the element
            # named in an earlier sentence; a bare status or cause clause is.
            has_subject = bool(obs.work_types) or bool(re.search(
                r"\b(" + "|".join(ELEMENTS) + r")s?\b", clause.lower()))
            in_sentence = len(numbers) == 1 and bool(sentence_refs)
            if not own_refs:
                if in_sentence:
                    obs.refs = [sentence_refs[0]["surface"]]
                    obs.element_refs = [sentence_refs[0]]
                elif previous is not None and not has_subject:
                    obs.refs, obs.element_refs = list(previous.refs), list(previous.element_refs)
            if previous is not None and not own_refs and (in_sentence or not has_subject):
                if obs.kind == "note":
                    if previous.kind in ("blocker", "delay"):
                        previous.expected_date = previous.expected_date or obs.expected_date
                    previous.remaining_days = previous.remaining_days if previous.remaining_days is not None \
                        else obs.remaining_days
                    continue
                if {obs.kind, previous.kind} <= {"started", "progress"}:
                    # "concreting started today, 40% done": one progress report
                    previous.kind = "progress" if "progress" in (obs.kind, previous.kind) else previous.kind
                    obs.kind = previous.kind
                if {obs.kind, previous.kind} <= {"blocker", "delay"}:
                    # "concreting delayed - cement not received": one blocker, with its cause
                    if obs.kind == "blocker" and previous.kind == "delay":
                        previous.kind = "blocker"
                        previous.delay = True
                        previous.blocker_category = obs.blocker_category or previous.blocker_category
                    obs.kind = previous.kind
                if obs.kind == previous.kind:
                    previous.text = f"{previous.text}, {obs.text}"
                    previous.reading = f"{previous.reading}, {obs.reading}"
                    previous.work_types = list(dict.fromkeys(previous.work_types + obs.work_types))
                    for key in ("percent_complete", "quantity", "unit", "expected_date", "occurred_on",
                                "remaining_days", "blocker_category"):
                        if getattr(previous, key) is None and getattr(obs, key) is not None:
                            setattr(previous, key, getattr(obs, key))
                    previous.delay = previous.delay or obs.delay
                    continue
            if obs.kind != "note" or obs.refs:
                merged.append(obs)
    return merged


def _analyse(text: str, sentence: str, reference: _dt.date, known_codes: Optional[List[str]],
             contractors: Optional[List[str]]) -> Observation:
    """One clause: its references, work, numbers, dates and what happened."""
    codes = {c.upper(): c for c in (known_codes or [])}
    low = sentence.lower()
    obs = Observation(text=text, reading=sentence)
    obs.work_types = _work_types(sentence)
    obs.element_refs = element_refs(sentence)
    refs = [r["surface"] for r in obs.element_refs]
    for m in _CODE.finditer(sentence):
        if m.group(1).upper() in codes or re.search(r"\d", m.group(1)):
            refs.append(m.group(1))
    for code_upper, code in codes.items():
        if code_upper in sentence.upper() and code not in refs:
            refs.append(code)
    obs.refs = list(dict.fromkeys(refs))
    chain = _CHAINAGE.search(sentence)
    obs.location = chain.group(0) if chain else None

    # dates: the first date not introduced as an expectation is when it happened
    for day, span, _surface in dates.find_dates(sentence, reference):
        before = sentence[max(0, span[0] - 28):span[0]]
        after = sentence[span[1]:span[1] + 24]
        expecting = _EXPECT.search(before) or re.search(r"^\s*(?:by|tak|expected)\b", after, re.I)
        if expecting and day >= reference - _dt.timedelta(days=1):
            obs.expected_date = obs.expected_date or day.isoformat()
        elif obs.occurred_on is None:
            obs.occurred_on = day.isoformat()
    percent = _PERCENT.search(sentence)
    if percent and 0 <= float(percent.group(1)) <= 100:
        obs.percent_complete = float(percent.group(1))
    qty = _QTY.search(sentence)
    if qty:
        obs.quantity, obs.unit = float(qty.group(1)), _unit(qty.group(2))
    remaining = _REMAINING.search(sentence)
    if remaining:
        obs.remaining_days = int(remaining.group(1))
    for name in contractors or []:
        if name and name.lower() in low:
            obs.contractor = name

    # what happened
    blocker = bool(_BLOCKER.search(sentence) or _NOT_DONE.search(sentence))
    obs.delay = bool(_DELAY.search(sentence))
    intention = re.search(r"\b(expect(?:s|ed)? to|will|shall|to be|plan(?:s|ned)? to|intend|likely to|from tomorrow)\b",
                          low)
    cleared = bool(_CLEARED.search(sentence)) and not _NOT_DONE.search(sentence) and "not received" not in low
    partial = obs.percent_complete is not None and obs.percent_complete < 100
    inspection = re.search(r"\b(inspect(?:ed|ion)|pour card|checklist|rfi)\b", low) and re.search(
        r"\b(done|completed?|passed|approved|cleared|ok|accepted|signed)\b", low)
    if inspection and not blocker:
        # an inspection passed is a recorded fact - it does not complete the activity
        obs.kind = "inspection"
    elif blocker:
        obs.kind = "blocker"
        obs.blocker_category = _category(sentence) or "other"
    elif cleared and re.search(r"\b(resumed|restarted|cleared|restored)\b", low) and not intention:
        obs.kind = "blocker_cleared"
    elif _COMPLETE.search(sentence) and not partial and not intention and not re.search(
            r"\b(to be completed|will be completed|complete by)\b", low):
        obs.kind = "completed"
    elif obs.percent_complete is not None or (obs.quantity is not None and
                                              re.search(r"\b(poured|done|laid|executed|placed)\b", low)):
        obs.kind = "progress"
    elif obs.delay:
        obs.kind = "delay"
        obs.blocker_category = _category(sentence)
    elif _STARTED.search(sentence) and not intention and not re.search(r"\bwill start\b|\bto start\b", low):
        obs.kind = "started"
    if obs.kind == "completed" and obs.percent_complete is None:
        obs.percent_complete = 100.0
    score = 0.35 + (0.25 if obs.refs else 0) + (0.15 if obs.work_types else 0) + (0.15 if obs.kind != "note" else 0)
    score += 0.1 if (obs.occurred_on or obs.percent_complete is not None or obs.expected_date) else 0
    obs.confidence = round(min(0.95, score), 2)
    return obs

def _unit(raw: str) -> str:
    raw = raw.lower().replace(" ", "")
    return {"m³": "m3", "cum": "m3", "cu.m": "m3", "cum.": "m3", "cum3": "m3", "m²": "m2", "tonne": "t",
            "tonnes": "t", "ton": "t", "tons": "t", "mt": "t", "rm": "rmt"}.get(raw, raw)


# ------------------------------------------------------------------- model --

def extract_model(original: str, reading: str, reference: _dt.date, candidates: List[Dict[str, str]],
                  timeout: float = 240.0) -> Dict[str, Any]:
    """The EXTRACTION model's reading as JSON - validated against the text."""
    from jarvis import models
    route = models.route("extraction")
    if not route.get("usable"):
        return {"status": "unavailable", "reason": route.get("reason"), "observations": []}
    from core.ollama_client import ollama_client
    from security.injection import SYSTEM_RULE, neutralise
    catalogue = "\n".join(f"- {c['code']}: {c['name']}" for c in candidates[:60])
    prompt = (
        f"Today is {reference.isoformat()}. Read the construction site note and return JSON only:\n"
        '{"observations": [{"activity_code": "one code from the list or null", "reference": "words in the note naming '
        'the work/location", "kind": "completed|started|progress|blocker|blocker_cleared|delay|note", '
        '"percent_complete": number or null, "quantity": number or null, "unit": string or null, '
        '"occurred_on": "YYYY-MM-DD or null", "expected_date": "YYYY-MM-DD or null", "blocker": "short text or null", '
        '"blocker_category": "material|weather|labour|equipment|design|access|permit|power|utility|other|null"}],'
        ' "english": "the note in plain English"}\n'
        "Use only what the note says. Do not invent numbers or dates.\n\nSchedule activities:\n"
        f"{catalogue}\n\n<untrusted_document>\n{neutralise(original)}\n</untrusted_document>\n"
        + (f"Normalised reading: {neutralise(reading)}" if reading and reading != original else ""))
    started = time.time()
    reply = ollama_client.chat(route["model"], [{"role": "system", "content": SYSTEM_RULE},
                                                {"role": "user", "content": prompt}],
                               retries=0, timeout=timeout, task_type="json_structured",
                               purpose="ZEUS field extraction", json_format=True)
    elapsed = int((time.time() - started) * 1000)
    if not reply or reply.startswith("["):
        return {"status": "failed", "reason": (reply or "no reply")[:200], "model": route["model"],
                "duration_ms": elapsed, "observations": []}
    try:
        data = json.loads(reply)
    except ValueError:
        match = re.search(r"\{.*\}", reply, re.S)
        try:
            data = json.loads(match.group(0)) if match else {}
        except ValueError:
            data = {}
    kept, dropped = [], []
    haystack = f"{original}\n{reading}"
    codes = {c["code"] for c in candidates}
    for item in (data.get("observations") or [])[:12]:
        if not isinstance(item, dict):
            continue
        clean, reasons = _verify_model_item(item, haystack, reference, codes)
        (kept if clean else dropped).append(clean or {"item": item, "reasons": reasons})
        if clean and reasons:
            clean["dropped_fields"] = reasons
    return {"status": "ok", "model": route["model"], "duration_ms": elapsed, "observations": kept,
            "rejected": dropped, "english": str(data.get("english") or "")[:2000], "raw": reply[:4000]}


def _in_text(number, text: str) -> bool:
    if number is None:
        return True
    value = float(number)
    for token in re.findall(r"\d+(?:\.\d+)?", text):
        if abs(float(token) - value) < 1e-6:
            return True
    return False


def _verify_model_item(item: Dict[str, Any], text: str, reference: _dt.date, codes: set):
    reasons = []
    clean: Dict[str, Any] = {"kind": item.get("kind") if item.get("kind") in (
        "completed", "started", "progress", "blocker", "blocker_cleared", "delay", "inspection", "note") else "note",
                             "reference": str(item.get("reference") or "")[:120],
                             "blocker": str(item.get("blocker") or "")[:300] or None,
                             "blocker_category": item.get("blocker_category") if item.get("blocker_category") in
                             tuple(CATEGORIES) + ("other",) else None}
    code = item.get("activity_code")
    clean["activity_code"] = code if code in codes else None
    if code and code not in codes:
        reasons.append(f"activity code '{code}' is not in the schedule")
    for key in ("percent_complete", "quantity"):
        value = item.get(key)
        try:
            value = float(value) if value is not None else None
        except (TypeError, ValueError):
            value = None
        if value is not None and not _in_text(value, text):
            reasons.append(f"{key} {value:g} does not appear in the note")
            value = None
        clean[key] = value
    clean["unit"] = str(item.get("unit") or "")[:12] or None
    text_dates = {d.isoformat() for d, _s, _t in dates.find_dates(text, reference)}
    for key in ("occurred_on", "expected_date"):
        value = dates.iso(item.get(key)) if item.get(key) else None
        if value and value not in text_dates:
            reasons.append(f"{key} {value} is not a date written in the note")
            value = None
        clean[key] = value
    return clean, reasons


def merge(rules: List[Observation], model: Dict[str, Any]) -> List[Observation]:
    """Rules first; the model fills gaps and adds an activity guess. A model
    observation with no rule counterpart is added only when it names an
    activity code from the schedule and a kind."""
    items = model.get("observations") or []
    used = set()
    for obs in rules:
        for index, item in enumerate(items):
            if index in used:
                continue
            same_kind = item["kind"] == obs.kind or obs.kind == "note"
            overlap = bool(item.get("reference")) and any(r.lower() in item["reference"].lower() or
                                                          item["reference"].lower() in r.lower() for r in obs.refs)
            if same_kind or overlap:
                used.add(index)
                if obs.kind == "note" and item["kind"] != "note":
                    obs.kind = item["kind"]
                for key in ("percent_complete", "quantity", "unit", "expected_date", "blocker_category"):
                    if getattr(obs, key) is None and item.get(key) is not None:
                        setattr(obs, key, item[key])
                if item.get("activity_code"):
                    obs.model_guess = item["activity_code"]
                    if item["activity_code"] not in obs.refs:
                        obs.refs.append(item["activity_code"])
                obs.method = "rules+model"
                obs.confidence = round(min(0.97, obs.confidence + 0.05), 2)
                break
    for index, item in enumerate(items):
        if index in used or not item.get("activity_code") or item["kind"] == "note":
            continue
        obs = Observation(text=item.get("reference") or "", reading=item.get("reference") or "", kind=item["kind"],
                          refs=[item["activity_code"]], percent_complete=item.get("percent_complete"),
                          quantity=item.get("quantity"), unit=item.get("unit"), occurred_on=item.get("occurred_on"),
                          expected_date=item.get("expected_date"), blocker_category=item.get("blocker_category"),
                          method="model", model_guess=item["activity_code"], confidence=0.55)
        rules.append(obs)
    return rules
