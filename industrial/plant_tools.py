"""
Industrial plant tools, registered into the EXISTING ZEUS tool registry.

Nothing here is a second assistant, a second database or a second document
generator. Each tool is a thin shell over industrial.plant_ops / plant_docs,
so the dashboard, the decision engine, the voice path and the industrial agents
all reach the same implementation, with the same security confirmation, the
same timeouts and the same audit log as every other ZEUS tool.

Risk levels are chosen for consequence, not convenience: reading plant records
is SAFE, writing records is LOW/MEDIUM, and approving a permit or releasing an
isolation is HIGH_RISK - which the registry refuses to run without an explicit
human confirmation.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from core.logger import get_logger
from core.security_manager import RiskLevel
from core.tool_registry import registry
from industrial import plant_data as pd
from industrial import plant_docs as docs
from industrial import plant_ops as ops

logger = get_logger("industrial.plant_tools")


def _current_operator() -> str:
    """Whoever is logged in, for records that must name a person."""
    try:
        from auth.session_manager import session_manager
        return session_manager.username or ""
    except Exception:
        return ""


def _remember_equipment(tag: str):
    """Keep the last equipment discussed, so 'generate the report for that pump'
    resolves later. Uses the existing memory layer - no new store."""
    if not tag:
        return
    try:
        from memory import database
        database.set_fact("last_equipment", tag)
    except Exception as e:
        logger.debug(f"could not record last equipment: {e}")


def last_equipment() -> str:
    try:
        from memory import database
        return database.get_fact("last_equipment") or ""
    except Exception:
        return ""


_PRONOUNS = {"it", "that", "this", "that one", "them", "same", "the same",
             "that pump", "this pump", "the pump", "that equipment",
             "this equipment", "the equipment", "that unit", "that machine"}


def _resolve(reference: str) -> str:
    """Resolve a spoken/typed reference to equipment.

    A PRONOUN resolves to the last equipment discussed, which is what makes
    "generate the report for that pump" work after "analyse Pump P-204".
    An EMPTY reference stays empty: an omitted filter means "everything", and
    quietly narrowing an unfiltered query to the last equipment discussed makes
    'show active alerts' answer about one pump.
    """
    reference = (reference or "").strip()
    if not reference:
        return ""
    if reference.lower() in _PRONOUNS:
        return last_equipment()
    return reference


# ----------------------------------------------------------- operations --

@registry.register("plant_status",
                   "Summarise plant and operational status: equipment availability, "
                   "open alerts and maintenance due", RiskLevel.SAFE, timeout=30.0)
def plant_status():
    summary = ops.operations_summary()
    summary["summary"] = ops.operations_status_text()
    return summary


@registry.register("monitoring_status",
                   "Show stored condition-monitoring readings and which sources produced them",
                   RiskLevel.SAFE, params={"equipment": "optional equipment tag"}, timeout=30.0)
def monitoring_status(equipment: str = ""):
    return ops.monitoring_snapshot(_resolve(equipment))


@registry.register("equipment_trend",
                   "Show the trend of a measured parameter for one piece of equipment",
                   RiskLevel.SAFE,
                   params={"equipment": "equipment tag", "parameter": "e.g. vibration_rms"},
                   timeout=30.0)
def equipment_trend(equipment: str, parameter: str = "vibration_rms", points: int = 24):
    reference = _resolve(equipment)
    result = ops.monitoring_trend(reference, parameter, points)
    if result.get("equipment"):
        _remember_equipment(result["equipment"])
    return result


# ------------------------------------------------------------ equipment --

@registry.register("list_equipment", "List registered plant equipment", RiskLevel.SAFE,
                   params={"status": "optional status filter",
                           "location": "optional location filter"}, timeout=30.0)
def list_equipment(status: str = "", location: str = ""):
    rows = pd.list_equipment(status=status, location=location)
    return {"equipment": rows, "count": len(rows),
            "by_status": pd.equipment_status_summary()["by_status"],
            "demo_rows": sum(1 for r in rows if r["source"] == pd.DEMO)}


@registry.register("equipment_details",
                   "Everything stored about one piece of equipment: specification, "
                   "maintenance, inspections, alerts, spares and related documents",
                   RiskLevel.SAFE, params={"equipment": "tag or name, e.g. P-204"},
                   timeout=60.0)
def equipment_details(equipment: str):
    reference = _resolve(equipment)
    dossier = ops.equipment_dossier(reference)
    if dossier.get("equipment"):
        _remember_equipment(dossier["equipment"]["tag"])
        dossier["summary"] = ops.equipment_summary_text(reference)
    return dossier


@registry.register("register_equipment", "Add or update a piece of equipment on the register",
                   RiskLevel.LOW_RISK,
                   params={"tag": "unique equipment tag", "name": "equipment name"},
                   timeout=30.0)
def register_equipment(tag: str, name: str = "", equipment_type: str = "", location: str = "",
                       unit: str = "", status: str = "operational", criticality: str = "medium",
                       manufacturer: str = "", model: str = ""):
    pd.upsert_equipment(tag, name=name or tag, equipment_type=equipment_type, location=location,
                        unit=unit, status=status, criticality=criticality,
                        manufacturer=manufacturer, model=model, source="LOCAL")
    _remember_equipment(tag)
    return {"registered": tag, "equipment": pd.get_equipment(tag)}


# ----------------------------------------------------------- maintenance --

@registry.register("maintenance_history",
                   "Show maintenance history for one piece of equipment, or for the plant",
                   RiskLevel.SAFE, params={"equipment": "optional equipment tag"}, timeout=45.0)
def maintenance_history(equipment: str = "", limit: int = 25):
    reference = _resolve(equipment)
    result = ops.maintenance_overview(reference, limit=limit)
    if reference and result.get("equipment") and not result.get("error"):
        _remember_equipment(result["equipment"])
    return result


@registry.register("log_maintenance", "Record completed maintenance work", RiskLevel.LOW_RISK,
                   params={"equipment": "equipment tag", "description": "what was done"},
                   timeout=30.0)
def log_maintenance(equipment: str, description: str, maintenance_type: str = "corrective",
                    technician: str = "", duration_hours: float = 0.0, findings: str = "",
                    parts_used: str = ""):
    reference = _resolve(equipment)
    found = pd.resolve_equipment(reference)
    if not found:
        return {"error": f"No equipment matching '{equipment}'"}
    record_id = pd.add_maintenance(found["tag"], description, maintenance_type=maintenance_type,
                                   technician=technician or _current_operator(),
                                   duration_hours=duration_hours, findings=findings,
                                   parts_used=parts_used, source="LOCAL")
    _remember_equipment(found["tag"])
    return {"logged": True, "record_id": record_id, "equipment": found["tag"]}


@registry.register("schedule_maintenance",
                   "Schedule maintenance for equipment and add it to the ZEUS calendar",
                   RiskLevel.LOW_RISK,
                   params={"equipment": "equipment tag", "description": "work to be done",
                           "due_date": "YYYY-MM-DD (optional)"}, timeout=30.0)
def schedule_maintenance(equipment: str, description: str = "", due_date: str = "",
                         maintenance_type: str = "preventive"):
    reference = _resolve(equipment)
    result = ops.schedule_maintenance(reference, description or "Scheduled maintenance",
                                      due_date=due_date, maintenance_type=maintenance_type,
                                      technician=_current_operator())
    if result.get("equipment"):
        _remember_equipment(result["equipment"])
    return result


@registry.register("maintenance_due", "List maintenance due within a number of days",
                   RiskLevel.SAFE, params={"days": "look-ahead window, default 30"},
                   timeout=30.0)
def maintenance_due(days: int = 30):
    rows = pd.maintenance_due(int(days))
    return {"due": rows, "count": len(rows), "window_days": int(days)}


# ----------------------------------------------------------- inspections --

@registry.register("inspection_history", "Show inspection history for equipment",
                   RiskLevel.SAFE, params={"equipment": "optional equipment tag"}, timeout=30.0)
def inspection_history(equipment: str = "", limit: int = 25):
    reference = _resolve(equipment)
    tag = ""
    if reference:
        found = pd.resolve_equipment(reference)
        if not found:
            return {"error": f"No equipment matching '{equipment}'"}
        tag = found["tag"]
        _remember_equipment(tag)
    rows = pd.inspection_history(tag, limit=limit)
    return {"equipment": tag or "all equipment", "inspections": rows, "count": len(rows)}


@registry.register("log_inspection", "Record an inspection result", RiskLevel.LOW_RISK,
                   params={"equipment": "equipment tag", "result": "pass/attention/fail"},
                   timeout=30.0)
def log_inspection(equipment: str, result: str, inspection_type: str = "visual",
                   findings: str = "", recommendation: str = "", next_due: str = ""):
    found = pd.resolve_equipment(_resolve(equipment))
    if not found:
        return {"error": f"No equipment matching '{equipment}'"}
    record_id = pd.add_inspection(found["tag"], result, inspection_type=inspection_type,
                                  inspector=_current_operator(), findings=findings,
                                  recommendation=recommendation, next_due=next_due,
                                  source="LOCAL")
    _remember_equipment(found["tag"])
    return {"logged": True, "record_id": record_id, "equipment": found["tag"]}


# --------------------------------------------------------------- alerts --

@registry.register("list_plant_alerts",
                   "List industrial alerts (open by default) with severity and equipment",
                   RiskLevel.SAFE,
                   params={"status": "open/acknowledged/resolved/all",
                           "severity": "optional severity filter"}, timeout=30.0)
def list_plant_alerts(status: str = "open", severity: str = "", equipment: str = ""):
    return ops.alert_overview(status=status, severity=severity,
                              equipment_reference=_resolve(equipment))


@registry.register("raise_plant_alert", "Raise an industrial alert", RiskLevel.LOW_RISK,
                   params={"description": "what is wrong", "equipment": "equipment tag",
                           "severity": "critical/high/medium/low"}, timeout=30.0)
def raise_plant_alert(description: str, equipment: str = "", severity: str = "medium",
                      category: str = "condition"):
    tag = ""
    if equipment:
        found = pd.resolve_equipment(_resolve(equipment))
        tag = found["tag"] if found else equipment.strip()
    alert = pd.raise_alert(description, equipment_tag=tag, category=category,
                           severity=severity, source="LOCAL")
    return {"raised": True, "alert": alert}


@registry.register("acknowledge_alert", "Acknowledge an open industrial alert",
                   RiskLevel.LOW_RISK, params={"alert_id": "e.g. ALM-20260904-A1B2C3"},
                   timeout=30.0)
def acknowledge_alert(alert_id: str):
    alert = pd.acknowledge_alert(alert_id, by=_current_operator())
    if not alert:
        return {"error": f"No alert {alert_id}"}
    return {"acknowledged": alert["status"] == "acknowledged", "alert": alert}


@registry.register("resolve_alert", "Mark an industrial alert resolved", RiskLevel.MEDIUM_RISK,
                   params={"alert_id": "alert id"}, timeout=30.0)
def resolve_alert(alert_id: str):
    alert = pd.resolve_alert(alert_id, by=_current_operator())
    if not alert:
        return {"error": f"No alert {alert_id}"}
    return {"resolved": alert["status"] == "resolved", "alert": alert}


# ------------------------------------------------------------ inventory --

@registry.register("inventory_summary",
                   "Show spare parts and equipment inventory, including items below minimum "
                   "stock", RiskLevel.SAFE, params={"category": "optional category filter"},
                   timeout=30.0)
def inventory_summary(category: str = "", low_stock_only: bool = False):
    return ops.inventory_summary(category=category, low_stock_only=low_stock_only)


@registry.register("find_part", "Find a spare part by number or description", RiskLevel.SAFE,
                   params={"query": "part number or words from the description"}, timeout=30.0)
def find_part(query: str):
    return ops.part_lookup(query)


@registry.register("update_stock", "Set the stock quantity for a part", RiskLevel.LOW_RISK,
                   params={"part_number": "part number", "quantity": "new count"},
                   timeout=30.0)
def update_stock(part_number: str, quantity: int, storage_location: str = ""):
    if not pd.find_part(part_number):
        return {"error": f"No part matching '{part_number}'"}
    fields: Dict[str, Any] = {"quantity": int(quantity), "source": "LOCAL"}
    if storage_location:
        fields["storage_location"] = storage_location
    pd.upsert_inventory(part_number, **fields)
    return {"updated": part_number, "item": pd.find_part(part_number)[0]}


# --------------------------------------------------------------- safety --

@registry.register("safety_status",
                   "Safety position: critical alerts, overdue inspections, permits awaiting "
                   "approval and active isolations", RiskLevel.SAFE, timeout=30.0)
def safety_status():
    return ops.safety_overview()


@registry.register("find_sop",
                   "Find a standard operating procedure or manual in the ingested industrial "
                   "documents", RiskLevel.SAFE, params={"topic": "what the procedure covers"},
                   timeout=60.0)
def find_sop(topic: str):
    return ops.find_sop(topic)


# -------------------------------------------------- permits and lockout --

@registry.register("list_permits", "List permits to work", RiskLevel.SAFE,
                   params={"status": "optional status filter"}, timeout=30.0)
def list_permits(status: str = ""):
    return ops.permit_overview(status=status)


@registry.register("request_permit", "Raise a permit to work in REQUESTED state",
                   RiskLevel.MEDIUM_RISK,
                   params={"permit_type": "e.g. hot work, confined space",
                           "equipment": "equipment tag", "description": "the work"},
                   timeout=30.0)
def request_permit(permit_type: str, equipment: str = "", description: str = "",
                   hazards: str = "", precautions: str = "", work_location: str = ""):
    return ops.request_permit(permit_type, equipment_reference=_resolve(equipment),
                              description=description, hazards=hazards,
                              precautions=precautions, work_location=work_location,
                              requested_by=_current_operator())


@registry.register("approve_permit",
                   "Record a HUMAN approval of a permit to work. Requires confirmation.",
                   RiskLevel.HIGH_RISK,
                   params={"permit_id": "permit id", "approver": "name of the approver"},
                   timeout=30.0)
def approve_permit(permit_id: str, approver: str = ""):
    # Reaching this line means the registry already obtained an explicit human
    # confirmation (HIGH_RISK is above CONFIRM_THRESHOLD). The approver still
    # has to be named - an unsigned approval is not an approval.
    approver = (approver or _current_operator()).strip()
    if not approver:
        return {"error": "Name the person approving this permit", "approved": False}
    return ops.approve_permit(permit_id, approver, confirmed=True)


@registry.register("list_isolations", "List lockout/tagout records", RiskLevel.SAFE,
                   params={"equipment": "optional equipment tag"}, timeout=30.0)
def list_isolations(equipment: str = "", active_only: bool = True):
    return ops.loto_overview(equipment_reference=_resolve(equipment), active_only=active_only)


@registry.register("record_isolation",
                   "Open a lockout/tagout record for an isolation a person has applied",
                   RiskLevel.MEDIUM_RISK,
                   params={"equipment": "equipment tag",
                           "isolation_points": "valves/breakers isolated"}, timeout=30.0)
def record_isolation(equipment: str, isolation_points: str = "", permit_id: str = "",
                     notes: str = ""):
    return ops.record_isolation(_resolve(equipment), isolation_points=isolation_points,
                                applied_by=_current_operator(), permit_id=permit_id,
                                notes=notes)


@registry.register("set_isolation_state",
                   "Record verification or release of a lockout/tagout isolation. "
                   "Safety-critical: requires confirmation.",
                   RiskLevel.HIGH_RISK,
                   params={"loto_id": "LOTO record id",
                           "state": "isolated/verified/released"}, timeout=30.0)
def set_isolation_state(loto_id: str, state: str, person: str = ""):
    # ZEUS never operates an isolation device. This records what a competent
    # person did, after that person confirmed it through the security prompt.
    person = (person or _current_operator()).strip()
    if not person:
        return {"error": "Name the person responsible for this isolation state",
                "changed": False}
    return ops.set_isolation_state(loto_id, state, person, confirmed=True)


# ------------------------------------------------------- data and export --

@registry.register("export_plant_data",
                   "Export a plant dataset to Excel or CSV: equipment, maintenance, "
                   "inspections, alerts, inventory, permits, loto, readings",
                   RiskLevel.LOW_RISK,
                   params={"dataset": "which dataset", "file_format": "xlsx or csv",
                           "equipment": "optional equipment filter"}, timeout=120.0)
def export_plant_data(dataset: str, file_format: str = "xlsx", equipment: str = "",
                      filename: str = ""):
    file_format = (file_format or "xlsx").lower().lstrip(".")
    if file_format == "csv":
        return docs.export_dataset_csv(dataset, filename=filename)
    if file_format not in ("xlsx", "excel"):
        return {"error": f"Unsupported export format '{file_format}'",
                "supported": ["xlsx", "csv"]}
    return docs.export_dataset_xlsx(dataset, filename=filename,
                                    equipment_reference=_resolve(equipment))


@registry.register("list_plant_datasets", "List the plant datasets that can be exported",
                   RiskLevel.SAFE, timeout=15.0)
def list_plant_datasets():
    return {"datasets": docs.available_datasets(),
            "formats": ["xlsx", "csv"],
            "example": "export the maintenance history to Excel"}


# ------------------------------------------------------ report creation --

@registry.register("maintenance_report",
                   "Generate a real Word maintenance report from stored records",
                   RiskLevel.LOW_RISK,
                   params={"equipment": "optional equipment tag"}, timeout=240.0)
def maintenance_report(equipment: str = "", filename: str = "", narrative: bool = True):
    reference = _resolve(equipment)
    result = docs.maintenance_report_docx(reference, filename=filename, narrative=narrative)
    if result.get("equipment"):
        _remember_equipment(result["equipment"])
    return result


@registry.register("safety_report", "Generate a real Word safety status report",
                   RiskLevel.LOW_RISK, timeout=240.0)
def safety_report(filename: str = "", narrative: bool = True):
    return docs.safety_report_docx(filename=filename, narrative=narrative)


@registry.register("approval_note",
                   "Generate a Word inspection approval note with an unsigned signature block",
                   RiskLevel.LOW_RISK, params={"equipment": "equipment tag"}, timeout=120.0)
def approval_note(equipment: str, findings: str = "", recommendation: str = "",
                  approver: str = "", filename: str = ""):
    return docs.inspection_note_docx(_resolve(equipment), findings=findings,
                                     recommendation=recommendation,
                                     approver=approver or _current_operator(),
                                     filename=filename)


@registry.register("operations_presentation",
                   "Generate a real PowerPoint on the current operations and maintenance "
                   "position", RiskLevel.LOW_RISK, timeout=240.0)
def operations_presentation(filename: str = "", narrative: bool = True):
    return docs.operations_deck_pptx(filename=filename, narrative=narrative)


@registry.register("equipment_presentation",
                   "Generate a real PowerPoint reviewing one piece of equipment",
                   RiskLevel.LOW_RISK, params={"equipment": "equipment tag"}, timeout=180.0)
def equipment_presentation(equipment: str, filename: str = ""):
    reference = _resolve(equipment)
    result = docs.equipment_deck_pptx(reference, filename=filename)
    if result.get("equipment"):
        _remember_equipment(result["equipment"])
    return result


# ---------------------------------------------------- system and set-up --

@registry.register("system_information",
                   "Local system information: model, Ollama, STT, TTS, memory, database, "
                   "CPU and RAM", RiskLevel.SAFE, timeout=45.0)
def system_information():
    return ops.system_information()


@registry.register("plant_data_sources",
                   "Report which plant records are real and which are DEMO sample data",
                   RiskLevel.SAFE, timeout=30.0)
def plant_data_sources():
    return pd.data_sources()


@registry.register("seed_industrial_demo_data",
                   "Load the clearly-labelled DEMO refinery dataset so the industrial "
                   "commands have data to work with", RiskLevel.MEDIUM_RISK,
                   params={"force": "re-seed even if equipment already exists"}, timeout=60.0)
def seed_industrial_demo_data(force: bool = False):
    return pd.seed_demo_data(force=bool(force))


@registry.register("list_generated_documents",
                   "List documents ZEUS has generated: reports, spreadsheets and decks, "
                   "newest first", RiskLevel.SAFE,
                   params={"kind": "optional filter: docx, xlsx, pptx, pdf, csv"},
                   timeout=30.0)
def list_generated_documents(kind: str = "", limit: int = 25):
    """Browses the existing output folder - no second document database."""
    from core import office
    base = office.documents_dir()
    patterns = [f"*.{kind.lower().lstrip('.')}"] if kind else \
        ["*.docx", "*.xlsx", "*.pptx", "*.pdf", "*.csv", "*.txt"]
    found = []
    for pattern in patterns:
        found.extend(base.rglob(pattern))
    found = sorted({p for p in found if p.is_file()},
                   key=lambda p: p.stat().st_mtime, reverse=True)[:int(limit)]
    import datetime
    return {
        "folder": str(base),
        "documents": [{"name": p.name, "path": str(p), "kind": p.suffix.lstrip("."),
                       "bytes": p.stat().st_size,
                       "modified": datetime.datetime.fromtimestamp(
                           p.stat().st_mtime).strftime("%Y-%m-%d %H:%M")}
                      for p in found],
        "count": len(found),
    }


@registry.register("find_generated_document",
                   "Find a previously generated document by name or words in the filename",
                   RiskLevel.SAFE, params={"query": "part of the file name"}, timeout=30.0)
def find_generated_document(query: str, limit: int = 25):
    everything = list_generated_documents(limit=500)["documents"]
    needle = (query or "").strip().lower()
    hits = [d for d in everything if needle in d["name"].lower()][:int(limit)]
    return {"query": query, "matches": hits, "match_count": len(hits),
            "searched": len(everything)}


@registry.register("open_document", "Open a generated document in its default application",
                   RiskLevel.LOW_RISK, params={"name_or_path": "file name or full path"},
                   timeout=30.0)
def open_document(name_or_path: str):
    from pathlib import Path as _Path
    from core import office
    from core.security_manager import security_manager as guard

    candidate = _Path(name_or_path).expanduser()
    if not candidate.exists():
        matches = find_generated_document(name_or_path, limit=5)["matches"]
        if not matches:
            return {"error": f"No generated document matching '{name_or_path}'"}
        if len(matches) > 1 and matches[0]["name"].lower() != name_or_path.strip().lower():
            return {"error": "Several documents match", "candidates": [m["name"] for m in matches]}
        candidate = _Path(matches[0]["path"])
    if not guard.validate_path(str(candidate)):
        return {"error": "Access to that path is blocked by security policy"}
    opened = office.open_when_done(candidate)
    return {"path": str(candidate), "opened": opened,
            "note": None if opened else "Could not launch a viewer on this system."}


@registry.register("view_logs", "Show recent lines from the ZEUS application log",
                   RiskLevel.SAFE, params={"lines": "how many lines, default 50",
                                           "level": "optional filter: ERROR/WARNING/INFO"},
                   timeout=30.0)
def view_logs(lines: int = 50, level: str = ""):
    """Reads the existing ZEUS log files - no second logging system."""
    from pathlib import Path
    log_dir = Path(__file__).resolve().parent.parent / "logs"
    if not log_dir.exists():
        return {"error": "No log directory yet", "path": str(log_dir)}
    files = sorted(log_dir.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return {"error": "No log files yet", "path": str(log_dir)}
    newest = files[0]
    try:
        content = newest.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as e:
        return {"error": f"Could not read {newest.name}: {e}"}
    if level:
        wanted = level.upper()
        content = [line for line in content if wanted in line]
    tail = content[-int(lines):]
    return {"file": str(newest), "lines": tail, "line_count": len(tail),
            "filtered_by": level.upper() if level else None,
            "other_log_files": [f.name for f in files[1:6]]}


# =======================================================================
#  Executive deliverables: one analysis, three renderings
#  (shared IndustrialAnalysis -> DOCX / XLSX / PPTX, so the numbers agree)
# =======================================================================

def _analysis_for(subject: str = "", narrative: bool = True):
    """Pick the right analysis for what the user named.

    An equipment tag gives the asset analysis; anything else gives the plant.
    """
    from industrial import analysis as _analysis
    subject = _resolve(subject)
    if subject:
        built = _analysis.build_equipment_analysis(subject, narrative=narrative)
        if built is None:
            return None, {"error": f"No equipment matching '{subject}'",
                          "hint": "Use 'show the equipment list' to see what is registered."}
        _remember_equipment(subject if "-" in subject else built.title.split(" ")[0])
        return built, None
    return _analysis.build_operations_analysis(narrative=narrative), None


def _output_path(stem: str, extension: str):
    from core import office
    return office.unique_path(office.documents_dir("Industrial"),
                              office.safe_stem(stem, "analysis"), extension)


@registry.register("executive_presentation",
                   "Build a professional executive presentation: recommendation, KPIs, "
                   "diagnosis, chart, evidence, actions, risks and roadmap",
                   RiskLevel.LOW_RISK,
                   params={"subject": "equipment tag, or blank for the whole plant"},
                   timeout=300.0)
def executive_presentation(subject: str = "", filename: str = "", narrative: bool = True):
    from industrial import deck_builder
    built, error = _analysis_for(subject, narrative)
    if error:
        return error
    path = _output_path(filename or f"executive_deck_{subject or 'operations'}", "pptx")
    return deck_builder.build_deck(built, path)


@registry.register("executive_workbook",
                   "Build a multi-sheet Excel workbook with a management summary, live "
                   "formulas, filtered data sheets and a sources sheet",
                   RiskLevel.LOW_RISK,
                   params={"subject": "equipment tag, or blank for the whole plant"},
                   timeout=300.0)
def executive_workbook(subject: str = "", filename: str = "", narrative: bool = True):
    from industrial import workbook_builder
    built, error = _analysis_for(subject, narrative)
    if error:
        return error
    path = _output_path(filename or f"analysis_workbook_{subject or 'operations'}", "xlsx")
    return workbook_builder.build_workbook(built, path)


@registry.register("executive_report",
                   "Build a full Word report from the same analysis as the deck and the "
                   "workbook", RiskLevel.LOW_RISK,
                   params={"subject": "equipment tag, or blank for the whole plant"},
                   timeout=300.0)
def executive_report(subject: str = "", filename: str = "", narrative: bool = True,
                     approval: bool = False):
    from industrial import report_builder
    built, error = _analysis_for(subject, narrative)
    if error:
        return error
    path = _output_path(filename or f"analysis_report_{subject or 'operations'}", "docx")
    return report_builder.build_report(built, path, include_approval=bool(approval),
                                       approver=_current_operator())


@registry.register("executive_briefing_pack",
                   "Build the Word report, the Excel workbook and the PowerPoint deck "
                   "from ONE analysis, so every number agrees across the three files",
                   RiskLevel.LOW_RISK,
                   params={"subject": "equipment tag, or blank for the whole plant"},
                   timeout=600.0)
def executive_briefing_pack(subject: str = "", narrative: bool = True):
    from industrial import deck_builder, report_builder, workbook_builder
    built, error = _analysis_for(subject, narrative)
    if error:
        return error
    stem = f"briefing_{subject or 'operations'}"
    results = {
        "report": report_builder.build_report(built, _output_path(stem, "docx")),
        "workbook": workbook_builder.build_workbook(built, _output_path(stem, "xlsx")),
        "presentation": deck_builder.build_deck(built, _output_path(stem, "pptx")),
    }
    failures = {name: r for name, r in results.items() if "error" in r}
    return {
        "pack": {name: r.get("saved") for name, r in results.items() if "saved" in r},
        "verified": not failures,
        "failed": failures or None,
        "analysis": built.summary_dict(),
        "consistency": "all three files were rendered from a single analysis object",
    }


@registry.register("analyze_spreadsheet",
                   "Read a local spreadsheet or CSV, profile every sheet and column, and "
                   "report the totals, concentrations and outliers it contains",
                   RiskLevel.SAFE, params={"path": "path to an .xlsx, .xlsm or .csv file"},
                   timeout=180.0)
def analyze_spreadsheet(path: str, title: str = ""):
    from core.security_manager import security_manager as guard
    from industrial import analysis as _analysis
    if not guard.validate_path(str(path)):
        return {"error": "Access to that path is blocked by security policy"}
    built = _analysis.build_workbook_analysis(path, title=title, narrative=True)
    _LAST_FILE_ANALYSIS["analysis"] = built
    return {
        "headline": built.headline,
        "summary": built.executive_summary,
        "kpis": [{"label": k.label, "value": k.value, "note": k.note} for k in built.kpis],
        "findings": [{"title": f.title, "detail": f.detail, "magnitude": f.magnitude}
                     for f in built.findings],
        "sheets": [{"name": t.title, "rows": len(t.rows), "columns": len(t.columns)}
                   for t in built.tables],
        "next": "Say 'create a presentation from that spreadsheet' to turn this into a deck.",
    }


_LAST_FILE_ANALYSIS: Dict[str, Any] = {}


@registry.register("spreadsheet_to_presentation",
                   "Read a spreadsheet, work out what it says, and build an executive "
                   "presentation from the analysis - not a pasted table",
                   RiskLevel.LOW_RISK,
                   params={"path": "path to an .xlsx, .xlsm or .csv file"},
                   timeout=300.0)
def spreadsheet_to_presentation(path: str = "", filename: str = ""):
    from core.security_manager import security_manager as guard
    from industrial import analysis as _analysis, deck_builder
    if path:
        if not guard.validate_path(str(path)):
            return {"error": "Access to that path is blocked by security policy"}
        built = _analysis.build_workbook_analysis(path, narrative=True)
    else:
        built = _LAST_FILE_ANALYSIS.get("analysis")
        if built is None:
            return {"error": "No spreadsheet has been analysed yet",
                    "hint": "Say 'analyse the spreadsheet <path>' first, or give the path."}
    from pathlib import Path as _Path
    stem = filename or f"deck_{_Path(path).stem if path else 'spreadsheet'}"
    return deck_builder.build_deck(built, _output_path(stem, "pptx"))


@registry.register("spreadsheet_to_report",
                   "Read a spreadsheet and write a Word report of what it contains",
                   RiskLevel.LOW_RISK, params={"path": "path to an .xlsx, .xlsm or .csv"},
                   timeout=300.0)
def spreadsheet_to_report(path: str = "", filename: str = ""):
    from core.security_manager import security_manager as guard
    from industrial import analysis as _analysis, report_builder
    if path:
        if not guard.validate_path(str(path)):
            return {"error": "Access to that path is blocked by security policy"}
        built = _analysis.build_workbook_analysis(path, narrative=True)
    else:
        built = _LAST_FILE_ANALYSIS.get("analysis")
        if built is None:
            return {"error": "No spreadsheet has been analysed yet",
                    "hint": "Say 'analyse the spreadsheet <path>' first, or give the path."}
    from pathlib import Path as _Path
    stem = filename or f"report_{_Path(path).stem if path else 'spreadsheet'}"
    return report_builder.build_report(built, _output_path(stem, "docx"))
