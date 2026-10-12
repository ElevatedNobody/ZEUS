"""
Telemetry adapter: what the interface shows as "field telemetry", and where
it really comes from.

This workstation has no live connection to a plant historian or a DCS. So:

  * every figure here is read from the plant database, and every figure
    carries the `source` of the rows it was computed from;
  * if ANY contributing row is DEMO data, the block is labelled DEMO and the
    interface prints that label - a seeded refinery is never presented as a
    live plant;
  * `live` is True only when readings from a non-DEMO source arrived in the
    last `LIVE_WINDOW_MINUTES` - which is what a real feed would produce.

Adapters that bring real data in (all end in plant_data.record_reading, which
refuses an anonymous reading):

    import_readings_csv(path, source)    a historian/CSV export, now
    (an OPC-UA or Modbus bridge would call record_reading the same way)
"""

from __future__ import annotations

import csv
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List

LIVE_WINDOW_MINUTES = 15
DEMO = "DEMO"

# Parameters shown first on the telemetry panel, when present.
_PREFERRED = [("T-101", "overhead_temperature"), ("F-101", "coil_outlet_temperature"),
              ("C-301", "discharge_pressure"), ("C-201", "discharge_pressure"),
              ("P-101", "vibration_velocity"), ("E-405", "duty_percent"),
              ("B-701", "steam_flow"), ("TK-810", "level")]


def _label(sources) -> str:
    sources = set(sources)
    if not sources:
        return "NO DATA"
    if sources == {DEMO}:
        return "DEMO DATA"
    if DEMO in sources:
        return "MIXED (includes DEMO)"
    return "RECORDED"


def telemetry_snapshot(max_series: int = 6, points: int = 24) -> Dict[str, Any]:
    from industrial import plant_data
    plant_data.init_plant_schema()
    series: List[Dict[str, Any]] = []
    seen = set()
    candidates = list(_PREFERRED)
    for row in plant_data.latest_readings(limit=400):
        key = (row["equipment_tag"], row["parameter"])
        if key not in candidates:
            candidates.append(key)
    for tag, parameter in candidates:
        if (tag, parameter) in seen or len(series) >= max_series:
            continue
        trend = plant_data.reading_trend(tag, parameter, points=points)
        if not trend.get("points"):
            continue
        seen.add((tag, parameter))
        series.append({"equipment": tag, "parameter": parameter.replace("_", " "),
                       "unit": trend.get("unit", ""), "latest": trend.get("latest"),
                       "minimum": trend.get("minimum"), "maximum": trend.get("maximum"),
                       "direction": trend.get("direction"), "values": trend.get("values", []),
                       "sources": trend.get("sources", []), "label": _label(trend.get("sources", []))})

    newest_real = None
    try:
        conn = plant_data._conn()
        try:
            row = conn.execute("SELECT MAX(created_at) AS t FROM plant_readings WHERE source != ?",
                               (DEMO,)).fetchone()
            newest_real = row["t"] if row else None
        finally:
            conn.close()
    except Exception:
        newest_real = None
    live = False
    if newest_real:
        try:
            live = datetime.utcnow() - datetime.fromisoformat(str(newest_real)) < \
                timedelta(minutes=LIVE_WINDOW_MINUTES)
        except ValueError:
            live = False

    all_sources = {s for item in series for s in item["sources"]}
    return {"series": series, "label": _label(all_sources), "live": live,
            "newest_non_demo_reading": newest_real,
            "note": ("No live plant connection on this workstation - values are the stored "
                     "readings, labelled by their source." if not live else
                     "Readings from a connected source arrived in the last "
                     f"{LIVE_WINDOW_MINUTES} minutes."),
            "measured_at": time.time()}


def production_kpis() -> Dict[str, Any]:
    """Monthly production KPIs. Only the demo refinery provides these today, so
    they are always returned with their DEMO label."""
    try:
        from industrial.demo_refinery import COMPANY as PLANT, PRODUCTION_KPI
    except Exception:
        return {"label": "NO DATA", "months": []}
    months = [{"month": m, "crude_kt": crude, "utilisation_pct": util, "energy_mbn": energy,
               "downtime_h": down, "on_spec_pct": spec}
              for m, crude, util, energy, down, spec in PRODUCTION_KPI]
    latest = months[-1] if months else {}
    previous = months[-2] if len(months) > 1 else {}
    change = None
    if latest and previous and previous.get("crude_kt"):
        change = round((latest["crude_kt"] - previous["crude_kt"]) / previous["crude_kt"] * 100, 1)
    return {"label": "DEMO DATA", "plant": PLANT.get("site") or PLANT.get("name", "demo refinery"),
            "capacity_bpd": PLANT.get("capacity_bpd"), "months": months, "latest": latest,
            "change_pct_vs_previous": change}


def operations_overview() -> Dict[str, Any]:
    """Counts behind the right-hand operational panel."""
    from industrial import plant_data
    plant_data.init_plant_schema()
    status = plant_data.equipment_status_summary()
    alerts = plant_data.list_alerts(status="open", limit=200) if hasattr(plant_data, "list_alerts") else []
    due = plant_data.maintenance_due(within_days=14)
    permits = [p for p in plant_data.list_permits() if str(p.get("status", "")).lower() in
               ("requested", "open", "approved", "active")]
    sources = plant_data.data_sources()
    by_severity: Dict[str, int] = {}
    for alert in alerts:
        by_severity[alert.get("severity") or "unknown"] = by_severity.get(alert.get("severity") or "unknown", 0) + 1
    return {"equipment": status, "open_alerts": len(alerts), "alerts_by_severity": by_severity,
            "top_alerts": [{"id": a.get("alert_id") or a.get("id"), "equipment": a.get("equipment_tag"),
                            "severity": a.get("severity"), "description": a.get("description"),
                            "source": a.get("source")} for a in alerts[:5]],
            "maintenance_due_14d": len(due), "open_permits": len(permits),
            "label": "DEMO DATA" if sources.get("demo_rows") and not sources.get("non_demo_rows")
            else ("MIXED (includes DEMO)" if sources.get("demo_rows") else "RECORDED"),
            "live_sensor_feed": sources.get("live_sensor_feed", False)}


def import_readings_csv(path: str, source: str) -> Dict[str, Any]:
    """Import readings from a CSV export. Columns (header row required):
        equipment_tag, parameter, value, unit, measured_at (ISO, optional)
    `source` names the system the export came from (e.g. 'PI historian export')."""
    from industrial import plant_data
    if not source or source.strip().upper() == DEMO:
        return {"error": "name the real system the readings came from (source cannot be DEMO)"}
    target = Path(path)
    if not target.is_file():
        return {"error": f"'{path}' does not exist"}
    imported, skipped = 0, []
    with target.open(newline="", encoding="utf-8-sig") as handle:
        for number, row in enumerate(csv.DictReader(handle), start=2):
            try:
                plant_data.record_reading(row["equipment_tag"], row["parameter"], float(row["value"]),
                                          unit=row.get("unit", ""), source=source.strip(),
                                          measured_at=row.get("measured_at", ""))
                imported += 1
            except Exception as error:
                skipped.append(f"line {number}: {error}")
    return {"imported": imported, "skipped": len(skipped), "problems": skipped[:10],
            "source": source.strip()}
