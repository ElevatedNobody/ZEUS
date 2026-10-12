"""
Offline conflict-safe synchronisation.

A site laptop in a tunnel, on a transmission corridor or at an oilfield keeps
working with no network: every capture is written to its own store at once
(SQLite, synchronous=FULL), with a global id, the device id, a Lamport clock
and the activity state it was based on (`base_head`).

Devices exchange SYNC BUNDLES - one zip per export, holding the events, the
evidence records and files they reference (content-addressed, hashes checked)
and the project / schedule structure a fresh device needs. A bundle travels on
whatever exists: a hub folder on a LAN share when the network is up, a USB
stick when it is not. No server, no cloud, no open port.

On import nothing is overwritten:
  * an event already present (same id) is a duplicate and is skipped;
  * an event whose content does not match its hash is refused;
  * two submissions for the same activity field, from different devices, where
    neither device knew of the other's (neither is an ancestor of the other
    through `base_head`) and whose values differ, are a CONFLICT: both are
    kept, both are marked, a conflict record explains why and recommends -
    but does not apply - a resolution. An engineer resolves it; the resolution
    is an event; the discarded value stays in the history as superseded.

"Online" is shown only when the hub folder was actually reached and written.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from jarvis import dates, events, store

FORMAT = "jarvis-sync/1"
BUNDLE_SUFFIX = ".jvsync"
_FIELD = {"progress_update": "percent_complete", "activity_started": "actual_start",
          "activity_completed": "actual_finish", "remaining_update": "remaining_days"}
_PAYLOAD_KEY = {"percent_complete": "percent_complete", "actual_start": "actual_start",
                "actual_finish": "actual_finish", "remaining_days": "remaining_days"}
_STRUCTURE = ("jv_projects", "jv_calendars", "jv_schedules", "jv_schedule_versions", "jv_wbs", "jv_activities",
              "jv_activity_versions", "jv_dependencies", "jv_contract_refs", "jv_entity_mappings")
_RECORDS = ("jv_evidence", "jv_documents", "jv_image_evidence", "jv_voice_evidence", "jv_field_observations",
            "jv_extractions", "jv_correspondence", "jv_links")


# ------------------------------------------------------------------ state --

def _get(conn, key: str) -> Optional[str]:
    return events._state(conn, key)


def _set(conn, key: str, value: str) -> None:
    events._set_state(conn, key, value)


def set_hub(folder: str) -> Dict[str, Any]:
    path = Path(folder).expanduser()
    with store.connection() as conn:
        _set(conn, "hub_path", str(path))
        store.audit("set_sync_hub", "sync", events.device_id(conn), "", {"hub": str(path)}, conn=conn)
    return {"ok": True, "hub_path": str(path), "reachable": probe_hub(str(path))}


def probe_hub(hub: Optional[str]) -> Dict[str, Any]:
    """Reachable only if the folder exists AND a file could be written and
    removed there just now - never assumed."""
    if not hub:
        return {"reachable": False, "reason": "no sync hub is configured"}
    path = Path(hub)
    try:
        if not path.is_dir():
            return {"reachable": False, "reason": f"{hub} is not reachable (not mounted / no network)"}
        probe = path / f".jarvis_probe_{os.getpid()}"
        probe.write_text("probe", encoding="utf-8")
        probe.unlink()
        return {"reachable": True, "reason": "folder reached and written just now"}
    except OSError as error:
        return {"reachable": False, "reason": f"{hub}: {error.strerror or error}"}


def status() -> Dict[str, Any]:
    with store.connection() as conn:
        me = events.device_id(conn)
        hub = _get(conn, "hub_path")
        outbox = conn.execute("SELECT COUNT(*) FROM jv_events WHERE device_id = ? AND sync_state = 'local'",
                              (me,)).fetchone()[0]
        received = conn.execute("SELECT COUNT(*) FROM jv_events WHERE sync_state = 'received'").fetchone()[0]
        peers = [r[0] for r in conn.execute("SELECT DISTINCT device_id FROM jv_events WHERE device_id != ? ORDER BY 1",
                                            (me,))]
        last = {d: dict(r) if r else None for d, r in (
            ("export", conn.execute("SELECT * FROM jv_sync_events WHERE direction = 'export' AND status = 'ok' "
                                    "ORDER BY at DESC LIMIT 1").fetchone()),
            ("import", conn.execute("SELECT * FROM jv_sync_events WHERE direction = 'import' AND status = 'ok' "
                                    "ORDER BY at DESC LIMIT 1").fetchone()))}
        last_ok = _get(conn, "last_successful_sync")
        conflicts = conn.execute("SELECT COUNT(*) FROM jv_sync_conflicts WHERE status = 'open'").fetchone()[0]
        history = [dict(r) for r in conn.execute("SELECT direction, peer_device, bundle_path, events_total, events_new, "
                                                 "duplicates, conflicts, status, detail, at FROM jv_sync_events "
                                                 "ORDER BY at DESC LIMIT 20")]
    probe = probe_hub(hub)
    return {"ok": True, "device_id": me, "hub_path": hub, "connectivity": "ONLINE" if probe["reachable"] else "OFFLINE",
            "connectivity_detail": probe["reason"], "pending_sync": outbox, "received_events": received,
            "open_conflicts": conflicts, "peers": peers, "last_export": last["export"], "last_import": last["import"],
            "last_successful_sync": last_ok, "history": history,
            "storage": "local SQLite (synchronous=FULL) - captures persist without a network"}


# ----------------------------------------------------------------- export --

def _rows(conn, table: str, where: str = "", params: Tuple = ()) -> List[Dict[str, Any]]:
    return [dict(r) for r in conn.execute(f"SELECT * FROM {table} {where}", params)]


def export_bundle(target_dir: Optional[str] = None, all_events: bool = False) -> Dict[str, Any]:
    with store.connection() as conn:
        me = events.device_id(conn)
        hub = _get(conn, "hub_path")
        folder = Path(target_dir) if target_dir else (Path(hub) / me if hub else None)
        if folder is None:
            return {"ok": False, "error": "no destination - give a folder or configure a sync hub"}
        if not target_dir:
            probe = probe_hub(hub)
            if not probe["reachable"]:
                return {"ok": False, "offline": True, "error": f"OFFLINE - {probe['reason']}. "
                                                               "Captures stay queued on this device."}
        sql = "SELECT * FROM jv_events " + ("" if all_events else "WHERE device_id = ? AND sync_state = 'local' ") + \
              "ORDER BY seq"
        rows = [dict(r) for r in conn.execute(sql, () if all_events else (me,))]
        project_ids = sorted({r["project_id"] for r in rows}) or [r[0] for r in conn.execute("SELECT project_id FROM jv_projects")]
        evidence_ids = sorted({e for r in rows for e in store.loads(r["evidence_ids"], [])})
        structure = {t: _rows(conn, t, _project_filter(t, project_ids), tuple(project_ids)) for t in _STRUCTURE}
        records = {}
        marks = ",".join("?" for _ in evidence_ids) or "''"
        for table in _RECORDS:
            if table in ("jv_evidence", "jv_documents", "jv_image_evidence", "jv_voice_evidence",
                         "jv_field_observations", "jv_extractions"):
                records[table] = _rows(conn, table, f"WHERE evidence_id IN ({marks})", tuple(evidence_ids))
            elif table == "jv_correspondence":
                records[table] = _rows(conn, table, f"WHERE evidence_id IN ({marks})", tuple(evidence_ids))
            else:
                records[table] = _rows(conn, table, f"WHERE from_kind = 'evidence' AND from_id IN ({marks})",
                                       tuple(evidence_ids))
        lamport = int(_get(conn, "lamport") or 0)
    payload_events = [{k: r[k] for k in r if k not in ("seq", "received_at", "prev_hash", "chain_hash", "sync_state")}
                      for r in rows]
    events_json = json.dumps(payload_events, ensure_ascii=False, sort_keys=True, default=str)
    manifest = {"format": FORMAT, "device_id": me, "exported_at": store.now_iso(), "lamport": lamport,
                "projects": project_ids, "events": len(payload_events),
                "events_sha256": hashlib.sha256(events_json.encode("utf-8")).hexdigest(), "files": []}
    folder.mkdir(parents=True, exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    # Bundles are found by their suffix and ordered by their manifest, so the
    # CRUDEX_ prefix (JARVIS_ before the rename) changes nothing for importers.
    bundle = folder / f"CRUDEX_{me}_{stamp}{BUNDLE_SUFFIX}"
    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("events.json", events_json)
        zf.writestr("structure.json", json.dumps(structure, ensure_ascii=False, default=str))
        zf.writestr("records.json", json.dumps(records, ensure_ascii=False, default=str))
        for ev in records.get("jv_evidence", []):
            if ev.get("stored_path") and Path(ev["stored_path"]).is_file():
                name = f"files/{ev['sha256']}{Path(ev['stored_path']).suffix}"
                zf.write(ev["stored_path"], name)
                manifest["files"].append(name)
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
    digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
    with store.connection() as conn:
        ids = [r["event_id"] for r in rows if r["device_id"] == me]
        for i in range(0, len(ids), 400):
            chunk = ids[i:i + 400]
            conn.execute(f"UPDATE jv_events SET sync_state = 'synced' WHERE event_id IN ({','.join('?' for _ in chunk)}) "
                         f"AND sync_state = 'local'", chunk)
        store.insert(conn, "jv_sync_events", {"sync_id": store.new_id("SYN"), "direction": "export", "device_id": me,
                                              "peer_device": None, "bundle_path": str(bundle), "bundle_hash": digest,
                                              "events_total": len(rows), "events_new": len(rows), "duplicates": 0,
                                              "conflicts": 0, "status": "ok", "detail": "", "at": store.now()})
        store.audit("export_sync_bundle", "sync", bundle.name, "", {"events": len(rows), "sha256": digest}, conn=conn)
    return {"ok": True, "bundle": str(bundle), "events": len(rows), "files": len(manifest["files"]),
            "sha256": digest, "device_id": me}


def _project_filter(table: str, project_ids: List[str]) -> str:
    marks = ",".join("?" for _ in project_ids) or "''"
    if table in ("jv_activity_versions", "jv_dependencies"):
        return f"WHERE version_id IN (SELECT version_id FROM jv_schedule_versions WHERE project_id IN ({marks}))"
    return f"WHERE project_id IN ({marks})"


# ----------------------------------------------------------------- import --

def import_bundle(path: str) -> Dict[str, Any]:
    bundle = Path(path)
    if not bundle.is_file():
        raise FileNotFoundError(f"{bundle} does not exist")
    digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
    with zipfile.ZipFile(bundle) as zf:
        manifest = json.loads(zf.read("manifest.json"))
        if manifest.get("format") != FORMAT:
            raise ValueError(f"{bundle.name} is not a ZEUS sync bundle ({manifest.get('format')})")
        events_json = zf.read("events.json").decode("utf-8")
        if hashlib.sha256(events_json.encode("utf-8")).hexdigest() != manifest["events_sha256"]:
            raise ValueError(f"{bundle.name}: the events do not match the manifest hash - refused")
        incoming = json.loads(events_json)
        structure = json.loads(zf.read("structure.json"))
        records = json.loads(zf.read("records.json"))
        scratch = Path(tempfile.mkdtemp(prefix="jarvis_sync_"))
        zf.extractall(scratch, [n for n in zf.namelist() if n.startswith("files/")])
    try:
        return _apply_bundle(bundle, digest, manifest, incoming, structure, records, scratch)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _apply_bundle(bundle, digest, manifest, incoming, structure, records, scratch) -> Dict[str, Any]:
    with store.connection() as conn:
        me = events.device_id(conn)
        if conn.execute("SELECT 1 FROM jv_sync_events WHERE bundle_hash = ? AND direction = 'import' AND status = 'ok'",
                        (digest,)).fetchone():
            return {"ok": True, "skipped": True, "reason": "this bundle was already imported", "bundle": str(bundle)}
        for table in _STRUCTURE:
            for row in structure.get(table, []):
                store.insert(conn, table, row, or_ignore=True)
        copied, refused_files = 0, []
        for ev in records.get("jv_evidence", []):
            if ev.get("stored_path"):
                source = scratch / "files" / f"{ev['sha256']}{Path(ev['stored_path']).suffix}"
                if source.is_file():
                    if hashlib.sha256(source.read_bytes()).hexdigest() != ev["sha256"]:
                        refused_files.append(ev["evidence_id"])
                        continue
                    target = store.evidence_root() / ev["project_id"] / ev["sha256"][:2] / source.name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if not target.exists():
                        shutil.copy2(source, target)
                    ev = dict(ev, stored_path=str(target))
                    copied += 1
            store.insert(conn, "jv_evidence", ev, or_ignore=True)
        for table in _RECORDS[1:]:
            for row in records.get(table, []):
                if table == "jv_links":
                    row = {k: v for k, v in row.items() if k != "link_id"}
                store.insert(conn, table, row, or_ignore=True)
        new, duplicates, refused, touched, decisions = 0, 0, [], set(), 0
        for ev in sorted(incoming, key=lambda e: (e.get("lamport") or 0, e.get("captured_at") or "")):
            if conn.execute("SELECT 1 FROM jv_events WHERE event_id = ?", (ev["event_id"],)).fetchone():
                duplicates += 1
                continue
            try:
                events.append(conn, ev["project_id"], ev["event_type"], activity_id=ev.get("activity_id"),
                              occurred_at=ev.get("occurred_at"), captured_at=ev.get("captured_at"),
                              device=ev.get("device_id"), actor=ev.get("actor"), source=ev.get("source") or "sync",
                              payload=store.loads(ev.get("payload"), {}), evidence_ids=store.loads(ev.get("evidence_ids"), []),
                              observation_id=ev.get("observation_id"), confidence=ev.get("confidence"),
                              origin=ev.get("origin") or "company", event_id=ev["event_id"],
                              base_head=ev.get("base_head"), lamport=ev.get("lamport"), sync_state="received",
                              expected_content_hash=ev.get("content_hash"))
            except ValueError as error:
                refused.append({"event_id": ev["event_id"], "reason": str(error)})
                continue
            new += 1
            if ev.get("activity_id"):
                touched.add((ev["project_id"], ev["activity_id"]))
            decisions += _apply_remote_decision(conn, ev)
            decisions += _apply_earlier_decisions(conn, ev["event_id"])
        found = []
        for project_id, activity_id in sorted(touched):
            found.extend(detect_conflicts(conn, project_id, activity_id))
        projects = sorted({p for p, _a in touched} | {e["project_id"] for e in incoming if e["event_type"] in
                                                       ("approval", "conflict_resolved", "schedule_changed")})
        store.insert(conn, "jv_sync_events", {"sync_id": store.new_id("SYN"), "direction": "import", "device_id": me,
                                              "peer_device": manifest.get("device_id"), "bundle_path": str(bundle),
                                              "bundle_hash": digest, "events_total": len(incoming), "events_new": new,
                                              "duplicates": duplicates, "conflicts": len(found), "status": "ok",
                                              "detail": store.dumps({"refused": refused, "files_copied": copied,
                                                                     "files_refused": refused_files}),
                                              "at": store.now()})
        store.audit("ingest_sync_bundle", "sync", bundle.name, "", {"new": new, "duplicates": duplicates,
                                                                   "conflicts": len(found), "refused": len(refused)},
                    conn=conn)
        recomputed = []
        if decisions or found:
            from jarvis import forecast
            for project_id in projects:
                try:
                    forecast.recompute(conn, project_id, None, "synchronised updates applied")
                    recomputed.append(project_id)
                except forecast.NoSchedule:
                    pass
    return {"ok": True, "bundle": str(bundle), "from_device": manifest.get("device_id"), "events": len(incoming),
            "new": new, "duplicates": duplicates, "refused": refused, "conflicts": found,
            "files_copied": copied, "files_refused": refused_files, "recomputed": recomputed}


def _apply_earlier_decisions(conn: sqlite3.Connection, event_id: str) -> int:
    """Bundles can arrive in any order: a decision about this event (an approval,
    a conflict resolution) may already be here. Apply it now - order-independent."""
    applied = 0
    for row in conn.execute("SELECT * FROM jv_events WHERE event_type IN ('approval', 'conflict_resolved') AND "
                            "(json_extract(payload, '$.target') = ? OR json_extract(payload, '$.chosen_event') = ? OR "
                            "EXISTS (SELECT 1 FROM json_each(jv_events.payload, '$.discarded_events') "
                            "WHERE json_each.value = ?)) ORDER BY lamport, seq", (event_id, event_id, event_id)).fetchall():
        applied += _apply_remote_decision(conn, events._decode(dict(row)))
    return applied


def _apply_remote_decision(conn: sqlite3.Connection, ev: Dict[str, Any]) -> int:
    payload = store.loads(ev.get("payload"), {}) if isinstance(ev.get("payload"), str) else (ev.get("payload") or {})
    if ev["event_type"] == "approval" and payload.get("target"):
        row = conn.execute("SELECT status FROM jv_event_status WHERE event_id = ?", (payload["target"],)).fetchone()
        if row and row["status"] == "submitted":
            conn.execute("UPDATE jv_event_status SET status = ?, decided_by = ?, decided_at = ?, decision_event_id = ? "
                         "WHERE event_id = ?", (payload.get("decision", "approved"), ev.get("actor"),
                                                ev.get("captured_at"), ev["event_id"], payload["target"]))
            return 1
    if ev["event_type"] == "conflict_resolved":
        if payload.get("chosen_event"):
            events.set_status(conn, payload["chosen_event"], "approved", "chosen in a conflict resolution", ev["event_id"])
        for discarded in payload.get("discarded_events") or []:
            events.set_status(conn, discarded, "superseded", "not chosen in a conflict resolution", ev["event_id"])
        if payload.get("conflict_id"):
            conn.execute("UPDATE jv_sync_conflicts SET status = 'resolved', resolution_event_id = ? WHERE conflict_id = ?",
                         (ev["event_id"], payload["conflict_id"]))
        return 1
    return 0


# --------------------------------------------------------------- conflicts --

def _ancestors(conn: sqlite3.Connection, event_id: Optional[str]) -> set:
    seen = set()
    while event_id and event_id not in seen:
        seen.add(event_id)
        row = conn.execute("SELECT base_head FROM jv_events WHERE event_id = ?", (event_id,)).fetchone()
        event_id = row["base_head"] if row else None
    return seen


def _value(event: Dict[str, Any], field_name: str):
    payload = event.get("payload") or {}
    value = payload.get(_PAYLOAD_KEY[field_name])
    if value is None and field_name in ("actual_start", "actual_finish"):
        value = dates.iso(event.get("occurred_at"))
    return value


def _differs(field_name: str, a, b) -> bool:
    if a is None or b is None:
        return False
    if field_name in ("percent_complete", "remaining_days"):
        return abs(float(a) - float(b)) > 0.5
    return dates.iso(a) != dates.iso(b)


def detect_conflicts(conn: sqlite3.Connection, project_id: str, activity_id: str) -> List[Dict[str, Any]]:
    rows = [e for e in events.list_events(conn, project_id, activity_id=activity_id, types=tuple(_FIELD))
            if e.get("status") not in ("rejected", "superseded")]
    found = []
    for i, a in enumerate(rows):
        for b in rows[i + 1:]:
            field_name = _FIELD[a["event_type"]]
            if _FIELD[b["event_type"]] != field_name or a["device_id"] == b["device_id"]:
                continue
            value_a, value_b = _value(a, field_name), _value(b, field_name)
            if not _differs(field_name, value_a, value_b):
                continue
            if a["event_id"] in _ancestors(conn, b.get("base_head")) or b["event_id"] in _ancestors(conn, a.get("base_head")):
                continue                       # sequential: the later device had seen the earlier update
            first, second = sorted((a, b), key=lambda e: e["event_id"])
            # the same conflict has the same id on every device, so a resolution made
            # anywhere closes it everywhere
            conflict_id = "CNF-" + hashlib.sha256(
                f"{first['event_id']}|{second['event_id']}|{field_name}".encode("ascii")).hexdigest()[:16]
            recommendation = recommend(conn, first, second, field_name)
            reason = (f"{field_name.replace('_', ' ')} reported as {_value(first, field_name)} on {first['device_id']} "
                      f"({first.get('actor') or 'unknown'}, captured {first['captured_at']}) and as "
                      f"{_value(second, field_name)} on {second['device_id']} ({second.get('actor') or 'unknown'}, "
                      f"captured {second['captured_at']}); both were captured from the same known state - neither "
                      f"device had the other's update - so neither silently overwrites the other.")
            cur = conn.execute(
                "INSERT OR IGNORE INTO jv_sync_conflicts (conflict_id, project_id, activity_id, field, event_a, event_b, "
                "value_a, value_b, device_a, device_b, actor_a, actor_b, captured_a, captured_b, reason, recommendation, "
                "status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)",
                (conflict_id, project_id, activity_id, field_name, first["event_id"], second["event_id"],
                 str(_value(first, field_name)), str(_value(second, field_name)), first["device_id"],
                 second["device_id"], first.get("actor"), second.get("actor"), first["captured_at"],
                 second["captured_at"], reason, store.dumps(recommendation), store.now()))
            if not cur.rowcount:
                continue
            for e in (first, second):
                if e.get("status") in ("submitted", None):
                    events.set_status(conn, e["event_id"], "conflicted", "concurrent offline submission")
            events.append(conn, project_id, "conflict_detected", activity_id=activity_id, source="sync", actor="ZEUS",
                          evidence_ids=sorted(set(first.get("evidence_ids", []) + second.get("evidence_ids", []))),
                          base_head=None, payload={"conflict_id": conflict_id, "field": field_name,
                                                   "event_a": first["event_id"], "event_b": second["event_id"],
                                                   "value_a": _value(first, field_name),
                                                   "value_b": _value(second, field_name), "summary": reason})
            found.append({"conflict_id": conflict_id, "field": field_name, "value_a": _value(first, field_name),
                          "value_b": _value(second, field_name), "device_a": first["device_id"],
                          "device_b": second["device_id"], "reason": reason, "recommendation": recommendation})
    return found


def recommend(conn: sqlite3.Connection, a: Dict[str, Any], b: Dict[str, Any], field_name: str) -> Dict[str, Any]:
    """A reasoned suggestion, never applied by itself."""
    def score(e):
        reasons, total = [], 0.0
        ids = list(e.get("evidence_ids") or [])
        marks = ",".join("?" for _ in ids) or "''"
        kinds = [r[0] for r in conn.execute(f"SELECT kind FROM jv_evidence WHERE evidence_id IN ({marks})", ids)]
        if any(k in ("image", "document", "correspondence", "spreadsheet") for k in kinds):
            total += 2
            reasons.append(f"backed by {', '.join(sorted(set(kinds)))} evidence")
        if (e.get("confidence") or 0) >= 0.8:
            total += 0.5
            reasons.append(f"link confidence {e['confidence']:.2f}")
        return total, reasons
    sa, ra = score(a)
    sb, rb = score(b)
    later = a if a["captured_at"] > b["captured_at"] else b
    if later is a:
        sa += 0.5
        ra.append("captured later (more recent observation)")
    else:
        sb += 0.5
        rb.append("captured later (more recent observation)")
    if field_name == "percent_complete":
        approved = conn.execute(
            "SELECT MAX(CAST(json_extract(e.payload, '$.percent_complete') AS REAL)) FROM jv_events e JOIN jv_event_status s "
            "ON s.event_id = e.event_id WHERE e.activity_id = ? AND s.status = 'approved' AND e.event_type = 'progress_update'",
            (a["activity_id"],)).fetchone()[0]
        for e, reasons, bump in ((a, ra, "a"), (b, rb, "b")):
            value = _value(e, field_name)
            if approved is not None and value is not None and float(value) < float(approved):
                if bump == "a":
                    sa -= 2
                else:
                    sb -= 2
                reasons.append(f"lower than the already approved {approved:g}% (progress would go backwards)")
    choice = "a" if sa >= sb else "b"
    return {"suggested": choice, "suggested_event": (a if choice == "a" else b)["event_id"],
            "a": {"score": sa, "reasons": ra}, "b": {"score": sb, "reasons": rb},
            "note": "a recommendation only - an engineer decides; the other value is kept in the history"}


def conflicts(project_id: Optional[str] = None, status: str = "open") -> List[Dict[str, Any]]:
    sql = ("SELECT c.*, a.code AS activity_code, a.name AS activity_name FROM jv_sync_conflicts c LEFT JOIN jv_activities a "
           "ON a.activity_id = c.activity_id WHERE (? = '' OR c.status = ?)")
    params: List[Any] = [status or "", status or ""]
    if project_id:
        sql += " AND c.project_id = ?"
        params.append(project_id)
    rows = store.query(sql + " ORDER BY c.created_at DESC", params)
    with store.connection() as conn:
        for r in rows:
            r["recommendation"] = store.loads(r["recommendation"], {})
            for side in ("a", "b"):
                ev = events.get(conn, r[f"event_{side}"])
                if ev:
                    r[f"version_{side}"] = {"event_id": ev["event_id"], "event_type": ev["event_type"],
                                            "device_id": ev["device_id"], "actor": ev["actor"],
                                            "captured_at": ev["captured_at"], "occurred_at": ev["occurred_at"],
                                            "source": ev["source"], "status": ev["status"],
                                            "note": (ev.get("payload") or {}).get("note"),
                                            "evidence_ids": ev.get("evidence_ids")}
    return rows


def resolve(conflict_id: str, choice: str, value: Optional[str] = None, reason: str = "") -> Dict[str, Any]:
    from jarvis.service import require_role
    require_role("engineer", "resolve a sync conflict")
    if choice not in ("a", "b", "value"):
        raise ValueError("choice is 'a', 'b' or 'value'")
    with store.connection() as conn:
        c = conn.execute("SELECT * FROM jv_sync_conflicts WHERE conflict_id = ?", (conflict_id,)).fetchone()
        if not c:
            raise ValueError(f"unknown conflict {conflict_id}")
        if c["status"] != "open":
            raise ValueError(f"conflict {conflict_id} is already {c['status']}")
        if choice == "value":
            if value is None or str(value).strip() == "":
                raise ValueError("give the value to record")
            chosen, discarded, final = None, [c["event_a"], c["event_b"]], value
        else:
            chosen = c["event_a"] if choice == "a" else c["event_b"]
            discarded = [c["event_b"] if choice == "a" else c["event_a"]]
            final = c["value_a"] if choice == "a" else c["value_b"]
        occurred = events.get(conn, chosen or c["event_b"])["occurred_at"]
        resolution = events.append(conn, c["project_id"], "conflict_resolved", activity_id=c["activity_id"],
                                   source="review", occurred_at=occurred,
                                   payload={"conflict_id": conflict_id, "field": c["field"],
                                            "value": float(final) if c["field"] in ("percent_complete", "remaining_days")
                                            else dates.iso(final), "chosen_event": chosen,
                                            "discarded_events": discarded, "reason": reason, "occurred_at": occurred,
                                            "summary": f"{c['field']} set to {final}"})
        if chosen:
            events.set_status(conn, chosen, "approved", "chosen in a conflict resolution", resolution["event_id"])
            conn.execute("UPDATE jv_event_status SET decided_by = ?, decided_at = ? WHERE event_id = ?",
                         (resolution["actor"], resolution["captured_at"], chosen))
        for d in discarded:
            events.set_status(conn, d, "superseded", "not chosen in a conflict resolution", resolution["event_id"])
        conn.execute("UPDATE jv_sync_conflicts SET status = 'resolved', resolution_event_id = ?, chosen_event = ?, "
                     "chosen_value = ?, resolved_by = ?, resolved_at = ? WHERE conflict_id = ?",
                     (resolution["event_id"], chosen, str(final), resolution["actor"], resolution["captured_at"],
                      conflict_id))
        store.audit("resolve_sync_conflict", "conflict", conflict_id, c["project_id"],
                    {"choice": choice, "value": str(final), "reason": reason}, conn=conn)
        from jarvis import forecast
        try:
            outcome = forecast.recompute(conn, c["project_id"], resolution["event_id"], "sync conflict resolved")
        except forecast.NoSchedule:
            outcome = None
    return {"ok": True, "conflict_id": conflict_id, "value": final, "chosen_event": chosen,
            "superseded": discarded, "resolution_event_id": resolution["event_id"], "forecast": outcome}


# ------------------------------------------------------------ one sync run --

def _causal_order(bundle: Path):
    """Apply bundles in Lamport order: a bundle exported after its device had
    seen another's events carries a higher clock than those events."""
    try:
        with zipfile.ZipFile(bundle) as zf:
            manifest = json.loads(zf.read("manifest.json"))
        return int(manifest.get("lamport") or 0), manifest.get("exported_at") or "", bundle.name
    except Exception:
        return 0, "", bundle.name


def run(hub: Optional[str] = None) -> Dict[str, Any]:
    """Export the outbox to the hub and import every other device's bundles
    not imported yet. Offline is reported as offline - nothing is faked."""
    with store.connection() as conn:
        hub = hub or _get(conn, "hub_path")
        me = events.device_id(conn)
    probe = probe_hub(hub)
    if not probe["reachable"]:
        return {"ok": False, "offline": True, "connectivity": "OFFLINE", "reason": probe["reason"],
                "pending_sync": status()["pending_sync"]}
    exported = export_bundle(str(Path(hub) / me))
    imported = []
    for bundle in sorted((b for b in Path(hub).glob(f"*/*{BUNDLE_SUFFIX}") if b.parent.name != me), key=_causal_order):
        try:
            imported.append(import_bundle(str(bundle)))
        except Exception as error:
            imported.append({"ok": False, "bundle": str(bundle), "error": str(error)})
    ok = exported.get("ok") and all(i.get("ok") for i in imported)
    if ok:
        with store.connection() as conn:
            _set(conn, "last_successful_sync", store.now_iso())
    return {"ok": bool(ok), "connectivity": "ONLINE", "exported": exported, "imported": imported,
            "conflicts": sum(len(i.get("conflicts") or []) for i in imported)}
