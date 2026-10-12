"""
When sources disagree, ZEUS shows the disagreement.

    SOURCE A  |  SOURCE B  |  CONFLICT  |  what may explain it
                                            date, revision, scope, definition, unit, authority

A numeric conflict is two facts for the same metric, entity and period whose
values differ by more than the tolerance. It is never averaged or silently
resolved: a SUGGESTED preference is given (higher authority, then the later
revision / date) and the user sees both.

Also here: the section-by-section comparison behind "what changed between two
SOP revisions".
"""

from __future__ import annotations

import difflib
import re
from typing import Any, Dict, Iterable, List, Optional

from knowledge_fabric import authority, store
from knowledge_fabric.access import AccessContext


def _key(fact: Dict[str, Any]) -> tuple:
    return (fact.get("metric_norm"), fact.get("entity_id") or (fact.get("entity_label") or "").lower() or None,
            fact.get("year"), fact.get("quarter"), fact.get("month"))


def numeric_conflicts(facts: Iterable[Dict[str, Any]], tolerance: float = 0.01) -> List[Dict[str, Any]]:
    groups: Dict[tuple, List[Dict[str, Any]]] = {}
    for fact in facts:
        if fact.get("year") is None:
            continue
        groups.setdefault(_key(fact), []).append(fact)
    conflicts = []
    for key, items in groups.items():
        by_doc: Dict[str, Dict[str, Any]] = {}
        for fact in items:
            by_doc.setdefault(fact["document_id"], fact)
        docs = list(by_doc.values())
        for i in range(len(docs)):
            for j in range(i + 1, len(docs)):
                a, b = docs[i], docs[j]
                va, vb = float(a["value"]), float(b["value"])
                scale = max(abs(va), abs(vb), 1e-9)
                if abs(va - vb) / scale <= tolerance:
                    continue
                conflicts.append(_describe(key, a, b))
    return conflicts


def _issued(fact: Dict[str, Any]) -> str:
    """When the reporting document was issued: its date, else its year - but a
    record that spans years (a 1990-2026 ledger) has no single issue year."""
    if fact.get("doc_date"):
        return str(fact["doc_date"])
    if fact.get("time_start") and fact.get("time_end") and fact["time_start"] != fact["time_end"]:
        return ""
    return str(fact["doc_year"]) if fact.get("doc_year") else ""


def derived(fact: Dict[str, Any]) -> bool:
    """A figure ZEUS computed (a month summed from daily rows), not one a document states."""
    return "(sum)" in (fact.get("cell_ref") or "") or (fact.get("source_text") or "").startswith("sum of ")


def _period(year, quarter, month) -> str:
    return f"{year}" + (f" Q{quarter}" if quarter else "") + (f"-{int(month):02d}" if month else "")


def _describe(key: tuple, a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    metric, entity, year, quarter, month = key
    if derived(a) != derived(b):
        return _reconciliation(key, a, b)
    explanations = []
    if (a.get("unit") or "") != (b.get("unit") or ""):
        explanations.append(f"UNIT: '{a.get('unit')}' vs '{b.get('unit')}' - may be the same quantity in different units")
    if (a.get("metric") or "").lower() != (b.get("metric") or "").lower():
        explanations.append(f"DEFINITION: reported as '{a.get('metric')}' vs '{b.get('metric')}'")
    if a.get("revision") != b.get("revision") and (a.get("revision") or b.get("revision")):
        explanations.append(f"REVISION: {a.get('revision') or '-'} vs {b.get('revision') or '-'}")
    date_a, date_b = _issued(a), _issued(b)
    if date_a != date_b:
        explanations.append(f"DATE: issued {date_a or 'undated'} vs {date_b or 'undated'} "
                            f"- a later document may restate the figure")
    if (a.get("status") or "") != (b.get("status") or ""):
        explanations.append(f"STATUS: {a.get('status') or '-'} vs {b.get('status') or '-'}")
    ra, rb = int(a.get("authority") or 5), int(b.get("authority") or 5)
    explanations.append(f"AUTHORITY: {authority.describe(ra)} vs {authority.describe(rb)}")
    if (a.get("entity_label") or "") != (b.get("entity_label") or ""):
        explanations.append(f"SCOPE: '{a.get('entity_label') or 'whole company'}' vs '{b.get('entity_label') or 'whole company'}'")
    if ra != rb:
        preferred, reason = (a, "higher authority") if ra < rb else (b, "higher authority")
    elif date_a and date_b and date_a != date_b:
        preferred = a if date_a > date_b else b
        reason = "same authority - the later document (a restatement)"
    else:
        preferred, reason = None, "same authority and date - needs a human decision"
    return {"type": "numeric", "metric": metric, "entity": entity, "period": _period(year, quarter, month),
            "source_a": _side(a), "source_b": _side(b),
            "difference": round(float(b["value"]) - float(a["value"]), 4),
            "explanations": explanations,
            "suggested_preference": _side(preferred) if preferred else None, "preference_reason": reason}


def _side(f: Dict[str, Any]) -> Dict[str, Any]:
    return {"document_id": f["document_id"], "title": f.get("title"), "value": f["value"], "unit": f.get("unit"),
            "metric_as_written": f.get("metric"), "node_id": f.get("node_id"), "cell": f.get("cell_ref"),
            "page": f.get("page"), "date": f.get("doc_date") or f.get("doc_year"), "revision": f.get("revision"),
            "status": f.get("status"), "authority": authority.describe(f.get("authority") or 5),
            "derived": derived(f)}


def _reconciliation(key: tuple, a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    """A stated figure against one ZEUS summed from finer rows: not two documents
    disagreeing but a ledger that does not add up to its report. The stated
    figure is preferred; the gap is shown, never hidden."""
    metric, entity, year, quarter, month = key
    stated, summed = (b, a) if derived(a) else (a, b)
    gap = float(summed["value"]) - float(stated["value"])
    pct = round(gap / abs(float(stated["value"])) * 100, 2) if float(stated["value"]) else None
    return {"type": "reconciliation", "metric": metric, "entity": entity, "period": _period(year, quarter, month),
            "month": month, "quarter": quarter, "year": year,
            "source_a": _side(stated), "source_b": _side(summed), "difference": round(gap, 4), "percent": pct,
            "explanations": [f"RECONCILIATION: {summed.get('title')} is a sum of its rows, "
                             f"{stated.get('title')} states the figure"],
            "suggested_preference": _side(stated),
            "preference_reason": "the stated figure - the summed rows may be incomplete, or the report may carry "
                                 "adjustments the rows do not"}


# "Crosses INR 50 Cr annual revenue", "revenue passed Rs 100 crore"
_CLAIM = re.compile(r"\b(?:cross(?:es|ed)?|pass(?:es|ed)?|exceed(?:s|ed)?|reach(?:es|ed)?|surpass(?:es|ed)?|tops?|topped)\s+"
                    r"(?:the\s+)?(?:INR|Rs\.?|₹)\s*([\d,]+(?:\.\d+)?)\s*(?:cr|crore)s?\b\s*(?:of\s+)?(?:annual\s+|yearly\s+)?"
                    r"(revenue|sales|turnover|net profit|profit|ebitda)", re.IGNORECASE)
_CLAIM_METRIC = {"revenue": "revenue", "sales": "revenue", "turnover": "revenue", "net profit": "net profit",
                 "profit": "net profit", "ebitda": "ebitda"}


def statements_vs_figures(evidence: Iterable[Dict[str, Any]], series: Iterable[Dict[str, Any]],
                          facts: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """A dated statement that a figure crossed a threshold, checked against the
    recorded figure for that year. A disagreement is reported with both sides -
    neither is corrected."""
    by_metric = {s["metric"]: s for s in series if not s.get("entity") and "cr" in (s.get("unit") or "").lower()}
    fact_ids = {f.get("fact_id"): f.get("id") for f in facts if f.get("id")}
    out: List[Dict[str, Any]] = []
    for e in evidence:
        if e.get("kind") != "event" or not e.get("year"):
            continue
        claim = _CLAIM.search(e.get("text") or "")
        if not claim:
            continue
        metric = _CLAIM_METRIC[claim.group(2).lower()]
        s = by_metric.get(metric)
        point = next((p for p in (s or {}).get("points", []) if p["year"] == e["year"]), None)
        threshold = float(claim.group(1).replace(",", ""))
        if not point or point["value"] >= threshold:
            continue
        fid = fact_ids.get(point.get("fact_id"))
        out.append({"type": "statement", "metric": metric, "period": str(e["year"]), "evidence_id": e.get("evidence_id"),
                    "fact_ref": fid, "claimed": threshold, "recorded": point["value"], "unit": s.get("unit"),
                    "detail": f"'{claim.group(0)}' ({e.get('citation') or e.get('title')}) but the recorded {metric} "
                              f"for {e['year']} is {_num(point['value'])} {s.get('unit')}",
                    "preference_reason": "not resolved - the records disagree; check the source documents"})
        out[-1]["line"] = (f"{metric} {e['year']}: a record states '{claim.group(0)}'"
                           + (f" [{e['evidence_id']}]" if e.get("evidence_id") else "")
                           + f", but the recorded {metric} for {e['year']} is {_num(point['value'])} {s.get('unit')}"
                           + (f" [{fid}]" if fid else "") + ". Not resolved - the records disagree.")
    return out


def _num(value: Any) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        text = f"{value:,.2f}"
        return text.rstrip("0").rstrip(".") if "." in text else text
    return "n/a" if value is None else str(value)


def line(c: Dict[str, Any]) -> str:
    """One plain-text line for any kind of conflict - the chat answer, the reports
    and the UI all say the same thing."""
    kind = c.get("type")
    if kind in ("numeric", "reconciliation"):
        a, b = c["source_a"], c["source_b"]
        unit_a = f" {a['unit']}" if a.get("unit") else ""
        unit_b = f" {b['unit']}" if b.get("unit") else ""
        head = "RECONCILIATION" if kind == "reconciliation" else "CONFLICT"
        return (f"{c['metric']} {c['period']}: SOURCE A {a['title']} = {_num(a['value'])}{unit_a}; "
                f"SOURCE B {b['title']}{' (summed rows)' if b.get('derived') else ''} = {_num(b['value'])}{unit_b}. "
                f"{head}: " + "; ".join(c.get("explanations", [])[:3]) + f". Preferred: {c['preference_reason']}.")
    if kind == "reconciliation_summary":
        return (f"{c['metric']}: {c['count']} periods ({c['first_period']} to {c['last_period']}) where "
                f"{c['stated_title']} differs from the sum of the rows in {c['summed_title']}; largest gap "
                f"{_num(c['largest_gap_percent'])}% ({c['largest_gap_period']}). Preferred: {c['preference_reason']}.")
    if kind == "record":
        return f"{c['register']} {c['record_key']}: {c['detail']}" + (f" [{c['evidence_id']}]" if c.get("evidence_id") else "")
    if kind == "statement":
        return c.get("line") or f"{c['metric']} {c['period']}: {c['detail']}"
    return (f"different causes stated for {c.get('subject')}: "
            + ", ".join(s.get("cause") or "?" for s in c.get("stated", [])))


def summarise_reconciliations(items: List[Dict[str, Any]], keep: int = 2) -> List[Dict[str, Any]]:
    """Many monthly reconciliation gaps -> one line per metric and document pair,
    with the count, the months and the largest gap; the first few stay itemised."""
    groups: Dict[tuple, List[Dict[str, Any]]] = {}
    for item in items:
        groups.setdefault((item["metric"], item["source_a"]["document_id"], item["source_b"]["document_id"]), []).append(item)
    out: List[Dict[str, Any]] = []
    for (metric, _, _), rows in groups.items():
        if len(rows) <= keep:
            out += rows
            continue
        worst = max(rows, key=lambda r: abs(r.get("percent") or 0))
        periods = sorted(r["period"] for r in rows)
        out.append({"type": "reconciliation_summary", "metric": metric, "count": len(rows),
                    "first_period": periods[0], "last_period": periods[-1],
                    "stated_title": rows[0]["source_a"]["title"], "summed_title": rows[0]["source_b"]["title"],
                    "largest_gap_percent": worst.get("percent"), "largest_gap_period": worst["period"],
                    "unit": rows[0]["source_a"].get("unit"),
                    "preference_reason": rows[0]["preference_reason"]})
    return out


def statement_differences(edges: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Different stated causes for the same failure - shown together, because
    several causes can be true at once; the user decides whether they conflict."""
    groups: Dict[tuple, List[Dict[str, Any]]] = {}
    for edge in edges:
        if edge.get("relation") in ("FAILED_DUE_TO", "CAUSED") and edge.get("confidence", 0) >= 0.5:
            subject = edge["src"] if edge["relation"] == "FAILED_DUE_TO" else edge["dst"]
            groups.setdefault((subject, edge["relation"]), []).append(edge)
    out = []
    for (subject, relation), items in groups.items():
        causes = {}
        for e in items:
            other = e["dst"] if relation == "FAILED_DUE_TO" else e["src"]
            causes.setdefault(other, e)
        if len(causes) > 1:
            out.append({"type": "statements", "subject": subject, "relation": relation,
                        "stated": [{"cause": (e.get("dst_name") if relation == "FAILED_DUE_TO" else e.get("src_name")),
                                    "document_id": e.get("document_id"), "evidence_node": e.get("evidence_node")}
                                   for e in causes.values()],
                        "note": "different causes are stated in different places - they may all be true, or conflict"})
    return out


# ------------------------------------------------------------ revisions ----

def compare_documents(ctx: AccessContext, document_a: str, document_b: str) -> Dict[str, Any]:
    """Section-by-section comparison of two documents (e.g. two SOP revisions)."""
    with store.connection() as conn:
        docs = {}
        for doc_id in (document_a, document_b):
            row = conn.execute("SELECT * FROM kf_documents WHERE document_id = ?", (doc_id,)).fetchone()
            if not row or not ctx.can_see(dict(row)):
                return {"error": "one of the documents does not exist or is not visible to your role"}
            docs[doc_id] = dict(row)

        def sections(doc_id):
            out: Dict[str, List[str]] = {}
            for n in conn.execute("SELECT kind, heading, section_path, text FROM kf_nodes WHERE document_id = ? "
                                  "AND kind NOT IN ('document') ORDER BY ordinal", (doc_id,)):
                key = (n["section_path"] or n["heading"] or "(top)").split(" > ")[-1].strip().lower()
                key = re.sub(r"^\d+(\.\d+)*\s*", "", key)
                if n["kind"] in ("section", "subsection"):
                    out.setdefault(key, [])
                elif n["text"]:
                    out.setdefault(key, []).append(n["text"])
            return out

        sa, sb = sections(document_a), sections(document_b)
    changes = []
    for key in list(dict.fromkeys(list(sa) + list(sb))):
        ta, tb = "\n".join(sa.get(key, [])), "\n".join(sb.get(key, []))
        if key not in sa:
            changes.append({"section": key, "change": "added", "new": tb[:1500]})
        elif key not in sb:
            changes.append({"section": key, "change": "removed", "old": ta[:1500]})
        elif ta.strip() != tb.strip():
            diff = [line for line in difflib.unified_diff(ta.splitlines(), tb.splitlines(), lineterm="", n=0)
                    if line and not line.startswith(("---", "+++", "@@"))]
            ratio = difflib.SequenceMatcher(None, ta, tb).ratio()
            changes.append({"section": key, "change": "modified", "similarity": round(ratio, 3), "diff": diff[:80]})
    a, b = docs[document_a], docs[document_b]
    return {"document_a": {"document_id": document_a, "title": a["title"], "revision": a["revision"], "date": a["doc_date"]},
            "document_b": {"document_id": document_b, "title": b["title"], "revision": b["revision"], "date": b["doc_date"]},
            "changes": changes, "unchanged_sections": sum(1 for k in sa if k in sb and "\n".join(sa[k]).strip() == "\n".join(sb[k]).strip())}
