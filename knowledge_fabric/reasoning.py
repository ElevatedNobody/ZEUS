"""
Decision support: FACTS -> EVIDENCE -> PATTERNS -> POSSIBLE DRIVERS -> OPTIONS
-> TRADE-OFFS -> RISKS -> IMPLEMENTATION -> KPIs -> MONITORING.

Every driver says how it is known:
    evidenced    a company document states it (with the evidence id)
    correlated   the numbers moved together (Pearson r, years, p) - NOT proof of cause
    hypothesis   plausible from the practice library but not shown by the records

Recommendations come from config/knowledge_fabric/playbook.json (a practice
library, not company facts), are attached only to drivers the evidence
supports, and are written in operational form - action, owner, timeline,
dependencies, cost category, expected impact, risk, KPI, data required,
success condition, monitoring frequency. Expected impact is described, never
invented as a number: quantifying it is listed under "data required".
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from knowledge_fabric import analytics

PLAYBOOK_PATH = Path(__file__).resolve().parent.parent / "config" / "knowledge_fabric" / "playbook.json"
_playbook_cache: Optional[Dict[str, Any]] = None

_CAUSAL_STATEMENT = re.compile(
    r"(?P<subject>[^.;\n]{0,90}?\b(?:profit|profitability|loss(?:es)?|margin|revenue|production|throughput|cost|costs|"
    r"downtime|output|performance|availability|earnings|ebitda|grm)\b[^.;\n]{0,70}?)\s*"
    r"\b(?:was |were )?(?:due to|because of|owing to|on account of|driven by|impacted by|affected by|"
    r"as a result of|attributed to|attributable to|caused by|following)\s+(?P<cause>[^.;\n]{3,140})",
    re.IGNORECASE)


def playbook() -> Dict[str, Any]:
    global _playbook_cache
    if _playbook_cache is None:
        try:
            _playbook_cache = json.loads(PLAYBOOK_PATH.read_text(encoding="utf-8"))
        except Exception:
            _playbook_cache = {"drivers": {}}
    return _playbook_cache


def categorize(text: str) -> Optional[str]:
    low = (text or "").lower()
    best, hits = None, 0
    for key, spec in playbook().get("drivers", {}).items():
        count = sum(1 for word in spec.get("match", []) if word in low)
        if count > hits:
            best, hits = key, count
    return best


# -------------------------------------------------------------- patterns ----

def patterns(series: analytics.Series) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    pts = series.points
    if not pts:
        return out
    negatives = [p for p in pts if p.value < 0]
    if negatives:
        out.append({"pattern": "negative values", "metric": series.metric,
                    "years": [p.year for p in negatives],
                    "detail": f"{series.metric} below zero in {len(negatives)} of {len(pts)} years with data",
                    "inputs": [p.ref() for p in negatives]})
    run: List[analytics.Point] = []
    runs = []
    for prev, cur in zip(pts, pts[1:]):
        if cur.year == prev.year + 1 and cur.value < prev.value:
            run = run or [prev]
            run.append(cur)
        else:
            if len(run) >= 3:
                runs.append(run)
            run = []
    if len(run) >= 3:
        runs.append(run)
    for r in runs:
        out.append({"pattern": "consecutive decline", "metric": series.metric, "years": [p.year for p in r],
                    "detail": f"{series.metric} fell every year from {r[0].year} to {r[-1].year}",
                    "inputs": [p.ref() for p in r]})
    for prev, cur in zip(pts, pts[1:]):
        if prev.value and cur.year == prev.year + 1:
            pct = (cur.value - prev.value) / abs(prev.value) * 100
            if abs(pct) >= 25:
                out.append({"pattern": "sharp change", "metric": series.metric, "years": [prev.year, cur.year],
                            "detail": f"{series.metric} changed {pct:+.1f}% from {prev.year} to {cur.year}",
                            "inputs": [prev.ref(), cur.ref()]})
    return out


# --------------------------------------------------------------- drivers ----

def drivers(target: analytics.Series, other_series: Iterable[analytics.Series], evidence: List[Any],
            edges: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """(drivers, correlation calculations)."""
    found: List[Dict[str, Any]] = []
    correlations: List[Dict[str, Any]] = []
    for item in evidence:
        for m in _CAUSAL_STATEMENT.finditer(item.text or ""):
            cause = re.sub(r"\s+", " ", m.group("cause")).strip(" ,")
            statement = re.sub(r"\s+", " ", m.group(0)).strip()
            found.append({"driver": cause[:120], "category": categorize(cause + " " + statement),
                          "kind": "evidenced", "statement": statement[:300], "evidence_ids": [item.evidence_id],
                          "authority": item.authority_label, "origin": item.origin, "year": item.year})
    for edge in edges:
        if edge.get("relation") in ("CAUSED", "FAILED_DUE_TO") and edge.get("confidence", 0) >= 0.5:
            cause = edge["dst_name"] if edge["relation"] == "FAILED_DUE_TO" else edge["src_name"]
            subject = edge["src_name"] if edge["relation"] == "FAILED_DUE_TO" else edge["dst_name"]
            found.append({"driver": f"{cause} ({subject})", "category": categorize(f"{cause} {subject} failure"),
                          "kind": "evidenced", "statement": f"{subject} {edge['relation'].replace('_', ' ').lower()} {cause}",
                          "evidence_node": edge.get("evidence_node"), "evidence_ids": [], "year": None})
    if target.points:
        for other in other_series:
            if other.metric == target.metric or len(other.points) < 4:
                continue
            calc = analytics.correlation(target, other)
            if not calc.ok:
                continue
            r = calc.value["r"]
            correlations.append(calc.to_dict())
            if abs(r) >= 0.6:
                direction = "rose" if r < 0 else "fell"
                found.append({"driver": f"{other.metric} (moved {'against' if r < 0 else 'with'} {target.metric}, r = {r:+.2f})",
                              "category": categorize(other.metric), "kind": "correlated",
                              "statement": f"in the years where {target.metric} fell, {other.metric} {direction} "
                                           f"(Pearson r = {r:+.2f} over {len(calc.value['years'])} years)",
                              "evidence_ids": [], "calculation": calc.name,
                              "caveat": "correlation shows co-movement, not cause"})
    # de-duplicate by (driver text, kind)
    seen, unique = set(), []
    for d in found:
        key = (d["driver"].lower()[:60], d["kind"])
        if key not in seen:
            seen.add(key)
            unique.append(d)
    order = {"evidenced": 0, "correlated": 1, "hypothesis": 2}
    unique.sort(key=lambda d: order.get(d["kind"], 3))
    return unique, correlations


# ------------------------------------------------------- recommendations ----

def recommendations(driver_list: List[Dict[str, Any]], gaps: Optional[Dict[str, Any]] = None,
                    target_metric: str = "") -> List[Dict[str, Any]]:
    book = playbook()
    library = book.get("drivers", {})
    recs: List[Dict[str, Any]] = []
    used: Dict[str, Dict[str, Any]] = {}
    for driver in driver_list:
        category = driver.get("category")
        if not category or category not in library:
            continue
        confidence = "high" if driver["kind"] == "evidenced" and driver.get("origin", "company") == "company" \
            else "medium" if driver["kind"] in ("evidenced", "correlated") else "low"
        for action in library[category]["actions"]:
            key = action["action"]
            if key in used:
                entry = used[key]
                entry["basis"].append(driver["driver"])
                entry["evidence_ids"] = sorted(set(entry["evidence_ids"] + driver.get("evidence_ids", [])))
                if confidence == "high":
                    entry["confidence"] = "high"
                continue
            entry = {**action, "category": category, "addresses": library[category]["label"],
                     "basis": [driver["driver"]], "evidence_ids": list(driver.get("evidence_ids", [])),
                     "confidence": confidence, "source": "practice library matched to evidence"}
            used[key] = entry
            recs.append(entry)
    if gaps and (gaps.get("missing_years") or not driver_list):
        foundation = book.get("data_foundation")
        if foundation:
            recs.insert(0, {**foundation, "category": "data_foundation",
                            "addresses": "incomplete records for the period analysed",
                            "basis": [f"missing years: {', '.join(map(str, gaps.get('missing_years', [])[:12]))}"
                                      if gaps.get("missing_years") else "no evidence-backed driver found"],
                            "evidence_ids": [], "confidence": "high", "source": "coverage report"})
    for index, rec in enumerate(recs, 1):
        rec["id"] = f"R{index}"
    return recs


def options_and_tradeoffs(recs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    speed = {"0-3 months": 3, "0-6 months": 2.5, "3-6 months": 2, "3-12 months": 1.5, "6-18 months": 1,
             "12-24 months": 0.5}
    cost = {"OPEX - low": 3, "OPEX - low (working-capital release)": 3, "OPEX - medium": 2, "CAPEX - medium": 1,
            "CAPEX - high": 0.5}
    out = []
    for rec in recs:
        out.append({"option": rec["id"], "action": rec["action"], "addresses": rec["addresses"],
                    "cost": rec.get("cost_category"), "time_to_effect": rec.get("timeline"),
                    "confidence": rec.get("confidence"),
                    "tradeoff": f"{rec.get('cost_category')} over {rec.get('timeline')}; risk: {rec.get('risk')}",
                    "priority_score": round(speed.get(rec.get("timeline"), 1) + cost.get(rec.get("cost_category"), 1)
                                            + {"high": 3, "medium": 2, "low": 1}.get(rec.get("confidence"), 1), 2)})
    out.sort(key=lambda o: -o["priority_score"])
    return out


def implementation_plan(recs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    phases = [("Phase 1 - quick wins", ("0-3 months", "0-6 months")),
              ("Phase 2 - structural fixes", ("3-6 months", "3-12 months", "6-18 months")),
              ("Phase 3 - long-term", ("12-24 months", "per turnaround"))]
    out = []
    for name, timelines in phases:
        items = [r for r in recs if r.get("timeline") in timelines]
        if items:
            out.append({"phase": name, "window": " / ".join(timelines),
                        "actions": [{"id": r["id"], "action": r["action"], "owner": r["owner"],
                                     "dependencies": r.get("dependencies", []), "kpi": r.get("kpi")} for r in items]})
    other = [r for r in recs if not any(r.get("timeline") in t for _, t in phases)]
    if other:
        out.append({"phase": "Other", "window": "as scheduled",
                    "actions": [{"id": r["id"], "action": r["action"], "owner": r["owner"],
                                 "dependencies": r.get("dependencies", []), "kpi": r.get("kpi")} for r in other]})
    return out


def kpi_table(recs: List[Dict[str, Any]], series_list: List[analytics.Series]) -> List[Dict[str, Any]]:
    rows, seen = [], set()
    for rec in recs:
        for kpi in [k.strip() for k in (rec.get("kpi") or "").split(";") if k.strip()]:
            if kpi.lower() in seen:
                continue
            seen.add(kpi.lower())
            baseline = None
            for series in series_list:
                if series.points and (series.metric in kpi.lower() or kpi.lower() in series.metric):
                    last = series.points[-1]
                    baseline = {"value": last.value, "unit": series.unit, "year": last.year, "source": last.ref()}
                    break
            rows.append({"kpi": kpi, "owner": rec["owner"], "linked_to": rec["id"],
                         "baseline": baseline or "not in the records - establish from the data listed under 'data required'",
                         "target": "to be set by management against the baseline",
                         "monitoring_frequency": rec.get("monitoring_frequency")})
    return rows


def risks(recs: List[Dict[str, Any]], gaps: Optional[Dict[str, Any]], contradiction_count: int) -> List[Dict[str, Any]]:
    out = [{"risk": rec["risk"], "linked_to": rec["id"], "mitigation": f"track {rec.get('kpi')} {rec.get('monitoring_frequency')}"}
           for rec in recs if rec.get("risk")]
    if gaps and gaps.get("missing_years"):
        out.insert(0, {"risk": f"the records for {len(gaps['missing_years'])} year(s) in the period are missing - "
                               f"conclusions may change when they are added", "linked_to": "coverage",
                       "mitigation": "collect the missing documents (see coverage)"})
    if contradiction_count:
        out.insert(0, {"risk": f"{contradiction_count} conflicting figure(s) between sources", "linked_to": "contradictions",
                       "mitigation": "confirm the authoritative figure with the document owner"})
    return out
