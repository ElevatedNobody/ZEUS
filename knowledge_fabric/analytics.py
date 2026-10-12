"""
The numerical analysis engine. The model interprets; this module calculates.

Every calculation returns a `Calculation` that lists the exact facts it used -
each a table cell in a named document - so a number in an answer, a workbook or
a slide can be traced to its source, and a number that has no inputs cannot
exist.

Refusals are answers too:
  * values in different units are never added or compared silently;
  * CAGR is undefined when the start or end value is not positive - it says so;
  * a trend line is not extrapolated when it does not fit (R^2 < 0.5) or has too
    few points - an "extrapolation" is labelled as such, never as a forecast model.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

LOSS_METRICS = ("net loss", "loss")


@dataclass
class Point:
    year: int
    value: float
    unit: Optional[str]
    fact_id: Optional[int]
    document_id: str
    node_id: Optional[str]
    title: str = ""
    cell_ref: str = ""
    authority: int = 5
    period: Optional[str] = None
    quarter: Optional[int] = None
    doc_year: Optional[int] = None          # the year of the document that reports it (for restatements)

    def ref(self) -> Dict[str, Any]:
        return {"fact_id": self.fact_id, "year": self.year, "quarter": self.quarter, "value": self.value,
                "unit": self.unit, "document_id": self.document_id, "node_id": self.node_id,
                "title": self.title, "cell": self.cell_ref}


@dataclass
class Series:
    metric: str
    entity: Optional[str]
    unit: Optional[str]
    points: List[Point] = field(default_factory=list)
    conflicts: List[Dict[str, Any]] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def years(self) -> List[int]:
        return [p.year for p in self.points]

    @property
    def values(self) -> List[float]:
        return [p.value for p in self.points]

    def value_in(self, year: int) -> Optional[Point]:
        return next((p for p in self.points if p.year == year), None)

    def to_dict(self) -> Dict[str, Any]:
        return {"metric": self.metric, "entity": self.entity, "unit": self.unit,
                "points": [p.ref() for p in self.points], "conflicts": self.conflicts, "notes": self.notes}


@dataclass
class Calculation:
    name: str
    value: Any
    unit: Optional[str]
    method: str
    inputs: List[Dict[str, Any]]
    notes: List[str] = field(default_factory=list)
    ok: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _fold(metric_norm: str, value: float, target: str) -> Tuple[float, Optional[str]]:
    if target == "net profit" and metric_norm in LOSS_METRICS:
        return -abs(value), f"'{metric_norm}' of {value:g} counted as net profit of {-abs(value):g}"
    return value, None


def build_series(facts: Iterable[Dict[str, Any]], metric: str, entity_id: Optional[str] = None,
                 annual_only: bool = True) -> Series:
    """One value per year for `metric` (and optionally one entity).

    When several documents give a value for the same year, the most
    authoritative wins (lowest authority rank, then the later document - a
    restatement - then the later ingestion) and the disagreement is recorded as
    a conflict - never averaged away."""
    by_year: Dict[int, List[Point]] = {}
    notes: List[str] = []
    units: Dict[str, int] = {}
    for fact in facts:
        norm_name = (fact.get("metric_norm") or "").lower()
        if not (norm_name == metric or metric in norm_name or (metric == "net profit" and norm_name in LOSS_METRICS + ("profit",))):
            continue
        if entity_id and fact.get("entity_id") and fact["entity_id"] != entity_id and not str(fact["entity_id"]).startswith(entity_id + "@"):
            continue
        if fact.get("year") is None:
            continue
        if annual_only and (fact.get("quarter") or fact.get("month")):
            continue
        value, note = _fold(norm_name, float(fact["value"]), metric)
        if note and note not in notes:
            notes.append("losses reported as positive 'net loss' figures are counted as negative net profit")
        unit = fact.get("unit")
        units[unit or ""] = units.get(unit or "", 0) + 1
        by_year.setdefault(int(fact["year"]), []).append(Point(
            year=int(fact["year"]), value=value, unit=unit, fact_id=fact.get("fact_id"),
            document_id=fact.get("document_id", ""), node_id=fact.get("node_id"), title=fact.get("title", ""),
            cell_ref=fact.get("cell_ref", ""), authority=int(fact.get("authority") or 5),
            period=fact.get("period"), quarter=fact.get("quarter"), doc_year=fact.get("doc_year")))
    dominant_unit = max(units.items(), key=lambda kv: kv[1])[0] if units else None
    if len([u for u in units if u]) > 1:
        notes.append(f"values reported in different units ({', '.join(sorted(u for u in units if u))}); "
                     f"only '{dominant_unit}' values are used")
    series = Series(metric=metric, entity=entity_id, unit=dominant_unit or None, notes=notes)
    other_unit_only = []
    for year in sorted(by_year):
        points = [p for p in by_year[year] if (p.unit or "") == (dominant_unit or "")]
        if not points:
            other_unit_only.append(year)       # never mixed into a series of another unit
            continue
        points.sort(key=lambda p: (p.authority, -(p.doc_year or 0), -(p.fact_id or 0)))
        chosen = points[0]
        distinct = {round(p.value, 6) for p in points}
        if len(distinct) > 1:
            series.conflicts.append({"year": year, "chosen": chosen.ref(),
                                     "others": [p.ref() for p in points[1:] if round(p.value, 6) != round(chosen.value, 6)],
                                     "rule": "most authoritative source, then the later document (restatement)"})
        series.points.append(chosen)
    if other_unit_only:
        series.notes.append(f"{', '.join(map(str, other_unit_only))}: only reported in another unit - left out "
                            f"rather than converted")
    return series


def _refs(points: Sequence[Point]) -> List[Dict[str, Any]]:
    return [p.ref() for p in points]


def describe(series: Series) -> List[Calculation]:
    """The standard set for a series: count, sum, mean, median, min, max, std dev,
    variance, total change, CAGR, YoY, trend."""
    pts = series.points
    unit = series.unit
    out: List[Calculation] = []
    if not pts:
        return [Calculation(f"{series.metric}: no data", None, unit, "no facts found for the period", [], ok=False)]
    values = [p.value for p in pts]
    out.append(Calculation("years with data", len(pts), None, "count", _refs(pts)))
    out.append(Calculation(f"total {series.metric}", round(sum(values), 4), unit, "sum", _refs(pts)))
    out.append(Calculation(f"average {series.metric}", round(statistics.fmean(values), 4), unit, "arithmetic mean", _refs(pts)))
    out.append(Calculation(f"median {series.metric}", round(statistics.median(values), 4), unit, "median", _refs(pts)))
    low = min(pts, key=lambda p: p.value)
    high = max(pts, key=lambda p: p.value)
    out.append(Calculation(f"minimum {series.metric}", low.value, unit, f"minimum (in {low.year})", [low.ref()]))
    out.append(Calculation(f"maximum {series.metric}", high.value, unit, f"maximum (in {high.year})", [high.ref()]))
    if len(values) >= 2:
        out.append(Calculation(f"standard deviation of {series.metric}", round(statistics.stdev(values), 4), unit,
                               "sample standard deviation", _refs(pts)))
        out.append(Calculation(f"variance of {series.metric}", round(statistics.variance(values), 4),
                               f"({unit})^2" if unit else None, "sample variance", _refs(pts)))
        out.append(change(series, pts[0].year, pts[-1].year))
        out.append(cagr(series))
        out.append(yoy(series))
        out.append(trend(series))
        out.append(declines(series))
    if series.metric in PROFIT_METRICS:
        out.append(loss_years(series))
    return out


PROFIT_METRICS = ("net profit", "profit before tax", "pbt", "ebitda", "ebit", "gross profit", "operating profit",
                  "pat proxy", "net income")


def declines(series: Series) -> Calculation:
    """Every year the value fell from the year before - consecutive years only."""
    rows = []
    for prev, cur in zip(series.points, series.points[1:]):
        if cur.year == prev.year + 1 and cur.value < prev.value:
            rows.append({"year": cur.year, "from": prev.value, "to": cur.value,
                         "percent": round((cur.value - prev.value) / abs(prev.value) * 100, 2) if prev.value else None})
    return Calculation(f"declines in {series.metric}", rows, series.unit,
                       "years where value(t) < value(t-1)", _refs(series.points))


def loss_years(series: Series) -> Calculation:
    """The years with a loss (a value below zero) - an empty list is a finding too."""
    rows = [{"year": p.year, "value": p.value} for p in series.points if p.value < 0]
    return Calculation(f"loss years in {series.metric}", rows, series.unit, "years with a value below zero",
                       _refs(series.points))


def compare_periods(series: Series, periods: Sequence[Sequence[int]]) -> Calculation:
    """Each period read whole - years with figures, total, yearly average, CAGR
    inside it - and the change of the yearly average from the first period to
    the last. A period with missing years says so; nothing is filled in."""
    rows: List[Dict[str, Any]] = []
    used: List[Point] = []
    notes: List[str] = []
    for start, end in periods:
        pts = [p for p in series.points if start <= p.year <= end]
        row: Dict[str, Any] = {"period": f"{start}-{end}", "start": start, "end": end,
                               "years": len(pts), "expected": end - start + 1}
        if pts:
            values = [p.value for p in pts]
            row.update(total=round(sum(values), 4), average=round(statistics.fmean(values), 4),
                       first_year=pts[0].year, first=pts[0].value, last_year=pts[-1].year, last=pts[-1].value)
            if len(pts) >= 2 and pts[0].value > 0 and pts[-1].value > 0:
                row["cagr"] = round(((pts[-1].value / pts[0].value) ** (1 / (pts[-1].year - pts[0].year)) - 1) * 100, 3)
            used += pts
        if row["years"] < row["expected"]:
            notes.append(f"{start}-{end}: figures for {row['years']} of {row['expected']} years")
        rows.append(row)
    value: Dict[str, Any] = {"periods": rows}
    first, last = rows[0], rows[-1]
    ok = all(r["years"] for r in rows)
    if ok and len(rows) >= 2:
        diff = last["average"] - first["average"]
        value["average_change"] = {"absolute": round(diff, 4),
                                   "percent": round(diff / abs(first["average"]) * 100, 2) if first["average"] else None}
    return Calculation(f"periods of {series.metric}", value, series.unit,
                       "per period: count, sum, mean, CAGR first-to-last year; change = mean(last) - mean(first)",
                       _refs(used), notes, ok=ok)


def change(series: Series, start: int, end: int) -> Calculation:
    a, b = series.value_in(start), series.value_in(end)
    if not a or not b:
        missing = [y for y, p in ((start, a), (end, b)) if not p]
        return Calculation(f"change in {series.metric} {start}-{end}", None, series.unit,
                           "difference", [], [f"no value for {', '.join(map(str, missing))}"], ok=False)
    diff = b.value - a.value
    pct = (diff / abs(a.value) * 100) if a.value else None
    notes = [] if pct is not None else ["percentage change undefined: the start value is zero"]
    return Calculation(f"change in {series.metric} {start}-{end}",
                       {"absolute": round(diff, 4), "percent": round(pct, 2) if pct is not None else None},
                       series.unit, "end minus start; percent of |start|", [a.ref(), b.ref()], notes)


def cagr(series: Series, start: Optional[int] = None, end: Optional[int] = None) -> Calculation:
    pts = series.points
    a = series.value_in(start) if start else (pts[0] if pts else None)
    b = series.value_in(end) if end else (pts[-1] if pts else None)
    if not a or not b or a.year == b.year:
        return Calculation(f"CAGR of {series.metric}", None, "%", "compound annual growth rate", [],
                           ["needs two different years"], ok=False)
    if a.value <= 0 or b.value <= 0:
        return Calculation(f"CAGR of {series.metric} {a.year}-{b.year}", None, "%", "compound annual growth rate",
                           [a.ref(), b.ref()], ["undefined: CAGR needs positive start and end values "
                                                f"({a.value:g} in {a.year}, {b.value:g} in {b.year})"], ok=False)
    years = b.year - a.year
    rate = (b.value / a.value) ** (1 / years) - 1
    return Calculation(f"CAGR of {series.metric} {a.year}-{b.year}", round(rate * 100, 3), "%",
                       f"(end/start)^(1/{years}) - 1", [a.ref(), b.ref()])


def yoy(series: Series) -> Calculation:
    rows = []
    for prev, cur in zip(series.points, series.points[1:]):
        if cur.year != prev.year + 1:
            rows.append({"year": cur.year, "absolute": None, "percent": None,
                         "note": f"gap: no value for {prev.year + 1}-{cur.year - 1}"})
            continue
        diff = cur.value - prev.value
        rows.append({"year": cur.year, "absolute": round(diff, 4),
                     "percent": round(diff / abs(prev.value) * 100, 2) if prev.value else None})
    return Calculation(f"year-on-year change in {series.metric}", rows, series.unit,
                       "value(t) - value(t-1); percent of |value(t-1)|", _refs(series.points))


def trend(series: Series) -> Calculation:
    pts = series.points
    if len(pts) < 3:
        return Calculation(f"trend of {series.metric}", None, series.unit, "least squares", _refs(pts),
                           ["needs at least three years"], ok=False)
    xs, ys = [float(p.year) for p in pts], [p.value for p in pts]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    slope = sxy / sxx if sxx else 0.0
    intercept = my - slope * mx
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys))
    r2 = 1 - ss_res / ss_tot if ss_tot else 1.0
    direction = "rising" if slope > 0 else "falling" if slope < 0 else "flat"
    notes = [] if r2 >= 0.5 else [f"weak fit (R^2 = {r2:.2f}): the values move, but not along a steady line"]
    return Calculation(f"trend of {series.metric}", {"slope_per_year": round(slope, 4), "intercept": round(intercept, 4),
                                                     "r2": round(r2, 3), "direction": direction},
                       series.unit, "ordinary least squares on (year, value)", _refs(pts), notes)


def rolling_average(series: Series, window: int = 3) -> Calculation:
    rows = []
    for i in range(len(series.points)):
        chunk = series.points[max(0, i - window + 1):i + 1]
        if len(chunk) == window and chunk[-1].year - chunk[0].year == window - 1:
            rows.append({"year": series.points[i].year, "value": round(statistics.fmean(p.value for p in chunk), 4)})
    return Calculation(f"{window}-year rolling average of {series.metric}", rows, series.unit,
                       f"mean of the last {window} consecutive years", _refs(series.points))


def correlation(a: Series, b: Series) -> Calculation:
    common = sorted(set(a.years) & set(b.years))
    name = f"correlation of {a.metric} with {b.metric}"
    if len(common) < 4:
        return Calculation(name, None, None, "Pearson", [], [f"only {len(common)} overlapping year(s); needs 4+"], ok=False)
    xs = [a.value_in(y).value for y in common]
    ys = [b.value_in(y).value for y in common]
    try:
        r = statistics.correlation(xs, ys)
    except statistics.StatisticsError as error:
        return Calculation(name, None, None, "Pearson", [], [str(error)], ok=False)
    notes = ["correlation is not causation: it says the two moved together, not that one drove the other"]
    try:
        from scipy import stats as _stats
        p_value = float(_stats.pearsonr(xs, ys).pvalue)
        notes.append(f"p = {p_value:.3f} over {len(common)} years")
    except Exception:
        p_value = None
    inputs = [a.value_in(y).ref() for y in common] + [b.value_in(y).ref() for y in common]
    return Calculation(name, {"r": round(r, 3), "years": common, "p_value": round(p_value, 4) if p_value is not None else None},
                       None, "Pearson r over overlapping years", inputs, notes)


def extrapolate(series: Series, years_ahead: int = 1) -> Calculation:
    fit = trend(series)
    name = f"linear extrapolation of {series.metric}"
    if not fit.ok or len(series.points) < 4 or fit.value["r2"] < 0.5:
        reason = fit.notes[0] if fit.notes else "fewer than four years of data"
        return Calculation(name, None, series.unit, "linear trend", fit.inputs,
                           [f"not extrapolated: {reason}"], ok=False)
    last = series.points[-1].year
    rows = [{"year": last + k, "value": round(fit.value["slope_per_year"] * (last + k) + fit.value["intercept"], 4)}
            for k in range(1, years_ahead + 1)]
    return Calculation(name, rows, series.unit, "extends the least-squares line", fit.inputs,
                       ["an EXTRAPOLATION of the past trend, not a forecast model - no drivers, no uncertainty bands"])


def scenario(series: Series, changes_percent: Dict[str, float], base_year: Optional[int] = None) -> Calculation:
    """What-if on the latest (or a given) year's value: {"-10% cost": -10, ...}."""
    base = series.value_in(base_year) if base_year else (series.points[-1] if series.points else None)
    if not base:
        return Calculation(f"scenarios for {series.metric}", None, series.unit, "percentage change of a base value", [],
                           ["no base value"], ok=False)
    rows = [{"scenario": label, "change_percent": pct, "value": round(base.value * (1 + pct / 100.0), 4)}
            for label, pct in changes_percent.items()]
    return Calculation(f"scenarios for {series.metric} (base {base.year})", rows, series.unit,
                       "base x (1 + change%)", [base.ref()], ["scenarios are arithmetic on assumptions, not predictions"])


def parts_vs_whole(ranking: Calculation, company: Series) -> Calculation:
    """Do the per-site (per-unit) totals add up to the company's stated figure for
    the same years? The gap is reported, never spread over the parts."""
    name = f"{company.metric}: entities vs company"
    rows = ranking.value or []
    years = sorted({i.get("year") for i in ranking.inputs if i.get("year")})
    units = {r.get("unit") for r in rows}
    whole = [p for p in company.points if p.year in years]
    if not rows or not whole or len(units) != 1 or units != {company.unit}:
        return Calculation(name, None, company.unit, "sum of entity totals vs sum of company figures", [],
                           ["not comparable: different units or no company figures for the same years"], ok=False)
    parts_total = round(sum(r["total"] for r in rows), 4)
    whole_total = round(sum(p.value for p in whole), 4)
    gap = parts_total - whole_total
    derived = all("(sum)" in (i.get("cell") or "") for i in ranking.inputs)
    return Calculation(name, {"entities_total": parts_total, "company_total": whole_total,
                              "difference": round(gap, 4),
                              "percent": round(gap / abs(whole_total) * 100, 2) if whole_total else None,
                              "years": years, "entities_from_daily_rows": derived},
                       company.unit, "sum of entity totals minus sum of the company's figures for the same years",
                       _refs(whole) + ranking.inputs[:50])


def rank_entities(facts: Iterable[Dict[str, Any]], metric: str, year: Optional[int] = None,
                  descending: bool = True) -> Calculation:
    """'Which unit had the highest maintenance cost?' - summed per entity. Where an
    entity has yearly figures its monthly / quarterly ones are not added on top."""
    totals: Dict[str, Dict[str, Any]] = {}
    facts = [f for f in facts if metric in (f.get("metric_norm") or "").lower()
             and (f.get("entity_id") or f.get("entity_label"))]
    # One value per entity and period: the same site-year in two documents (a full
    # ledger and a sample sheet of it) is counted once - the most authoritative,
    # then the later document - never added twice.
    chosen: Dict[tuple, Dict[str, Any]] = {}
    for fact in sorted(facts, key=lambda f: (int(f.get("authority") or 5), -(f.get("doc_year") or 0),
                                             -(f.get("fact_id") or 0))):
        key = (fact.get("entity_id") or fact.get("entity_label"), fact.get("year"), fact.get("quarter"),
               fact.get("month"), fact.get("unit"))
        chosen.setdefault(key, fact)
    facts = list(chosen.values())
    annual = {f.get("entity_id") or f.get("entity_label") for f in facts if not (f.get("month") or f.get("quarter"))}
    for fact in facts:
        if year and fact.get("year") != year:
            continue
        if (fact.get("month") or fact.get("quarter")) and (fact.get("entity_id") or fact.get("entity_label")) in annual:
            continue
        key = fact.get("entity_id") or fact.get("entity_label")
        entry = totals.setdefault(key, {"entity": fact.get("entity_label") or key, "total": 0.0,
                                        "unit": fact.get("unit"), "inputs": []})
        if entry["unit"] and fact.get("unit") and entry["unit"] != fact.get("unit"):
            continue
        entry["total"] += float(fact["value"])
        entry["inputs"].append({"fact_id": fact.get("fact_id"), "year": fact.get("year"), "value": fact["value"],
                                "document_id": fact.get("document_id"), "node_id": fact.get("node_id"),
                                "cell": fact.get("cell_ref"), "title": fact.get("title")})
    ranking = sorted(totals.values(), key=lambda e: -e["total"] if descending else e["total"])
    inputs = [i for e in ranking for i in e["inputs"]]
    if not ranking:
        return Calculation(f"{metric} by entity", None, None, "sum per entity", [], ["no per-entity values found"], ok=False)
    return Calculation(f"{metric} by entity" + (f" ({year})" if year else ""),
                       [{"entity": e["entity"], "total": round(e["total"], 4), "unit": e["unit"]} for e in ranking],
                       ranking[0]["unit"], "sum per entity", inputs)
