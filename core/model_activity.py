"""
ZEUS Model Activity Tracker
Records which model is currently/recently handling which task, so the
dashboard can show real-time "Model: X | Task: Y" instead of a static
model name. Same thread-safe ring-buffer pattern as background_events.py.
"""

import threading
import time
from collections import deque
from typing import List, Optional

_lock = threading.Lock()
_activity: deque = deque(maxlen=50)
_current: Optional[dict] = None


def start_activity(model: str, task_type: str, purpose: str) -> dict:
    """Call when a model invocation begins. Returns the entry so
    finish_activity() can be called with it once the call completes."""
    global _current
    entry = {"model": model, "task_type": task_type, "purpose": purpose,
              "started_at": time.time(), "finished_at": None, "status": "running"}
    with _lock:
        _current = entry
        _activity.append(entry)
    return entry


def finish_activity(entry: dict, status: str = "done"):
    global _current
    with _lock:
        entry["finished_at"] = time.time()
        entry["status"] = status
        if _current is entry:
            _current = None


def get_current() -> Optional[dict]:
    with _lock:
        return dict(_current) if _current else None


def get_recent(limit: int = 10) -> List[dict]:
    with _lock:
        return [dict(e) for e in list(_activity)[-limit:]][::-1]
