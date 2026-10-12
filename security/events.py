"""
Security events: persisted, counted, and pushed to whoever is listening.

An event is something a security control actually did or saw - a tool call it
refused, a connection it blocked, an instruction it found inside a document, a
model whose digest did not match. Each one is written to the `security_events`
table and to the tamper-evident audit chain, and live listeners (the Security
Centre, the alert strip in the interface) are told immediately.

Detection rules that need history (five failed logins in a minute, a burst of
blocked connections) run here too, so an alert is raised by code watching
events rather than by anybody remembering to look.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Callable, Dict, List, Optional

from core.logger import get_logger

logger = get_logger("security.events")

SEVERITIES = ("info", "notice", "warning", "critical")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS security_events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL NOT NULL,
    kind      TEXT NOT NULL,
    severity  TEXT NOT NULL,
    detail    TEXT,
    username  TEXT
);
CREATE INDEX IF NOT EXISTS idx_security_events_ts ON security_events(ts DESC);
"""

_listeners: List[Callable[[Dict], None]] = []
_lock = threading.Lock()
_recent: deque = deque(maxlen=400)
_counts: Dict[str, int] = {}
_schema_ready_for: Optional[str] = None


def _connection():
    from memory import database
    global _schema_ready_for
    conn = database.get_connection()
    marker = str(database.DB_PATH)
    if _schema_ready_for != marker:
        conn.executescript(_SCHEMA)
        conn.commit()
        _schema_ready_for = marker
    return conn


def _current_user() -> str:
    try:
        from auth.session_manager import session_manager
        return session_manager.username or ""
    except Exception:
        return ""


def subscribe(callback: Callable[[Dict], None]) -> None:
    with _lock:
        if callback not in _listeners:
            _listeners.append(callback)


def unsubscribe(callback: Callable[[Dict], None]) -> None:
    with _lock:
        if callback in _listeners:
            _listeners.remove(callback)


def record(kind: str, detail: str = "", severity: str = "warning",
           username: Optional[str] = None, persist: bool = True) -> Dict:
    """Record one event. Never raises: a failure to record must not turn a
    refused action into a crash."""
    severity = severity if severity in SEVERITIES else "warning"
    event = {"ts": time.time(), "kind": kind, "severity": severity,
             "detail": (detail or "")[:1000],
             "username": username if username is not None else _current_user()}
    with _lock:
        _recent.appendleft(event)
        _counts[kind] = _counts.get(kind, 0) + 1
        listeners = list(_listeners)
    if persist:
        try:
            conn = _connection()
            try:
                conn.execute("INSERT INTO security_events (ts, kind, severity, detail, username) "
                             "VALUES (?, ?, ?, ?, ?)",
                             (event["ts"], kind, severity, event["detail"], event["username"]))
                conn.commit()
            finally:
                conn.close()
        except Exception as error:
            logger.debug(f"security event not persisted: {error}")
        try:
            from security.audit_chain import audit_chain
            audit_chain.append("security_event", f"{kind}: {event['detail']}",
                               actor=event["username"], severity=severity)
        except Exception as error:
            logger.debug(f"security event not chained: {error}")
    for callback in listeners:
        try:
            callback(event)
        except Exception as error:
            logger.debug(f"security listener failed: {error}")
    _detect(event)
    return event


def recent(limit: int = 50, persisted: bool = True) -> List[Dict]:
    if persisted:
        try:
            conn = _connection()
            try:
                rows = conn.execute("SELECT ts, kind, severity, detail, username FROM "
                                    "security_events ORDER BY id DESC LIMIT ?",
                                    (int(limit),)).fetchall()
                return [dict(row) for row in rows]
            finally:
                conn.close()
        except Exception:
            pass
    with _lock:
        return list(_recent)[:limit]


def counts() -> Dict[str, int]:
    with _lock:
        return dict(_counts)


def totals_since(seconds: float) -> Dict[str, int]:
    """Counts by kind over the last `seconds`, from the persisted table."""
    cutoff = time.time() - seconds
    try:
        conn = _connection()
        try:
            rows = conn.execute("SELECT kind, COUNT(*) AS n FROM security_events WHERE ts >= ? "
                                "GROUP BY kind", (cutoff,)).fetchall()
            return {row["kind"]: row["n"] for row in rows}
        finally:
            conn.close()
    except Exception:
        return {}


# ------------------------------------------------------------- detection --

_WINDOWS = {
    # kind -> (threshold, seconds, alert kind)
    "tool_denied": (5, 60, "alert_repeated_denials"),
    "egress_blocked": (10, 60, "alert_egress_burst"),
    "login_failed": (5, 120, "alert_login_attack"),
    "injection_flagged": (3, 300, "alert_injection_campaign"),
}
_window_hits: Dict[str, deque] = {}
_last_alert: Dict[str, float] = {}


def _detect(event: Dict) -> None:
    rule = _WINDOWS.get(event["kind"])
    if not rule:
        return
    threshold, seconds, alert = rule
    now = event["ts"]
    hits = _window_hits.setdefault(event["kind"], deque(maxlen=threshold * 4))
    hits.append(now)
    recent_hits = [t for t in hits if now - t <= seconds]
    if len(recent_hits) >= threshold and now - _last_alert.get(alert, 0) > seconds:
        _last_alert[alert] = now
        record(alert, f"{len(recent_hits)} '{event['kind']}' events in {seconds}s",
               severity="critical")
