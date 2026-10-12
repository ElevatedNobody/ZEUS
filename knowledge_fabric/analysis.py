"""
ONE verified analysis -> many output formats.

An `AnalysisObject` is everything a question produced: the plan, the evidence,
the facts, the calculations, the contradictions, the coverage gaps, the drivers,
the recommendations, the implementation plan, the KPIs, the risks, the answer -
and the validation of that answer. It is persisted (kf_analyses) with an id, so
"now create a management presentation" and "create a detailed Word report" render
THE SAME analysis instead of asking the question again and getting a second answer.

`validate_answer()` holds a written answer to the evidence:
  * every cited id ([E3], [F2], [C1], [G4], [M1]) must exist;
  * every figure in the text must come from a calculation, a fact or the evidence
    text itself - a number the model computed or invented fails validation.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from knowledge_fabric import store

FACT_IDS = 60           # [F1..F60] - the facts shown to the model (context.build)
GRAPH_IDS = 40          # [G1..G40]


@dataclass
class AnalysisObject:
    analysis_id: str
    question: str
    username: str
    created_at: float
    plan: Dict[str, Any]
    evidence: List[Dict[str, Any]] = field(default_factory=list)
    facts: List[Dict[str, Any]] = field(default_factory=list)
    series: List[Dict[str, Any]] = field(default_factory=list)
    calculations: List[Dict[str, Any]] = field(default_factory=list)
    graph_edges: List[Dict[str, Any]] = field(default_factory=list)
    contradictions: List[Dict[str, Any]] = field(default_factory=list)
    gaps: Dict[str, Any] = field(default_factory=dict)
    patterns: List[Dict[str, Any]] = field(default_factory=list)
    drivers: List[Dict[str, Any]] = field(default_factory=list)
    options: List[Dict[str, Any]] = field(default_factory=list)
    recommendations: List[Dict[str, Any]] = field(default_factory=list)
    implementation_plan: List[Dict[str, Any]] = field(default_factory=list)
    kpis: List[Dict[str, Any]] = field(default_factory=list)
    risks: List[Dict[str, Any]] = field(default_factory=list)
    assumptions: List[str] = field(default_factory=list)
    memory_used: List[Dict[str, Any]] = field(default_factory=list)
    answer: str = ""
    summary: str = ""
    narrative_source: str = "deterministic"
    validation: Dict[str, Any] = field(default_factory=dict)
    retrieval: Dict[str, Any] = field(default_factory=dict)
    stages: List[Dict[str, Any]] = field(default_factory=list)
    artifacts: List[Dict[str, Any]] = field(default_factory=list)
    records: Dict[str, Any] = field(default_factory=dict)      # a register answer (knowledge_fabric/registers.py)
    reasoning: Dict[str, Any] = field(default_factory=dict)    # the model's facts / inferences / assumptions, checked

    @property
    def verified(self) -> bool:
        """The answer passed validation: every cited id exists and every number is
        traceable. Internal - what a person is shown is `verification_state`."""
        return bool(self.validation.get("ok"))

    def cited_origins(self) -> List[str]:
        """Where the cited evidence comes from - passages [E#], table facts [F#] and
        the facts behind calculations [C#]."""
        cited = set(self.validation.get("cited", []))
        evidence = {e.get("evidence_id"): e for e in self.evidence}
        facts = {f.get("id"): f for f in self.facts if f.get("id")}
        by_fact_id = {f.get("fact_id"): f for f in self.facts if f.get("fact_id") is not None}
        calcs = {c.get("id"): c for c in self.calculations}
        origins = set()
        for ident in cited:
            if ident in evidence:
                origins.add(evidence[ident].get("origin"))
            elif ident in facts:
                origins.add(facts[ident].get("origin"))
            elif ident in calcs:
                for item in calcs[ident].get("inputs", []):
                    fact = by_fact_id.get(item.get("fact_id"))
                    if fact is not None:
                        origins.add(fact.get("origin"))
        if self.records.get("records") and any(i.startswith("E") for i in cited):
            origins |= {e.get("origin") for e in self.evidence if e.get("kind") == "record"}
        return sorted(o for o in origins if o)

    @property
    def verification_state(self) -> str:
        """DRAFT -> RUNNING -> EVIDENCE_FOUND -> VALIDATED | VERIFIED | INSUFFICIENT_DATA | FAILED.

        VERIFIED needs BOTH a passing validation AND cited company records; the same
        checks over sample, demo, general-knowledge or test material give VALIDATED.
        No evidence at all is INSUFFICIENT_DATA, never a verified answer."""
        if not self.validation:
            if self.evidence or self.facts:
                return "EVIDENCE_FOUND"
            return "RUNNING" if self.stages else "DRAFT"
        if not self.evidence and not self.facts:
            return "INSUFFICIENT_DATA"
        if not self.validation.get("ok"):
            return "FAILED"
        return "VERIFIED" if "company" in self.cited_origins() else "VALIDATED"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AnalysisObject":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def evidence_by_id(self) -> Dict[str, Dict[str, Any]]:
        return {e["evidence_id"]: e for e in self.evidence}


def new(question: str, username: str, plan: Dict[str, Any]) -> AnalysisObject:
    return AnalysisObject(analysis_id=time.strftime("A%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6],
                          question=question, username=username, created_at=time.time(), plan=plan)


def save(obj: AnalysisObject) -> None:
    with store.connection() as conn:
        conn.execute("INSERT OR REPLACE INTO kf_analyses(analysis_id, username, question, created_at, verified, payload) "
                     "VALUES (?, ?, ?, ?, ?, ?)",
                     (obj.analysis_id, obj.username, obj.question, obj.created_at, 1 if obj.verified else 0,
                      json.dumps(obj.to_dict(), default=str)))


def load(analysis_id: str, username: Optional[str] = None) -> Optional[AnalysisObject]:
    """An analysis belongs to the user who asked it (and administrators)."""
    with store.connection() as conn:
        row = conn.execute("SELECT username, payload FROM kf_analyses WHERE analysis_id = ?", (analysis_id,)).fetchone()
    if not row:
        return None
    if username is not None and row["username"] and row["username"] != username:
        return None
    return AnalysisObject.from_dict(json.loads(row["payload"]))


def latest(username: str) -> Optional[AnalysisObject]:
    with store.connection() as conn:
        row = conn.execute("SELECT payload FROM kf_analyses WHERE username = ? ORDER BY created_at DESC LIMIT 1",
                           (username,)).fetchone()
    return AnalysisObject.from_dict(json.loads(row["payload"])) if row else None


def history(username: str, limit: int = 30) -> List[Dict[str, Any]]:
    with store.connection() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT analysis_id, question, created_at, verified FROM kf_analyses WHERE username = ? "
            "ORDER BY created_at DESC LIMIT ?", (username, limit))]


# ------------------------------------------------------------ validation ----

_CITATION = re.compile(r"\[(E|F|C|G|M)(\d+)\]")
_NUMBER = re.compile(r"(?<![A-Za-z\[\]#\-\d])[-+]?\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|(?<![A-Za-z\[\]#\-\d])[-+]?\d+(?:\.\d+)?")


def _numbers_in(value: Any, out: set) -> None:
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, (int, float)):
        for v in (value, round(value, 1), round(value, 2), round(value, 0)):
            out.add(_canon(v))
            out.add(_canon(abs(v)))
        return
    if isinstance(value, dict):
        for v in value.values():
            _numbers_in(v, out)
        return
    if isinstance(value, (list, tuple)):
        for v in value:
            _numbers_in(v, out)
        return
    if isinstance(value, str):
        for m in _NUMBER.finditer(value):
            try:
                out.add(_canon(float(m.group(0).replace(",", ""))))
            except ValueError:
                pass


def _canon(value: float) -> str:
    value = float(value)
    if value == int(value):
        return str(int(value))
    return f"{value:.4f}".rstrip("0").rstrip(".")


def allowed_numbers(obj: AnalysisObject) -> set:
    """Every number the answer may state: calculated values, fact values and
    years, the evidence text, the question, the coverage report and memory."""
    allowed: set = set()
    for c in obj.calculations:
        _numbers_in(c.get("value"), allowed)
        _numbers_in(c.get("method", ""), allowed)
        _numbers_in(c.get("name", ""), allowed)
        _numbers_in(c.get("notes", []), allowed)
        _numbers_in([(i.get("year"), i.get("value")) for i in c.get("inputs", [])], allowed)
    _numbers_in([(f.get("value"), f.get("year")) for f in obj.facts], allowed)
    for e in obj.evidence:
        _numbers_in(e.get("text", ""), allowed)
        _numbers_in(e.get("year"), allowed)
        _numbers_in(e.get("page"), allowed)
        _numbers_in((e.get("title") or "") + " " + (e.get("section_path") or "") + " " + (e.get("filename") or ""), allowed)
    _numbers_in(obj.plan.get("scope", {}), allowed)
    _numbers_in(obj.question, allowed)
    _numbers_in(obj.gaps, allowed)
    _numbers_in([s.get("points") for s in obj.series], allowed)
    _numbers_in([(p.get("detail"), p.get("years")) for p in obj.patterns], allowed)
    _numbers_in([(d.get("statement"), d.get("driver"), d.get("year")) for d in obj.drivers], allowed)
    _numbers_in(obj.recommendations, allowed)
    _numbers_in(obj.contradictions, allowed)
    _numbers_in(len(obj.contradictions), allowed)          # "22 in total"
    _numbers_in(obj.assumptions, allowed)
    _numbers_in([m.get("text", "") for m in obj.memory_used], allowed)
    if obj.records:                              # every matching register row, not only the cited ones
        _numbers_in([list(r.get("data", {}).values()) + [r.get("record_key")] for r in obj.records.get("records", [])], allowed)
        _numbers_in((obj.records.get("conditions"), obj.records.get("as_of"), obj.records.get("notes")), allowed)
    return allowed


# Models also write "[F2, F3, C1]", "[E1; E4]" and bare "C13" - every form is
# read and every id checked; none can slip past as unparsed text.
_CITATION_GROUP = re.compile(r"\[\s*([EFCGM]\d+(?:\s*[,;]\s*[EFCGM]?\d+)*)\s*\]")
_BARE_CITATION = re.compile(r"(?<![\w\-\[])([EFCGM])(\d{1,3})(?![\w\-])")
# The '### Evidence' list under an answer (service.detail_sections) names every
# passage retrieved, used or not - a reading list, not a claim. Counted as
# citations it made every listed passage 'cited', so the answer's origin and a
# VERIFIED state came from company passages the answer never used.
_EVIDENCE_LIST = re.compile(r"^### Evidence\n(?:(?:- \[E\d+\] |  )[^\n]*(?:\n|$))+", re.MULTILINE)


def citations_in(text: str) -> List[str]:
    ids: List[str] = []
    for group in _CITATION_GROUP.findall(text or ""):
        kind = ""
        for item in re.split(r"\s*[,;]\s*", group):
            if item and item[0].isalpha():
                kind = item[0]
                ids.append(item)
            elif item and kind:
                ids.append(kind + item)
    for kind, number in _BARE_CITATION.findall(_CITATION_GROUP.sub(" ", text or "")):
        ids.append(kind + number)
    return ids


_SECTION = re.compile(r"^\s*(?:#+\s*|\*\*)?(answer|facts?|inferences?|assumptions?)\s*(?:\*\*)?\s*:?\s*(?:\*\*)?\s*$", re.I)


def separate_reasoning(text: str) -> Dict[str, List[str]]:
    """The model's answer split into ANSWER / FACTS / INFERENCES / ASSUMPTIONS by its
    headings. Empty lists when the model did not use them."""
    parts: Dict[str, List[str]] = {"answer": [], "facts": [], "inferences": [], "assumptions": []}
    current = "answer"
    for line in (text or "").splitlines():
        heading = _SECTION.match(line)
        if heading:
            word = heading.group(1).lower()
            current = "facts" if word.startswith("fact") else "inferences" if word.startswith("inference") \
                else "assumptions" if word.startswith("assumption") else "answer"
            continue
        item = re.sub(r"^\s*(?:[-*]|\d+[.)])\s+", "", line).strip()
        if item:
            parts[current].append(item)
    return parts


def check_reasoning(parts: Dict[str, List[str]]) -> List[str]:
    """Facts must be cited; an inference must name what it builds on; an
    assumption may not introduce a number (it would read as a finding)."""
    problems = []
    for fact in parts.get("facts", []):
        if not citations_in(fact):
            problems.append(f"a fact without a citation: '{fact[:80]}'")
    for inference in parts.get("inferences", []):
        if not citations_in(inference):
            problems.append(f"an inference that does not say what it builds on: '{inference[:80]}'")
    for assumption in parts.get("assumptions", []):
        stripped = _BARE_CITATION.sub(" ", _CITATION_GROUP.sub(" ", assumption))
        if any(abs(float(m.group(0).replace(",", ""))) > 12 for m in _NUMBER.finditer(stripped)
               if m.group(0).replace(",", "").replace(".", "", 1).lstrip("-").isdigit()):
            problems.append(f"an assumption that states a number: '{assumption[:80]}'")
    return problems


def validate_answer(obj: AnalysisObject, text: str) -> Dict[str, Any]:
    counts = {"E": len(obj.evidence), "F": min(len(obj.facts), FACT_IDS), "C": len(obj.calculations),
              "G": min(len(obj.graph_edges), GRAPH_IDS), "M": len(obj.memory_used)}
    cited, invalid = set(), []
    for ident in citations_in(_EVIDENCE_LIST.sub("", text or "")):
        kind, number = ident[0], int(ident[1:])
        if number < 1 or number > counts.get(kind, 0):
            invalid.append(ident)
        else:
            cited.add(ident)
    allowed = allowed_numbers(obj)
    stripped = _BARE_CITATION.sub(" ", _CITATION_GROUP.sub(" ", text or ""))
    unknown = []
    for m in _NUMBER.finditer(stripped):
        raw = m.group(0)
        try:
            value = float(raw.replace(",", ""))
        except ValueError:
            continue
        if abs(value) <= 12 and value == int(value):
            continue                         # list counters, "3 actions", "Phase 2"
        if _canon(value) not in allowed and _canon(abs(value)) not in allowed:
            unknown.append(raw)
    factual_sentences = [s for s in re.split(r"(?<=[.!?])\s+", stripped) if re.search(r"\d", s) and len(s) > 25]
    uncited = [s[:120] for s in re.split(r"(?<=[.!?])\s+", text or "")
               if re.search(r"\d", s) and len(s) > 25 and not citations_in(s)]
    ok = not invalid and not unknown
    return {"ok": ok, "cited": sorted(cited), "invalid_citations": sorted(set(invalid)),
            "unsupported_numbers": sorted(set(unknown))[:30], "uncited_numeric_sentences": uncited[:10],
            "numeric_sentences": len(factual_sentences)}
