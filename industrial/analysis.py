"""
The shared analytical model behind every deliverable.

One analysis object feeds the Word report, the Excel workbook and the
PowerPoint deck, so a number cannot say 4.8 mm/s in the deck and 4.6 in the
workbook. Build the analysis once, render it three ways:

    IndustrialAnalysis ──> report_builder   ──> .docx
                      ├──> workbook_builder ──> .xlsx
                      └──> deck_builder     ──> .pptx

Everything here is plain data. No rendering, no file IO, no model calls - a
builder assembles facts from the local database (or from a spreadsheet the
user attached) and, where a language model is available, an optional narrative
is layered on top. Every figure carries where it came from, and anything
derived from DEMO rows is flagged, so a generated deck can never quietly
present sample data as a plant measurement.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from core.logger import get_logger

logger = get_logger("industrial.analysis")

GOOD, BAD, NEUTRAL, WARN = "good", "bad", "neutral", "warn"


@dataclass
class KPI:
    """A headline number. `tone` drives colour, not the value's sign - a
    falling defect count is good, a falling availability is bad."""
    label: str
    value: str
    note: str = ""
    tone: str = NEUTRAL
    delta: str = ""

    def as_row(self) -> List[Any]:
        return [self.label, self.value, self.delta, self.note]


@dataclass
class Finding:
    """Something the data says, with its size. A finding without a magnitude
    is an opinion, so `magnitude` is part of the shape."""
    title: str
    detail: str
    magnitude: str = ""
    severity: str = "medium"
    evidence: str = ""


@dataclass
class TableBlock:
    """A table that appears in all three outputs. `formats` maps a column name
    to an Excel number format so the workbook stays typed."""
    title: str
    columns: List[str]
    rows: List[List[Any]]
    formats: Dict[str, str] = field(default_factory=dict)
    note: str = ""
    total_columns: List[str] = field(default_factory=list)
    highlight: Dict[str, str] = field(default_factory=dict)   # column -> rule

    def column_index(self, name: str) -> int:
        return self.columns.index(name)


@dataclass
class Recommendation:
    title: str
    rationale: str = ""
    impact: str = ""
    effort: str = ""
    owner: str = ""
    priority: int = 2          # 1 = do first


@dataclass
class Risk:
    risk: str
    likelihood: str = "medium"
    impact: str = "medium"
    mitigation: str = ""


@dataclass
class Milestone:
    phase: str
    window: str
    actions: List[str] = field(default_factory=list)
    outcome: str = ""


@dataclass
class IndustrialAnalysis:
    """What every generated deliverable is rendered from."""
    title: str
    subtitle: str = ""
    headline: str = ""                      # the single sentence that matters
    executive_summary: str = ""
    kpis: List[KPI] = field(default_factory=list)
    findings: List[Finding] = field(default_factory=list)
    tables: List[TableBlock] = field(default_factory=list)
    financials: Optional[TableBlock] = None
    risks: List[Risk] = field(default_factory=list)
    recommendations: List[Recommendation] = field(default_factory=list)
    timeline: List[Milestone] = field(default_factory=list)
    decision_request: str = ""
    source_data: List[str] = field(default_factory=list)
    citations: List[str] = field(default_factory=list)
    contains_demo_data: bool = False
    generated_at: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M"))
    narrative_included: bool = False

    def demo_banner(self) -> str:
        return ("DEMO DATA - figures come from generated sample records, not from a "
                "real plant.") if self.contains_demo_data else ""

    def table(self, title: str) -> Optional[TableBlock]:
        for block in self.tables:
            if block.title.lower() == title.lower():
                return block
        return None

    def summary_dict(self) -> Dict[str, Any]:
        return {
            "title": self.title, "headline": self.headline,
            "kpis": [k.label + ": " + k.value for k in self.kpis],
            "findings": len(self.findings), "tables": len(self.tables),
            "recommendations": len(self.recommendations),
            "risks": len(self.risks), "milestones": len(self.timeline),
            "demo_data": self.contains_demo_data,
        }


# --------------------------------------------------------- narrative ------

def add_narrative(analysis: IndustrialAnalysis, extra_context: str = "") -> bool:
    """Ask the local model for an executive summary over the assembled facts.

    Optional by design. Returns False when Ollama is unreachable, and the
    caller carries on with a deterministic summary - a report must never fail
    because a model is down, and must never invent numbers the data lacks.
    """
    try:
        from core.model_router import model_router
        from core.ollama_client import ollama_client
        if not ollama_client.is_available():
            return False
        model = model_router.route("reasoning")
        if not model:
            return False

        facts = [f"TITLE: {analysis.title}"]
        facts += [f"KPI: {k.label} = {k.value} ({k.note})" for k in analysis.kpis]
        facts += [f"FINDING: {f.title} - {f.detail} [{f.magnitude}]"
                  for f in analysis.findings[:12]]
        for block in analysis.tables[:2]:
            facts.append(f"TABLE {block.title}: columns {', '.join(block.columns)}, "
                         f"{len(block.rows)} rows")
        if extra_context:
            facts.append(extra_context)

        text = ollama_client.chat(model, [
            {"role": "system", "content":
                "You are a plant reliability and operations manager writing for an "
                "executive audience. Use ONLY the figures given. Never invent a "
                "measurement, a cost or a date. Be specific and brief."},
            {"role": "user", "content":
                "\n".join(facts) +
                "\n\nWrite an executive summary of at most 120 words: the situation, "
                "the single most important issue with its number, and the action you "
                "recommend. No headings, no preamble."},
        ], retries=0, timeout=180.0)

        if not text or text.strip().startswith("["):
            return False
        analysis.executive_summary = text.strip()
        analysis.narrative_included = True
        return True
    except Exception as e:
        logger.debug(f"narrative skipped: {e}")
        return False


# ---------------------------------------------------------- builders ------

def _severity_rank(value: str) -> int:
    return {"critical": 0, "high": 1, "medium": 2, "low": 3}.get((value or "").lower(), 4)


def build_operations_analysis(narrative: bool = True) -> IndustrialAnalysis:
    """The whole plant: availability, alerts, maintenance load, spares, safety.

    Every number is computed from the local tables. Nothing is estimated.
    """
    from industrial import plant_data as pd
    from industrial import plant_ops as ops

    summary = ops.operations_summary()
    alerts = ops.alert_overview(status="open")
    maintenance = ops.maintenance_overview(limit=500)
    inventory = ops.inventory_summary()
    safety = ops.safety_overview()
    equipment = pd.list_equipment(limit=10000)

    analysis = IndustrialAnalysis(
        title="Operations and Maintenance Review",
        subtitle="Plant status, reliability exposure and the actions it implies",
        contains_demo_data=summary["provenance"]["contains_demo_data"],
    )

    critical = len(safety["critical_open"])
    overdue = len(safety["overdue_inspections"])
    low_stock = inventory["below_minimum_count"]
    due_30 = summary["maintenance_due_30d"]

    analysis.kpis = [
        KPI("Availability",
            f"{summary['availability_pct']}%" if summary["availability_pct"] is not None else "n/a",
            f"{summary['equipment_operational']} of {summary['equipment_total']} operational",
            GOOD if (summary["availability_pct"] or 0) >= 90 else WARN),
        KPI("Open alerts", str(summary["open_alerts"]),
            ", ".join(f"{n} {sev}" for sev, n in summary["alerts_by_severity"].items())
            or "none", BAD if critical else (WARN if summary["open_alerts"] else GOOD)),
        KPI("Critical / overdue", str(critical + overdue),
            f"{critical} critical alerts, {overdue} overdue inspections",
            BAD if (critical + overdue) else GOOD),
        KPI("Due in 30 days", str(due_30),
            "planned and predicted work", WARN if due_30 else GOOD),
        KPI("Spares below min.", str(low_stock),
            f"of {inventory['item_count']} stock lines",
            BAD if low_stock else GOOD),
    ]

    # ---- findings, worst first, each one quantified
    for alert in sorted(alerts["alerts"], key=lambda a: _severity_rank(a["severity"]))[:6]:
        analysis.findings.append(Finding(
            title=f"{alert.get('equipment_tag') or 'Plant'}: {alert['category']} alert",
            detail=alert["description"],
            magnitude=alert["severity"].upper(),
            severity=alert["severity"],
            evidence=f"{alert['alert_id']}\nraised {str(alert['raised_at'])[:10]}"))
    for inspection in safety["overdue_inspections"][:3]:
        analysis.findings.append(Finding(
            title=f"{inspection['equipment_tag']}: inspection overdue",
            detail=f"{inspection.get('inspection_type')} was due {inspection.get('next_due')}",
            magnitude="COMPLIANCE",
            severity="high",
            evidence=f"last inspected {inspection.get('inspected_on')}"))
    for item in inventory["below_minimum"][:3]:
        analysis.findings.append(Finding(
            title=f"{item['part_number']}: below minimum stock",
            detail=f"{item.get('description')} - {item['quantity']} on hand against a "
                   f"minimum of {item['minimum_stock']}",
            magnitude=f"{item['quantity']}/{item['minimum_stock']}",
            severity="medium",
            evidence=f"linked to {item.get('equipment_tag') or 'no equipment'}"))

    # ---- tables
    analysis.tables.append(TableBlock(
        title="Equipment register",
        columns=["Tag", "Name", "Type", "Unit", "Location", "Status", "Criticality", "Source"],
        rows=[[e["tag"], e["name"], e.get("equipment_type"), e.get("unit"),
               e.get("location"), e.get("status"), e.get("criticality"), e.get("source")]
              for e in equipment],
        highlight={"Status": "status"},
        note="Every registered item and its current state."))

    analysis.tables.append(TableBlock(
        title="Open alerts",
        columns=["Alert", "Raised", "Equipment", "Category", "Severity", "Description"],
        rows=[[a["alert_id"], a["raised_at"], a.get("equipment_tag"), a.get("category"),
               a["severity"], a["description"]]
              for a in sorted(alerts["alerts"], key=lambda a: _severity_rank(a["severity"]))],
        highlight={"Severity": "severity"},
        note="Sorted worst first."))

    analysis.tables.append(TableBlock(
        title="Maintenance history",
        columns=["Equipment", "Date", "Type", "Description", "Technician", "Hours", "Status"],
        rows=[[m["equipment_tag"], m.get("performed_on"), m.get("maintenance_type"),
               m.get("description"), m.get("technician"), m.get("duration_hours") or 0,
               m.get("status")] for m in maintenance["records"]],
        formats={"Hours": "0.0"},
        total_columns=["Hours"],
        note="Recorded work, newest first."))

    analysis.tables.append(TableBlock(
        title="Spares and inventory",
        columns=["Part number", "Description", "Category", "Quantity", "Minimum",
                 "Unit", "Location", "Equipment"],
        rows=[[i["part_number"], i.get("description"), i.get("category"),
               i.get("quantity") or 0, i.get("minimum_stock") or 0, i.get("unit"),
               i.get("storage_location"), i.get("equipment_tag")]
              for i in inventory["items"]],
        formats={"Quantity": "#,##0", "Minimum": "#,##0"},
        total_columns=["Quantity"],
        highlight={"Quantity": "below_minimum"},
        note="Lines at or below minimum are highlighted."))

    # ---- maintenance load by type, the one place a chart earns its place
    by_type = maintenance["by_type"]
    if by_type:
        analysis.tables.append(TableBlock(
            title="Maintenance by type",
            columns=["Maintenance type", "Records"],
            rows=[[key, value] for key, value in sorted(by_type.items(),
                                                        key=lambda kv: -kv[1])],
            formats={"Records": "#,##0"},
            total_columns=["Records"],
            note="Where the maintenance effort actually goes."))

    # ---- recommendations, derived from the findings rather than invented
    priority = 1
    for alert in safety["critical_open"][:3]:
        analysis.recommendations.append(Recommendation(
            title=f"Close out {alert['alert_id']} on {alert.get('equipment_tag') or 'plant'}",
            rationale=alert["description"],
            impact="Removes a critical open alert",
            effort="Planned work order",
            priority=priority))
        priority += 1
    if overdue:
        analysis.recommendations.append(Recommendation(
            title=f"Clear {overdue} overdue inspection(s)",
            rationale="Statutory and reliability inspections past their due date",
            impact="Restores compliance position",
            effort="Schedule with the inspection contractor",
            priority=priority))
        priority += 1
    if low_stock:
        analysis.recommendations.append(Recommendation(
            title=f"Replenish {low_stock} spare part line(s) below minimum",
            rationale="Stock-outs extend downtime when the linked equipment fails",
            impact="Protects mean time to repair",
            effort="Raise purchase requisitions",
            priority=priority))
        priority += 1
    if due_30:
        analysis.recommendations.append(Recommendation(
            title=f"Confirm resourcing for {due_30} job(s) due within 30 days",
            rationale="Planned and predictive work already scheduled",
            impact="Avoids deferral into breakdown maintenance",
            effort="Weekly planning meeting",
            priority=priority))

    # ---- risks
    if critical:
        analysis.risks.append(Risk(
            "A critical alert remains open on running equipment",
            likelihood="high", impact="high",
            mitigation="Assign an owner and a date to each critical alert this week"))
    if overdue:
        analysis.risks.append(Risk(
            "Inspection overdue on safety-critical equipment",
            likelihood="certain", impact="high",
            mitigation="Book the inspection; consider an interim risk assessment"))
    if low_stock:
        analysis.risks.append(Risk(
            "Spare unavailable at the moment of failure",
            likelihood="medium", impact="high",
            mitigation="Replenish below-minimum lines for critical equipment first"))
    analysis.risks.append(Risk(
        "Condition data is not continuously collected",
        likelihood="high", impact="medium",
        mitigation="Connect the historian to plant_readings so trends are live"))

    # ---- timeline
    today = datetime.now()
    window = lambda a, b: (f"{(today + timedelta(days=a)).strftime('%d %b')} - "
                           f"{(today + timedelta(days=b)).strftime('%d %b')}")
    analysis.timeline = [
        Milestone("Stabilise", window(0, 7),
                  ["Own and date every critical alert",
                   "Confirm isolation and permit status for work in progress",
                   "Raise requisitions for below-minimum spares"],
                  "No unowned critical alerts"),
        Milestone("Recover", window(7, 30),
                  ["Clear overdue inspections",
                   f"Deliver the {due_30} job(s) due this month",
                   "Review repeat failures for a root cause"],
                  "Compliance position restored"),
        Milestone("Improve", window(30, 90),
                  ["Move repeat corrective work onto a preventive interval",
                   "Connect condition data to the workbench",
                   "Set minimum stock from actual consumption"],
                  "Fewer breakdowns, evidenced by the record"),
    ]

    analysis.headline = _operations_headline(summary, critical, overdue, low_stock)
    analysis.decision_request = (
        "Approve the priority actions above and confirm ownership for each critical alert.")
    analysis.executive_summary = _operations_summary_text(summary, critical, overdue,
                                                          low_stock, due_30)
    analysis.source_data = [
        f"plant_equipment: {len(equipment)} rows",
        f"plant_alerts: {alerts['alert_count']} open",
        f"plant_maintenance: {maintenance['record_count']} records",
        f"plant_inventory: {inventory['item_count']} lines",
        f"database: local SQLite, generated {analysis.generated_at}",
    ]
    if analysis.contains_demo_data:
        analysis.citations.append("Rows marked DEMO are generated sample data.")

    if narrative:
        add_narrative(analysis)
    return analysis


def _operations_headline(summary, critical, overdue, low_stock) -> str:
    if critical:
        return (f"{critical} critical alert(s) are open on a plant running at "
                f"{summary['availability_pct']}% availability - close them before "
                f"anything else.")
    if overdue:
        return (f"Availability is {summary['availability_pct']}%, but {overdue} "
                f"inspection(s) are overdue - compliance is the exposure, not capacity.")
    if low_stock:
        return (f"Plant is stable at {summary['availability_pct']}% availability; "
                f"{low_stock} spare line(s) below minimum are the main risk to repair time.")
    return (f"Plant is stable at {summary['availability_pct']}% availability with no "
            f"critical exposure outstanding.")


def _operations_summary_text(summary, critical, overdue, low_stock, due_30) -> str:
    parts = [f"{summary['equipment_operational']} of {summary['equipment_total']} registered "
             f"items are operational ({summary['availability_pct']}%), with "
             f"{summary['equipment_in_maintenance']} in maintenance and "
             f"{summary['equipment_shutdown']} shut down."]
    if summary["open_alerts"]:
        parts.append(f"{summary['open_alerts']} alerts are open"
                     + (f", {critical} of them critical." if critical else "."))
    else:
        parts.append("No alerts are open.")
    if overdue:
        parts.append(f"{overdue} inspection(s) are past their due date.")
    if low_stock:
        parts.append(f"{low_stock} spare part line(s) sit at or below minimum stock.")
    parts.append(f"{due_30} maintenance job(s) fall due within 30 days.")
    return " ".join(parts)


def build_equipment_analysis(equipment_reference: str,
                             narrative: bool = True) -> Optional[IndustrialAnalysis]:
    """One asset in depth: history, inspections, alerts, spares, condition."""
    from industrial import plant_data as pd
    from industrial import plant_ops as ops

    dossier = ops.equipment_dossier(equipment_reference)
    if dossier.get("error"):
        return None
    item = dossier["equipment"]
    tag = item["tag"]
    history = dossier["maintenance_records"]
    inspections = dossier["inspections"]
    open_alerts = dossier["open_alerts"]
    spares = dossier["spare_parts"]

    hours = round(sum(float(r.get("duration_hours") or 0) for r in history), 1)
    corrective = sum(1 for r in history if r.get("maintenance_type") == "corrective")
    preventive = sum(1 for r in history if r.get("maintenance_type") == "preventive")

    analysis = IndustrialAnalysis(
        title=f"{tag} - Equipment Review",
        subtitle=f"{item['name']} · {item.get('location') or 'location unknown'}",
        contains_demo_data=dossier["provenance"]["contains_demo_data"],
    )

    trend = pd.reading_trend(tag, "vibration_rms", 24)
    analysis.kpis = [
        KPI("Status", (item.get("status") or "unknown").title(),
            f"criticality {item.get('criticality')}",
            GOOD if item.get("status") == "operational" else WARN),
        KPI("Maintenance records", str(len(history)),
            f"{hours} recorded hours", NEUTRAL),
        KPI("Corrective / preventive", f"{corrective} / {preventive}",
            "a corrective-heavy history points to an unresolved cause",
            BAD if corrective > preventive else GOOD),
        KPI("Open alerts", str(len(open_alerts)),
            open_alerts[0]["severity"].upper() + " highest" if open_alerts else "none",
            BAD if open_alerts else GOOD),
    ]
    if trend.get("points"):
        analysis.kpis.append(KPI(
            f"Vibration ({trend['unit']})", str(trend["latest"]),
            f"{trend['direction']} from {trend['values'][0]} over {trend['points']} readings",
            BAD if trend["direction"] == "rising" else GOOD,
            delta=f"{round(trend['latest'] - trend['values'][0], 2):+}"))

    for alert in open_alerts:
        analysis.findings.append(Finding(
            title=f"Open {alert['severity']} alert",
            detail=alert["description"], magnitude=alert["severity"].upper(),
            severity=alert["severity"], evidence=alert["alert_id"]))
    for inspection in inspections[:3]:
        if (inspection.get("result") or "").lower() not in ("pass", "ok"):
            analysis.findings.append(Finding(
                title=f"Inspection result: {inspection.get('result')}",
                detail=inspection.get("findings") or "",
                magnitude=(inspection.get("inspection_type") or "").upper(),
                severity="high", evidence=f"inspected {inspection.get('inspected_on')}"))
    if corrective > preventive and history:
        analysis.findings.append(Finding(
            title="Corrective work outweighs preventive",
            detail=f"{corrective} corrective against {preventive} preventive interventions "
                   f"on this asset",
            magnitude=f"{corrective}:{preventive}", severity="medium",
            evidence="plant_maintenance"))

    analysis.tables.append(TableBlock(
        title="Specification",
        columns=["Field", "Value"],
        rows=[["Tag", item["tag"]], ["Name", item["name"]],
              ["Type", item.get("equipment_type")], ["Unit", item.get("unit")],
              ["Location", item.get("location")], ["Status", item.get("status")],
              ["Criticality", item.get("criticality")],
              ["Manufacturer", item.get("manufacturer")], ["Model", item.get("model")],
              ["Installed", item.get("installed_on")]]))
    analysis.tables.append(TableBlock(
        title="Maintenance history",
        columns=["Date", "Type", "Description", "Technician", "Hours", "Findings", "Status"],
        rows=[[m.get("performed_on"), m.get("maintenance_type"), m.get("description"),
               m.get("technician"), m.get("duration_hours") or 0, m.get("findings"),
               m.get("status")] for m in history],
        formats={"Hours": "0.0"}, total_columns=["Hours"]))
    analysis.tables.append(TableBlock(
        title="Inspection history",
        columns=["Date", "Type", "Inspector", "Result", "Findings", "Recommendation",
                 "Next due"],
        rows=[[i.get("inspected_on"), i.get("inspection_type"), i.get("inspector"),
               i.get("result"), i.get("findings"), i.get("recommendation"),
               i.get("next_due")] for i in inspections]))
    if spares:
        analysis.tables.append(TableBlock(
            title="Spare parts",
            columns=["Part number", "Description", "Quantity", "Minimum", "Location"],
            rows=[[s["part_number"], s.get("description"), s.get("quantity") or 0,
                   s.get("minimum_stock") or 0, s.get("storage_location")] for s in spares],
            formats={"Quantity": "#,##0", "Minimum": "#,##0"},
            highlight={"Quantity": "below_minimum"}))
    if trend.get("points"):
        analysis.tables.append(TableBlock(
            title="Condition trend",
            columns=["Reading", f"Value ({trend['unit']})"],
            rows=[[index + 1, value] for index, value in enumerate(trend["values"])],
            formats={f"Value ({trend['unit']})": "0.00"},
            note=f"{trend['parameter']}, source: {', '.join(trend['sources'])}"))

    if open_alerts:
        analysis.recommendations.append(Recommendation(
            title=f"Resolve the open alert on {tag}",
            rationale=open_alerts[0]["description"],
            impact="Removes the active exposure on this asset",
            effort="Work order", priority=1))
    if trend.get("direction") == "rising":
        analysis.recommendations.append(Recommendation(
            title="Inspect before the trend reaches the alarm level",
            rationale=f"{trend['parameter']} has risen from {trend['values'][0]} to "
                      f"{trend['latest']} {trend['unit']}",
            impact="Converts a breakdown into planned work",
            effort="Half a shift", priority=2))
    if corrective > preventive:
        analysis.recommendations.append(Recommendation(
            title="Move this asset onto a preventive interval",
            rationale=f"{corrective} corrective interventions on record",
            impact="Reduces repeat failure and unplanned downtime",
            effort="Planning change", priority=3))
    low = [s for s in spares if (s.get("quantity") or 0) <= (s.get("minimum_stock") or 0)]
    if low:
        analysis.recommendations.append(Recommendation(
            title=f"Replenish {len(low)} spare line(s) for this asset",
            rationale=", ".join(s["part_number"] for s in low),
            impact="Protects repair time when it fails",
            effort="Purchase requisition", priority=2))

    if open_alerts or trend.get("direction") == "rising":
        analysis.risks.append(Risk(
            f"Unplanned failure of {tag} ({item.get('criticality')} criticality)",
            likelihood="medium" if not open_alerts else "high", impact="high",
            mitigation="Inspect at the next opportunity and confirm spares on hand"))

    analysis.headline = _equipment_headline(tag, item, open_alerts, trend, corrective, preventive)
    analysis.executive_summary = analysis.headline + " " + (
        f"{len(history)} maintenance records totalling {hours} hours are on file, with "
        f"{len(inspections)} inspections and {len(spares)} linked spare part lines.")
    analysis.decision_request = (f"Approve the recommended action on {tag} and confirm the "
                                 f"owner and date.")
    analysis.source_data = [
        f"plant_equipment: {tag}",
        f"plant_maintenance: {len(history)} records",
        f"plant_inspections: {len(inspections)} records",
        f"plant_alerts: {len(dossier['alerts'])} total",
        f"plant_readings: {trend.get('points', 0)} readings",
    ]
    for document in dossier.get("related_documents") or []:
        if document.get("source"):
            analysis.citations.append(str(document["source"]))

    if narrative:
        add_narrative(analysis, extra_context=f"Equipment {tag} criticality "
                                              f"{item.get('criticality')}")
    return analysis


def _equipment_headline(tag, item, open_alerts, trend, corrective, preventive) -> str:
    if open_alerts:
        worst = open_alerts[0]
        return (f"{tag} is {item.get('status')} with a {worst['severity']} alert open: "
                f"{worst['description']}")
    if trend.get("direction") == "rising":
        return (f"{tag} is running, but {trend['parameter']} has risen to {trend['latest']} "
                f"{trend['unit']} - inspect before it becomes a failure.")
    if corrective > preventive:
        return (f"{tag} is running, but its history is corrective-heavy "
                f"({corrective} vs {preventive}) - the underlying cause is unresolved.")
    return f"{tag} is {item.get('status')} with no outstanding exposure on record."


# ------------------------------------------------- spreadsheet analysis ---

def _read_tabular(path: str, max_rows: int = 5000) -> Dict[str, Any]:
    """Read an .xlsx/.xlsm/.csv into columns and rows, finding the header row.

    Real spreadsheets rarely start at A1 - they have a title, a blank line and
    then the header. This looks for the first row where most cells are short
    non-empty strings and the row below it has data.
    """
    from pathlib import Path
    file_path = Path(path).expanduser()
    if not file_path.exists():
        return {"error": f"'{path}' does not exist"}

    suffix = file_path.suffix.lower()
    grids: Dict[str, List[List[Any]]] = {}
    if suffix in (".csv", ".tsv", ".txt"):
        import csv
        delimiter = "\t" if suffix == ".tsv" else ","
        with file_path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
            grids[file_path.stem[:31]] = [row for _, row in
                                          zip(range(max_rows), csv.reader(handle, delimiter=delimiter))]
    elif suffix in (".xlsx", ".xlsm", ".xltx"):
        try:
            from openpyxl import load_workbook
        except ImportError:
            return {"error": "openpyxl is not installed - run: pip install openpyxl"}
        book = load_workbook(str(file_path), data_only=True)
        for sheet in book.worksheets:
            rows = []
            for _, row in zip(range(max_rows), sheet.iter_rows(values_only=True)):
                rows.append(list(row))
            grids[sheet.title] = rows
    else:
        return {"error": f"Cannot read '{suffix}' as a table. Supported: .xlsx, .xlsm, .csv"}

    sheets = {}
    for name, rows in grids.items():
        header_index = _find_header_row(rows)
        if header_index is None:
            continue
        columns = [str(c).strip() if c is not None else f"Column {i + 1}"
                   for i, c in enumerate(rows[header_index])]
        body = []
        for row in rows[header_index + 1:]:
            if row is None or all(c is None or str(c).strip() == "" for c in row):
                continue
            padded = list(row) + [None] * (len(columns) - len(row))
            body.append(padded[:len(columns)])
        if body:
            sheets[name] = {"columns": columns, "rows": body,
                            "title_rows": [str(c) for r in rows[:header_index] for c in r
                                           if c not in (None, "")][:3]}
    if not sheets:
        return {"error": "No table with a header row was found in that file"}
    return {"file": str(file_path), "sheets": sheets}


def _find_header_row(rows: List[List[Any]], look: int = 12) -> Optional[int]:
    best, best_score = None, 0
    for index, row in enumerate(rows[:look]):
        if not row:
            continue
        cells = [c for c in row if c is not None and str(c).strip() != ""]
        if len(cells) < 2:
            continue
        strings = sum(1 for c in cells if isinstance(c, str) and 0 < len(str(c)) <= 40)
        below = rows[index + 1] if index + 1 < len(rows) else []
        below_filled = sum(1 for c in (below or []) if c is not None and str(c).strip() != "")
        score = strings + (2 if below_filled >= max(2, len(cells) - 2) else 0)
        if score > best_score:
            best, best_score = index, score
    return best


def _profile_column(values: List[Any]) -> Dict[str, Any]:
    numbers = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
    filled = [v for v in values if v is not None and str(v).strip() != ""]
    profile = {"filled": len(filled), "numeric": len(numbers),
               "is_numeric": bool(numbers) and len(numbers) >= max(2, 0.6 * len(filled))}
    if profile["is_numeric"]:
        profile.update(total=round(sum(numbers), 4), average=round(sum(numbers) / len(numbers), 4),
                       minimum=min(numbers), maximum=max(numbers), count=len(numbers))
    else:
        counts: Dict[str, int] = {}
        for value in filled:
            key = str(value).strip()
            counts[key] = counts.get(key, 0) + 1
        profile["distinct"] = len(counts)
        profile["top"] = sorted(counts.items(), key=lambda kv: -kv[1])[:5]
    return profile


def _number(value: float) -> str:
    """Readable, never scientific. 1.06e+06 tells a reader nothing."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number == int(number) and abs(number) < 1e15:
        return f"{int(number):,}"
    if abs(number) >= 1000:
        return f"{number:,.0f}"
    return f"{number:,.2f}".rstrip("0").rstrip(".")


def build_workbook_analysis(path: str, title: str = "",
                            narrative: bool = True) -> IndustrialAnalysis:
    """Turn a spreadsheet into an executive story, not a pasted table.

    Profiles every sheet and column, then reports what a reader would want to
    know first: the size of the dataset, the biggest numeric totals, the
    concentration in the largest category, and any value that sits far from the
    rest of its column. Numbers come from the file - nothing is invented.
    """
    from pathlib import Path
    data = _read_tabular(path)
    if data.get("error"):
        analysis = IndustrialAnalysis(title=title or "Spreadsheet analysis")
        analysis.headline = data["error"]
        analysis.executive_summary = data["error"]
        return analysis

    file_name = Path(data["file"]).name
    analysis = IndustrialAnalysis(
        title=title or f"Analysis of {file_name}",
        subtitle=f"{len(data['sheets'])} sheet(s) read from {file_name}",
    )
    analysis.source_data.append(f"file: {data['file']}")

    total_rows = 0
    numeric_summaries: List[tuple] = []       # (total, column, sheet, profile)
    for sheet_name, sheet in data["sheets"].items():
        columns, rows = sheet["columns"], sheet["rows"]
        total_rows += len(rows)
        analysis.source_data.append(f"sheet '{sheet_name}': {len(rows)} rows x "
                                    f"{len(columns)} columns")

        formats = {}
        profiles = {}
        for index, column in enumerate(columns):
            profile = _profile_column([row[index] for row in rows])
            profiles[column] = profile
            if profile["is_numeric"]:
                formats[column] = "#,##0.##"
                numeric_summaries.append((profile["total"], column, sheet_name, profile))

        analysis.tables.append(TableBlock(
            title=sheet_name[:31], columns=columns, rows=rows, formats=formats,
            total_columns=[c for c in columns if profiles[c]["is_numeric"]][:4],
            note=(" · ".join(sheet["title_rows"]) if sheet["title_rows"] else
                  f"{len(rows)} rows as read from the file")))

        # ---- category concentration: the single most useful non-numeric fact
        for column, profile in profiles.items():
            if profile.get("is_numeric") or not profile.get("top"):
                continue
            if 1 < profile["distinct"] <= max(12, len(rows) // 3):
                leader, count = profile["top"][0]
                share = count / max(1, profile["filled"])
                if share >= 0.4:
                    analysis.findings.append(Finding(
                        title=f"{column} is concentrated in '{leader}'",
                        detail=f"{count} of {profile['filled']} rows in '{sheet_name}' "
                               f"carry this value.",
                        magnitude=f"{share * 100:.0f}%",
                        severity="medium" if share < 0.7 else "high",
                        evidence=f"{sheet_name}.{column}"))
                break

        # ---- outliers: a value far from its column's average
        for column, profile in profiles.items():
            if not profile.get("is_numeric") or profile["count"] < 4:
                continue
            average = profile["average"]
            spread = profile["maximum"] - profile["minimum"]
            if spread and profile["maximum"] > average * 2.5:
                analysis.findings.append(Finding(
                    title=f"{column}: one value dominates",
                    detail=f"The largest {column} in '{sheet_name}' is "
                           f"{_number(profile['maximum'])} against an average of {_number(average)}.",
                    magnitude=f"{profile['maximum'] / average:.1f}x avg",
                    severity="medium", evidence=f"{sheet_name}.{column}"))
                break

    # ---- KPIs from the file itself
    analysis.kpis.append(KPI("Rows analysed", f"{total_rows:,}",
                             f"across {len(data['sheets'])} sheet(s)", NEUTRAL))
    for total, column, sheet_name, profile in sorted(numeric_summaries, reverse=True)[:3]:
        analysis.kpis.append(KPI(
            column[:26], _number(total),
            f"{profile['count']} values, avg {_number(profile['average'])}", NEUTRAL))
    if len(analysis.kpis) == 1:
        analysis.kpis.append(KPI("Numeric columns", "0",
                                 "nothing in this file could be summed", WARN))

    biggest = max(numeric_summaries, default=None)
    if biggest:
        total, column, sheet_name, profile = biggest
        analysis.headline = (f"{file_name} totals {_number(total)} across "
                             f"{profile['count']} '{column}' values; the largest single "
                             f"entry is {_number(profile['maximum'])}.")
    else:
        analysis.headline = (f"{file_name} contains {total_rows:,} rows with no numeric "
                             f"column to total.")

    analysis.executive_summary = (
        f"{file_name} holds {total_rows:,} data rows across "
        f"{len(data['sheets'])} sheet(s). " + analysis.headline +
        (f" {len(analysis.findings)} pattern(s) stand out and are listed below."
         if analysis.findings else ""))

    analysis.recommendations.append(Recommendation(
        title="Confirm the assumptions behind the largest figures",
        rationale="The totals above drive every downstream conclusion",
        impact="Prevents a decision resting on a mis-keyed cell",
        effort="Review", priority=1))
    if analysis.findings:
        analysis.recommendations.append(Recommendation(
            title=f"Investigate the {len(analysis.findings)} highlighted pattern(s)",
            rationale="Concentration and outliers usually mean either a real "
                      "dependency or a data-entry problem",
            impact="Either a genuine finding or a corrected dataset",
            effort="Analyst review", priority=2))

    analysis.decision_request = ("Confirm these figures are the ones to plan against, or "
                                "supply a corrected file.")
    analysis.citations.append(f"All figures read directly from {file_name}.")

    if narrative:
        add_narrative(analysis, extra_context=f"Source file: {file_name}")
    return analysis
