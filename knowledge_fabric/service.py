"""
ZEUS company intelligence: one question -> one verified AnalysisObject.

    QUERY -> UNDERSTANDING -> SEARCHING -> EVIDENCE FOUND -> ANALYZING
          -> VALIDATING -> GENERATING -> READY

Each stage is emitted when the work it names has actually happened, with the
real counts (evidence found, facts used, calculations made), so the interface
can show live progress without inventing any. Reasoning is summarised, never
streamed: the model's hidden thinking is not exposed.

Who does what:
    planner      what kind of question, which period, which sources
    retrieval    access-filtered hybrid search (documents, tables, entities, graph)
    Hermes       what the user and project remember - kept in its own section
    analytics    every number (the model never does arithmetic)
    reasoning    patterns, drivers (evidenced / correlated / hypothesis),
                 recommendations from the practice library, plan, KPIs, risks
    model        optional: writes the summary from the labelled context; its
                 text is kept only if every citation exists and every number
                 comes from the evidence or the calculations
    renderers    chat / DOCX / XLSX / PPTX / PDF from the SAME saved analysis

No evidence -> the answer says so and lists the gaps. Nothing is invented.
"""

from __future__ import annotations

import re
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from core.logger import get_logger
from knowledge_fabric import FIRST_YEAR, LAST_YEAR, analysis as analysis_mod, analytics, authority, context, contradictions, registers, \
    coverage, planner, reasoning, retrieval, store
from knowledge_fabric.access import AccessContext
from knowledge_fabric.analysis import AnalysisObject

logger = get_logger("knowledge_fabric.service")

STAGES = ("QUERY", "UNDERSTANDING", "SEARCHING", "EVIDENCE FOUND", "ANALYZING", "VALIDATING", "GENERATING", "READY")

# Metrics looked at as possible drivers when the question asks "why" or "what can we do".
DRIVER_METRICS = ("maintenance cost", "downtime", "crude cost", "energy intensity index", "interest cost",
                  "revenue", "crude throughput", "production", "operating cost", "capacity utilisation", "grm",
                  "net profit")
DECISION_INTENTS = {"ROOT_CAUSE", "RECOMMENDATION", "DECISION_SUPPORT"}
# What each verification state means, in the words shown to a person.
_STATE_WORDS = {"VERIFIED": "VERIFIED against company records",
                "VALIDATED": "VALIDATED against the evidence, which is not company records (sample / general / test)",
                "INSUFFICIENT_DATA": "INSUFFICIENT DATA - no record you can access answers it",
                "FAILED": "FAILED validation - a number or citation could not be traced"}
_FOLLOW_UP = re.compile(r"\b(?:this|that|the same|previous|above|last|your)\s+(?:analysis|answer|report|findings|results|data)\b|"
                        r"\bfrom (?:it|this|that)\b|\bof (?:it|this|that)\b|\bsame analysis\b", re.IGNORECASE)
_SCENARIO = re.compile(r"\b(reduce[sd]?|cut|lower(?:ed)?|decrease[sd]?|fall[s]?|drop[s]?|increase[sd]?|raise[sd]?|"
                       r"improve[sd]?|grow[s]?|rise[s]?)\b[^.%]{0,50}?(\d+(?:\.\d+)?)\s*(?:%|percent)", re.IGNORECASE)


# Which specialist agents (core/orchestration/agents.py) do the work of each stage.
STAGE_AGENTS = {
    "QUERY": ["analysis"],
    "UNDERSTANDING": ["temporal", "entity"],
    "SEARCHING": ["company_retrieval", "graph"],
    "EVIDENCE FOUND": ["company_retrieval"],
    "ANALYZING": ["financial_analysis"],
    "VALIDATING": ["validation", "decision_support", "recommendation"],
    "GENERATING": ["citation", "artifact"],
    "READY": ["analysis"],
}


class _Stages:
    def __init__(self, obj: AnalysisObject, callback: Optional[Callable[[Dict[str, Any]], None]]):
        self.obj, self.callback, self.started = obj, callback, time.time()

    def __call__(self, stage: str, detail: str, **data: Any) -> None:
        event = {"stage": stage, "detail": detail, "t": round(time.time() - self.started, 3),
                 "agents": STAGE_AGENTS.get(stage, []), **data}
        self.obj.stages.append(event)
        if self.callback:
            try:
                self.callback(event)
            except Exception as error:
                logger.debug(f"progress listener failed: {error}")


# ------------------------------------------------------------- helpers ------

def corpus_years(ctx: AccessContext) -> Tuple[Optional[int], Optional[int]]:
    """The years the caller's visible records cover - 'last five years' means the
    last five years of the records, not of the calendar."""
    if not ctx.authenticated:
        return None, None
    with store.connection() as conn:
        sql, params = ctx.clause("d")
        row = conn.execute(f"SELECT MIN(COALESCE(d.time_start, d.year)), MAX(COALESCE(d.time_end, d.year)) "
                           f"FROM kf_documents d WHERE {sql}", params).fetchone()
    return (row[0], row[1]) if row else (None, None)


# "Compare 2010-2015 with 2020-2025" names no measure: compare the headline figures
# the records actually hold for those years - never a measure with no data.
HEADLINE_METRICS = ("revenue", "net profit", "ebitda", "operating cost", "production", "crude throughput")


def headline_metrics(ctx: AccessContext, window: Optional[Sequence[int]], limit: int = 3) -> List[str]:
    if not window:
        return []
    with store.connection() as conn:
        sql, params = ctx.clause("d")
        rows = conn.execute(
            f"SELECT f.metric_norm, COUNT(DISTINCT f.year) AS years FROM kf_facts f "
            f"JOIN kf_documents d ON d.document_id = f.document_id WHERE ({sql}) "
            f"AND f.metric_norm IN ({store.placeholders(HEADLINE_METRICS)}) AND f.entity_id IS NULL AND f.entity_label IS NULL "
            f"AND f.month IS NULL AND f.quarter IS NULL AND f.year BETWEEN ? AND ? GROUP BY f.metric_norm",
            params + list(HEADLINE_METRICS) + [window[0], window[1]]).fetchall()
    found = {r[0]: r[1] for r in rows if r[1] >= 2}
    return [m for m in HEADLINE_METRICS if m in found][:limit]


def _fmt(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, (int, float)):
        text = f"{value:,.2f}"
        return text.rstrip("0").rstrip(".") if "." in text else text
    return str(value)


def _unit(unit: Optional[str]) -> str:
    return f" {unit}" if unit else ""


def _label(series: analytics.Series, facts: List[Dict[str, Any]]) -> str:
    if not series.entity:
        return "Company"
    name = next((f["entity_label"] for f in facts
                 if str(f.get("entity_id") or "").split("@")[0] == series.entity.split("@")[0] and f.get("entity_label")),
                series.entity.split("@")[0].split(":", 1)[-1])
    sites = {str(f["entity_id"]).split("@", 1)[1] for f in facts
             if str(f.get("entity_id") or "").startswith(series.entity.split("@")[0] + "@")}
    return f"{name} ({', '.join(sorted(sites))})" if len(sites) > 1 else name


def _entity_facts(facts: List[Dict[str, Any]], entity_id: str) -> List[Dict[str, Any]]:
    return [f for f in facts if f.get("entity_id") == entity_id or str(f.get("entity_id") or "").startswith(entity_id + "@")]


def _collapse(entity_ids: List[str]) -> List[str]:
    """UNIT:5 already covers UNIT:5@site - one series, not two copies."""
    bases = set(entity_ids)
    return [e for e in dict.fromkeys(entity_ids) if not ("@" in e and e.split("@")[0] in bases)]


def _build_series(facts: List[Dict[str, Any]], metrics: List[str], entity_ids: List[str]) -> List[analytics.Series]:
    out: List[analytics.Series] = []
    entity_ids = _collapse(entity_ids)
    for metric in metrics:
        matched = False
        for entity_id in entity_ids:
            series = analytics.build_series(_entity_facts(facts, entity_id), metric, entity_id)
            if series.points:
                out.append(series)
                matched = True
        if matched:
            continue
        # Company figures only: a row for a site or unit (even one not resolved to an
        # entity, e.g. site code 'PER') is never mixed into the company's series.
        company = [f for f in facts if not f.get("entity_id") and not f.get("entity_label")]
        series = analytics.build_series(company, metric) if company else None
        if series and series.points:
            out.append(series)
            continue
        by_entity: Dict[str, List[Dict[str, Any]]] = {}
        for fact in facts:
            if fact.get("entity_id"):
                by_entity.setdefault(fact["entity_id"], []).append(fact)
        for entity_id, group in by_entity.items():
            series = analytics.build_series(group, metric, entity_id)
            if series.points:
                out.append(series)
    return out


def _memory(question: str) -> Tuple[List[Dict[str, Any]], str, str]:
    """Hermes memory - its own section, its own ids [M#], never company evidence."""
    try:
        from memory import recall
        from security.injection import neutralise
        mem = recall.build(question)
    except Exception as error:
        return [], "", f"memory unavailable: {error}"
    if mem.status != "ok" or not mem.results:
        return [], "", mem.summary()
    used, lines = [], [recall.MEMORY_RULE]
    for index, result in enumerate(mem.results, 1):
        text = neutralise(result.text).replace("\n", " ").strip()
        used.append({"id": f"M{index}", "memory_id": result.memory_id, "category": result.category,
                     "recorded": time.strftime("%Y-%m-%d", time.localtime(result.timestamp)), "text": text[:600]})
        lines.append(f"[M{index}] ({result.category}, recorded {used[-1]['recorded']}) {text[:600]}")
    return used, "\n".join(lines), mem.summary()


def _scenario_changes(question: str) -> List[Tuple[Optional[str], str, float]]:
    """[(metric the change applies to, label, percent)] - the metric is the one
    named just before the change ("if maintenance cost falls 10%")."""
    out = []
    for m in _SCENARIO.finditer(question or ""):
        verb, number = m.group(1), m.group(2)
        sign = -1 if verb.lower().startswith(("reduc", "cut", "lower", "decreas", "fall", "drop")) else 1
        window = question[max(0, m.start() - 60):m.start() + len(verb)].lower()
        best, where = None, -1
        for phrase, metric in planner._METRIC_WORDS.items():
            position = max((x.start() for x in re.finditer(rf"\b{re.escape(phrase)}\b", window)), default=-1)
            if position > where or (position == where and position >= 0 and len(metric) > len(best or "")):
                best, where = metric, position
        out.append((best, f"{verb.lower()} {number}%", sign * float(number)))
    return out


_WORD_NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}


def _horizon(question: str) -> int:
    """'next three years' -> 3; default one year; never more than five."""
    m = re.search(r"\bnext\s+(\d+|one|two|three|four|five)\s+years?\b", (question or "").lower())
    if not m:
        return 1
    value = _WORD_NUMBERS.get(m.group(1)) or int(m.group(1))
    return max(1, min(5, value))


def _forecast_metrics(question: str, metrics: List[str]) -> List[str]:
    """The metric(s) named after 'forecast' / 'predict' / 'project'."""
    m = re.search(r"\b(?:forecast|predict|project(?:ion)?|extrapolate)\w*\b(.{0,80})", question or "", re.IGNORECASE)
    named = planner.metrics_in(m.group(1)) if m else []
    return [x for x in named if x in metrics] or metrics


def _profit_impact(cost: analytics.Series, profit: analytics.Series, label: str, percent: float) -> analytics.Calculation:
    """If a cost changes by x% and nothing else changes, profit moves by the
    opposite amount. Arithmetic on an assumption, labelled as such."""
    base_cost, base_profit = cost.points[-1], profit.points[-1]
    delta = base_cost.value * percent / 100.0
    return analytics.Calculation(
        f"net profit if {cost.metric} {label} (base {base_cost.year}, all else equal)",
        round(base_profit.value - delta, 4), profit.unit,
        f"net profit {base_profit.year} - ({cost.metric} {base_cost.year} x {percent:+g}%)",
        [base_cost.ref(), base_profit.ref()],
        ["assumes every other cost and revenue stays at its base-year level - not a forecast"])


def _register_answer(question: str, ctx: AccessContext, result, emit) -> Optional["registers.RegisterAnswer"]:
    """Records from a register the question names ('which leases expire in the
    next 90 days', 'total rent of active leases'). The rows become the first
    evidence (E1...), cited one by one; the passages found by search follow."""
    try:
        found = registers.answer(question, ctx)
    except Exception as error:                      # a register must never break the answer
        logger.warning(f"register query failed: {error}")
        return None
    if found is None:
        return None
    reg = found.register
    wanted = found.records[:registers.MAX_SHOWN]
    keys = {r["record_key"] for r in wanted}
    wanted += [r for r in _finding_records(found) if r["record_key"] not in keys][:10]
    items = []
    for index, rec in enumerate(wanted, 1):
        rec["evidence_id"] = f"E{index}"
        items.append(retrieval.EvidenceItem(
            evidence_id=f"E{index}", node_id=rec.get("node_id") or "", document_id=reg["document_id"],
            title=reg.get("title") or reg.get("filename") or "", text=registers.record_text(list(rec["data"]), list(rec["data"].values())),
            filename=reg.get("filename") or "", section_path=reg["name"], heading=rec["record_key"],
            year=None, document_type=reg.get("document_type") or "", status=reg.get("status") or "",
            authority=int(reg.get("authority") or 5), authority_label=authority.describe(int(reg.get("authority") or 5)),
            classification=reg.get("confidentiality") or "internal", origin=reg.get("origin") or "company",
            kind="record", source_hash=reg.get("source_hash") or "", score=1.0, legs=["register"]))
    by_key = {rec["record_key"]: rec["evidence_id"] for rec in wanted}
    for finding in found.findings:
        finding["evidence_id"] = by_key.get(finding["record_key"])
    for rec in found.records:
        rec.setdefault("evidence_id", by_key.get(rec["record_key"]))
    # Supporting passages only when they are about the same records ('lease' for the
    # lease register) - a general passage beside a table of rows only dilutes it.
    subject = registers._name_tokens(reg)
    passages = [e for e in result.evidence if e.node_id not in {i.node_id for i in items}
                and (e.document_id == reg["document_id"] or subject & set(registers.tokens(f"{e.title} {e.text[:600]}")))][:3]
    for index, item in enumerate(passages, len(items) + 1):
        item.evidence_id = f"E{index}"
    result.evidence = items + passages
    emit("SEARCHING", f"register {reg['name']}: {len(found.records)} matching records"
         + (f", {len(found.findings)} data-quality findings" if found.findings else ""))
    return found


def _finding_records(found) -> List[Dict[str, Any]]:
    wanted = {f["record_key"] for f in found.findings}
    if not wanted:
        return []
    with store.connection() as conn:
        return [r for r in registers._load_records(conn, found.register["register_id"]) if r["record_key"] in wanted]


def _add(calcs: List[Dict[str, Any]], calc: analytics.Calculation, label: str = "") -> Optional[str]:
    data = calc.to_dict()
    if label and label != "Company":
        data["name"] = f"{label}: {data['name']}"
    data["id"] = f"C{len(calcs) + 1}"
    calcs.append(data)
    return data["id"]


# ------------------------------------------------------------- pipeline -----

def analyze(question: str, ctx: Optional[AccessContext] = None, use_model: bool = True,
            progress: Optional[Callable[[Dict[str, Any]], None]] = None, top_k: int = 12,
            filters: Optional[Dict[str, Any]] = None, use_memory: bool = True, use_legacy: bool = True,
            make_artifacts: bool = True, reuse_previous: bool = True) -> AnalysisObject:
    if ctx is None:
        from knowledge_fabric.access import current
        ctx = current()
    question = (question or "").strip()
    obj = analysis_mod.new(question, ctx.username or "", {})
    emit = _Stages(obj, progress)
    emit("QUERY", "question received")

    if not ctx.authenticated:
        obj.answer = "Sign in first - no records are searched without an authenticated session."
        obj.summary = obj.answer
        obj.validation = {"ok": False, "reason": "not authenticated"}
        emit("READY", "refused: not signed in", refused=True)
        return obj

    earliest, latest = corpus_years(ctx)
    plan = planner.plan(question, latest_year=latest, earliest_year=earliest)
    compared = plan.scope.get("periods") or ([] if len(plan.scope.get("points") or []) < 2 else plan.scope["points"])
    if not plan.metrics and "COMPARISON" in plan.intents and compared:
        plan.metrics = headline_metrics(ctx, plan.scope.get("window"))
        if plan.metrics:
            plan.notes.append("no measure named - the headline figures the records hold are compared: "
                              + ", ".join(plan.metrics))
            if "structured" not in plan.sources:
                plan.sources.insert(-1, "structured")
    obj.plan = plan.to_dict()
    emit("UNDERSTANDING", f"{plan.primary.replace('_', ' ').lower()} question"
         + (f", period {plan.scope.get('start')}-{plan.scope.get('end')}" if plan.scope.get("start") else "")
         + (f", measures: {', '.join(plan.metrics)}" if plan.metrics else ""),
         intents=plan.intents, complexity=plan.complexity, formats=plan.formats)

    # "Now create a presentation from this analysis" - render the saved one, do not re-ask.
    if reuse_previous and plan.formats and "ARTIFACT_GENERATION" in plan.intents and \
            (_FOLLOW_UP.search(question) or (not plan.metrics and not plan.scope.get("start"))):
        previous = analysis_mod.latest(ctx.username or "")
        if previous is not None:
            emit("GENERATING", f"rendering the saved analysis {previous.analysis_id} as {', '.join(plan.formats)}")
            if make_artifacts:
                from knowledge_fabric import renderers
                for fmt in plan.formats:
                    previous.artifacts.append(renderers.create(previous, fmt))
                analysis_mod.save(previous)
            previous.stages = obj.stages
            emit("READY", "artifacts created from the saved analysis", reused=previous.analysis_id)
            return previous

    emit("SEARCHING", "searching " + ", ".join(plan.sources))
    result = retrieval.retrieve(question, ctx, obj.plan, top_k=top_k, filters=filters, use_legacy=use_legacy)
    record_answer = _register_answer(question, ctx, result, emit)
    obj.evidence = [{**e.to_dict(), "citation": e.citation()} for e in result.evidence]
    obj.retrieval = result.diagnostics
    entity_ids = retrieval.subject_entities(result.entities, plan.metrics)
    obj.plan["entities"] = entity_ids
    facts = list(result.facts)
    edges = list(result.graph_edges)[:analysis_mod.GRAPH_IDS]
    emit("EVIDENCE FOUND", f"{len(obj.evidence)} passages, {len(facts)} table facts, {len(edges)} relations",
         evidence=len(obj.evidence), facts=len(facts), relations=len(edges),
         origins=sorted({e["origin"] for e in obj.evidence}))

    memory_block = ""
    if use_memory:
        obj.memory_used, memory_block, memory_note = _memory(question)
        obj.retrieval["memory"] = memory_note

    # ---- ANALYZING ------------------------------------------------------------
    emit("ANALYZING", "calculating from the table facts" if facts else "no table facts - text evidence only")
    calcs: List[Dict[str, Any]] = []
    if record_answer is not None:
        for calc in record_answer.calculations:
            calc["id"] = f"C{len(calcs) + 1}"
            calcs.append(calc)
    series_list = _build_series(facts, plan.metrics, entity_ids) if record_answer is None else []
    full_describe = 3
    for index, series in enumerate(series_list):
        label = _label(series, facts)
        described = analytics.describe(series)
        for calc in described:
            if calc.name == "years with data":
                calc.name = f"years with {series.metric} data"
        for calc in (described if index < full_describe else described[:3]):
            _add(calcs, calc, label)
        points = plan.scope.get("points") or []
        if "COMPARISON" in plan.intents and len(points) >= 2:
            _add(calcs, analytics.change(series, int(points[0]), int(points[-1])), label)
        if len(plan.scope.get("periods") or []) >= 2:
            _add(calcs, analytics.compare_periods(series, plan.scope["periods"]), label)
        if "rolling" in question.lower():
            _add(calcs, analytics.rolling_average(series), label)
    target = series_list[0] if series_list else analytics.Series(plan.metrics[0] if plan.metrics else "", None, None)
    if series_list and "FORECAST" in plan.intents:
        wanted = _forecast_metrics(question, plan.metrics)
        for series in [s for s in series_list if s.metric in wanted][:3] or series_list[:1]:
            _add(calcs, analytics.extrapolate(series, _horizon(question)), _label(series, facts))
    if series_list and "SCENARIO" in plan.intents:
        changes = _scenario_changes(question)
        if not changes:
            obj.assumptions.append("no percentage change was named - say e.g. 'if maintenance cost falls 10%' to model a scenario")
        for metric, label, percent in changes:
            series = next((s for s in series_list if s.metric == metric), target)
            _add(calcs, analytics.scenario(series, {label: percent}), _label(series, facts))
            profit = next((s for s in series_list if s.metric == "net profit" and s.entity == series.entity), None)
            if profit is not None and "cost" in series.metric and series.points and profit.points \
                    and series.points[-1].year == profit.points[-1].year and (series.unit or "") == (profit.unit or ""):
                _add(calcs, _profit_impact(series, profit, label, percent), _label(series, facts))
    if plan.metrics and (len({s.entity for s in series_list if s.entity}) > 1
                         or set(plan.intents) & {"COMPARISON", "NUMERICAL"}):
        for metric in plan.metrics[:2]:
            ranking = analytics.rank_entities(facts, metric)
            if ranking.ok and len(ranking.value) > 1:
                named = registers.names_for_codes(ctx, [r["entity"] for r in ranking.value])
                for row in ranking.value:
                    if named.get(row["entity"]):
                        row["name"] = named[row["entity"]]
                _add(calcs, ranking)
                company = next((s for s in series_list if s.metric == metric and not s.entity), None)
                if company is not None:
                    _add(calcs, analytics.parts_vs_whole(ranking, company))

    driver_series: List[analytics.Series] = []
    explains = bool(set(plan.intents) & (DECISION_INTENTS | {"TREND", "HISTORICAL", "COMPARISON"}) or plan.formats)
    if set(plan.intents) & DECISION_INTENTS or plan.formats:
        others = [m for m in DRIVER_METRICS if m not in plan.metrics]
        window = tuple(plan.scope["window"]) if plan.scope.get("window") else None
        with store.connection() as conn:
            extra = retrieval.structured_facts(conn, ctx, others, window)
        known = {f.get("fact_id") for f in facts}
        facts += [f for f in extra if f.get("fact_id") not in known]
        driver_series = [s for s in _build_series(extra, others, []) if not s.entity]
    found_drivers, correlation_calcs = reasoning.drivers(target, driver_series, result.evidence, edges)         if explains else ([], [])
    by_name = {}
    for calc in correlation_calcs:
        data = {**calc, "id": f"C{len(calcs) + 1}"}
        calcs.append(data)
        by_name[calc["name"]] = data["id"]
    for driver in found_drivers:
        if driver.get("calculation") in by_name:
            driver["calculation_id"] = by_name[driver["calculation"]]
    obj.calculations = calcs
    obj.series = [{**s.to_dict(), "label": _label(s, facts)} for s in series_list]
    obj.facts = [{**f, "id": f"F{i}"} if i <= analysis_mod.FACT_IDS else dict(f) for i, f in enumerate(facts, 1)]
    obj.graph_edges = [{**e, "id": f"G{i}"} for i, e in enumerate(edges, 1)]
    obj.drivers = found_drivers
    obj.patterns = [p for s in series_list[:3] for p in reasoning.patterns(s)]

    # ---- VALIDATING -------------------------------------------------------------
    emit("VALIDATING", "checking conflicts between sources and coverage of the period")
    conflicts = contradictions.numeric_conflicts(facts)
    # Stated figures against ZEUS's own sums of daily rows are a reconciliation, not two
    # documents disagreeing - one line per metric instead of one per month.
    obj.contradictions = ([c for c in conflicts if c["type"] != "reconciliation"]
                          + contradictions.summarise_reconciliations([c for c in conflicts if c["type"] == "reconciliation"])
                          + contradictions.statement_differences(edges))
    obj.contradictions = contradictions.statements_vs_figures(obj.evidence, obj.series, obj.facts) + obj.contradictions
    if record_answer is not None:
        obj.contradictions = record_answer.findings + obj.contradictions
    for conflict in obj.contradictions:
        conflict.setdefault("line", contradictions.line(conflict))     # every kind readable in the UI
    if record_answer is not None:
        obj.records = record_answer.to_dict()
        obj.records["answer_md"] = registers.render(record_answer, [c["id"] for c in record_answer.calculations])
    window = tuple(plan.scope["window"]) if plan.scope.get("window") else None
    if not window and series_list:
        years = [y for s in series_list for y in s.years]
        window = (min(years), max(years))
    if not window and set(plan.intents) & {"HISTORICAL", "TREND"}:
        window = (FIRST_YEAR, LAST_YEAR)
    obj.gaps = coverage.gaps_for(ctx, window) if window else {}
    origins = {e["origin"] for e in obj.evidence}
    if obj.evidence and origins <= {"sample", "demo"}:
        obj.gaps["origin_warning"] = ("every passage found is SAMPLE or DEMO data - it illustrates the method "
                                      "and is not this company's history")
    elif obj.evidence and origins <= {"sample", "demo", "general_knowledge"}:
        obj.gaps["origin_warning"] = "no company records matched - the passages are general industry knowledge or sample data"
    notes = list(plan.notes)
    for s in series_list:
        notes += s.notes
    for c in calcs:
        notes += [n for n in c.get("notes", []) if not n.startswith("correlation is not causation")]
    obj.assumptions = list(dict.fromkeys(obj.assumptions + notes))

    if set(plan.intents) & DECISION_INTENTS or plan.formats:
        obj.recommendations = reasoning.recommendations(found_drivers, obj.gaps, target.metric)
        obj.options = reasoning.options_and_tradeoffs(obj.recommendations)
        obj.implementation_plan = reasoning.implementation_plan(obj.recommendations)
        obj.kpis = reasoning.kpi_table(obj.recommendations, series_list + driver_series)
        obj.risks = reasoning.risks(obj.recommendations, obj.gaps, len(obj.contradictions))

    # ---- GENERATING ------------------------------------------------------------
    emit("GENERATING", "writing the answer")
    deterministic, obj.summary = compose(obj)
    obj.answer = deterministic
    obj.narrative_source = "deterministic"
    if use_model and (obj.evidence or obj.facts) and not obj.records:
        narrative, note = _narrative(obj, result, memory_block, ctx)
        if narrative:
            obj.answer = narrative.strip() + "\n\n---\n\n" + detail_sections(obj)
            obj.narrative_source = "model (validated)"
        obj.retrieval["narrative"] = note
    else:
        obj.retrieval["context_injection"] = {
            "model_called": False, "evidence_retrieved": len(obj.evidence),
            "reason": ("the model was not asked (use_model off)" if not use_model else
                       "retrieval returned zero chunks - nothing to give the model" if not (obj.evidence or obj.facts) else
                       "a register answer: the engine's own count and rows are the answer")}
    obj.validation = analysis_mod.validate_answer(obj, obj.answer)
    if not obj.validation["ok"] and obj.narrative_source != "deterministic":
        obj.answer, obj.narrative_source = deterministic, "deterministic"
        obj.validation = analysis_mod.validate_answer(obj, obj.answer)
    analysis_mod.save(obj)

    if make_artifacts and plan.formats:
        from knowledge_fabric import renderers
        for fmt in plan.formats:
            obj.artifacts.append(renderers.create(obj, fmt))
        analysis_mod.save(obj)
    _audit(obj)
    state = obj.verification_state
    emit("READY", "answer ready - " + _STATE_WORDS.get(state, state), verified=obj.verified, verification_state=state,
         artifacts=[a.get("path") for a in obj.artifacts if a.get("ok")])
    if rag_debug_enabled():
        try:
            obj.retrieval["trace_file"] = write_trace(obj, result)
        except Exception as error:
            logger.warning(f"RAG trace not written: {error}")
    else:
        _model_exchange.pop(obj.analysis_id, None)
    analysis_mod.save(obj)
    return obj


def _audit(obj: AnalysisObject) -> None:
    if store._path_override.get() is not None:
        return                                  # an evaluation run on its own test database, not a user analysis
    try:
        from security.audit_chain import audit_chain
        audit_chain.append("company_intelligence",
                           f"analysis {obj.analysis_id}: intents={','.join(obj.plan.get('intents', []))} "
                           f"evidence={len(obj.evidence)} facts={len(obj.facts)} calculations={len(obj.calculations)} "
                           f"verified={obj.verified} state={obj.verification_state} artifacts={len(obj.artifacts)}",
                           actor=obj.username or "")
    except Exception as error:
        logger.debug(f"analysis not audited: {error}")


# --------------------------------------------------------------- answer -----

def _calc_by_name(obj: AnalysisObject, fragment: str, label: str = "") -> Optional[Dict[str, Any]]:
    for calc in obj.calculations:
        name = calc["name"]
        if fragment in name and (not label or name.startswith(label + ":") or label == "Company") and calc.get("ok"):
            if label == "Company" and ":" in name.split(fragment)[0]:
                continue
            return calc
    return None


_ACRONYMS = {"ebitda": "EBITDA", "ebit": "EBIT", "pbt": "PBT", "grm": "GRM", "pat proxy": "PAT"}


def _series_sentences(obj: AnalysisObject, series: Dict[str, Any]) -> List[str]:
    label, metric, unit = series["label"], series["metric"], series.get("unit")
    shown = _ACRONYMS.get(metric, metric)
    who = f"{label} {shown}" if label != "Company" else shown[:1].upper() + shown[1:]
    lines = []
    points = series["points"]
    count = _calc_by_name(obj, f"years with {metric} data", label)
    first, last = points[0]["year"], points[-1]["year"]
    periods = _calc_by_name(obj, f"periods of {metric}", label)
    if periods is None and len(obj.plan.get("scope", {}).get("periods") or []) >= 2:
        want = f"periods of {metric}" if label == "Company" else f"{label}: periods of {metric}"
        periods = next((c for c in obj.calculations if c["name"] == want), None)   # kept even when a period is empty
    if periods is not None:
        # Two (or more) periods, each read whole - the comparison IS the answer.
        rows = []
        for row in periods["value"]["periods"]:
            if not row["years"]:
                rows.append(f"{row['period']}: no figures in the records")
                continue
            text = (f"{row['period']}: average {_fmt(row['average'])}{_unit(unit)} a year, total "
                    f"{_fmt(row['total'])}{_unit(unit)} over {row['years']} year(s)")
            if row["years"] < row["expected"]:
                text += f" (of {row['expected']})"
            if row.get("cagr") is not None:
                text += f", CAGR {_fmt(row['cagr'])}%"
            rows.append(text)
        sentence = f"{who} - " + "; ".join(rows)
        moved = periods["value"].get("average_change")
        if moved and moved.get("percent") is not None:
            p_first, p_last = periods["value"]["periods"][0]["period"], periods["value"]["periods"][-1]["period"]
            word = "rose" if moved["absolute"] >= 0 else "fell"
            sentence += (f". The yearly average {word} {_fmt(abs(moved['percent']))}% "
                         f"({_fmt(abs(moved['absolute']))}{_unit(unit)}) from {p_first} to {p_last}")
        return [sentence + f" [{periods['id']}]."]
    asked = [int(y) for y in (obj.plan.get("scope", {}).get("points") or [])]
    if asked:
        stated = []
        for year in asked:
            p = next((p for p in points if p["year"] == year), None)
            fact = next((f for f in obj.facts if p and f.get("fact_id") == p["fact_id"] and f.get("id")), None)
            stated.append(f"{year}: {_fmt(p['value'])}{_unit(unit)}" + (f" [{fact['id']}]" if fact else "")
                          if p else f"{year}: no figure in the records")
        lines.append(f"{who} - " + "; ".join(stated) + ".")
    if len(points) == 1 and not asked:
        p = points[0]
        fact = next((f for f in obj.facts if f.get("fact_id") == p["fact_id"] and f.get("id")), None)
        return [f"{who} in {p['year']}: {_fmt(p['value'])}{_unit(unit)}" + (f" [{fact['id']}]" if fact else "") + "."]
    question = (obj.question or "").lower()
    losses = _calc_by_name(obj, f"loss years in {metric}", label)
    asks_loss = bool(re.search(r"\blos(?:s|ses|t|ing)\b|\bdeficit\b|\bin the red\b|\bnegative\b", question))
    if losses and asks_loss:
        if not losses["value"]:
            lines.append(f"No loss is recorded in {first}-{last}: {shown if label == 'Company' else who} was positive in all "
                         f"{len(points)} years with figures [{losses['id']}].")
        else:
            lines.append(f"{who} was a loss in {len(losses['value'])} year(s): "
                         + "; ".join(f"{r['year']}: {_fmt(r['value'])}{_unit(unit)}" for r in losses["value"])
                         + f" [{losses['id']}].")
    falls = _calc_by_name(obj, f"declines in {metric}", label)
    if falls and falls["value"] and (asks_loss or re.search(r"\b(?:fall|fell|drop|dropped|decline|declined|decrease|"
                                                         r"why|dip|down|worse|losses?)\b", question)):
        lines.append(f"{who} fell in {len(falls['value'])} year(s): "
                     + "; ".join(f"{r['year']}: {_fmt(r['from'])} to {_fmt(r['to'])}{_unit(unit)}"
                                 + (f" ({_fmt(r['percent'])}%)" if r.get("percent") is not None else "")
                                 for r in falls["value"][:6])
                     + f" [{falls['id']}].")
    lines.append(f"{who}: {len(points)} year(s) of figures between {first} and {last}"
                 + (f" [{count['id']}]" if count else "") + ".")
    low, high = _calc_by_name(obj, f"minimum {metric}", label), _calc_by_name(obj, f"maximum {metric}", label)
    if low and high and len(points) > 1:
        lines.append(f"Lowest {_fmt(low['value'])}{_unit(unit)} ({low['inputs'][0]['year']}) [{low['id']}], "
                     f"highest {_fmt(high['value'])}{_unit(unit)} ({high['inputs'][0]['year']}) [{high['id']}].")
    elif len(points) == 1:
        p = points[0]
        fact = next((f for f in obj.facts if f.get("fact_id") == p["fact_id"] and f.get("id")), None)
        lines.append(f"{p['year']}: {_fmt(p['value'])}{_unit(unit)}" + (f" [{fact['id']}]" if fact else "") + ".")
    change = _calc_by_name(obj, f"change in {metric} {first}-{last}", label)
    if change and change["value"].get("percent") is not None:
        lines.append(f"From {first} to {last} it changed by {_fmt(change['value']['absolute'])}{_unit(unit)} "
                     f"({_fmt(change['value']['percent'])}%) [{change['id']}].")
    cagr = _calc_by_name(obj, f"CAGR of {metric}", label)
    if cagr:
        lines.append(f"Compound annual growth {_fmt(cagr['value'])}% [{cagr['id']}].")
    trend = _calc_by_name(obj, f"trend of {metric}", label)
    if trend:
        fit = trend["value"]
        lines.append(f"Trend: {fit['direction']} by about {_fmt(abs(fit['slope_per_year']))}{_unit(unit)} a year "
                     f"(R² {_fmt(fit['r2'])}{', a weak fit' if fit['r2'] < 0.5 else ''}) [{trend['id']}].")
    return lines


def compose(obj: AnalysisObject) -> Tuple[str, str]:
    """The deterministic answer - every number from a calculation or a fact,
    every statement with its id. Returns (answer markdown, one-line summary)."""
    parts: List[str] = []
    if obj.records.get("answer_md"):
        # A register question: the records and the engine's counts ARE the answer.
        answer_md = obj.records["answer_md"]
        summary = re.sub(r"\*\*|\s*\[[A-Z]\d+\]", "", answer_md.split("\n", 1)[0]).strip()
        parts.append("### Answer\n" + answer_md)
        if obj.gaps.get("origin_warning"):
            parts.append(f"> **Data origin:** {obj.gaps['origin_warning']}.")
        parts.append(detail_sections(obj))
        return "\n\n".join(p for p in parts if p), summary
    if not obj.evidence and not obj.facts:
        summary = "No document you can access answers this question."
        parts.append(f"**{summary}**")
        if obj.gaps.get("missing_years"):
            parts.append("Records missing for: " + ", ".join(coverage._ranges(obj.gaps["missing_years"])) + ".")
        parts.append("Nothing has been estimated or invented. Add the relevant documents (RAG INGEST) and ask again.")
        return "\n\n".join(parts), summary

    findings: List[str] = []
    for series in obj.series[:4]:
        findings += _series_sentences(obj, series)
    ranking = next((c for c in obj.calculations if c["name"].endswith("by entity") or " by entity (" in c["name"]), None)
    if ranking and ranking.get("ok"):
        top = ranking["value"][0]
        years = sorted({i.get("year") for i in ranking.get("inputs", []) if i.get("year")})
        span = f" ({', '.join(coverage._ranges(years))})" if years else ""     # '1990-2003, 2024-2026' - never a false span
        shown = lambda row: f"{row['entity']} ({row['name']})" if row.get("name") else row["entity"]   # noqa: E731
        sentence = (f"Highest total {ranking['name'].split(' by entity')[0]}{span}: {shown(top)} with "
                    f"{_fmt(top['total'])}{_unit(top.get('unit'))}"
                    + "".join(f"{', then' if i == 1 else ','} {shown(row)} with {_fmt(row['total'])}"
                              for i, row in enumerate(ranking["value"][1:4], 1))
                    + f" [{ranking['id']}].")
        whole = next((c for c in obj.calculations if c["name"].endswith(": entities vs company") and c.get("ok")
                      and c["name"].startswith(ranking["name"].split(" by entity")[0])), None)
        if whole:
            w = whole["value"]
            source = "sums of the daily ledger rows" if w.get("entities_from_daily_rows") else "the per-entity figures"
            if w.get("percent") is not None and abs(w["percent"]) >= 1:
                sentence += (f" These are {source}; together they come to {_fmt(w['entities_total'])}{_unit(whole.get('unit'))}, "
                             f"{_fmt(abs(w['percent']))}% {'below' if w['difference'] < 0 else 'above'} the stated company "
                             f"figure of {_fmt(w['company_total'])}{_unit(whole.get('unit'))} for the same "
                             f"{len(w['years'])} year(s) [{whole['id']}].")
            else:
                sentence += f" These are {source}; they add up to the stated company figure [{whole['id']}]."
        if re.search(r"\bwhich\b.*\b(?:highest|lowest|most|least|largest|smallest|biggest|top|worst|best)\b",
                     obj.question, re.IGNORECASE):
            findings.insert(0, sentence)       # the ranking IS the answer
        elif re.search(r"\b(?:which|sites?|units?|plants?|regions?|branch(?:es)?|locations?|facilit(?:y|ies)|"
                       r"departments?|by (?:site|unit|plant|region|location))\b", obj.question, re.IGNORECASE):
            findings.append(sentence)          # asked about the parts, not only the company
    for calc in obj.calculations:
        name = calc["name"]
        title = name[:1].upper() + name[1:]
        if any(key in name for key in ("linear extrapolation", "scenarios for")):
            if calc.get("ok") and calc.get("value"):
                rows = "; ".join(f"{r.get('year') or r.get('scenario')}: {_fmt(r['value'])}" for r in calc["value"])
                findings.append(f"{title}: {rows}{_unit(calc.get('unit'))} [{calc['id']}] - "
                                + "; ".join(calc.get("notes", [])[:1]) + ".")
            elif calc.get("notes"):
                findings.append(f"{title}: {calc['notes'][0]}.")
        elif name.startswith("net profit if ") or ": net profit if " in name:
            findings.append(f"{title}: {_fmt(calc['value'])}{_unit(calc.get('unit'))} [{calc['id']}] - "
                            f"{calc['notes'][0]}.")

    primary = obj.plan.get("primary")
    causes = [e for e in obj.graph_edges if e.get("relation") in ("FAILED_DUE_TO", "CAUSED")]
    if causes and primary in ("ROOT_CAUSE", "MULTI_HOP", "RECOMMENDATION", "DECISION_SUPPORT"):
        stated: Dict[tuple, List[str]] = {}
        for e in causes:
            verb = "failed due to" if e["relation"] == "FAILED_DUE_TO" else "caused"
            stated.setdefault((e["src_name"], verb, e["dst_name"]), []).append(e["id"])
        lines = [f"{s} {v} {d} " + "".join(f"[{i}]" for i in ids) for (s, v, d), ids in list(stated.items())[:5]]
        findings.insert(0, "The records state: " + "; ".join(lines) + ".")
    # Dated events in the period asked about ('2020-04-01 COVID continuity protocol')
    # - what the records say HAPPENED, cited; the reader judges any link to the figures.
    window = obj.plan.get("scope", {}).get("window") or []
    spans = obj.plan.get("scope", {}).get("periods") or ([window] if window else [])
    events = [e for e in obj.evidence if e.get("kind") == "event"
              and (not spans or (e.get("year") and any(a <= e["year"] <= b for a, b in spans)))]
    if events and (obj.series or re.search(r"\b(?:why|when|happen\w*|cause\w*|events?|milestones?)\b",
                                           obj.question or "", re.IGNORECASE)):
        disputed = {c.get("evidence_id") for c in obj.contradictions if c.get("type") == "statement"}
        findings.append("Recorded events in the period: "
                        + "; ".join(f"{_event_label(e['text'])} [{e['evidence_id']}]"
                                    + (" - disagrees with the recorded figures, see Conflicts" if e["evidence_id"] in disputed else "")
                                    for e in events[:6]) + ".")
    if not findings or not obj.series:
        # A text question: quote what the best records say, with their ids - prose,
        # not raw table dumps, and not a far weaker passage just to fill a third slot.
        prose = [e for e in obj.evidence if e.get("kind") not in ("table", "row_range")]
        best = max((float(e.get("score") or 0) for e in prose), default=0.0)
        prose = [e for i, e in enumerate(prose) if i == 0 or not best or float(e.get("score") or 0) >= 0.5 * best]
        for e in prose[:2 if findings else 3]:
            warning = " (contains instruction-like text - quoted as data, not followed)" \
                if any(f.startswith("instruction-like") for f in e.get("flags", [])) else ""
            findings.append(f"{e['citation']} [{e['evidence_id']}]{warning}: \"{_snippet(e['text'], 300)}\"")

    summary = findings[0] if findings else f"The most relevant record is {obj.evidence[0]['citation']}."
    parts.append("### Answer\n" + "\n\n".join(findings) if not obj.series else "### Answer\n" + " ".join(findings))
    if obj.gaps.get("origin_warning"):
        parts.append(f"> **Data origin:** {obj.gaps['origin_warning']}.")
    parts.append(detail_sections(obj))
    return "\n\n".join(p for p in parts if p), summary


def _event_label(text: str) -> str:
    return registers.event_label(text)


def _snippet(text: str, limit: int = 220) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + " ..."


def detail_sections(obj: AnalysisObject) -> str:
    parts: List[str] = []
    if obj.evidence:
        rows = [f"- [{e['evidence_id']}] {e['citation']} - {e['authority_label']}"
                + (f" - **{e['origin'].upper()}**" if e["origin"] in ("sample", "demo") else "")
                + f"\n  \"{_snippet(e['text'])}\"" for e in obj.evidence[:6]]
        parts.append("### Evidence\n" + "\n".join(rows))
    if obj.patterns:
        parts.append("### Patterns\n" + "\n".join(f"- {p['detail']}" for p in obj.patterns[:6]))
    if obj.drivers:
        rows = []
        for d in obj.drivers[:8]:
            ids = "".join(f"[{i}]" for i in d.get("evidence_ids", []))
            if d.get("calculation_id"):
                ids += f"[{d['calculation_id']}]"
            kind = {"evidenced": "stated in the records", "correlated": "correlation - not proof of cause",
                    "hypothesis": "hypothesis"}.get(d["kind"], d["kind"])
            rows.append(f"- **{d['driver']}** ({kind}) - {d['statement']} {ids}".rstrip())
        parts.append("### Possible drivers\n" + "\n".join(rows))
    if obj.contradictions:
        rows = ["- " + contradictions.line(c) for c in obj.contradictions[:5]]
        if len(obj.contradictions) > 5:
            rows.append(f"- ... {len(obj.contradictions)} in total - the rest are in the analysis (Company Intelligence > Analyses)")
        parts.append("### Conflicts between sources\n" + "\n".join(rows))
    if obj.recommendations:
        rows = []
        for r in obj.recommendations[:8]:
            ids = "".join(f"[{i}]" for i in r.get("evidence_ids", []))
            rows.append(f"{r['id']}. **{r['action']}** - owner: {r['owner']}; timeline: {r['timeline']}; "
                        f"KPI: {r['kpi']}; confidence: {r['confidence']}. Addresses: {r['addresses']} {ids}".rstrip())
        parts.append("### Recommended actions\n" + "\n".join(rows)
                     + "\n\n_Actions come from the practice library and are matched to the drivers above; "
                       "their impact must be quantified from the data listed as required._")
    gaps = []
    if obj.gaps.get("missing_years"):
        gaps.append("no records for " + ", ".join(coverage._ranges(obj.gaps["missing_years"])))
    if obj.gaps.get("sample_only_years"):
        gaps.append("only sample data for " + ", ".join(coverage._ranges(obj.gaps["sample_only_years"])))
    if gaps:
        parts.append("### Data gaps\n" + "; ".join(gaps) + ".")
    if obj.assumptions:
        parts.append("### Assumptions and limits\n" + "\n".join(f"- {a}" for a in obj.assumptions[:8]))
    return "\n\n".join(parts)


# ------------------------------------------------------------- RAG trace ---
# Development only. The full trace holds document text and the exact prompt, so it
# is OFF unless AGNI_RAG_DEBUG=1, config.ini [knowledge] rag_debug = true, or the
# `python -m agni_ops rag trace` command asks for it. It is written to
# logs/rag_trace/<analysis>.json and never into the saved analysis.
from contextvars import ContextVar as _ContextVar

_trace_forced: "_ContextVar[bool]" = _ContextVar("agni_rag_trace_forced", default=False)
_model_exchange: Dict[str, Dict[str, Any]] = {}


def rag_debug_enabled() -> bool:
    import os
    if _trace_forced.get() or os.environ.get("AGNI_RAG_DEBUG") == "1":
        return True
    try:
        import configparser
        from pathlib import Path as _Path
        parser = configparser.ConfigParser()
        parser.read(_Path(__file__).resolve().parent.parent / "config.ini", encoding="utf-8")
        return parser.get("knowledge", "rag_debug", fallback="false").strip().lower() in ("1", "true", "yes", "on")
    except Exception:
        return False


def force_trace(on: bool = True):
    """`with force_trace(): analyze(...)` - the ops trace command."""
    from contextlib import contextmanager

    @contextmanager
    def scope():
        token = _trace_forced.set(on)
        try:
            yield
        finally:
            _trace_forced.reset(token)
    return scope()


def write_trace(obj: AnalysisObject, result: "retrieval.RetrievalResult") -> Optional[str]:
    """Every stage of one question, from the query to the sources - for debugging."""
    import json
    from core.paths import log_dir
    exchange = _model_exchange.pop(obj.analysis_id, {})
    trace = {
        "analysis_id": obj.analysis_id, "question": obj.question,
        "plan": {k: obj.plan.get(k) for k in ("primary", "intents", "complexity", "metrics", "sources")}
                | {"window": obj.plan.get("scope", {}).get("window")},
        "retrieval": {k: obj.retrieval.get(k) for k in ("legs", "vector_note", "embedding", "rerank", "candidates",
                                                        "elapsed_s", "legacy")},
        "retrieved": [{"id": e["evidence_id"], "citation": e["citation"], "origin": e["origin"],
                       "authority": e["authority_label"], "kind": e.get("kind"), "legs": e.get("legs"),
                       "score": e.get("score"), "text": (e.get("text") or "")[:600]} for e in obj.evidence],
        "facts": len(obj.facts), "calculations": [c["name"] for c in obj.calculations][:20],
        "context_injection": obj.retrieval.get("context_injection"),
        "model_request": exchange.get("request"), "model_response": exchange.get("response"),
        "narrative": obj.retrieval.get("narrative"), "answer_source": obj.narrative_source,
        "answer": obj.answer, "validation": obj.validation, "verification_state": obj.verification_state,
        "sources": sorted({e["citation"] for e in obj.evidence}),
    }
    folder = log_dir() / "rag_trace"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{obj.analysis_id}.json"
    path.write_text(json.dumps(trace, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return str(path)


_PROCEDURE_ASK = re.compile(r"\b(?:procedure|steps?|how (?:do|to|should)|read|sop|instructions?|process for|method)\b",
                            re.IGNORECASE)


def _narrative(obj: AnalysisObject, result: retrieval.RetrievalResult, memory_block: str,
               ctx: AccessContext) -> Tuple[Optional[str], str]:
    """The model's summary - kept only if it passes validation."""
    try:
        from core.model_router import model_router
        from core.ollama_client import ollama_client
    except Exception as error:
        return None, f"model client unavailable: {error}"
    pack = context.build(obj.question, result.evidence, obj.facts[:analysis_mod.FACT_IDS], obj.graph_edges,
                         obj.calculations, ctx.to_dict(), memory_block)
    structured = obj.plan.get("complexity") == "complex"
    task = ("## TASK\nWrite a concise answer: 2-3 sentences that answer the question directly, then at most "
            "5 bullet findings. Cite ids on every sentence that states a fact or a number, each id in its own brackets "
            "like [E1][C3]. Use numbers exactly as "
            "given in CALCULATED RESULTS or STRUCTURED DATA - never compute new ones. Do not list recommendations "
            "(they are shown separately). If the evidence is SAMPLE or DEMO data, say so in the first sentence.")
    if not structured and _PROCEDURE_ASK.search(obj.question or ""):
        # "Read the SOP" / "what is the procedure": the steps themselves, in order, from the evidence.
        task = ("## TASK\nAnswer from ENTERPRISE EVIDENCE only. If it contains the procedure or document asked "
                "for, give its content as numbered steps in the order the evidence gives them, each step with its "
                "evidence id like [E1]. Say which document it comes from. Do not call it partial or incomplete "
                "unless the evidence itself says steps are missing. Only if the evidence does not contain it at "
                "all, say so plainly - do not fill the gap from general knowledge. Use numbers exactly as written "
                "in the evidence. If the evidence is SAMPLE, DEMO or TEST data, say so first.")
    if structured:
        # A complex question: the reasoning is shown, with what is KNOWN kept apart from
        # what is INFERRED and what is ASSUMED - each part checked below.
        task = ("## TASK\nReason over the evidence, then write exactly four parts with these headings:\n"
                "Answer: 2-3 sentences that answer the question, cited.\n"
                "Facts: bullets, each a fact stated in the records or a calculated result, each with its ids like [E1][C3].\n"
                "Inferences: bullets, each a conclusion you draw FROM the facts, starting with 'Inference:' and citing the "
                "ids it builds on. Never present an inference as a fact or a possible cause as a proven one.\n"
                "Assumptions: bullets for anything assumed or missing; no numbers here.\n"
                "Use numbers exactly as given in CALCULATED RESULTS or STRUCTURED DATA - never compute new ones. "
                "Do not list recommendations. If the evidence is SAMPLE or DEMO data, say so in the Answer.")
    messages = [{"role": "system", "content": context.SYSTEM_POLICY},
                {"role": "user", "content": pack["prompt"] + "\n\n" + task}]
    # Proof, on every request, that what was retrieved is IN the model's input: each
    # evidence id must appear in the user message. Ids only - no document text here.
    sent = messages[1]["content"]
    in_prompt = [eid for eid in pack["ids"]["evidence"] if f"[{eid}]" in sent]
    injection = {"evidence_retrieved": len(result.evidence), "evidence_in_prompt": len(in_prompt),
                 "facts_in_prompt": pack["ids"]["facts"], "calculations_in_prompt": pack["ids"]["calculations"],
                 "memory_in_prompt": bool(memory_block), "sections": pack["sections"],
                 "prompt_chars": len(sent), "ok": len(in_prompt) == len(result.evidence), "model_called": False}
    if result.evidence and not injection["ok"]:
        injection["error"] = (f"context injection failed: {len(result.evidence) - len(in_prompt)} retrieved "
                              f"passage(s) are not in the model request")
    obj.retrieval["context_injection"] = injection
    try:
        model = model_router.route("reasoning")
        injection.update(model=model, model_called=True)
        if rag_debug_enabled():
            _model_exchange[obj.analysis_id] = {"request": {"model": model, "messages": messages}}
        text = ollama_client.chat(model, messages, retries=0, timeout=240, task_type="reasoning",
                                  purpose="ZEUS company intelligence")
    except Exception as error:
        injection["model_error"] = str(error)[:200]
        return None, f"model did not answer ({str(error)[:160]}) - deterministic answer used"
    if obj.analysis_id in _model_exchange:
        _model_exchange[obj.analysis_id]["response"] = text
    text = (text or "").strip()
    if not text:
        return None, "model returned nothing - deterministic answer used"
    from core.ollama_client import is_failure_text
    if is_failure_text(text):
        # The client reports failures as text; one must never be shown as a summary.
        return None, f"model unavailable ({text.strip('[]')[:200]}) - deterministic answer used"
    check = analysis_mod.validate_answer(obj, text)
    if not check["ok"]:
        problems = []
        if check["invalid_citations"]:
            problems.append("cited ids that do not exist: " + ", ".join(check["invalid_citations"][:6]))
        if check["unsupported_numbers"]:
            problems.append("numbers not in the evidence or calculations: " + ", ".join(check["unsupported_numbers"][:6]))
        return None, f"model summary discarded ({model}) - " + "; ".join(problems)
    if structured:
        parts = analysis_mod.separate_reasoning(text)
        if not parts["facts"]:
            return None, f"model reasoning discarded - it did not separate its facts ({model})"
        problems = analysis_mod.check_reasoning(parts)
        if problems:
            return None, f"model reasoning discarded ({model}) - " + "; ".join(problems[:3])
        obj.reasoning = {"model": model, **parts}
        return text, f"model reasoning validated ({model}): {len(parts['facts'])} facts, " \
                     f"{len(parts['inferences'])} inferences, {len(parts['assumptions'])} assumptions"
    return text, f"model summary validated ({model})"


# ------------------------------------------------------------ utilities -----

def artifact_from_analysis(analysis_id: Optional[str], fmt: str, username: str) -> Dict[str, Any]:
    """Render a saved analysis (latest when no id) in another format."""
    obj = analysis_mod.load(analysis_id, username) if analysis_id else analysis_mod.latest(username)
    if obj is None:
        return {"ok": False, "error": "no saved analysis - ask a question first"}
    from knowledge_fabric import renderers
    result = renderers.create(obj, fmt)
    obj.artifacts.append(result)
    analysis_mod.save(obj)
    return result
