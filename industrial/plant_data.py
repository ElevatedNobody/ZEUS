"""
Industrial plant data: equipment, maintenance, inspections, alerts, inventory,
permits to work and lockout/tagout records.

One store, not a new one. Every table lives in the SAME SQLite database as the
rest of ZEUS (memory/database.py), so a query here sees the same file the
conversation history, the industrial documents and the accounts live in. There
is no second database, no ORM and no service.

Honesty rules baked into the schema:

* Every row carries a `source` column. Rows created by `seed_demo_data()` are
  written with source='DEMO' and every reader surfaces that, because this
  workstation has no connection to a real plant historian. Nothing in this
  module invents a measurement and presents it as real.
* `record_reading()` exists for when a real source IS connected. It is the
  integration point: point it at an OPC-UA bridge, a CSV drop, a historian
  export, and the readings become real with source='<name of that source>'.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from core.logger import get_logger
from memory import database

logger = get_logger("industrial.plant")

DEMO = "DEMO"          # the marker every reader checks and reports

PLANT_SCHEMA = """
CREATE TABLE IF NOT EXISTS plant_equipment (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    tag            TEXT UNIQUE NOT NULL COLLATE NOCASE,
    name           TEXT NOT NULL,
    equipment_type TEXT,
    location       TEXT,
    unit           TEXT,
    status         TEXT DEFAULT 'unknown',
    criticality    TEXT DEFAULT 'medium',
    manufacturer   TEXT,
    model          TEXT,
    serial_number  TEXT,
    installed_on   TEXT,
    attributes     TEXT,
    source         TEXT DEFAULT 'DEMO',
    created_at     TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at     TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_plant_equipment_tag ON plant_equipment(tag);

CREATE TABLE IF NOT EXISTS plant_maintenance (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    equipment_tag  TEXT NOT NULL COLLATE NOCASE,
    performed_on   TEXT,
    maintenance_type TEXT,
    description    TEXT,
    technician     TEXT,
    duration_hours REAL,
    parts_used     TEXT,
    findings       TEXT,
    status         TEXT DEFAULT 'completed',
    next_due       TEXT,
    work_order     TEXT,
    source         TEXT DEFAULT 'DEMO',
    created_at     TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_plant_maint_tag ON plant_maintenance(equipment_tag);

CREATE TABLE IF NOT EXISTS plant_inspections (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    equipment_tag  TEXT NOT NULL COLLATE NOCASE,
    inspected_on   TEXT,
    inspection_type TEXT,
    inspector      TEXT,
    result         TEXT,
    findings       TEXT,
    recommendation TEXT,
    document_path  TEXT,
    next_due       TEXT,
    source         TEXT DEFAULT 'DEMO',
    created_at     TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_plant_insp_tag ON plant_inspections(equipment_tag);

CREATE TABLE IF NOT EXISTS plant_alerts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id       TEXT UNIQUE NOT NULL,
    raised_at      TEXT,
    equipment_tag  TEXT COLLATE NOCASE,
    category       TEXT,
    severity       TEXT,
    description    TEXT,
    status         TEXT DEFAULT 'open',
    acknowledged_by TEXT,
    acknowledged_at TEXT,
    resolved_at    TEXT,
    source         TEXT DEFAULT 'DEMO',
    created_at     TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_plant_alert_status ON plant_alerts(status);

CREATE TABLE IF NOT EXISTS plant_inventory (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    part_number    TEXT UNIQUE NOT NULL COLLATE NOCASE,
    description    TEXT,
    category       TEXT,
    quantity       INTEGER DEFAULT 0,
    minimum_stock  INTEGER DEFAULT 0,
    unit           TEXT,
    storage_location TEXT,
    supplier       TEXT,
    equipment_tag  TEXT COLLATE NOCASE,
    last_counted   TEXT,
    source         TEXT DEFAULT 'DEMO',
    created_at     TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at     TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS plant_permits (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    permit_id      TEXT UNIQUE NOT NULL,
    permit_type    TEXT,
    equipment_tag  TEXT COLLATE NOCASE,
    work_location  TEXT,
    description    TEXT,
    hazards        TEXT,
    precautions    TEXT,
    requested_by   TEXT,
    approved_by    TEXT,
    status         TEXT DEFAULT 'draft',
    valid_from     TEXT,
    valid_to       TEXT,
    approved_at    TEXT,
    closed_at      TEXT,
    source         TEXT DEFAULT 'LOCAL',
    created_at     TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS plant_loto (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    loto_id        TEXT UNIQUE NOT NULL,
    equipment_tag  TEXT NOT NULL COLLATE NOCASE,
    isolation_points TEXT,
    isolation_status TEXT DEFAULT 'not_isolated',
    applied_by     TEXT,
    applied_at     TEXT,
    verified_by    TEXT,
    verified_at    TEXT,
    released_by    TEXT,
    released_at    TEXT,
    permit_id      TEXT,
    notes          TEXT,
    source         TEXT DEFAULT 'LOCAL',
    created_at     TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS plant_readings (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    equipment_tag  TEXT NOT NULL COLLATE NOCASE,
    measured_at    TEXT,
    parameter      TEXT,
    value          REAL,
    unit           TEXT,
    quality        TEXT DEFAULT 'good',
    source         TEXT NOT NULL,
    created_at     TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_plant_readings_tag ON plant_readings(equipment_tag, measured_at);
"""

_INITIALISED = False


def init_plant_schema(force: bool = False):
    """Additive migration. Runs once per process; safe to call repeatedly."""
    global _INITIALISED
    if _INITIALISED and not force:
        return
    database.init_db()
    conn = database.get_connection()
    try:
        conn.executescript(PLANT_SCHEMA)
        conn.commit()
    finally:
        conn.close()
    _INITIALISED = True


def _conn() -> sqlite3.Connection:
    init_plant_schema()
    return database.get_connection()


def _rows(sql: str, params=()) -> List[Dict[str, Any]]:
    conn = _conn()
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _one(sql: str, params=()) -> Optional[Dict[str, Any]]:
    found = _rows(sql, params)
    return found[0] if found else None


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


# ------------------------------------------------------------- equipment --

def upsert_equipment(tag: str, **fields) -> str:
    """Create or update one piece of equipment. Returns the tag."""
    tag = (tag or "").strip()
    if not tag:
        raise ValueError("equipment tag is required")
    allowed = ("name", "equipment_type", "location", "unit", "status", "criticality",
               "manufacturer", "model", "serial_number", "installed_on", "source")
    data = {k: v for k, v in fields.items() if k in allowed and v is not None}
    attributes = fields.get("attributes")
    conn = _conn()
    try:
        existing = conn.execute("SELECT id FROM plant_equipment WHERE tag = ?", (tag,)).fetchone()
        if existing:
            if data:
                sets = ", ".join(f"{k} = ?" for k in data)
                conn.execute(f"UPDATE plant_equipment SET {sets}, updated_at = ? WHERE tag = ?",
                             list(data.values()) + [_now(), tag])
        else:
            data.setdefault("name", tag)
            data.setdefault("source", DEMO)
            cols = ["tag"] + list(data)
            if attributes is not None:
                cols.append("attributes")
            values = [tag] + list(data.values())
            if attributes is not None:
                values.append(json.dumps(attributes))
            conn.execute(f"INSERT INTO plant_equipment ({', '.join(cols)}) "
                         f"VALUES ({', '.join('?' * len(cols))})", values)
        conn.commit()
    finally:
        conn.close()
    return tag


def get_equipment(tag: str) -> Optional[Dict[str, Any]]:
    return _one("SELECT * FROM plant_equipment WHERE tag = ?", ((tag or "").strip(),))


def find_equipment(fragment: str) -> List[Dict[str, Any]]:
    """Tag or name match. 'P-204' and 'pump p-204' both find the same row."""
    fragment = (fragment or "").strip()
    if not fragment:
        return []
    like = f"%{fragment}%"
    return _rows("SELECT * FROM plant_equipment WHERE tag LIKE ? OR name LIKE ? "
                 "ORDER BY tag", (like, like))


def resolve_equipment(reference: str) -> Optional[Dict[str, Any]]:
    """Best single match for something a person typed or said.

    Tries the exact tag, then a tag-shaped token inside the phrase ('the pump
    P-204 in unit 2'), then a name/tag fragment. Returns None rather than
    guessing between several equally good matches.
    """
    reference = (reference or "").strip()
    if not reference:
        return None
    exact = get_equipment(reference)
    if exact:
        return exact

    for token in re.findall(r"[A-Za-z]{1,4}[- ]?\d{2,5}[A-Za-z]?", reference):
        candidate = get_equipment(token.replace(" ", "-"))
        if candidate:
            return candidate
    matches = find_equipment(reference)
    if len(matches) == 1:
        return matches[0]
    lowered = reference.lower()
    # Several matches: prefer one whose tag appears verbatim in the phrase.
    for row in matches:
        if row["tag"].lower() in lowered:
            return row
    if matches:
        return None

    # No substring match. People say "the crude feed pump", not the exact name,
    # so score every item by how many significant words of the phrase appear in
    # its name, and accept a clear winner.
    stop = {"the", "a", "an", "for", "of", "on", "in", "at", "my", "our", "this",
            "that", "show", "me", "please", "and", "to", "unit", "equipment"}
    words = [w for w in re.split(r"[^a-z0-9]+", lowered) if w and w not in stop]
    if not words:
        return None
    scored = []
    for row in list_equipment(limit=10000):
        haystack = f"{row['name']} {row.get('equipment_type') or ''} {row['tag']}".lower()
        score = sum(1 for w in words if w in haystack)
        if score:
            scored.append((score, row))
    if not scored:
        return None
    scored.sort(key=lambda pair: pair[0], reverse=True)
    best = scored[0][0]
    winners = [row for score, row in scored if score == best]
    return winners[0] if len(winners) == 1 else None


def list_equipment(status: str = "", location: str = "",
                   limit: int = 200) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM plant_equipment WHERE 1=1"
    params: list = []
    if status:
        sql += " AND status = ?"
        params.append(status)
    if location:
        sql += " AND location LIKE ?"
        params.append(f"%{location}%")
    sql += " ORDER BY tag LIMIT ?"
    params.append(limit)
    return _rows(sql, params)


def equipment_status_summary() -> Dict[str, Any]:
    rows = list_equipment(limit=10000)
    counts: Dict[str, int] = {}
    for row in rows:
        counts[row["status"] or "unknown"] = counts.get(row["status"] or "unknown", 0) + 1
    return {
        "total": len(rows),
        "by_status": counts,
        "demo_rows": sum(1 for r in rows if r["source"] == DEMO),
    }


# ----------------------------------------------------------- maintenance --

def add_maintenance(equipment_tag: str, description: str, maintenance_type: str = "corrective",
                    performed_on: str = "", technician: str = "", duration_hours: float = 0.0,
                    parts_used: str = "", findings: str = "", status: str = "completed",
                    next_due: str = "", work_order: str = "", source: str = "LOCAL") -> int:
    conn = _conn()
    try:
        cur = conn.execute(
            "INSERT INTO plant_maintenance (equipment_tag, performed_on, maintenance_type, "
            "description, technician, duration_hours, parts_used, findings, status, next_due, "
            "work_order, source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (equipment_tag.strip(), performed_on or _today(), maintenance_type, description,
             technician, duration_hours, parts_used, findings, status, next_due,
             work_order or f"WO-{uuid.uuid4().hex[:8].upper()}", source))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def maintenance_history(equipment_tag: str = "", limit: int = 50) -> List[Dict[str, Any]]:
    if equipment_tag:
        return _rows("SELECT * FROM plant_maintenance WHERE equipment_tag = ? "
                     "ORDER BY performed_on DESC, id DESC LIMIT ?",
                     (equipment_tag.strip(), limit))
    return _rows("SELECT * FROM plant_maintenance ORDER BY performed_on DESC, id DESC LIMIT ?",
                 (limit,))


def maintenance_due(within_days: int = 30) -> List[Dict[str, Any]]:
    cutoff = (datetime.now() + timedelta(days=within_days)).strftime("%Y-%m-%d")
    return _rows("SELECT * FROM plant_maintenance WHERE next_due != '' AND next_due <= ? "
                 "AND status != 'cancelled' ORDER BY next_due", (cutoff,))


# ----------------------------------------------------------- inspections --

def add_inspection(equipment_tag: str, result: str, inspection_type: str = "visual",
                   inspected_on: str = "", inspector: str = "", findings: str = "",
                   recommendation: str = "", document_path: str = "", next_due: str = "",
                   source: str = "LOCAL") -> int:
    conn = _conn()
    try:
        cur = conn.execute(
            "INSERT INTO plant_inspections (equipment_tag, inspected_on, inspection_type, "
            "inspector, result, findings, recommendation, document_path, next_due, source) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (equipment_tag.strip(), inspected_on or _today(), inspection_type, inspector,
             result, findings, recommendation, document_path, next_due, source))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def inspection_history(equipment_tag: str = "", limit: int = 50) -> List[Dict[str, Any]]:
    if equipment_tag:
        return _rows("SELECT * FROM plant_inspections WHERE equipment_tag = ? "
                     "ORDER BY inspected_on DESC, id DESC LIMIT ?", (equipment_tag.strip(), limit))
    return _rows("SELECT * FROM plant_inspections ORDER BY inspected_on DESC, id DESC LIMIT ?",
                 (limit,))


# ---------------------------------------------------------------- alerts --

SEVERITIES = ("critical", "high", "medium", "low", "info")


def raise_alert(description: str, equipment_tag: str = "", category: str = "condition",
                severity: str = "medium", source: str = "LOCAL",
                raised_at: str = "") -> Dict[str, Any]:
    severity = severity.lower() if severity.lower() in SEVERITIES else "medium"
    alert_id = f"ALM-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO plant_alerts (alert_id, raised_at, equipment_tag, category, severity, "
            "description, status, source) VALUES (?,?,?,?,?,?,'open',?)",
            (alert_id, raised_at or _now(), equipment_tag.strip(), category, severity,
             description, source))
        conn.commit()
    finally:
        conn.close()
    return get_alert(alert_id)


def get_alert(alert_id: str) -> Optional[Dict[str, Any]]:
    return _one("SELECT * FROM plant_alerts WHERE alert_id = ?", (alert_id,))


def list_alerts(status: str = "open", severity: str = "", equipment_tag: str = "",
                limit: int = 100) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM plant_alerts WHERE 1=1"
    params: list = []
    if status and status != "all":
        sql += " AND status = ?"
        params.append(status)
    if severity:
        sql += " AND severity = ?"
        params.append(severity.lower())
    if equipment_tag:
        sql += " AND equipment_tag = ?"
        params.append(equipment_tag.strip())
    sql += (" ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 "
            "WHEN 'medium' THEN 2 WHEN 'low' THEN 3 ELSE 4 END, raised_at DESC LIMIT ?")
    params.append(limit)
    return _rows(sql, params)


def acknowledge_alert(alert_id: str, by: str = "") -> Optional[Dict[str, Any]]:
    conn = _conn()
    try:
        conn.execute("UPDATE plant_alerts SET status = 'acknowledged', acknowledged_by = ?, "
                     "acknowledged_at = ? WHERE alert_id = ? AND status = 'open'",
                     (by, _now(), alert_id))
        conn.commit()
    finally:
        conn.close()
    return get_alert(alert_id)


def resolve_alert(alert_id: str, by: str = "") -> Optional[Dict[str, Any]]:
    conn = _conn()
    try:
        conn.execute("UPDATE plant_alerts SET status = 'resolved', resolved_at = ?, "
                     "acknowledged_by = COALESCE(NULLIF(acknowledged_by, ''), ?) "
                     "WHERE alert_id = ?", (_now(), by, alert_id))
        conn.commit()
    finally:
        conn.close()
    return get_alert(alert_id)


# ------------------------------------------------------------- inventory --

def upsert_inventory(part_number: str, **fields) -> str:
    part_number = (part_number or "").strip()
    if not part_number:
        raise ValueError("part number is required")
    allowed = ("description", "category", "quantity", "minimum_stock", "unit",
               "storage_location", "supplier", "equipment_tag", "last_counted", "source")
    data = {k: v for k, v in fields.items() if k in allowed and v is not None}
    conn = _conn()
    try:
        existing = conn.execute("SELECT id FROM plant_inventory WHERE part_number = ?",
                                (part_number,)).fetchone()
        if existing:
            if data:
                sets = ", ".join(f"{k} = ?" for k in data)
                conn.execute(f"UPDATE plant_inventory SET {sets}, updated_at = ? "
                             f"WHERE part_number = ?", list(data.values()) + [_now(), part_number])
        else:
            data.setdefault("source", DEMO)
            cols = ["part_number"] + list(data)
            conn.execute(f"INSERT INTO plant_inventory ({', '.join(cols)}) "
                         f"VALUES ({', '.join('?' * len(cols))})",
                         [part_number] + list(data.values()))
        conn.commit()
    finally:
        conn.close()
    return part_number


def list_inventory(category: str = "", low_stock_only: bool = False,
                   equipment_tag: str = "", limit: int = 500) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM plant_inventory WHERE 1=1"
    params: list = []
    if category:
        sql += " AND category LIKE ?"
        params.append(f"%{category}%")
    if equipment_tag:
        sql += " AND equipment_tag = ?"
        params.append(equipment_tag.strip())
    if low_stock_only:
        sql += " AND quantity <= minimum_stock"
    sql += " ORDER BY part_number LIMIT ?"
    params.append(limit)
    return _rows(sql, params)


def find_part(fragment: str) -> List[Dict[str, Any]]:
    like = f"%{(fragment or '').strip()}%"
    return _rows("SELECT * FROM plant_inventory WHERE part_number LIKE ? OR description LIKE ? "
                 "ORDER BY part_number", (like, like))


# --------------------------------------------------------------- permits --

PERMIT_STATUSES = ("draft", "requested", "approved", "rejected", "active", "closed")


def create_permit(permit_type: str, equipment_tag: str = "", work_location: str = "",
                  description: str = "", hazards: str = "", precautions: str = "",
                  requested_by: str = "", valid_from: str = "", valid_to: str = "") -> Dict[str, Any]:
    """Creates a permit in REQUESTED state. Never approved - see approve_permit."""
    permit_id = f"PTW-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:5].upper()}"
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO plant_permits (permit_id, permit_type, equipment_tag, work_location, "
            "description, hazards, precautions, requested_by, status, valid_from, valid_to) "
            "VALUES (?,?,?,?,?,?,?,?,'requested',?,?)",
            (permit_id, permit_type, equipment_tag.strip(), work_location, description,
             hazards, precautions, requested_by, valid_from or _today(), valid_to))
        conn.commit()
    finally:
        conn.close()
    return get_permit(permit_id)


def get_permit(permit_id: str) -> Optional[Dict[str, Any]]:
    return _one("SELECT * FROM plant_permits WHERE permit_id = ?", (permit_id,))


def list_permits(status: str = "", limit: int = 100) -> List[Dict[str, Any]]:
    if status and status != "all":
        return _rows("SELECT * FROM plant_permits WHERE status = ? ORDER BY created_at DESC "
                     "LIMIT ?", (status, limit))
    return _rows("SELECT * FROM plant_permits ORDER BY created_at DESC LIMIT ?", (limit,))


def set_permit_status(permit_id: str, status: str, by: str = "") -> Optional[Dict[str, Any]]:
    """Record a decision a HUMAN made. Callers must obtain that decision first -
    see industrial/plant_ops.py, where approval is gated on confirmation."""
    if status not in PERMIT_STATUSES:
        raise ValueError(f"unknown permit status: {status}")
    conn = _conn()
    try:
        if status == "approved":
            conn.execute("UPDATE plant_permits SET status = ?, approved_by = ?, approved_at = ? "
                         "WHERE permit_id = ?", (status, by, _now(), permit_id))
        elif status == "closed":
            conn.execute("UPDATE plant_permits SET status = ?, closed_at = ? WHERE permit_id = ?",
                         (status, _now(), permit_id))
        else:
            conn.execute("UPDATE plant_permits SET status = ? WHERE permit_id = ?",
                         (status, permit_id))
        conn.commit()
    finally:
        conn.close()
    return get_permit(permit_id)


# ------------------------------------------------------------------ LOTO --

def create_loto(equipment_tag: str, isolation_points: str = "", applied_by: str = "",
                permit_id: str = "", notes: str = "") -> Dict[str, Any]:
    """Opens a LOTO record in 'not_isolated'. This is a RECORD of a physical
    isolation someone performs by hand. ZEUS never operates plant equipment."""
    loto_id = f"LOTO-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:5].upper()}"
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO plant_loto (loto_id, equipment_tag, isolation_points, isolation_status, "
            "applied_by, applied_at, permit_id, notes) VALUES (?,?,?,'not_isolated',?,?,?,?)",
            (loto_id, equipment_tag.strip(), isolation_points, applied_by, _now(),
             permit_id, notes))
        conn.commit()
    finally:
        conn.close()
    return get_loto(loto_id)


def get_loto(loto_id: str) -> Optional[Dict[str, Any]]:
    return _one("SELECT * FROM plant_loto WHERE loto_id = ?", (loto_id,))


def list_loto(equipment_tag: str = "", active_only: bool = False,
              limit: int = 100) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM plant_loto WHERE 1=1"
    params: list = []
    if equipment_tag:
        sql += " AND equipment_tag = ?"
        params.append(equipment_tag.strip())
    if active_only:
        sql += " AND released_at IS NULL"
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    return _rows(sql, params)


def set_loto_state(loto_id: str, isolation_status: str, by: str = "") -> Optional[Dict[str, Any]]:
    """Record a physical isolation state change a HUMAN performed and verified."""
    valid = ("not_isolated", "isolated", "verified", "released")
    if isolation_status not in valid:
        raise ValueError(f"unknown isolation status: {isolation_status}")
    conn = _conn()
    try:
        if isolation_status == "verified":
            conn.execute("UPDATE plant_loto SET isolation_status = ?, verified_by = ?, "
                         "verified_at = ? WHERE loto_id = ?",
                         (isolation_status, by, _now(), loto_id))
        elif isolation_status == "released":
            conn.execute("UPDATE plant_loto SET isolation_status = ?, released_by = ?, "
                         "released_at = ? WHERE loto_id = ?",
                         (isolation_status, by, _now(), loto_id))
        else:
            conn.execute("UPDATE plant_loto SET isolation_status = ? WHERE loto_id = ?",
                         (isolation_status, loto_id))
        conn.commit()
    finally:
        conn.close()
    return get_loto(loto_id)


# -------------------------------------------------------------- readings --

def record_reading(equipment_tag: str, parameter: str, value: float, unit: str = "",
                   source: str = "", measured_at: str = "", quality: str = "good") -> int:
    """The integration point for a REAL data source.

    `source` is mandatory and is stored verbatim, so a reading can always be
    traced to whatever produced it - an OPC-UA bridge, a historian export, a
    manual round sheet. Readings written by seed_demo_data() carry 'DEMO'.
    """
    if not source:
        raise ValueError("a reading must name its source - readings are never anonymous")
    conn = _conn()
    try:
        cur = conn.execute(
            "INSERT INTO plant_readings (equipment_tag, measured_at, parameter, value, unit, "
            "quality, source) VALUES (?,?,?,?,?,?,?)",
            (equipment_tag.strip(), measured_at or _now(), parameter, float(value), unit,
             quality, source))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def latest_readings(equipment_tag: str = "", limit: int = 100) -> List[Dict[str, Any]]:
    if equipment_tag:
        return _rows("SELECT * FROM plant_readings WHERE equipment_tag = ? "
                     "ORDER BY measured_at DESC, id DESC LIMIT ?", (equipment_tag.strip(), limit))
    return _rows("SELECT * FROM plant_readings ORDER BY measured_at DESC, id DESC LIMIT ?",
                 (limit,))


def reading_trend(equipment_tag: str, parameter: str, points: int = 24) -> Dict[str, Any]:
    rows = _rows("SELECT value, measured_at, unit, source FROM plant_readings "
                 "WHERE equipment_tag = ? AND parameter = ? ORDER BY measured_at DESC LIMIT ?",
                 (equipment_tag.strip(), parameter, points))
    rows.reverse()
    values = [r["value"] for r in rows]
    trend = {
        "equipment": equipment_tag, "parameter": parameter,
        "points": len(values), "values": values,
        "unit": rows[0]["unit"] if rows else "",
        "sources": sorted({r["source"] for r in rows}),
    }
    if values:
        trend.update(latest=values[-1], minimum=min(values), maximum=max(values),
                     average=round(sum(values) / len(values), 3),
                     direction=("rising" if values[-1] > values[0]
                                else "falling" if values[-1] < values[0] else "flat"))
    return trend


# ------------------------------------------------------------------ demo --

def data_sources() -> Dict[str, Any]:
    """Which of the plant tables hold real rows and which hold DEMO rows.

    Every summary in the industrial layer reports this, so nobody mistakes the
    seeded refinery for a connected plant.
    """
    out: Dict[str, Any] = {}
    for table in ("plant_equipment", "plant_maintenance", "plant_inspections", "plant_alerts",
                  "plant_inventory", "plant_permits", "plant_loto", "plant_readings"):
        rows = _rows(f"SELECT source, COUNT(*) AS n FROM {table} GROUP BY source")
        out[table] = {r["source"]: r["n"] for r in rows}
    demo_rows = sum(counts.get(DEMO, 0) for counts in out.values())
    real_rows = sum(n for counts in out.values() for src, n in counts.items() if src != DEMO)
    return {
        "by_table": out,
        "demo_rows": demo_rows,
        "non_demo_rows": real_rows,
        "live_sensor_feed": False,
        "note": ("No live plant connection exists on this workstation. Rows marked DEMO are "
                 "generated sample data for demonstration; connect a real source through "
                 "plant_data.record_reading(source=...) to store real measurements."),
    }


def seed_demo_data(force: bool = False) -> Dict[str, Any]:
    """Populate a small, clearly-labelled refinery so every industrial command
    has something to answer with on a fresh install.

    Every row written here carries source='DEMO'. This is sample data, not a
    measurement of anything.
    """
    init_plant_schema()
    existing = _one("SELECT COUNT(*) AS n FROM plant_equipment")
    if existing and existing["n"] and not force:
        return {"seeded": False, "reason": "equipment already present",
                "equipment": existing["n"]}

    equipment = [
        ("P-204", "Crude Feed Pump A", "centrifugal_pump", "Unit 2 - Crude Distillation",
         "CDU", "operational", "high", "Flowserve", "3600-HP", "2019-04-11"),
        ("P-302", "Reflux Pump", "centrifugal_pump", "Unit 3 - Vacuum Distillation",
         "VDU", "maintenance", "medium", "Sulzer", "OHH-200", "2020-08-02"),
        ("E-205", "Crude Preheat Exchanger", "heat_exchanger", "Unit 2 - Crude Distillation",
         "CDU", "operational", "high", "Alfa Laval", "SX-400", "2018-01-20"),
        ("T-103", "Naphtha Storage Tank", "storage_tank", "Tank Farm West",
         "TANK FARM", "operational", "medium", "CB&I", "FR-2200", "2015-06-30"),
        ("C-101", "Wet Gas Compressor", "compressor", "Unit 1 - Gas Plant",
         "SRU", "operational", "critical", "Elliott", "MS-6000", "2017-11-15"),
        ("V-410", "Amine Contactor", "vessel", "Unit 4 - Sulphur Recovery",
         "SRU", "shutdown", "high", "Koch", "AC-900", "2016-03-08"),
        ("F-201", "Crude Charge Heater", "fired_heater", "Unit 2 - Crude Distillation",
         "CDU", "operational", "critical", "Foster Wheeler", "BH-1200", "2014-09-01"),
        ("PSV-2041", "Pump Discharge Relief Valve", "relief_valve",
         "Unit 2 - Crude Distillation", "CDU", "operational", "critical",
         "Farris", "2600-L", "2019-04-11"),
    ]
    for tag, name, etype, location, unit, status, crit, maker, model, installed in equipment:
        upsert_equipment(tag, name=name, equipment_type=etype, location=location, unit=unit,
                         status=status, criticality=crit, manufacturer=maker, model=model,
                         installed_on=installed, source=DEMO)

    day = lambda n: (datetime.now() - timedelta(days=n)).strftime("%Y-%m-%d")
    ahead = lambda n: (datetime.now() + timedelta(days=n)).strftime("%Y-%m-%d")

    maintenance = [
        ("P-204", day(212), "preventive", "Annual overhaul: bearings and mechanical seal renewed",
         "R. Sharma", 8.5, "Bearing 6312-C3 x2, Seal kit MSK-204", "Wear within limits",
         "completed", ahead(153)),
        ("P-204", day(96), "corrective", "Replaced outboard bearing after vibration rise",
         "A. Kulkarni", 4.0, "Bearing 6312-C3", "Vibration returned to 2.1 mm/s after repair",
         "completed", ""),
        ("P-204", day(21), "predictive", "Vibration route reading - upward trend on outboard",
         "Condition Monitoring", 0.5, "", "Trend rising; recommend inspection within 30 days",
         "completed", ahead(9)),
        ("P-302", day(4), "corrective", "Mechanical seal leak - pump removed for repair",
         "R. Sharma", 6.0, "Seal kit MSK-302", "Seal faces scored", "in_progress", ahead(3)),
        ("E-205", day(45), "preventive", "Tube bundle cleaning and hydrotest",
         "Contract crew", 16.0, "Gaskets 8x", "Fouling heavier than expected on shell side",
         "completed", ahead(135)),
        ("C-101", day(150), "preventive", "Compressor lube oil change and filter renewal",
         "M. Iyer", 5.0, "Oil 200L, Filter LF-9", "Oil analysis normal", "completed", ahead(30)),
        ("F-201", day(300), "inspection", "Refractory survey and burner tuning",
         "Contract crew", 24.0, "", "Two burner tips replaced", "completed", ahead(65)),
        ("T-103", day(500), "inspection", "API 653 external inspection",
         "TPI", 12.0, "", "Shell course 1 minor pitting, within limits", "completed", ahead(230)),
    ]
    for tag, on, mtype, desc, tech, hours, parts, findings, status, due in maintenance:
        add_maintenance(tag, desc, maintenance_type=mtype, performed_on=on, technician=tech,
                        duration_hours=hours, parts_used=parts, findings=findings,
                        status=status, next_due=due, source=DEMO)

    inspections = [
        ("P-204", day(21), "vibration", "Condition Monitoring", "attention",
         "Outboard bearing vibration 4.8 mm/s RMS, up from 2.1 mm/s",
         "Inspect bearing and alignment within 30 days", ahead(9)),
        ("E-205", day(12), "thermography", "Reliability", "attention",
         "Approach temperature increased 6 C over 90 days, fouling suspected",
         "Schedule cleaning at next opportunity", ahead(48)),
        ("T-103", day(500), "API 653 external", "TPI", "pass",
         "Shell course 1 minor pitting, remaining thickness above minimum",
         "Re-inspect per API 653 interval", ahead(230)),
        ("PSV-2041", day(400), "relief valve pop test", "Valve shop", "pass",
         "Set pressure within 3 percent of nameplate",
         "Next test due per statutory interval", ahead(-35)),
    ]
    for tag, on, itype, inspector, result, findings, rec, due in inspections:
        add_inspection(tag, result, inspection_type=itype, inspected_on=on, inspector=inspector,
                       findings=findings, recommendation=rec, next_due=due, source=DEMO)

    alerts = [
        ("High vibration on Crude Feed Pump P-204 outboard bearing (4.8 mm/s RMS)",
         "P-204", "condition", "high"),
        ("Preheat exchanger E-205 approach temperature rising - fouling suspected",
         "E-205", "process", "medium"),
        ("Relief valve PSV-2041 statutory test overdue by 35 days",
         "PSV-2041", "compliance", "critical"),
        ("Reflux pump P-302 out of service for seal repair", "P-302", "maintenance", "medium"),
        ("Naphtha tank T-103 inspection due within 8 months", "T-103", "inspection", "low"),
    ]
    for desc, tag, cat, sev in alerts:
        raise_alert(desc, equipment_tag=tag, category=cat, severity=sev, source=DEMO)

    inventory = [
        ("BRG-6312-C3", "Deep groove ball bearing 6312 C3", "bearing", 6, 4, "ea",
         "Store A - Rack 12", "SKF", "P-204"),
        ("SEAL-MSK-204", "Mechanical seal kit, 45 mm cartridge", "seal", 2, 2, "kit",
         "Store A - Rack 3", "John Crane", "P-204"),
        ("SEAL-MSK-302", "Mechanical seal kit, 50 mm cartridge", "seal", 1, 2, "kit",
         "Store A - Rack 3", "John Crane", "P-302"),
        ("GSK-E205-SH", "Shell side gasket set, spiral wound", "gasket", 8, 4, "set",
         "Store B - Bin 22", "Flexitallic", "E-205"),
        ("LUB-ISO46-200", "Turbine oil ISO VG 46, 200 L drum", "lubricant", 3, 2, "drum",
         "Store C - Bay 1", "Shell", "C-101"),
        ("FLT-LF9", "Lube oil filter element LF-9", "filter", 12, 6, "ea",
         "Store B - Bin 8", "Pall", "C-101"),
        ("BRN-TIP-F201", "Burner tip assembly", "burner", 1, 3, "ea",
         "Store D - Cage 2", "Zeeco", "F-201"),
        ("PSV-SPRING-2600", "Relief valve spring, 2600 series", "valve_part", 2, 1, "ea",
         "Store D - Cage 5", "Farris", "PSV-2041"),
    ]
    for pn, desc, cat, qty, minimum, unit, loc, supplier, tag in inventory:
        upsert_inventory(pn, description=desc, category=cat, quantity=qty, minimum_stock=minimum,
                         unit=unit, storage_location=loc, supplier=supplier, equipment_tag=tag,
                         last_counted=day(30), source=DEMO)

    # A short vibration trend, so "show the trend" has something honest to show.
    base = [2.1, 2.2, 2.4, 2.3, 2.7, 3.0, 3.2, 3.6, 3.9, 4.2, 4.5, 4.8]
    for i, value in enumerate(base):
        record_reading("P-204", "vibration_rms", value, unit="mm/s", source=DEMO,
                       measured_at=(datetime.now() - timedelta(days=len(base) - i)
                                    ).strftime("%Y-%m-%d %H:%M:%S"))

    summary = {
        "seeded": True,
        "source": DEMO,
        "equipment": len(equipment),
        "maintenance_records": len(maintenance),
        "inspections": len(inspections),
        "alerts": len(alerts),
        "inventory_items": len(inventory),
        "readings": len(base),
        "warning": "All seeded rows are marked DEMO. They are sample data, not plant readings.",
    }
    logger.info(f"Seeded DEMO plant data: {summary['equipment']} equipment, "
                f"{summary['alerts']} alerts")
    return summary
