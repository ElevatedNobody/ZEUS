"""
What the company records actually cover, 1989 -> 2026 - measured, never assumed.

    1989  MISSING
    1990  MISSING
    ...
    2017  AVAILABLE   4 documents (annual_report, maintenance_report, ...)
    ...

Per year: AVAILABLE (company documents exist for it), SAMPLE (only sample / demo
data), GENERAL (only general industrial knowledge), MISSING - with its documents,
sources, typed sections (a records pack's '## FINANCIAL_RECORD' counts), departments,
sites, figures and an evidence-based confidence. Missing document types and
departments are computed against the company corpus itself (what it holds in other
years), never against a fixed list. Also: missing periods, missing datasets (metrics
with figures in some years only), undated documents. `year_detail()` is the drill-down.

Computed on the documents the CALLER may see; the report says so.

`timeline()` feeds the Company Intelligence screen: per year, the documents,
financial changes (from table facts), incidents, projects, maintenance events
and decisions - each item carries the evidence it came from.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from knowledge_fabric import FIRST_YEAR, LAST_YEAR, store
from knowledge_fabric.access import AccessContext

_FORMAT_TYPES = ("other", "spreadsheet", "presentation", "register", "image", "scanned_document", "transcript",
                 "handwritten_document", "general_knowledge", "records_pack")


def _bucket(origin: str) -> str:
    return "company" if origin == "company" else "general" if origin == "general_knowledge" else "sample"


def _load(ctx: AccessContext, first: int, last: int) -> Dict[str, Any]:
    """Everything coverage is computed from - one pass, all behind the access filter."""
    with store.connection() as conn:
        access_sql, params = ctx.clause("d")
        docs = [dict(r) for r in conn.execute(
            f"SELECT document_id, title, filename, document_type, year, time_start, time_end, department, origin, "
            f"authority, source_path, metadata FROM kf_documents d WHERE {access_sql}", params)]
        tags = [dict(r) for r in conn.execute(
            f"SELECT t.node_id, t.document_id, t.year, t.document_type, t.department, t.site, d.origin FROM kf_section_tags t "
            f"JOIN kf_documents d ON d.document_id = t.document_id WHERE {access_sql} AND t.year BETWEEN ? AND ?",
            params + [first, last])]
        facts = [dict(r) for r in conn.execute(
            f"SELECT f.metric_norm, f.year, d.origin, f.document_id FROM kf_facts f JOIN kf_documents d "
            f"ON d.document_id = f.document_id WHERE {access_sql} AND f.year BETWEEN ? AND ? "
            f"AND f.quarter IS NULL AND f.month IS NULL", params + [first, last])]
        node_years = [dict(r) for r in conn.execute(
            f"SELECT n.year, n.document_id, COUNT(*) AS passages FROM kf_nodes n JOIN kf_documents d "
            f"ON d.document_id = n.document_id WHERE {access_sql} AND n.year BETWEEN ? AND ? "
            f"AND n.kind IN ('paragraph', 'section', 'subsection', 'row_range') GROUP BY n.year, n.document_id",
            params + [first, last])]
        events = [dict(r) for r in conn.execute(
            f"SELECT n.node_id, n.year, n.text, n.document_id, d.origin FROM kf_nodes n JOIN kf_documents d "
            f"ON d.document_id = n.document_id WHERE {access_sql} AND n.kind = 'event' AND n.year BETWEEN ? AND ? "
            f"ORDER BY n.year, n.text", params + [first, last])]
    return {"docs": docs, "tags": tags, "facts": facts, "node_years": node_years, "events": events}


def _confidence(company_docs: int, sources: int, facts: int) -> str:
    """Evidence strength for a year - counted, not guessed."""
    if company_docs >= 2 and facts >= 3:
        return "HIGH"
    if sources >= 2 or (company_docs >= 1 and facts):
        return "MODERATE"
    if sources:
        return "LOW"
    return "NONE"


def report(ctx: AccessContext, first: int = FIRST_YEAR, last: int = LAST_YEAR) -> Dict[str, Any]:
    """Per year: status, documents, sources, typed sections, departments, sites,
    figures and confidence. What counts as a 'missing' type or department is
    what the company corpus itself contains in other years - nothing is assumed
    about what a company 'should' have."""
    years = list(range(first, last + 1))
    data = _load(ctx, first, last)
    docs, tags, facts = data["docs"], data["tags"], data["facts"]
    by_doc = {d["document_id"]: d for d in docs}

    per_year: Dict[int, Dict[str, Any]] = {y: {"company": {}, "sample": {}, "general": {}, "types": {},
                                               "departments": set(), "sites": set(), "facts": 0, "metrics": set()}
                                           for y in years}
    undated = []
    for doc in docs:
        span = set()
        if doc["year"]:
            span.add(doc["year"])
        if doc["time_start"] and doc["time_end"] and doc["time_end"] - doc["time_start"] <= 5:
            span.update(range(doc["time_start"], doc["time_end"] + 1))
        for y in span:
            if y in per_year:
                per_year[y][_bucket(doc["origin"])][doc["document_id"]] = doc
                if doc["origin"] == "company":
                    kind = doc["document_type"] or "other"
                    if kind not in _FORMAT_TYPES:
                        per_year[y]["types"][kind] = per_year[y]["types"].get(kind, 0) + 1
                    if doc["department"]:
                        per_year[y]["departments"].add(doc["department"])
        if not span and not (doc["time_start"] and doc["time_end"]):
            undated.append({"document_id": doc["document_id"], "title": doc["title"], "origin": doc["origin"]})
    # A long document (1990-2026) counts for the years its own sections and passages are about.
    for row in data["node_years"]:
        doc = by_doc.get(row["document_id"])
        if doc and row["year"] in per_year and doc["document_id"] not in per_year[row["year"]][_bucket(doc["origin"])]:
            if doc["time_start"] and doc["time_end"] and doc["time_end"] - doc["time_start"] > 5:
                per_year[row["year"]][_bucket(doc["origin"])][doc["document_id"]] = doc
    for tag in tags:
        if tag["origin"] != "company" or tag["year"] not in per_year:
            continue
        slot = per_year[tag["year"]]
        slot["types"][tag["document_type"]] = slot["types"].get(tag["document_type"], 0) + 1
        if tag["department"]:
            slot["departments"].add(tag["department"])
        if tag["site"]:
            slot["sites"].add(tag["site"])
    for fact in facts:
        if fact["origin"] == "company" and fact["year"] in per_year:
            per_year[fact["year"]]["facts"] += 1
            per_year[fact["year"]]["metrics"].add(fact["metric_norm"])
    # Dated events (a milestone register) - the markers on the timeline, with their origin.
    from knowledge_fabric.registers import event_label
    for event in data.get("events", []):
        if event["year"] in per_year:
            per_year[event["year"]].setdefault("events", []).append(
                {"label": event_label(event["text"]), "node_id": event["node_id"], "origin": event["origin"]})

    # The catalogue is the corpus itself: every type / department the company records hold anywhere.
    type_catalogue = sorted({t for y in years for t in per_year[y]["types"]})
    department_catalogue = sorted({dep for y in years for dep in per_year[y]["departments"]})
    rows = []
    for y in years:
        slot = per_year[y]
        company, sample, general = slot["company"], slot["sample"], slot["general"]
        status = "AVAILABLE" if company else "SAMPLE" if sample else "GENERAL" if general else "MISSING"
        sources = len(company) + len(sample) + len(general)
        rows.append({
            "year": y, "status": status,
            "company_documents": len(company), "sample_documents": len(sample), "general_documents": len(general),
            "sources": sources, "types": dict(sorted(slot["types"].items())),
            "departments": sorted(slot["departments"]), "sites": sorted(slot["sites"]),
            "figures": slot["facts"], "metrics": sorted(slot["metrics"])[:30],
            "events": slot.get("events", [])[:8], "event_count": len(slot.get("events", [])),
            "missing_types": [t for t in type_catalogue if t not in slot["types"]] if company else [],
            "missing_departments": [d for d in department_catalogue if d not in slot["departments"]] if company else [],
            "confidence": _confidence(len(company), sources, slot["facts"]) if company else "NONE"})

    missing_periods, run = [], []
    for row in rows:
        if row["status"] != "AVAILABLE":
            run.append(row["year"])
        else:
            if run:
                missing_periods.append({"from": run[0], "to": run[-1], "years": len(run)})
            run = []
    if run:
        missing_periods.append({"from": run[0], "to": run[-1], "years": len(run)})

    company_docs = [d for d in docs if d["origin"] == "company"]
    available = [r["year"] for r in rows if r["status"] == "AVAILABLE"]
    type_gaps = {t: _ranges([r["year"] for r in rows if r["status"] == "AVAILABLE" and t not in r["types"]])
                 for t in type_catalogue}
    department_gaps = {d: _ranges([r["year"] for r in rows if r["status"] == "AVAILABLE" and d not in r["departments"]])
                       for d in department_catalogue}
    metric_years: Dict[str, set] = {}
    for fact in facts:
        if fact["origin"] == "company":
            metric_years.setdefault(fact["metric_norm"], set()).add(fact["year"])
    datasets = {m: {"years_with_figures": sorted(ys), "missing": _ranges([y for y in years if y not in ys])}
                for m, ys in sorted(metric_years.items(), key=lambda kv: -len(kv[1]))[:12]}
    # Where the company records came from: each named dataset, how many documents,
    # and what the dataset says about itself (a README disclaimer is shown, not hidden).
    by_corpus: Dict[str, Dict[str, Any]] = {}
    for doc in company_docs:
        meta = store.loads(doc["metadata"], {}) or {}
        if meta.get("dataset"):
            entry = by_corpus.setdefault(meta["dataset"], {"name": meta["dataset"], "documents": 0,
                                                            "note": meta.get("dataset_note") or ""})
            entry["documents"] += 1
            entry["note"] = entry["note"] or meta.get("dataset_note") or ""
    corpora = sorted(by_corpus.values(), key=lambda c: c["name"])
    return {"scope_note": f"coverage of the documents visible to role '{ctx.role}'"
                          + ("" if ctx.is_admin else " - an administrator may see more"),
            "range": [first, last], "years": rows,
            "summary": {"available_years": len(available), "sample_only_years": sum(1 for r in rows if r["status"] == "SAMPLE"),
                        "general_only_years": sum(1 for r in rows if r["status"] == "GENERAL"),
                        "missing_years": sum(1 for r in rows if r["status"] == "MISSING"),
                        "company_documents": len(company_docs),
                        "typed_sections": sum(1 for t in tags if t["origin"] == "company"),
                        "sample_or_demo_documents": sum(1 for d in docs if d["origin"] in ("sample", "demo")),
                        "general_documents": sum(1 for d in docs if d["origin"] == "general_knowledge"),
                        "undated_documents": len(undated),
                        "first_company_year": available[0] if available else None,
                        "last_company_year": available[-1] if available else None},
            "catalogue": {"document_types": type_catalogue, "departments": department_catalogue,
                          "note": "taken from the company records themselves - a type or department counts as missing "
                                  "in a year when the records hold it for other years but not that one"},
            "corpora": corpora,
            "missing_periods": missing_periods,
            "missing_document_types": [t for t, gaps in type_gaps.items() if gaps],
            "document_type_gaps": {t: gaps for t, gaps in type_gaps.items() if gaps},
            "missing_departments": [d for d, gaps in department_gaps.items() if gaps],
            "department_gaps": {d: gaps for d, gaps in department_gaps.items() if gaps},
            "missing_datasets": datasets, "undated": undated[:100]}


def year_detail(ctx: AccessContext, year: int) -> Dict[str, Any]:
    """What is behind one year of the timeline: its documents (with provenance),
    typed sections, figures, register events and how many passages mention it."""
    data = _load(ctx, year, year)
    by_doc = {d["document_id"]: d for d in data["docs"]}
    ids = {d["document_id"] for d in data["docs"] if d["year"] == year or (
        d["time_start"] and d["time_end"] and d["time_end"] - d["time_start"] <= 5 and d["time_start"] <= year <= d["time_end"])}
    ids |= {r["document_id"] for r in data["node_years"] if (d := by_doc.get(r["document_id"])) and
            d["time_start"] and d["time_end"] and d["time_end"] - d["time_start"] > 5}
    passages = {r["document_id"]: r["passages"] for r in data["node_years"]}
    documents = []
    for doc_id in ids:
        d = by_doc.get(doc_id)
        if not d:
            continue
        meta = store.loads(d["metadata"], {}) or {}
        documents.append({"document_id": doc_id, "title": d["title"], "filename": d["filename"], "type": d["document_type"],
                          "origin": d["origin"], "department": d["department"], "authority": d["authority"],
                          "dated": d["year"] or (f"{d['time_start']}-{d['time_end']}" if d["time_start"] else None),
                          "passages_for_year": passages.get(doc_id, 0), "dataset": meta.get("dataset"),
                          "source_path": d["source_path"]})
    documents.sort(key=lambda d: (d["origin"] != "company", d["authority"] or 5, d["title"] or ""))
    with store.connection() as conn:
        access_sql, params = ctx.clause("d")
        figures = [dict(r) for r in conn.execute(
            f"SELECT f.metric, f.metric_norm, f.value, f.unit, f.node_id, f.document_id, d.title, d.origin FROM kf_facts f "
            f"JOIN kf_documents d ON d.document_id = f.document_id WHERE {access_sql} AND f.year = ? "
            f"AND f.quarter IS NULL AND f.month IS NULL AND f.entity_id IS NULL AND f.entity_label IS NULL "
            f"ORDER BY d.authority, f.metric_norm",
            params + [year])]
        events = []
        for r in conn.execute(
                f"SELECT r.record_key, r.node_id, r.data, g.name, d.title, d.origin FROM kf_records r "
                f"JOIN kf_registers g ON g.register_id = r.register_id JOIN kf_documents d ON d.document_id = g.document_id "
                f"WHERE {access_sql} AND r.record_key LIKE ?", params + [f"{year}-%"]):
            details = store.loads(r["data"], {}) or {}
            from knowledge_fabric.registers import event_label, record_text
            events.append({"event": r["record_key"], "register": r["name"], "node_id": r["node_id"],
                           "label": event_label(record_text(list(details), list(details.values()))),
                           "details": details, "origin": r["origin"]})
        headings = {}
        tag_nodes = [t["node_id"] for t in data["tags"]]
        for start in range(0, len(tag_nodes), 400):
            chunk = tag_nodes[start:start + 400]
            for r in conn.execute(f"SELECT node_id, heading FROM kf_nodes WHERE node_id IN ({store.placeholders(chunk)})", chunk):
                headings[r["node_id"]] = r["heading"]
    seen, unique_figures = set(), []
    for f in figures:
        if f["metric_norm"] not in seen:
            seen.add(f["metric_norm"])
            unique_figures.append(f)
    return {"year": year, "documents": documents,
            "sections": [{**t, "title": (by_doc.get(t["document_id"]) or {}).get("title"), "heading": headings.get(t["node_id"])}
                         for t in data["tags"]],
            "figures": unique_figures[:40], "events": events,
            "note": "documents you may see; each item opens its evidence"}


def _ranges(years: List[int]) -> List[str]:
    out, run = [], []
    for y in sorted(years):
        if run and y != run[-1] + 1:
            out.append(f"{run[0]}-{run[-1]}" if len(run) > 1 else str(run[0]))
            run = []
        run.append(y)
    if run:
        out.append(f"{run[0]}-{run[-1]}" if len(run) > 1 else str(run[0]))
    return out


def gaps_for(ctx: AccessContext, window: Optional[tuple]) -> Dict[str, Any]:
    """The coverage of one analysis window - used to state what an answer could not see."""
    if not window:
        return {}
    rep = report(ctx, max(FIRST_YEAR, window[0]) if window[0] >= FIRST_YEAR else window[0], window[1])
    return {"window": list(window),
            "available_years": [r["year"] for r in rep["years"] if r["status"] == "AVAILABLE"],
            "sample_only_years": [r["year"] for r in rep["years"] if r["status"] == "SAMPLE"],
            "missing_years": [r["year"] for r in rep["years"] if r["status"] in ("MISSING", "GENERAL")]}


_DECISION = re.compile(r"\b(?:board|management|committee)\s+(?:approved|decided|resolved|sanctioned)\b|"
                       r"\bdecision\s+(?:was\s+)?(?:taken|made)\b|\bit was (?:decided|resolved)\b", re.IGNORECASE)


def timeline(ctx: AccessContext, first: int = FIRST_YEAR, last: int = LAST_YEAR,
             include_samples: bool = True) -> Dict[str, Any]:
    with store.connection() as conn:
        access_sql, params = ctx.clause("d")
        origins = ("company", "sample", "demo") if include_samples else ("company",)
        origin_sql = f"d.origin IN ({store.placeholders(origins)})"
        docs = [dict(r) for r in conn.execute(
            f"SELECT d.document_id, d.title, d.document_type, d.year, d.origin, d.authority FROM kf_documents d "
            f"WHERE {access_sql} AND {origin_sql} AND d.year BETWEEN ? AND ? ORDER BY d.year",
            params + list(origins) + [first, last])]
        facts = [dict(r) for r in conn.execute(
            f"SELECT f.metric_norm, f.year, f.value, f.unit, f.document_id, f.node_id, d.origin, d.authority "
            f"FROM kf_facts f JOIN kf_documents d ON d.document_id = f.document_id "
            f"WHERE {access_sql} AND {origin_sql} AND f.year BETWEEN ? AND ? AND f.quarter IS NULL AND f.month IS NULL "
            f"AND f.entity_id IS NULL AND f.metric_norm IN ('net profit', 'revenue', 'net loss', 'production', 'crude throughput')",
            params + list(origins) + [first, last])]
        incidents = [dict(r) for r in conn.execute(
            f"SELECT e.entity_id, e.canonical_name, e.type, n.year, n.node_id, n.document_id, d.origin FROM kf_mentions m "
            f"JOIN kf_entities e ON e.entity_id = m.entity_id JOIN kf_nodes n ON n.node_id = m.node_id "
            f"JOIN kf_documents d ON d.document_id = n.document_id WHERE {access_sql} AND {origin_sql} "
            f"AND e.type IN ('INCIDENT', 'PROJECT') AND n.year BETWEEN ? AND ?", params + list(origins) + [first, last])]
        failures = [dict(r) for r in conn.execute(
            f"SELECT r.src, r.dst, r.relation, r.evidence_node, r.document_id, n.year, se.canonical_name AS src_name, "
            f"de.canonical_name AS dst_name, d.origin FROM kf_relations r JOIN kf_nodes n ON n.node_id = r.evidence_node "
            f"JOIN kf_documents d ON d.document_id = r.document_id JOIN kf_entities se ON se.entity_id = r.src "
            f"JOIN kf_entities de ON de.entity_id = r.dst WHERE {access_sql} AND {origin_sql} "
            f"AND r.relation = 'FAILED_DUE_TO' AND n.year BETWEEN ? AND ?", params + list(origins) + [first, last])]
        decisions = [dict(r) for r in conn.execute(
            f"SELECT n.node_id, n.document_id, n.year, n.text, d.origin FROM kf_nodes n "
            f"JOIN kf_documents d ON d.document_id = n.document_id WHERE {access_sql} AND {origin_sql} "
            f"AND n.kind = 'paragraph' AND n.year BETWEEN ? AND ? AND (n.text LIKE '%approved%' OR n.text LIKE '%decided%' "
            f"OR n.text LIKE '%resolved%' OR n.text LIKE '%decision%') LIMIT 400", params + list(origins) + [first, last])]

    years: Dict[int, Dict[str, Any]] = {}

    def slot(y):
        return years.setdefault(y, {"year": y, "documents": [], "financial": [], "incidents": [], "projects": [],
                                    "maintenance": [], "decisions": [], "origins": set()})

    for d in docs:
        s = slot(d["year"])
        s["documents"].append({"document_id": d["document_id"], "title": d["title"], "type": d["document_type"],
                               "origin": d["origin"]})
        s["origins"].add(d["origin"])
    series: Dict[str, Dict[int, Dict[str, Any]]] = {}
    for f in sorted(facts, key=lambda f: f["authority"] or 5):
        metric = "net profit" if f["metric_norm"] == "net loss" else f["metric_norm"]
        value = -abs(f["value"]) if f["metric_norm"] == "net loss" else f["value"]
        series.setdefault(metric, {}).setdefault(f["year"], {**f, "value": value})
    for metric, by_year in series.items():
        ordered = sorted(by_year.values(), key=lambda f: f["year"])
        for prev, cur in zip(ordered, ordered[1:]):
            if cur["year"] == prev["year"] + 1 and prev["value"]:
                pct = (cur["value"] - prev["value"]) / abs(prev["value"]) * 100
                if abs(pct) >= 10:
                    slot(cur["year"])["financial"].append({"metric": metric, "change_percent": round(pct, 1),
                                                           "from": prev["value"], "to": cur["value"], "unit": cur["unit"],
                                                           "node_id": cur["node_id"], "origin": cur["origin"]})
    for item in incidents:
        key = "incidents" if item["type"] == "INCIDENT" else "projects"
        entries = slot(item["year"])[key]
        if not any(e["entity_id"] == item["entity_id"] for e in entries):
            entries.append({"entity_id": item["entity_id"], "name": item["canonical_name"], "node_id": item["node_id"],
                            "origin": item["origin"]})
    for f in failures:
        slot(f["year"])["maintenance"].append({"event": f"{f['src_name']} failed due to {f['dst_name']}",
                                               "node_id": f["evidence_node"], "origin": f["origin"]})
    for d in decisions:
        match = _DECISION.search(d["text"] or "")
        if match:
            sentence = next((s for s in re.split(r"(?<=[.!?])\s+", d["text"]) if _DECISION.search(s)), d["text"][:200])
            slot(d["year"])["decisions"].append({"text": sentence.strip()[:240], "node_id": d["node_id"],
                                                 "origin": d["origin"]})
    out = []
    for y in sorted(years):
        entry = years[y]
        entry["origins"] = sorted(entry["origins"])
        out.append(entry)
    return {"range": [first, last], "years": out,
            "note": "every item links to the passage it came from; SAMPLE / DEMO items are labelled as such"}
