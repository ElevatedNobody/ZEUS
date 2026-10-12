"""
Industrial capabilities over the plant data: operations, monitoring,
maintenance, inventory, alerts, equipment, safety, permits, LOTO and system
information.

These are the FUNCTIONS behind the industrial commands. They read the shared
database through plant_data, reuse the existing ZEUS knowledge base for
documents, and hand structured dictionaries back to the tool layer - no UI, no
server, no second data store.

Two safety rules are enforced here rather than left to the caller:

* Nothing in this module operates plant equipment. Permits and LOTO records
  describe work that people do; changing a permit to approved or releasing an
  isolation is recorded only after an explicit human confirmation obtained
  through the existing security manager.
* Every summary reports whether the rows behind it are DEMO data, so an answer
  can never be mistaken for a live plant reading.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from core.logger import get_logger
from industrial import plant_data as pd

logger = get_logger("industrial.ops")


def _provenance(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """What produced these rows. Attached to every result that carries data."""
    sources = sorted({(r.get("source") or "unknown") for r in rows})
    demo = pd.DEMO in sources
    out = {"sources": sources, "contains_demo_data": demo}
    if demo:
        out["notice"] = ("Rows marked DEMO are generated sample data, not measurements "
                         "from a real plant.")
    return out


def _equipment_or_error(reference: str) -> Dict[str, Any]:
    equipment = pd.resolve_equipment(reference)
    if equipment:
        return {"ok": True, "equipment": equipment}
    matches = pd.find_equipment(reference)
    if matches:
        return {"ok": False, "error": f"'{reference}' matches several items",
                "candidates": [m["tag"] for m in matches[:10]]}
    return {"ok": False, "error": f"No equipment matching '{reference}' is on record",
            "hint": "Use 'show the equipment list' to see what is registered."}


# ----------------------------------------------------------- operations --

def operations_summary() -> Dict[str, Any]:
    """Plant/operational picture assembled from what is actually stored."""
    equipment = pd.list_equipment(limit=10000)
    alerts = pd.list_alerts(status="open", limit=500)
    due = pd.maintenance_due(within_days=30)
    by_unit: Dict[str, Dict[str, int]] = {}
    for row in equipment:
        unit = row.get("unit") or "unassigned"
        bucket = by_unit.setdefault(unit, {})
        state = row.get("status") or "unknown"
        bucket[state] = bucket.get(state, 0) + 1

    severity_counts: Dict[str, int] = {}
    for alert in alerts:
        severity_counts[alert["severity"]] = severity_counts.get(alert["severity"], 0) + 1

    running = sum(1 for e in equipment if e.get("status") == "operational")
    return {
        "equipment_total": len(equipment),
        "equipment_operational": running,
        "equipment_in_maintenance": sum(1 for e in equipment if e.get("status") == "maintenance"),
        "equipment_shutdown": sum(1 for e in equipment if e.get("status") == "shutdown"),
        "availability_pct": round(100.0 * running / len(equipment), 1) if equipment else None,
        "by_unit": by_unit,
        "open_alerts": len(alerts),
        "alerts_by_severity": severity_counts,
        "maintenance_due_30d": len(due),
        "provenance": _provenance(equipment + alerts),
    }


def operations_status_text() -> str:
    """One readable paragraph - what the assistant says out loud."""
    s = operations_summary()
    if not s["equipment_total"]:
        return ("No equipment is registered yet. Run 'seed industrial demo data' to load the "
                "sample refinery, or register equipment with 'register equipment <tag>'.")
    parts = [f"{s['equipment_operational']} of {s['equipment_total']} registered items are "
             f"operational ({s['availability_pct']}%)."]
    if s["equipment_in_maintenance"]:
        parts.append(f"{s['equipment_in_maintenance']} in maintenance.")
    if s["equipment_shutdown"]:
        parts.append(f"{s['equipment_shutdown']} shut down.")
    if s["open_alerts"]:
        by = s["alerts_by_severity"]
        worst = ", ".join(f"{n} {sev}" for sev, n in by.items())
        parts.append(f"{s['open_alerts']} open alerts ({worst}).")
    else:
        parts.append("No open alerts.")
    if s["maintenance_due_30d"]:
        parts.append(f"{s['maintenance_due_30d']} maintenance items due within 30 days.")
    if s["provenance"]["contains_demo_data"]:
        parts.append("Source: DEMO sample data.")
    return " ".join(parts)


# ----------------------------------------------------------- monitoring --

def monitoring_snapshot(equipment_tag: str = "") -> Dict[str, Any]:
    """Current condition indicators from stored readings.

    This workstation has no live sensor feed. What comes back is whatever has
    been written into plant_readings - by the DEMO seed, or by a real source
    connected through plant_data.record_reading().
    """
    readings = pd.latest_readings(equipment_tag, limit=200)
    by_equipment: Dict[str, Dict[str, Any]] = {}
    for row in readings:
        slot = by_equipment.setdefault(row["equipment_tag"], {})
        if row["parameter"] not in slot:            # readings come back newest first
            slot[row["parameter"]] = {"value": row["value"], "unit": row["unit"],
                                      "measured_at": row["measured_at"], "source": row["source"]}
    return {
        "live_feed_connected": False,
        "readings_stored": len(readings),
        "equipment_reporting": len(by_equipment),
        "latest": by_equipment,
        "provenance": _provenance(readings),
        "integration_note": ("No live plant historian is connected. To feed real data, call "
                             "industrial.plant_data.record_reading(tag, parameter, value, "
                             "source='<your source>') from a collector script or plugin."),
    }


def monitoring_trend(equipment_reference: str, parameter: str = "vibration_rms",
                     points: int = 24) -> Dict[str, Any]:
    found = _equipment_or_error(equipment_reference)
    if not found["ok"]:
        return found
    tag = found["equipment"]["tag"]
    trend = pd.reading_trend(tag, parameter, points)
    if not trend.get("points"):
        return {"equipment": tag, "parameter": parameter, "points": 0,
                "note": f"No stored readings for {parameter} on {tag}."}
    return trend


# ---------------------------------------------------------- maintenance --

def maintenance_overview(equipment_reference: str = "", limit: int = 25) -> Dict[str, Any]:
    tag = ""
    if equipment_reference:
        found = _equipment_or_error(equipment_reference)
        if not found["ok"]:
            return found
        tag = found["equipment"]["tag"]
    history = pd.maintenance_history(tag, limit=limit)
    due = pd.maintenance_due(30)
    if tag:
        due = [d for d in due if d["equipment_tag"].lower() == tag.lower()]
    total_hours = round(sum(float(r.get("duration_hours") or 0) for r in history), 1)
    return {
        "equipment": tag or "all equipment",
        "records": history,
        "record_count": len(history),
        "total_hours": total_hours,
        "by_type": _count(history, "maintenance_type"),
        "open_items": [r for r in history if r.get("status") in ("in_progress", "planned")],
        "due_within_30_days": due,
        "provenance": _provenance(history),
    }


def schedule_maintenance(equipment_reference: str, description: str, due_date: str = "",
                         maintenance_type: str = "preventive",
                         technician: str = "") -> Dict[str, Any]:
    """Plan a job. Uses the plant maintenance table AND the existing ZEUS
    calendar, so a scheduled job shows up where the operator already looks."""
    found = _equipment_or_error(equipment_reference)
    if not found["ok"]:
        return found
    tag = found["equipment"]["tag"]
    due = due_date or (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")
    record_id = pd.add_maintenance(tag, description, maintenance_type=maintenance_type,
                                   performed_on="", technician=technician, status="planned",
                                   next_due=due, source="LOCAL")
    calendar_note = None
    try:
        from memory import database
        database.add_event(f"Maintenance {tag}: {description}"[:120], due, "")
        calendar_note = f"added to the ZEUS calendar for {due}"
    except Exception as e:                                   # calendar is optional
        logger.debug(f"calendar entry skipped: {e}")
    return {"scheduled": True, "equipment": tag, "record_id": record_id, "due": due,
            "type": maintenance_type, "description": description, "calendar": calendar_note}


def _count(rows: List[Dict[str, Any]], key: str) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for row in rows:
        value = row.get(key) or "unspecified"
        out[value] = out.get(value, 0) + 1
    return out


# ------------------------------------------------------------ inventory --

def inventory_summary(category: str = "", low_stock_only: bool = False) -> Dict[str, Any]:
    items = pd.list_inventory(category=category, low_stock_only=low_stock_only)
    low = [i for i in items if (i.get("quantity") or 0) <= (i.get("minimum_stock") or 0)]
    return {
        "items": items,
        "item_count": len(items),
        "categories": _count(items, "category"),
        "below_minimum": low,
        "below_minimum_count": len(low),
        "total_units": sum(int(i.get("quantity") or 0) for i in items),
        "provenance": _provenance(items),
    }


def part_lookup(fragment: str) -> Dict[str, Any]:
    matches = pd.find_part(fragment)
    return {"query": fragment, "matches": matches, "match_count": len(matches),
            "provenance": _provenance(matches)}


# --------------------------------------------------------------- alerts --

def alert_overview(status: str = "open", severity: str = "",
                   equipment_reference: str = "") -> Dict[str, Any]:
    tag = ""
    if equipment_reference:
        found = _equipment_or_error(equipment_reference)
        if not found["ok"]:
            return found
        tag = found["equipment"]["tag"]
    alerts = pd.list_alerts(status=status, severity=severity, equipment_tag=tag)
    return {
        "status_filter": status,
        "alerts": alerts,
        "alert_count": len(alerts),
        "by_severity": _count(alerts, "severity"),
        "by_category": _count(alerts, "category"),
        "unresolved_critical": [a for a in alerts
                                if a["severity"] == "critical" and a["status"] != "resolved"],
        "provenance": _provenance(alerts),
    }


# ------------------------------------------------------------ equipment --

def equipment_dossier(equipment_reference: str) -> Dict[str, Any]:
    """Everything stored about one item, pulled together from every table plus
    the industrial document knowledge base."""
    found = _equipment_or_error(equipment_reference)
    if not found["ok"]:
        return found
    item = found["equipment"]
    tag = item["tag"]

    maintenance = pd.maintenance_history(tag, limit=25)
    inspections = pd.inspection_history(tag, limit=25)
    alerts = pd.list_alerts(status="all", equipment_tag=tag, limit=50)
    spares = pd.list_inventory(equipment_tag=tag)
    readings = pd.latest_readings(tag, limit=20)
    isolations = pd.list_loto(equipment_tag=tag, limit=10)

    documents: List[Dict[str, Any]] = []
    try:
        from industrial.knowledge_base import knowledge_base
        hits = knowledge_base.search(tag, top_k=5)
        for hit in hits or []:
            documents.append({"source": hit.get("source_path") or hit.get("document"),
                              "page": hit.get("page"), "score": hit.get("score")})
    except Exception as e:
        logger.debug(f"document lookup for {tag} skipped: {e}")

    last_done = next((m for m in maintenance if m.get("status") == "completed"), None)
    return {
        "equipment": item,
        "maintenance_records": maintenance,
        "maintenance_count": len(maintenance),
        "last_maintenance": last_done,
        "inspections": inspections,
        "alerts": alerts,
        "open_alerts": [a for a in alerts if a["status"] == "open"],
        "spare_parts": spares,
        "recent_readings": readings,
        "loto_records": isolations,
        "related_documents": documents,
        "provenance": _provenance([item] + maintenance + inspections + alerts + spares),
    }


def equipment_summary_text(equipment_reference: str) -> str:
    dossier = equipment_dossier(equipment_reference)
    if not dossier.get("equipment"):
        return dossier.get("error", "Equipment not found.")
    item = dossier["equipment"]
    lines = [f"{item['tag']} - {item['name']} ({item.get('equipment_type') or 'type unknown'})",
             f"Location: {item.get('location') or 'unknown'}   Status: {item.get('status')}   "
             f"Criticality: {item.get('criticality')}"]
    last = dossier.get("last_maintenance")
    if last:
        lines.append(f"Last maintenance {last.get('performed_on')}: {last.get('description')}")
    lines.append(f"{dossier['maintenance_count']} maintenance records, "
                 f"{len(dossier['inspections'])} inspections, "
                 f"{len(dossier['open_alerts'])} open alerts, "
                 f"{len(dossier['spare_parts'])} spare part lines.")
    for alert in dossier["open_alerts"][:3]:
        lines.append(f"  ALERT [{alert['severity']}] {alert['description']}")
    if dossier["provenance"]["contains_demo_data"]:
        lines.append("Source: DEMO sample data.")
    return "\n".join(lines)


# --------------------------------------------------------------- safety --

def safety_overview() -> Dict[str, Any]:
    alerts = pd.list_alerts(status="open", limit=500)
    safety_alerts = [a for a in alerts if a["category"] in ("safety", "compliance")]
    overdue_inspections = [i for i in pd.inspection_history(limit=500)
                           if i.get("next_due") and i["next_due"] < datetime.now().strftime("%Y-%m-%d")]
    permits = pd.list_permits(limit=200)
    active_loto = pd.list_loto(active_only=True, limit=100)
    return {
        "open_safety_alerts": safety_alerts,
        "safety_alert_count": len(safety_alerts),
        "critical_open": [a for a in alerts if a["severity"] == "critical"],
        "overdue_inspections": overdue_inspections,
        "permits_awaiting_approval": [p for p in permits if p["status"] == "requested"],
        "active_isolations": active_loto,
        "provenance": _provenance(alerts + overdue_inspections),
        "caution": ("This is a summary of locally stored records. It is not a substitute for "
                    "the site safety case, the SOPs or a competent person's judgement."),
    }


def find_sop(topic: str, top_k: int = 5) -> Dict[str, Any]:
    """Look for a procedure in the existing industrial knowledge base. Reports
    honestly when nothing has been ingested rather than inventing a procedure."""
    try:
        from industrial.knowledge_base import knowledge_base
        stats = knowledge_base.stats()
        if not stats.get("chunks"):
            return {"query": topic, "results": [],
                    "error": "The industrial knowledge base is empty.",
                    "hint": "Ingest documents first: 'ingest the industrial folder <path>' "
                            "or run scripts/ingest_industrial_knowledge.py"}
        hits = knowledge_base.search(topic, top_k=top_k) or []
        return {"query": topic, "results": hits, "result_count": len(hits),
                "note": "Answers come from ingested local documents only."}
    except Exception as e:
        return {"query": topic, "error": f"Knowledge base unavailable: {e}"}


# -------------------------------------------------- permits and lockout --

def permit_overview(status: str = "") -> Dict[str, Any]:
    permits = pd.list_permits(status=status)
    return {"permits": permits, "permit_count": len(permits),
            "by_status": _count(permits, "status"),
            "awaiting_approval": [p for p in permits if p["status"] == "requested"]}


def request_permit(permit_type: str, equipment_reference: str = "", description: str = "",
                   hazards: str = "", precautions: str = "", work_location: str = "",
                   requested_by: str = "") -> Dict[str, Any]:
    """Raise a permit in REQUESTED state. Never approved here - approval is a
    human decision, see approve_permit()."""
    tag = ""
    if equipment_reference:
        found = _equipment_or_error(equipment_reference)
        if not found["ok"]:
            return found
        tag = found["equipment"]["tag"]
        work_location = work_location or found["equipment"].get("location") or ""
    permit = pd.create_permit(permit_type, equipment_tag=tag, work_location=work_location,
                              description=description, hazards=hazards, precautions=precautions,
                              requested_by=requested_by)
    return {"created": True, "permit": permit,
            "status": permit["status"],
            "next_step": "A responsible person must approve this permit before work starts."}


def approve_permit(permit_id: str, approver: str, confirmed: bool = False) -> Dict[str, Any]:
    """Record a human approval.

    `confirmed` must be True and `approver` must be named. The tool layer obtains
    that confirmation through the existing security manager; this function
    refuses rather than approving a safety document on the model's say-so.
    """
    permit = pd.get_permit(permit_id)
    if not permit:
        return {"error": f"No permit {permit_id} on record"}
    if not confirmed:
        return {"error": "Permit approval requires explicit human confirmation",
                "permit": permit, "approved": False}
    if not (approver or "").strip():
        return {"error": "An approval must name the person approving it",
                "permit": permit, "approved": False}
    updated = pd.set_permit_status(permit_id, "approved", by=approver.strip())
    logger.warning(f"Permit {permit_id} approved by {approver}")
    return {"approved": True, "permit": updated}


def loto_overview(equipment_reference: str = "", active_only: bool = True) -> Dict[str, Any]:
    tag = ""
    if equipment_reference:
        found = _equipment_or_error(equipment_reference)
        if not found["ok"]:
            return found
        tag = found["equipment"]["tag"]
    records = pd.list_loto(equipment_tag=tag, active_only=active_only)
    return {"loto_records": records, "record_count": len(records),
            "by_status": _count(records, "isolation_status"),
            "scope": "This is an information and workflow record. ZEUS does not operate "
                     "isolation devices."}


def record_isolation(equipment_reference: str, isolation_points: str = "",
                     applied_by: str = "", permit_id: str = "",
                     notes: str = "") -> Dict[str, Any]:
    found = _equipment_or_error(equipment_reference)
    if not found["ok"]:
        return found
    record = pd.create_loto(found["equipment"]["tag"], isolation_points=isolation_points,
                            applied_by=applied_by, permit_id=permit_id, notes=notes)
    return {"created": True, "loto": record,
            "reminder": "The physical isolation and its verification are done by a competent "
                        "person. This record only documents them."}


def set_isolation_state(loto_id: str, state: str, person: str,
                        confirmed: bool = False) -> Dict[str, Any]:
    """Change a recorded isolation state. Verification and release are
    safety-critical, so they require an explicit human confirmation."""
    record = pd.get_loto(loto_id)
    if not record:
        return {"error": f"No LOTO record {loto_id}"}
    if state in ("verified", "released") and not confirmed:
        return {"error": f"Setting a LOTO record to '{state}' requires explicit human "
                         f"confirmation", "loto": record, "changed": False}
    if not (person or "").strip():
        return {"error": "A LOTO state change must name the person responsible",
                "loto": record, "changed": False}
    updated = pd.set_loto_state(loto_id, state, by=person.strip())
    logger.warning(f"LOTO {loto_id} set to {state} by {person}")
    return {"changed": True, "loto": updated}


# --------------------------------------------------- system information --

def system_information() -> Dict[str, Any]:
    """Local system and subsystem status. Never includes credentials."""
    info: Dict[str, Any] = {"application": "ZEUS Autonomous AI Command & Intelligence System"}

    try:
        import platform
        info["python"] = platform.python_version()
        info["os"] = f"{platform.system()} {platform.release()}"
    except Exception:
        pass

    try:
        import psutil
        vm = psutil.virtual_memory()
        info["cpu_percent"] = psutil.cpu_percent(interval=0.2)
        info["cpu_cores"] = psutil.cpu_count(logical=True)
        info["ram_total_gb"] = round(vm.total / (1024 ** 3), 1)
        info["ram_used_percent"] = vm.percent
    except Exception:
        info["resources"] = "psutil not installed"

    try:
        from core.ollama_client import ollama_client
        reachable = ollama_client.is_available()
        info["ollama"] = {"reachable": reachable,
                          "models": len(ollama_client.list_models() or []) if reachable else 0}
    except Exception as e:
        info["ollama"] = {"reachable": False, "error": str(e)}

    try:
        from core.model_router import model_router
        info["models"] = {task: model_router.route(task)
                          for task in ("reasoning", "json_structured", "embedding", "vision")}
    except Exception:
        pass

    try:
        from voice.offline import voice_status
        status = voice_status()
        info["voice"] = {
            "enabled": status.get("enabled"),
            "stt": ("ready" if status["stt"]["model"]["present"] else "model missing"),
            "tts": ("ready" if status["tts"]["model"]["present"] else "model missing"),
            "external_services": "disabled (local Whisper + local Chatterbox)",
        }
    except Exception as e:
        info["voice"] = {"error": str(e)}

    try:
        from memory import database
        info["database"] = {"path": str(database.DB_PATH), "exists": database.DB_PATH.exists()}
        info["plant_data"] = pd.data_sources()
    except Exception as e:
        info["database"] = {"error": str(e)}

    try:
        from industrial.knowledge_base import knowledge_base
        info["knowledge_base"] = knowledge_base.stats()
    except Exception:
        pass

    try:
        from core.tool_registry import registry
        info["tools_registered"] = len(registry.list_tools())
    except Exception:
        pass

    info["network_services"] = "none - no REST/web server; Ollama on loopback only"
    return info
