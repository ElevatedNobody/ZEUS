"""
ZEUS checks for the operator BAT files - every PASS is a real function that
ran and was validated on this machine.

    health         databases, event chain, evidence hashes, model routing,
                   offline capture, and the ZEUS test suite
    db             integrity + foreign keys of medha.db, knowledge_fabric.db,
                   jarvis_projects.db; the event hash chain; evidence files
    offline        network policy, loopback-only model runtime, no cloud code,
                   capture -> restart -> still there, conflict detection
    models [--live] which local model serves each ZEUS task; --live makes real
                   calls (vernacular, extraction, causal phrasing, embeddings,
                   OCR, vision) and validates the answers
    reindex [--embed] rebuild the project search index

Self-tests that write run against a TEMPORARY database, never the
project data; the operator's records are only read.
"""

from __future__ import annotations

import os
import re
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import List

from agni_ops.status import FAIL, NOT_TESTED, PASS, UNAVAILABLE, WARN, Result

ROOT = Path(__file__).resolve().parent.parent
GROUP = "jarvis"


def _r(component, status, reason="", **kw) -> Result:
    return Result(component, status, reason=reason, group=GROUP, **kw)


# ---------------------------------------------------------------- database --

def _sqlite_check(path: Path, label: str) -> Result:
    if not path.exists():
        return _r(f"database {label}", NOT_TESTED, f"{path.name} does not exist yet (created on first use)",
                  where=str(path))
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
        try:
            check = [row[0] for row in conn.execute("PRAGMA integrity_check").fetchall()]
            fk = conn.execute("PRAGMA foreign_key_check").fetchall()
            tables = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.DatabaseError as error:
        return _r(f"database {label}", FAIL, f"cannot be read: {error}", where=str(path))
    if check != ["ok"]:
        return _r(f"database {label}", FAIL, "integrity_check reported problems", where=str(path),
                  evidence="; ".join(check[:10]))
    if fk:
        return _r(f"database {label}", WARN, f"{len(fk)} foreign-key violation(s)", where=str(path),
                  evidence=str(fk[:10]))
    return _r(f"database {label}", PASS, f"integrity ok, {tables} tables, {path.stat().st_size // 1024} KB",
              where=str(path))


def db_checks() -> List[Result]:
    from core.paths import data_dir
    from jarvis import events, store
    results = [_sqlite_check(data_dir() / "medha.db", "medha.db (accounts, memory, audit anchor)"),
               _sqlite_check(data_dir() / "knowledge_fabric.db", "knowledge_fabric.db (company records RAG)"),
               _sqlite_check(store.db_path(), "jarvis_projects.db (projects, events, evidence)")]
    if store.db_path().exists():
        chain = events.verify_chain()
        results.append(_r("ZEUS event hash chain", PASS if chain["ok"] else FAIL,
                          f"{chain['events']} events verified" if chain["ok"] else
                          f"{len(chain['problems'])} problem(s): {chain['problems'][:2]}", where=str(store.db_path())))
        results.append(evidence_files())
    return results


def evidence_files(limit: int = 2000) -> Result:
    from jarvis import capture, store
    rows = store.query("SELECT evidence_id, stored_path, sha256 FROM jv_evidence WHERE stored_path IS NOT NULL LIMIT ?",
                       (limit,))
    missing, changed = [], []
    for row in rows:
        path = Path(row["stored_path"])
        if not path.is_file():
            missing.append(row["evidence_id"])
        elif capture._sha256(path) != row["sha256"]:
            changed.append(row["evidence_id"])
    if not rows:
        return _r("evidence files", NOT_TESTED, "no file evidence captured yet")
    if missing or changed:
        return _r("evidence files", FAIL, f"{len(missing)} missing, {len(changed)} changed since capture",
                  evidence=f"missing {missing[:5]} changed {changed[:5]}")
    return _r("evidence files", PASS, f"{len(rows)} stored copies present and matching their SHA-256")


# ------------------------------------------------------------------ models --

def model_routes() -> List[Result]:
    from jarvis import models
    report = models.routing_report(fresh=True)
    out = []
    for route in report["routes"]:
        status = PASS if route["usable"] else (WARN if route.get("fallback") else UNAVAILABLE)
        out.append(_r(f"route {route['task']}", status, f"{route['model'] or '-'} [{route['state']}] {route['reason']}"[:240],
                      fallback=route.get("fallback", "")))
    return out


def live_models() -> List[Result]:
    """Real calls, validated - in a temporary database."""
    from jarvis import extraction, models, store, vernacular
    results: List[Result] = []
    with store.using_database(Path(tempfile.mkdtemp(prefix="jarvis_live_")) / "live.db"):
        route = models.route("vernacular", fresh=True)
        if not route["usable"]:
            results.append(_r("live vernacular translation", UNAVAILABLE, route["reason"], fallback=route.get("fallback", "")))
        else:
            started = time.time()
            reading = vernacular.model_translate(vernacular.normalize(
                "पियर P17: सीमेंट की आपूर्ति नहीं हुई, ढलाई रुकी हुई है। सप्लाई 11 सितंबर तक अपेक्षित।"))
            ok = bool(reading.translation) and "17" in reading.translation and "11" in reading.translation and \
                re.search(r"cement", reading.translation, re.I)
            results.append(_r("live vernacular translation", PASS if ok else FAIL,
                              f"{route['model']} in {time.time() - started:.1f}s: {reading.translation[:160]!r}"
                              if reading.translation else "no translation returned (or it changed a number)"))
        route = models.route("extraction", fresh=True)
        if not route["usable"]:
            results.append(_r("live structured extraction", UNAVAILABLE, route["reason"]))
        else:
            import datetime as dt
            started = time.time()
            text = "Pier P17 dhalai shuru ho gayi aaj, 40% ho gaya"
            out = extraction.extract_model(text, vernacular.normalize(text).normalized, dt.date(2026, 9, 12),
                                           [{"code": "BR-P17-CON", "name": "Pier concreting - Pier P17"},
                                            {"code": "BR-P17-EXC", "name": "Foundation excavation - Pier P17"}])
            obs = out.get("observations") or []
            ok = out.get("status") == "ok" and any(o.get("percent_complete") == 40 for o in obs) and \
                any(o.get("activity_code") == "BR-P17-CON" for o in obs)
            results.append(_r("live structured extraction", PASS if ok else FAIL,
                              f"{out.get('model')} in {time.time() - started:.1f}s: {obs[:2]}"[:300] if obs else
                              f"{out.get('status')}: {out.get('reason', '')}"[:300],
                              evidence=str(out.get("rejected"))[:600]))
        route = models.route("causal_explanation", fresh=True)
        if not route["usable"]:
            results.append(_r("live causal phrasing", UNAVAILABLE, route["reason"], fallback=route.get("fallback", "")))
        else:
            from jarvis import explain
            facts = [{"kind": "FACT", "text": "Forecast project finish moved from 27 Sep 2026 to 01 Oct 2026 (+4 days)."},
                     {"kind": "FACT", "text": "Triggering update: blocker reported - cement supply not received; approved."},
                     {"kind": "FACT", "text": "BR-P17-CON finish 09 Sep 2026 -> 13 Sep 2026 (+4 d)."},
                     {"kind": "INFERENCE", "text": "BR-P17-CON was on the critical path."}]
            template = "The forecast finish moved +4 days, from 27 Sep 2026 to 01 Oct 2026."
            started = time.time()
            text, model = explain._model_narrative(template, facts)
            results.append(_r("live causal phrasing", PASS if text else WARN,
                              f"{model} in {time.time() - started:.1f}s: {text[:160]!r}" if text else
                              "the model's sentence named a date that is not in the facts (or failed) - the "
                              "deterministic explanation is used", fallback="deterministic template"))
        route = models.route("retrieval", fresh=True)
        if route["usable"]:
            from core.capabilities import embedding_provider
            started = time.time()
            vector = embedding_provider.embed("pier concreting delayed by cement shortage", timeout=60)
            results.append(_r("live embeddings", PASS if vector else FAIL,
                              f"{route['model']} returned {len(vector)} dimensions in {time.time() - started:.1f}s"
                              if vector else "no vector returned"))
        else:
            results.append(_r("live embeddings", WARN, route["reason"], fallback="full-text search"))
        results.append(_live_ocr())
        results.append(_live_vision())
    return results


def _live_ocr() -> Result:
    from jarvis import capture, demo
    folder = Path(tempfile.mkdtemp(prefix="jarvis_ocr_"))
    photo = folder / "sign.jpg"
    demo._photo(photo)
    started = time.time()
    info = capture._image_text(photo)
    ok = "P17" in (info.get("text") or "").upper() and "EXCAVATION" in (info.get("text") or "").upper()
    return _r("live OCR on a site signboard", PASS if ok else FAIL,
              f"{info.get('engine')} in {time.time() - started:.1f}s read {info.get('text', '')[:80]!r}")


def _live_vision() -> Result:
    from jarvis import capture, demo, models
    route = models.route("image_evidence", fresh=True)
    if not route["usable"]:
        return _r("live site-photo description", UNAVAILABLE, f"{route['model']}: {route['reason']}"[:240],
                  fallback="OCR text and the operator's caption")
    photo = Path(tempfile.mkdtemp(prefix="jarvis_vis_")) / "site.jpg"
    demo._photo(photo)
    started = time.time()
    out = capture._vision_description(photo)
    return _r("live site-photo description", PASS if out.get("text") else FAIL,
              f"{out.get('model')} in {time.time() - started:.1f}s: {out.get('text', out.get('reason', ''))[:140]!r}")


# ----------------------------------------------------------------- offline --

def offline_checks() -> List[Result]:
    results: List[Result] = []
    try:
        from security.egress import egress_guard
        from core.ollama_client import ollama_client
        import configparser
        cfg = configparser.ConfigParser()
        cfg.read(ROOT / "config.ini")
        mode = cfg.get("security", "network_mode", fallback="sovereign")
        results.append(_r("network policy", PASS if mode in ("sovereign", "lockdown") else WARN,
                          f"config.ini network_mode = {mode} (loopback + LAN only when sovereign)"))
        host = ollama_client.host
        loop = bool(re.match(r"https?://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?", host))
        results.append(_r("model runtime on loopback", PASS if loop else FAIL, f"Ollama at {host}"))
    except Exception as error:
        results.append(_r("network policy", FAIL, str(error)))
    banned = re.compile(r"^\s*(?:import|from)\s+(openai|anthropic|google\.generativeai|boto3|azure|cohere|mistralai)\b"
                        r"|https?://(?!127\.0\.0\.1|localhost)", re.M)
    offenders = []
    for path in sorted((ROOT / "jarvis").glob("*.py")) + sorted((ROOT / "ui" / "medha" / "js" / "screens").glob("jarvis*.js")):
        if banned.search(path.read_text(encoding="utf-8")):
            offenders.append(path.name)
    results.append(_r("no cloud dependency in ZEUS code", PASS if not offenders else FAIL,
                      "no cloud SDK import and no external URL" if not offenders else f"found in {offenders}"))
    results += offline_capture_roundtrip()
    return results


def offline_capture_roundtrip() -> List[Result]:
    """Capture on a device with no hub, 'restart', find it again; then two devices
    that edited offline -> the conflict is detected and both are kept."""
    from jarvis import events, service, store, sync
    folder = Path(tempfile.mkdtemp(prefix="jarvis_offline_"))
    schedule = folder / "s.csv"
    schedule.write_text("# data_date: 2026-09-01\ncode,name,type,duration,start,finish,predecessors\n"
                        "P17-CON,Pier concreting - Pier P17,task,4,2026-09-01,2026-09-04,\n", encoding="utf-8")
    results = []
    with store.using_database(folder / "office.db"):
        project = service.create_project("OFFLINE-CHECK", "Offline check (temporary)", origin="test_fixture")
        service.import_schedule(project["project_id"], str(schedule), kind="update")
        seed = sync.export_bundle(str(folder / "seed"), all_events=True)["bundle"]
    ids = {}
    for device, text, when in (("a", "Pier P17 concreting 60% complete", "2026-09-03T10:31:00"),
                               ("b", "Pier P17 concreting 55% complete", "2026-09-03T10:42:00")):
        with store.using_database(folder / f"{device}.db"):
            sync.import_bundle(seed)
            captured = service.capture_update(project["project_id"], text=text, captured_at=when)
            ids[device] = captured["events"][0]["event_id"] if captured["events"] else None
    store.reset_cache()
    with store.using_database(folder / "a.db"):
        with store.connection() as conn:
            again = events.get(conn, ids["a"]) if ids["a"] else None
        status = sync.status()
        ok = bool(again) and again["sync_state"] == "local" and status["connectivity"] == "OFFLINE"
        results.append(_r("offline capture survives a restart", PASS if ok else FAIL,
                          f"event {str(ids['a'])[:8]} still stored, {status['pending_sync']} queued, connectivity "
                          f"{status['connectivity']} ({status['connectivity_detail']})"))
        bundle_a = sync.export_bundle(str(folder / "usb"))["bundle"]
    with store.using_database(folder / "b.db"):
        imported = sync.import_bundle(bundle_a)
        with store.connection() as conn:
            a, b = events.get(conn, ids["a"]), events.get(conn, ids["b"])
    ok = len(imported["conflicts"]) == 1 and a and b and a["status"] == b["status"] == "conflicted"
    results.append(_r("conflicting offline submissions are kept, not overwritten", PASS if ok else FAIL,
                      f"{len(imported['conflicts'])} conflict detected; both versions stored as "
                      f"{a['status'] if a else '-'} / {b['status'] if b else '-'}"))
    return results


# ------------------------------------------------------------------ health --

def health(run_suite: bool = True) -> List[Result]:
    from agni_ops import suites
    results = db_checks() + model_routes() + offline_capture_roundtrip()
    if run_suite:
        results += suites.regression(timeout=1800, paths=["tests/jarvis"], component="ZEUS suite (pytest)")
    return results


def reindex(embed: bool = False) -> List[Result]:
    from jarvis import search, store
    if not store.db_path().exists():
        return [_r("ZEUS project index", NOT_TESTED, "no ZEUS project database yet")]
    started = time.time()
    counts = search.reindex(embed=embed)
    return [_r("ZEUS project index", PASS, f"{counts['evidence']} evidence, {counts['correspondence']} letters, "
                                                   f"{counts['activities']} activities, {counts['forecast_changes']} "
                                                   f"forecast changes, {counts['vectors']} vectors in "
                                                   f"{time.time() - started:.1f}s")]
