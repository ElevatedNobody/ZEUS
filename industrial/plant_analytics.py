"""
Plant data -> IndustrialAnalysis.

This is the seam that makes generated deliverables real. Every deck, workbook
and report ZEUS produces is rendered from an `IndustrialAnalysis`, and until
now the only way to get one was to hand it a document. So a question like
"build a deck on maintenance performance" fell through to a generic topic deck
whose every figure came out of the model's imagination.

The builders here assemble the same structure from what is actually in the
plant database: real equipment, real work orders, real readings, real spares.
The renderers are untouched - they already produce good output, and pointing
them at true numbers is the whole change.

    plant database
        -> build an IndustrialAnalysis (here)
            -> deck_builder     -> .pptx
            -> workbook_builder -> .xlsx
            -> report_builder   -> .docx

Two rules run through all of it.

NOTHING IS INVENTED. Every KPI, row and finding traces to a query. Where the
data cannot answer something, the analysis says so rather than filling the gap
- an empty section is honest, a plausible fabricated one is not.

DEMO DATA IS LABELLED. If any contributing row carries source='DEMO', the
analysis is flagged and every renderer prints the banner. A deliverable built
on sample data must never be mistakable for one built on a real plant.
"""

from __future__ import annotations

import json
import statistics
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from core.logger import get_logger
from industrial.analysis import (
    BAD,
    GOOD,
    NEUTRAL,
    WARN,
    Finding,
    IndustrialAnalysis,
    KPI,
    Milestone,
    Recommendation,
    Risk,
    TableBlock,
)

logger = get_logger("plant_analytics")

MONEY = "#,##0.0"
NUMBER = "#,##0"
DECIMAL = "0.00"
PERCENT = "0.0"


def _plant():
    from industrial import plant_data
    plant_data.init_plant_schema()
    return plant_data


def _demo(rows: List[Dict[str, Any]]) -> bool:
    return any(str(row.get("source", "")).upper() == "DEMO" for row in rows)


def _date(value: str) -> Optional[date]:
    for pattern in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(str(value)[:19], pattern).date()
        except (ValueError, TypeError):
            continue
    return None


def _attributes(row: Dict[str, Any]) -> Dict[str, Any]:
    """The attributes column as a dict, whatever shape it is on disk.

    A value that was serialised twice decodes to a string rather than a dict,
    so decoding is repeated until it stops being a string. Anything that is
    still not a dict at the end is discarded rather than returned - a caller
    that does `.items()` on a str gets an AttributeError three frames away
    from the cause.
    """
    raw = row.get("attributes")
    for _ in range(3):
        if isinstance(raw, dict):
            return raw
        if not isinstance(raw, str) or not raw.strip():
            return {}
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return {}
    return raw if isinstance(raw, dict) else {}


# ================================================== one piece of equipment ==

def equipment_dossier(reference: str) -> Optional[IndustrialAnalysis]:
    """Everything the plant knows about one asset.

    This is what "create a maintenance report for P-101" should produce: its
    condition, what has been done to it, what was found, what the instruments
    say, and what follows from that - each traceable to a record.
    """
    plant = _plant()
    equipment = plant.resolve_equipment(reference)
    if not equipment:
        return None

    tag = equipment["tag"]
    maintenance = plant.maintenance_history(tag, limit=50)
    inspections = plant.inspection_history(tag, limit=50)
    readings = _readings(tag)
    attributes = _attributes(equipment)
    contributing = [equipment] + maintenance + inspections

    analysis = IndustrialAnalysis(
        title=f"{tag} — {equipment.get('name', 'Equipment')}",
        subtitle=(f"{equipment.get('unit') or 'Plant'} · "
                  f"{str(equipment.get('equipment_type', '')).replace('_', ' ').title()} · "
                  f"{str(equipment.get('criticality', 'medium')).title()} criticality"),
        contains_demo_data=_demo(contributing),
    )

    # --- headline --------------------------------------------------------
    status = str(equipment.get("status", "unknown")).replace("_", " ")
    open_work = [m for m in maintenance if m.get("status") == "in_progress"]
    attention = [i for i in inspections if i.get("result") in ("attention", "fail")]

    if open_work:
        analysis.headline = (
            f"{tag} is under maintenance: {open_work[0].get('description', '')[:120]}")
    elif attention:
        analysis.headline = (
            f"{tag} is running with an open inspection finding from "
            f"{attention[0].get('inspected_on', 'an earlier survey')}.")
    else:
        analysis.headline = (
            f"{tag} is {status} with no outstanding inspection findings.")

    # --- KPIs ------------------------------------------------------------
    analysis.kpis.append(KPI(
        "Status", status.title(),
        note=f"{equipment.get('unit', '')} · installed {equipment.get('installed_on', '—')}",
        tone=BAD if status in ("failed", "out of service") else
             WARN if status in ("maintenance", "degraded") else GOOD))
    analysis.kpis.append(KPI(
        "Criticality", str(equipment.get("criticality", "medium")).title(),
        note="Consequence x likelihood assessment",
        tone=BAD if equipment.get("criticality") == "critical" else NEUTRAL))

    if maintenance:
        hours = sum(float(m.get("duration_hours") or 0) for m in maintenance)
        analysis.kpis.append(KPI(
            "Interventions", str(len(maintenance)),
            note=f"{hours:.0f} maintenance hours on record", tone=NEUTRAL))
        last = maintenance[0]
        analysis.kpis.append(KPI(
            "Last worked", str(last.get("performed_on", "—")),
            note=str(last.get("maintenance_type", "")).title(), tone=NEUTRAL))

    # The instruments, where there are any. A trend is the one thing a
    # maintenance report cannot get from prose.
    for parameter, series in readings.items():
        if len(series) < 4:
            continue
        latest = series[-1][1]
        earliest = series[0][1]
        peak = max(value for _, value in series)
        change = latest - earliest
        tone = NEUTRAL
        if parameter in ("vibration_velocity", "bearing_temperature"):
            tone = BAD if latest > peak * 0.9 and change > 0 else GOOD if change < 0 else WARN
        analysis.kpis.append(KPI(
            parameter.replace("_", " ").title(),
            f"{latest:g}",
            note=f"peak {peak:g} over {len(series)} readings",
            delta=f"{change:+.1f}", tone=tone))
        if len(analysis.kpis) >= 8:
            break

    # --- what the record says --------------------------------------------
    for inspection in inspections[:4]:
        result = str(inspection.get("result", "")).lower()
        analysis.findings.append(Finding(
            title=(f"{str(inspection.get('inspection_type', 'Inspection')).title()} "
                   f"inspection, {inspection.get('inspected_on', '')}"),
            detail=str(inspection.get("findings", ""))[:600],
            # `magnitude` renders as a small chip on the card, so it has to be
            # a SIZE - a few characters. A recommendation sentence here came
            # out clipped mid-word as "PLAN BEAR...", which reads as a bug.
            magnitude=str(inspection.get("result", "")).upper()[:10],
            severity=("high" if result in ("fail", "attention") else "low"),
            evidence=(f"Inspection record, {inspection.get('inspector', 'inspector')}, "
                      f"{inspection.get('inspected_on', '')}")))

    for work in maintenance[:3]:
        if not work.get("findings"):
            continue
        analysis.findings.append(Finding(
            title=f"{str(work.get('maintenance_type', 'Work')).title()}, "
                  f"{work.get('performed_on', '')}",
            detail=str(work.get("findings", ""))[:600],
            magnitude=(f"{work.get('duration_hours', 0)} h"
                       if work.get("duration_hours") else ""),
            severity="medium" if work.get("status") == "in_progress" else "info",
            evidence=f"Work order {work.get('work_order', '—')}"))

    # --- tables ----------------------------------------------------------
    if maintenance:
        analysis.tables.append(TableBlock(
            title="Maintenance history",
            columns=["Date", "Type", "Description", "Technician", "Hours",
                     "Status", "Work order"],
            rows=[[m.get("performed_on", ""), str(m.get("maintenance_type", "")).title(),
                   str(m.get("description", ""))[:110], m.get("technician", ""),
                   float(m.get("duration_hours") or 0),
                   str(m.get("status", "")).replace("_", " ").title(),
                   m.get("work_order", "")] for m in maintenance],
            formats={"Hours": DECIMAL}, total_columns=["Hours"],
            note="Every intervention on record, most recent first."))

    if inspections:
        analysis.tables.append(TableBlock(
            title="Inspection history",
            columns=["Date", "Type", "Inspector", "Result", "Findings", "Next due"],
            rows=[[i.get("inspected_on", ""), str(i.get("inspection_type", "")).title(),
                   i.get("inspector", ""), str(i.get("result", "")).title(),
                   str(i.get("findings", ""))[:150], i.get("next_due", "")]
                  for i in inspections],
            highlight={"Result": "attention"},
            note="Results are as recorded by the inspector."))

    for parameter, series in list(readings.items())[:2]:
        if len(series) < 6:
            continue
        # Thinned to about 30 points: a chart of 240 markers is unreadable and
        # the trend is what the reader needs, not every sample.
        step = max(1, len(series) // 30)
        analysis.tables.append(TableBlock(
            title=f"{parameter.replace('_', ' ').title()} trend",
            columns=["Date", parameter.replace("_", " ").title()],
            rows=[[when, value] for when, value in series[::step]],
            formats={parameter.replace("_", " ").title(): DECIMAL},
            note=f"{len(series)} readings, thinned for legibility."))

    spares = _spares_for(tag)
    if spares:
        analysis.tables.append(TableBlock(
            title="Associated spares",
            columns=["Part", "Description", "In stock", "Minimum", "Status",
                     "Location"],
            rows=[[s.get("part_number", ""), str(s.get("description", ""))[:60],
                   int(s.get("quantity") or 0), int(s.get("minimum_stock") or 0),
                   ("BELOW MINIMUM" if (s.get("quantity") or 0) < (s.get("minimum_stock") or 0)
                    else "OK"),
                   s.get("storage_location", "")] for s in spares],
            highlight={"Status": "BELOW MINIMUM"}))

    # --- specification ---------------------------------------------------
    if attributes:
        analysis.tables.append(TableBlock(
            title="Equipment data",
            columns=["Attribute", "Value"],
            rows=([["Manufacturer", equipment.get("manufacturer", "—")],
                   ["Model", equipment.get("model", "—")],
                   ["Serial", equipment.get("serial_number", "—")],
                   ["Installed", equipment.get("installed_on", "—")],
                   ["Unit", equipment.get("unit", "—")]] +
                  [[str(k).replace("_", " ").title(), str(v)]
                   for k, v in attributes.items()])))

    # --- what follows ----------------------------------------------------
    analysis.recommendations.extend(_equipment_recommendations(
        equipment, maintenance, inspections, spares))
    analysis.risks.extend(_equipment_risks(equipment, inspections, spares))

    analysis.executive_summary = _equipment_summary(
        equipment, maintenance, inspections, readings)
    analysis.source_data = [
        f"Equipment register: {tag}",
        f"Maintenance history: {len(maintenance)} record(s)",
        f"Inspection register: {len(inspections)} record(s)",
        f"Instrument readings: {sum(len(s) for s in readings.values())} value(s)",
    ]
    analysis.citations = (
        [f"Work order {m.get('work_order')}" for m in maintenance if m.get("work_order")][:6] +
        [f"Inspection {i.get('inspection_type')} {i.get('inspected_on')}"
         for i in inspections][:6])
    return analysis


def _equipment_summary(equipment, maintenance, inspections, readings) -> str:
    tag = equipment["tag"]
    parts = [
        f"{tag} is a {str(equipment.get('equipment_type', 'asset')).replace('_', ' ')} "
        f"in {equipment.get('unit', 'the plant')}, rated "
        f"{equipment.get('criticality', 'medium')} criticality, currently "
        f"{str(equipment.get('status', 'unknown')).replace('_', ' ')}."]

    if maintenance:
        hours = sum(float(m.get("duration_hours") or 0) for m in maintenance)
        span = ""
        first, last = _date(maintenance[-1].get("performed_on")), _date(
            maintenance[0].get("performed_on"))
        if first and last and first != last:
            span = f" between {first} and {last}"
        parts.append(
            f"The record carries {len(maintenance)} intervention(s){span}, totalling "
            f"{hours:.0f} maintenance hours.")

    attention = [i for i in inspections if i.get("result") in ("attention", "fail")]
    if attention:
        latest = attention[0]
        parts.append(
            f"The most recent inspection to raise a finding was the "
            f"{latest.get('inspection_type', '')} survey of "
            f"{latest.get('inspected_on', '')}: "
            f"{str(latest.get('findings', ''))[:220]}")
    elif inspections:
        parts.append(f"The last {len(inspections)} inspection(s) passed without a "
                     f"recorded finding.")

    for parameter, series in readings.items():
        if len(series) < 6:
            continue
        first_value, last_value = series[0][1], series[-1][1]
        direction = ("risen" if last_value > first_value * 1.1 else
                     "fallen" if last_value < first_value * 0.9 else "held steady")
        parts.append(
            f"{parameter.replace('_', ' ').title()} has {direction} from "
            f"{first_value:g} to {last_value:g} across {len(series)} readings.")
        break

    open_work = [m for m in maintenance if m.get("status") == "in_progress"]
    if open_work:
        parts.append(f"Work order {open_work[0].get('work_order', '')} is open: "
                     f"{str(open_work[0].get('description', ''))[:180]}")
    return " ".join(parts)


def _equipment_recommendations(equipment, maintenance, inspections,
                               spares) -> List[Recommendation]:
    """Only what the data supports. No generic maintenance advice."""
    out: List[Recommendation] = []
    tag = equipment["tag"]

    for inspection in inspections:
        if inspection.get("result") in ("attention", "fail") and inspection.get(
                "recommendation"):
            out.append(Recommendation(
                title=str(inspection["recommendation"])[:140],
                rationale=(f"Raised by the {inspection.get('inspection_type', '')} "
                           f"inspection of {inspection.get('inspected_on', '')}."),
                owner=str(inspection.get("inspector", "")), priority=1))

    open_work = [m for m in maintenance if m.get("status") == "in_progress"]
    for work in open_work:
        out.append(Recommendation(
            title=f"Close out work order {work.get('work_order', '')} and verify "
                  f"before return to service",
            rationale=str(work.get("findings", ""))[:200],
            owner=str(work.get("technician", "")), priority=1,
            effort=f"{work.get('duration_hours', 0)} h to date"))

    short = [s for s in spares if (s.get("quantity") or 0) < (s.get("minimum_stock") or 0)]
    for spare in short:
        out.append(Recommendation(
            title=f"Replenish {spare.get('part_number')} to minimum stock",
            rationale=(f"{spare.get('quantity')} in stock against a minimum of "
                       f"{spare.get('minimum_stock')}; {tag} depends on this item."),
            owner="Stores", priority=2))

    overdue = [m for m in maintenance if m.get("next_due")
               and (_date(m["next_due"]) or date.max) < date.today()]
    if overdue:
        # Named with its work order and date. "Schedule the overdue task" is
        # not something the reader can check against anything; a reference and
        # a date can be looked up in thirty seconds.
        task = overdue[0]
        out.append(Recommendation(
            title=(f"Schedule the follow-up to {task.get('work_order', 'the last job')}, "
                   f"due {task.get('next_due')}"),
            rationale=(f"The {task.get('maintenance_type', '')} task of "
                       f"{task.get('performed_on')} set a next-due date of "
                       f"{task.get('next_due')}, which has passed with nothing "
                       f"recorded against it."),
            owner=str(task.get("technician", "")), priority=2))
    return out


def _equipment_risks(equipment, inspections, spares) -> List[Risk]:
    out: List[Risk] = []
    criticality = str(equipment.get("criticality", "medium"))
    attributes = _attributes(equipment)

    if criticality == "critical" and not attributes.get("spare"):
        out.append(Risk(
            risk=f"{equipment['tag']} is critical with no installed spare recorded",
            likelihood="low", impact="high",
            mitigation="Confirm whether a standby exists; if not, hold a critical spare."))

    for inspection in inspections:
        if inspection.get("result") in ("attention", "fail"):
            out.append(Risk(
                risk=str(inspection.get("findings", ""))[:160],
                likelihood="medium",
                impact="high" if criticality == "critical" else "medium",
                mitigation=str(inspection.get("recommendation", ""))[:160]))
            break

    short = [s for s in spares if (s.get("quantity") or 0) < (s.get("minimum_stock") or 0)]
    if short:
        out.append(Risk(
            risk=f"{len(short)} associated spare(s) below minimum stock",
            likelihood="medium", impact="medium",
            mitigation=f"Raise a purchase requisition for "
                       f"{', '.join(s['part_number'] for s in short[:3])}."))
    return out


def _readings(tag: str) -> Dict[str, List]:
    plant = _plant()
    try:
        with plant._conn() as conn:
            rows = conn.execute(
                "SELECT parameter, measured_at, value FROM plant_readings "
                "WHERE equipment_tag = ? ORDER BY measured_at", (tag,)).fetchall()
    except Exception as error:
        logger.debug(f"readings unavailable for {tag}: {error}")
        return {}
    series: Dict[str, List] = defaultdict(list)
    for row in rows:
        series[row["parameter"]].append((str(row["measured_at"])[:10], float(row["value"])))
    return dict(series)


def _spares_for(tag: str) -> List[Dict[str, Any]]:
    plant = _plant()
    try:
        with plant._conn() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM plant_inventory WHERE equipment_tag = ? COLLATE NOCASE",
                (tag,)).fetchall()]
    except Exception:
        return []


# ================================================= fleet-level analyses ====

def reliability_overview() -> Optional[IndustrialAnalysis]:
    """The whole asset base: what is running, what needs attention, what is
    at risk. This is what a management deck is usually asking for."""
    plant = _plant()
    equipment = plant.list_equipment(limit=500)
    if not equipment:
        return None

    inspections, maintenance, alerts = _all_history()
    analysis = IndustrialAnalysis(
        title="Asset Reliability Overview",
        subtitle=f"{len(equipment)} assets across "
                 f"{len({e.get('unit') for e in equipment if e.get('unit')})} units",
        contains_demo_data=_demo(equipment + maintenance + inspections))

    by_status = Counter(str(e.get("status", "unknown")) for e in equipment)
    by_criticality = Counter(str(e.get("criticality", "medium")) for e in equipment)
    unavailable = sum(by_status[s] for s in ("maintenance", "out_of_service", "failed")
                      if s in by_status)
    availability = (len(equipment) - unavailable) / len(equipment) * 100

    analysis.headline = (
        f"{availability:.1f}% of the asset base is available; {unavailable} asset(s) "
        f"are out of service, of which "
        f"{sum(1 for e in equipment if e.get('criticality') == 'critical' and e.get('status') in ('maintenance', 'out_of_service', 'failed'))} "
        f"are critical.")

    analysis.kpis = [
        KPI("Assets", str(len(equipment)), note="in the register", tone=NEUTRAL),
        KPI("Availability", f"{availability:.1f}%",
            note=f"{unavailable} unavailable",
            tone=GOOD if availability > 95 else WARN if availability > 90 else BAD),
        KPI("Critical assets", str(by_criticality.get("critical", 0)),
            note="highest consequence class", tone=NEUTRAL),
        KPI("Open alerts", str(sum(1 for a in alerts if a.get("status") == "open")),
            note=f"{sum(1 for a in alerts if a.get('severity') in ('high', 'critical'))} "
                 f"high or critical",
            tone=BAD if any(a.get("severity") in ("high", "critical")
                            and a.get("status") == "open" for a in alerts) else GOOD),
        KPI("Findings open", str(sum(1 for i in inspections
                                     if i.get("result") in ("attention", "fail"))),
            note="inspections raising an action", tone=WARN),
        KPI("Work in progress", str(sum(1 for m in maintenance
                                        if m.get("status") == "in_progress")),
            note="open work orders", tone=NEUTRAL),
    ]

    analysis.tables.append(TableBlock(
        title="Status by unit",
        columns=["Unit", "Assets", "Running", "Standby", "Unavailable", "Critical"],
        rows=_status_by_unit(equipment),
        formats={"Assets": NUMBER, "Running": NUMBER, "Standby": NUMBER,
                 "Unavailable": NUMBER, "Critical": NUMBER},
        total_columns=["Assets", "Running", "Standby", "Unavailable", "Critical"],
        highlight={"Unavailable": ">0"}))

    attention = [e for e in equipment
                 if e.get("status") in ("maintenance", "degraded", "out_of_service",
                                        "failed")]
    if attention:
        analysis.tables.append(TableBlock(
            title="Assets needing attention",
            columns=["Tag", "Name", "Unit", "Status", "Criticality"],
            rows=[[e.get("tag"), str(e.get("name", ""))[:52], e.get("unit", ""),
                   str(e.get("status", "")).replace("_", " ").title(),
                   str(e.get("criticality", "")).title()] for e in attention],
            highlight={"Criticality": "Critical"}))

    open_findings = [i for i in inspections if i.get("result") in ("attention", "fail")]
    if open_findings:
        analysis.tables.append(TableBlock(
            title="Open inspection findings",
            columns=["Equipment", "Date", "Type", "Finding", "Recommendation"],
            rows=[[i.get("equipment_tag"), i.get("inspected_on"),
                   str(i.get("inspection_type", "")).title(),
                   str(i.get("findings", ""))[:120],
                   str(i.get("recommendation", ""))[:120]] for i in open_findings]))
        for finding in open_findings[:4]:
            analysis.findings.append(Finding(
                title=f"{finding.get('equipment_tag')} — "
                      f"{str(finding.get('inspection_type', '')).title()}",
                detail=str(finding.get("findings", ""))[:400],
                magnitude="OPEN",
                severity="high",
                evidence=f"Inspection {finding.get('inspected_on')}, "
                         f"{finding.get('inspector', '')}"))
            analysis.recommendations.append(Recommendation(
                title=str(finding.get("recommendation", ""))[:140],
                rationale=f"{finding.get('equipment_tag')}: "
                          f"{str(finding.get('findings', ''))[:160]}",
                owner=str(finding.get("inspector", "")), priority=1))

    short = _below_minimum()
    if short:
        analysis.tables.append(TableBlock(
            title="Spares below minimum",
            columns=["Part", "Description", "In stock", "Minimum", "For", "Supplier"],
            rows=[[s.get("part_number"), str(s.get("description", ""))[:52],
                   int(s.get("quantity") or 0), int(s.get("minimum_stock") or 0),
                   s.get("equipment_tag") or "—", s.get("supplier", "")]
                  for s in short],
            highlight={"In stock": "low"}))
        analysis.risks.append(Risk(
            risk=f"{len(short)} spare line(s) below minimum stock",
            likelihood="medium", impact="medium",
            mitigation="Raise requisitions before the next planned intervention."))

    analysis.executive_summary = (
        f"The register holds {len(equipment)} assets, {by_criticality.get('critical', 0)} "
        f"of them rated critical. Availability stands at {availability:.1f}%. "
        f"{len(open_findings)} inspection finding(s) remain open and "
        f"{sum(1 for m in maintenance if m.get('status') == 'in_progress')} work "
        f"order(s) are in progress. "
        + (f"{len(short)} spare line(s) sit below minimum stock." if short else
           "All monitored spares are at or above minimum stock."))
    analysis.source_data = ["Equipment register", "Inspection register",
                            "Maintenance history", "Inventory register",
                            "Alert log"]
    return analysis


def maintenance_performance() -> Optional[IndustrialAnalysis]:
    """Cost, effort and downtime over time - the management view."""
    from industrial import demo_refinery as demo

    plant = _plant()
    maintenance = plant.maintenance_history(limit=500)
    equipment = plant.list_equipment(limit=500)
    if not maintenance:
        return None

    analysis = IndustrialAnalysis(
        title="Maintenance Performance Review",
        subtitle="Cost, effort and downtime",
        contains_demo_data=_demo(maintenance + equipment))

    by_type = Counter(str(m.get("maintenance_type", "unknown")) for m in maintenance)
    hours = sum(float(m.get("duration_hours") or 0) for m in maintenance)
    planned = sum(1 for m in maintenance
                  if m.get("maintenance_type") in ("preventive", "predictive"))
    planned_share = planned / len(maintenance) * 100 if maintenance else 0

    analysis.headline = (
        f"{planned_share:.0f}% of interventions were planned; "
        f"{by_type.get('corrective', 0)} were corrective.")
    analysis.kpis = [
        KPI("Interventions", str(len(maintenance)), note="on record", tone=NEUTRAL),
        KPI("Planned share", f"{planned_share:.0f}%",
            note="preventive plus predictive",
            tone=GOOD if planned_share >= 70 else WARN if planned_share >= 50 else BAD),
        KPI("Maintenance hours", f"{hours:,.0f}", note="recorded effort", tone=NEUTRAL),
        KPI("Corrective", str(by_type.get("corrective", 0)),
            note="unplanned interventions",
            tone=BAD if by_type.get("corrective", 0) > planned else WARN),
    ]

    # The monthly series, when the demo dataset supplies it. Guarded, because
    # a real installation will not have these curated tables.
    cost = getattr(demo, "MAINTENANCE_COST", None)
    production = getattr(demo, "PRODUCTION_KPI", None)
    if cost and _demo(maintenance):
        analysis.tables.append(TableBlock(
            title="Monthly maintenance cost",
            columns=["Month", "Planned", "Unplanned", "Contract", "Spares", "Total"],
            rows=[list(row) for row in cost],
            formats={c: MONEY for c in ("Planned", "Unplanned", "Contract",
                                        "Spares", "Total")},
            total_columns=["Planned", "Unplanned", "Contract", "Spares", "Total"],
            note="INR lakh."))
        totals = [row[5] for row in cost]
        worst = max(cost, key=lambda r: r[5])
        analysis.kpis.append(KPI(
            "Peak month", worst[0], note=f"INR {worst[5]:,.1f} lakh", tone=BAD))
        analysis.kpis.append(KPI(
            "Mean monthly", f"{statistics.mean(totals):,.0f}",
            note="INR lakh", tone=NEUTRAL))
        analysis.findings.append(Finding(
            title=f"Cost peaked in {worst[0]}",
            detail=(f"Total maintenance spend reached INR {worst[5]:,.1f} lakh against "
                    f"a monthly mean of {statistics.mean(totals):,.1f}. Unplanned work "
                    f"accounted for INR {worst[2]:,.1f} lakh of it."),
            magnitude=f"{(worst[5] / statistics.mean(totals) - 1) * 100:.0f}% above mean",
            severity="high", evidence="Monthly maintenance cost ledger"))

    if production and _demo(maintenance):
        analysis.tables.append(TableBlock(
            title="Production and downtime",
            columns=["Month", "Crude processed (kt)", "Utilisation %",
                     "Energy intensity", "Unplanned downtime (h)", "On-spec %"],
            rows=[list(row) for row in production],
            formats={"Crude processed (kt)": NUMBER, "Utilisation %": PERCENT,
                     "Energy intensity": DECIMAL, "Unplanned downtime (h)": NUMBER,
                     "On-spec %": PERCENT},
            note="Monthly operating record."))
        downtime = [row[4] for row in production]
        analysis.kpis.append(KPI(
            "Downtime", f"{sum(downtime):,.0f} h",
            note=f"worst month {max(production, key=lambda r: r[4])[0]}",
            tone=WARN))

    analysis.tables.append(TableBlock(
        title="Interventions by type",
        columns=["Type", "Count", "Hours", "Share %"],
        rows=[[t.title(), n,
               round(sum(float(m.get("duration_hours") or 0) for m in maintenance
                         if m.get("maintenance_type") == t), 1),
               round(n / len(maintenance) * 100, 1)]
              for t, n in by_type.most_common()],
        formats={"Count": NUMBER, "Hours": DECIMAL, "Share %": PERCENT},
        total_columns=["Count", "Hours"]))

    heaviest = sorted(
        _by_equipment(maintenance).items(),
        key=lambda kv: sum(float(m.get("duration_hours") or 0) for m in kv[1]),
        reverse=True)[:10]
    if heaviest:
        analysis.tables.append(TableBlock(
            title="Effort by equipment",
            columns=["Equipment", "Interventions", "Hours", "Last worked"],
            rows=[[tag, len(rows),
                   round(sum(float(m.get("duration_hours") or 0) for m in rows), 1),
                   max((m.get("performed_on", "") for m in rows), default="")]
                  for tag, rows in heaviest],
            formats={"Interventions": NUMBER, "Hours": DECIMAL},
            total_columns=["Interventions", "Hours"]))

    if planned_share < 70:
        analysis.recommendations.append(Recommendation(
            title="Raise the planned share of maintenance work",
            rationale=(f"Only {planned_share:.0f}% of interventions were planned. "
                       f"Corrective work costs more per hour and is unbudgeted."),
            impact="Cost and availability", priority=1, owner="Reliability"))
    analysis.recommendations.append(Recommendation(
        title="Review the heaviest consumers of maintenance effort",
        rationale=(f"{heaviest[0][0]} accounts for the largest share of recorded hours."
                   if heaviest else "Effort is spread across the asset base."),
        impact="Targets the reliability programme where it pays", priority=2,
        owner="Reliability"))

    analysis.executive_summary = (
        f"{len(maintenance)} interventions are on record, totalling {hours:,.0f} hours. "
        f"{planned_share:.0f}% were planned. "
        + (f"Spend peaked in {max(cost, key=lambda r: r[5])[0]} at INR "
           f"{max(row[5] for row in cost):,.1f} lakh. " if cost and _demo(maintenance) else "")
        + "Effort is concentrated in a small number of assets, which is where the "
          "reliability programme should be aimed.")
    analysis.source_data = ["Maintenance history", "Equipment register",
                            "Monthly cost ledger", "Production record"]
    return analysis


def safety_performance() -> Optional[IndustrialAnalysis]:
    """HSE KPIs and the incident register."""
    from industrial import demo_refinery as demo

    safety = getattr(demo, "SAFETY_KPI", None)
    incidents = getattr(demo, "INCIDENTS", None)
    if not safety:
        return None

    analysis = IndustrialAnalysis(
        title="Safety Performance Report",
        subtitle=f"{len(safety)} months to {safety[-1][0]}",
        contains_demo_data=True)

    recordable = sum(row[1] for row in safety)
    near_miss = sum(row[2] for row in safety)
    lti = sum(row[3] for row in safety)
    permits = sum(row[4] for row in safety)
    observations = sum(row[5] for row in safety)
    training = sum(row[6] for row in safety)

    analysis.headline = (
        f"{recordable} recordable incident(s) and {lti} lost-time injury over "
        f"{len(safety)} months, against {near_miss} near misses reported.")
    analysis.kpis = [
        KPI("Recordable", str(recordable), note=f"over {len(safety)} months",
            tone=GOOD if recordable <= len(safety) * 0.5 else WARN),
        KPI("Lost time", str(lti), note="LTI cases",
            tone=GOOD if lti == 0 else BAD),
        KPI("Near misses", str(near_miss),
            note="reported - higher is better reporting culture", tone=GOOD),
        KPI("Reporting ratio", f"{near_miss / max(recordable, 1):.0f}:1",
            note="near misses per recordable",
            tone=GOOD if near_miss / max(recordable, 1) >= 10 else WARN),
        KPI("Permits", f"{permits:,}", note="issued", tone=NEUTRAL),
        KPI("Training", f"{training:,} h", note="delivered", tone=NEUTRAL),
    ]

    analysis.tables.append(TableBlock(
        title="Monthly safety KPIs",
        columns=["Month", "Recordable", "Near misses", "LTI", "Permits",
                 "Observations", "Training hours"],
        rows=[list(row) for row in safety],
        formats={c: NUMBER for c in ("Recordable", "Near misses", "LTI", "Permits",
                                     "Observations", "Training hours")},
        total_columns=["Recordable", "Near misses", "LTI", "Permits",
                       "Observations", "Training hours"],
        highlight={"LTI": ">0"}))

    if incidents:
        analysis.tables.append(TableBlock(
            title="Incident register",
            columns=["Reference", "Date", "Unit", "Category", "Severity", "Title",
                     "Lost days", "Cost"],
            rows=[[i["incident_id"], i["occurred_on"], i["unit"],
                   str(i["category"]).replace("_", " ").title(),
                   str(i["severity"]).title(), i["title"][:80],
                   i.get("lti", 0), i.get("cost_lakh", 0.0)] for i in incidents],
            formats={"Lost days": NUMBER, "Cost": MONEY},
            total_columns=["Lost days", "Cost"],
            highlight={"Severity": "Serious"},
            note="Cost in INR lakh."))
        for incident in incidents:
            if incident.get("severity") == "serious" or incident.get("lti", 0) > 0:
                analysis.findings.append(Finding(
                    title=f"{incident['incident_id']} — {incident['title']}",
                    detail=str(incident.get("description", ""))[:400],
                    magnitude=f"{incident.get('lti', 0)} lost days, INR "
                              f"{incident.get('cost_lakh', 0)} lakh",
                    severity="critical",
                    evidence=f"Root cause: {str(incident.get('root_cause', ''))[:200]}"))
                analysis.recommendations.append(Recommendation(
                    title=str(incident.get("actions", ""))[:140],
                    rationale=str(incident.get("root_cause", ""))[:200],
                    owner="HSE", priority=1))

    analysis.risks.append(Risk(
        risk="Deferred structural defects on access routes",
        likelihood="low", impact="high",
        mitigation="Automatic high-priority classification for access-route defects, "
                   "introduced after INC-2025-011."))

    analysis.executive_summary = (
        f"Over {len(safety)} months the site recorded {recordable} recordable "
        f"incident(s) and {lti} lost-time injury, with {near_miss} near misses "
        f"reported - a ratio of {near_miss / max(recordable, 1):.0f} to 1, which "
        f"indicates a functioning reporting culture. {permits:,} permits were issued "
        f"and {training:,} hours of training delivered.")
    analysis.source_data = ["Monthly HSE KPI record", "Incident register"]
    return analysis


def production_performance() -> Optional[IndustrialAnalysis]:
    from industrial import demo_refinery as demo

    production = getattr(demo, "PRODUCTION_KPI", None)
    if not production:
        return None

    analysis = IndustrialAnalysis(
        title="Production Performance",
        subtitle=f"{len(production)} months to {production[-1][0]}",
        contains_demo_data=True)

    crude = [row[1] for row in production]
    utilisation = [row[2] for row in production]
    downtime = [row[4] for row in production]
    onspec = [row[5] for row in production]

    analysis.headline = (
        f"Mean utilisation {statistics.mean(utilisation):.1f}% with "
        f"{sum(downtime):,.0f} hours of unplanned downtime over the period.")
    analysis.kpis = [
        KPI("Crude processed", f"{sum(crude):,} kt", note="cumulative", tone=NEUTRAL),
        KPI("Utilisation", f"{statistics.mean(utilisation):.1f}%",
            note=f"best {max(utilisation):.1f}%",
            tone=GOOD if statistics.mean(utilisation) > 92 else WARN),
        KPI("Unplanned downtime", f"{sum(downtime):,.0f} h",
            note=f"worst month {max(production, key=lambda r: r[4])[0]}",
            tone=BAD if sum(downtime) > len(production) * 20 else WARN),
        KPI("On-spec", f"{statistics.mean(onspec):.1f}%",
            note=f"low {min(onspec):.1f}%",
            tone=GOOD if statistics.mean(onspec) > 99 else WARN),
    ]
    analysis.tables.append(TableBlock(
        title="Monthly production",
        columns=["Month", "Crude processed (kt)", "Utilisation %",
                 "Energy intensity", "Unplanned downtime (h)", "On-spec %"],
        rows=[list(row) for row in production],
        formats={"Crude processed (kt)": NUMBER, "Utilisation %": PERCENT,
                 "Energy intensity": DECIMAL, "Unplanned downtime (h)": NUMBER,
                 "On-spec %": PERCENT},
        total_columns=["Crude processed (kt)", "Unplanned downtime (h)"]))

    worst = max(production, key=lambda r: r[4])
    analysis.findings.append(Finding(
        title=f"{worst[0]} lost {worst[4]} hours to unplanned events",
        detail=(f"Utilisation fell to {worst[2]:.1f}% and on-spec production to "
                f"{worst[5]:.1f}%, the weakest month in the period."),
        magnitude=f"{worst[4]} h", severity="high",
        evidence="Monthly production record"))
    analysis.executive_summary = (
        f"The site processed {sum(crude):,} kt over {len(production)} months at a mean "
        f"utilisation of {statistics.mean(utilisation):.1f}%. Unplanned downtime "
        f"totalled {sum(downtime):,.0f} hours, concentrated in {worst[0]}. On-spec "
        f"production averaged {statistics.mean(onspec):.1f}%.")
    analysis.source_data = ["Monthly production record"]
    return analysis


def inventory_status() -> Optional[IndustrialAnalysis]:
    plant = _plant()
    try:
        with plant._conn() as conn:
            stock = [dict(r) for r in conn.execute(
                "SELECT * FROM plant_inventory ORDER BY part_number").fetchall()]
    except Exception:
        return None
    if not stock:
        return None

    short = [s for s in stock if (s.get("quantity") or 0) < (s.get("minimum_stock") or 0)]
    analysis = IndustrialAnalysis(
        title="Spares and Inventory Status",
        subtitle=f"{len(stock)} line items",
        contains_demo_data=_demo(stock))
    analysis.headline = (
        f"{len(short)} of {len(stock)} line(s) are below minimum stock."
        if short else f"All {len(stock)} monitored lines are at or above minimum stock.")
    analysis.kpis = [
        KPI("Line items", str(len(stock)), note="tracked", tone=NEUTRAL),
        KPI("Below minimum", str(len(short)), note="need replenishment",
            tone=BAD if short else GOOD),
        KPI("Categories", str(len({s.get("category") for s in stock})),
            note="spare classes", tone=NEUTRAL),
    ]
    analysis.tables.append(TableBlock(
        title="Inventory register",
        columns=["Part", "Description", "Category", "In stock", "Minimum",
                 "Status", "For", "Supplier", "Location"],
        rows=[[s.get("part_number"), str(s.get("description", ""))[:56],
               s.get("category", ""), int(s.get("quantity") or 0),
               int(s.get("minimum_stock") or 0),
               "BELOW MINIMUM" if (s.get("quantity") or 0) < (s.get("minimum_stock") or 0)
               else "OK",
               s.get("equipment_tag") or "—", s.get("supplier", ""),
               s.get("storage_location", "")] for s in stock],
        formats={"In stock": NUMBER, "Minimum": NUMBER},
        highlight={"Status": "BELOW MINIMUM"}))
    for spare in short:
        analysis.recommendations.append(Recommendation(
            title=f"Replenish {spare.get('part_number')}",
            rationale=(f"{spare.get('quantity')} in stock against minimum "
                       f"{spare.get('minimum_stock')}"
                       + (f"; required by {spare.get('equipment_tag')}"
                          if spare.get("equipment_tag") else "")),
            owner="Stores", priority=1 if spare.get("equipment_tag") else 2))
    analysis.executive_summary = (
        f"The register tracks {len(stock)} spare lines across "
        f"{len({s.get('category') for s in stock})} categories. "
        + (f"{len(short)} line(s) are below minimum: "
           f"{', '.join(s['part_number'] for s in short)}." if short
           else "No line is below its minimum."))
    analysis.source_data = ["Inventory register"]
    return analysis


def company_overview() -> Optional[IndustrialAnalysis]:
    """The site itself - for an orientation deck or an annual summary."""
    from industrial import demo_refinery as demo

    company = getattr(demo, "COMPANY", None)
    if not company:
        return None
    plant = _plant()
    equipment = plant.list_equipment(limit=500)

    analysis = IndustrialAnalysis(
        title=company["site"],
        subtitle=f"{company['location']} · {company['capacity_mmtpa']} MMTPA",
        contains_demo_data=True)
    analysis.headline = (
        f"{company['name']} operates a {company['capacity_mmtpa']} MMTPA refinery "
        f"with a Nelson complexity of {company['complexity_nelson']}.")
    analysis.kpis = [
        KPI("Capacity", f"{company['capacity_mmtpa']} MMTPA",
            note=f"{company['capacity_bpd']:,} bpd", tone=NEUTRAL),
        KPI("Complexity", str(company["complexity_nelson"]),
            note="Nelson index", tone=NEUTRAL),
        KPI("Units", str(len(demo.UNITS)), note="process units", tone=NEUTRAL),
        KPI("Employees", f"{company['employees']:,}", note=company["shift_pattern"],
            tone=NEUTRAL),
        KPI("Registered assets", str(len(equipment)),
            note="in the equipment register", tone=NEUTRAL),
        KPI("Commissioned", company["founded"], note="original CDU and utilities",
            tone=NEUTRAL),
    ]
    analysis.tables.append(TableBlock(
        title="Process units",
        columns=["Unit", "Name", "Capacity (MMTPA)", "Purpose", "Commissioned"],
        rows=[[code, name, capacity or "—", purpose, year]
              for code, name, capacity, purpose, year in demo.UNITS],
        formats={"Capacity (MMTPA)": DECIMAL}))
    analysis.tables.append(TableBlock(
        title="Site history",
        columns=["Year", "Event"],
        rows=[[year, event] for year, event in demo.TIMELINE]))

    projects = getattr(demo, "PROJECTS", None)
    if projects:
        analysis.tables.append(TableBlock(
            title="Capital projects",
            columns=["Reference", "Project", "Status", "Start", "Finish",
                     "Budget", "Spent", "Owner"],
            rows=[[p["project_id"], p["name"], str(p["status"]).replace("_", " ").title(),
                   p["start"], p["finish"], p["budget_lakh"], p["spent_lakh"],
                   p["owner"]] for p in projects],
            formats={"Budget": MONEY, "Spent": MONEY},
            total_columns=["Budget", "Spent"], note="INR lakh."))
        analysis.timeline = [
            Milestone(phase=p["name"][:40], window=f"{p['start']} → {p['finish']}",
                      actions=[p["objective"]],
                      outcome=str(p["status"]).replace("_", " ").title())
            for p in projects if p["status"] != "completed"][:4]

    analysis.executive_summary = (
        f"{company['name']} operates {company['site']} at {company['location']}, "
        f"commissioned in {company['founded']} and now rated "
        f"{company['capacity_mmtpa']} MMTPA across {len(demo.UNITS)} process units. "
        f"The site runs {company['shift_pattern']} with {company['employees']:,} "
        f"employees and holds {company['certifications']}. "
        f"{company['maintenance_strategy']}")
    analysis.source_data = ["Company profile", "Unit register", "Project register",
                            "Equipment register"]
    return analysis


# ============================================================== helpers ====

def _all_history():
    plant = _plant()
    inspections = plant.inspection_history(limit=200)
    maintenance = plant.maintenance_history(limit=200)
    try:
        with plant._conn() as conn:
            alerts = [dict(r) for r in conn.execute(
                "SELECT * FROM plant_alerts ORDER BY raised_at DESC LIMIT 100").fetchall()]
    except Exception:
        alerts = []
    return inspections, maintenance, alerts


def _status_by_unit(equipment) -> List[List[Any]]:
    units: Dict[str, Counter] = defaultdict(Counter)
    for item in equipment:
        unit = item.get("unit") or "Unassigned"
        units[unit]["total"] += 1
        units[unit][str(item.get("status", "unknown"))] += 1
        if item.get("criticality") == "critical":
            units[unit]["critical"] += 1
    rows = []
    for unit in sorted(units):
        counts = units[unit]
        unavailable = (counts["maintenance"] + counts["out_of_service"]
                       + counts["failed"] + counts["degraded"])
        rows.append([unit, counts["total"], counts["running"], counts["standby"],
                     unavailable, counts["critical"]])
    return rows


def _by_equipment(rows) -> Dict[str, List]:
    grouped: Dict[str, List] = defaultdict(list)
    for row in rows:
        grouped[row.get("equipment_tag", "—")].append(row)
    return grouped


def _below_minimum() -> List[Dict[str, Any]]:
    plant = _plant()
    try:
        with plant._conn() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM plant_inventory WHERE quantity < minimum_stock "
                "ORDER BY part_number").fetchall()]
    except Exception:
        return []


# =============================================================== routing ==
# Which analysis answers a request. Deliberately keyword-driven and
# deterministic: a model deciding this would add a round trip and a failure
# mode to something a table of words settles reliably.

_TOPICS = [
    (("reliability", "asset health", "equipment status", "fleet", "availability",
      "asset overview", "equipment overview", "plant status"), reliability_overview),
    (("maintenance performance", "maintenance cost", "maintenance review",
      "maintenance kpi", "maintenance report", "downtime cost", "maintenance spend"),
     maintenance_performance),
    (("safety", "hse", "incident", "injury", "lti", "near miss"), safety_performance),
    (("production", "throughput", "utilisation", "utilization", "crude processed",
      "operations kpi", "yield"), production_performance),
    (("inventory", "spares", "stock", "stores", "reorder"), inventory_status),
    (("company", "refinery overview", "about the refinery", "site overview",
      "organisation", "organization", "history", "profile", "orientation"),
     company_overview),
]


def analysis_for(request: str) -> Optional[IndustrialAnalysis]:
    """Pick and build the analysis a request is asking for, or None.

    None means "this is not a plant-data question" - the caller then falls
    back to the ordinary topic path. It never guesses: a request that mentions
    no equipment and matches no topic gets no analysis, because inventing one
    would put fabricated plant figures into a document.
    """
    text = (request or "").lower()
    if not text.strip():
        return None

    # An explicit equipment tag wins over any topic word: "maintenance report
    # for P-101" is about P-101, not about maintenance in general.
    import re
    for match in re.findall(r"\b[A-Z]{1,4}-\d{1,4}[A-Z]?\b", request or "",
                            flags=re.IGNORECASE):
        try:
            dossier = equipment_dossier(match)
        except Exception as error:
            logger.debug(f"dossier failed for {match}: {error}")
            continue
        if dossier:
            return dossier

    for keywords, builder in _TOPICS:
        if any(keyword in text for keyword in keywords):
            try:
                return builder()
            except Exception as error:
                logger.warning(f"{builder.__name__} failed: {error}")
                return None
    return None


def available_analyses() -> List[Dict[str, str]]:
    """What can be built right now, for the interface and the help text."""
    out = []
    for keywords, builder in _TOPICS:
        out.append({"name": builder.__name__.replace("_", " ").title(),
                    "builder": builder.__name__,
                    "triggers": ", ".join(keywords[:4])})
    return out
