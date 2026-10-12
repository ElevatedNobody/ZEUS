"""
The pipeline status board the UI reads.

The dashboard already shows which MODEL is busy (core.model_activity). This
adds the rest of the line the brief asks for - TASK, AGENT, MODEL, TOOLS,
STATUS - as a small thread-safe record, because the work runs on a background
QThread and Qt widgets may only be touched from the GUI thread.

Deliberately narrow: it holds task and status text only. No prompts, no model
reasoning, no chain-of-thought. What is shown is what a person needs to know
about progress, not what the model was thinking.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Dict, List, Optional

_lock = threading.Lock()
_history: deque = deque(maxlen=40)
_current: Optional[Dict] = None


def begin(task: str, agent: str = "", model: str = "", kind: str = "") -> Dict:
    """A run has started. Returns the record so later stages can update it."""
    global _current
    record = {
        "task": task[:160],
        "kind": kind,
        "agent": agent,
        "model": model,
        "tools": [],
        "status": "planning",
        # Per-stage state, so the interface can show the pipeline honestly:
        # a stage the run has not reached stays absent, and the strip draws
        # it as not-started rather than inventing progress for it.
        "stages": {},
        "stage_notes": {},
        "started_at": time.time(),
        "finished_at": None,
        "ok": None,
        "detail": "",
    }
    with _lock:
        _current = record
        _history.append(record)
    return record


def update(record: Optional[Dict], **fields) -> Optional[Dict]:
    if record is None:
        return None
    with _lock:
        record.update(fields)
    return record


def note_stage(record: Optional[Dict], stage: str, state: str, note: str = ""):
    """Record how one pipeline stage ended. `state` is one of
    ok / running / failed / pending."""
    if record is None or not stage:
        return
    with _lock:
        record["stages"][stage] = state
        if note:
            record["stage_notes"][stage] = note[:28]


def note_tool(record: Optional[Dict], tool: str):
    if record is None or not tool:
        return
    with _lock:
        if tool not in record["tools"]:
            record["tools"].append(tool)


def finish(record: Optional[Dict], ok: bool = True, detail: str = ""):
    global _current
    if record is None:
        return
    with _lock:
        record["finished_at"] = time.time()
        record["status"] = "done" if ok else "failed"
        record["ok"] = ok
        record["detail"] = detail[:200]
        if _current is record:
            _current = None


def current() -> Optional[Dict]:
    with _lock:
        return dict(_current) if _current else None


def recent(limit: int = 10) -> List[Dict]:
    with _lock:
        return [dict(entry) for entry in list(_history)[-limit:]][::-1]


def status_line() -> str:
    """One line for a status bar: 'Building the deck · Report Agent ·
    qwen2.5:3b · deck_builder · validating'."""
    record = current()
    if not record:
        return ""
    parts = [record["task"]]
    if record["agent"]:
        parts.append(record["agent"])
    if record["model"]:
        parts.append(record["model"])
    if record["tools"]:
        parts.append(" → ".join(record["tools"][-3:]))
    parts.append(record["status"])
    return "  ·  ".join(part for part in parts if part)
