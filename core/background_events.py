"""
ZEUS Background Events
A thread-safe queue that background tasks (folder watchers, reminders,
scheduled jobs) push notifications into. The dashboard polls this
periodically so ZEUS can surface things proactively - "a file showed
up", "your reminder is due" - without the user having to ask.
"""

import threading
import time
from collections import deque
from typing import List, Tuple

_lock = threading.Lock()
_events: deque = deque(maxlen=200)


def push_event(message: str):
    with _lock:
        _events.append((time.time(), message))


def pop_all_events() -> List[Tuple[float, str]]:
    """Drain and return all pending events."""
    with _lock:
        events = list(_events)
        _events.clear()
    return events
