"""
What ZEUS is doing right now - one answer, driven only by real events.

    IDLE  LISTENING  UNDERSTANDING  THINKING  RETRIEVING  EXECUTING
    WAITING_FOR_APPROVAL  SPEAKING  ERROR  LOCKED

Several things can be true at once (speaking the last answer while listening
for the next; retrieving evidence inside a run that is executing), so each
subsystem PUSHES the activity it has started and POPS it when it stops, and the
state shown is the highest-priority activity that is actually live. Nothing
here is set on a timer to "look busy": the only timed state is ERROR, which is
shown for a few seconds after a real failure and then clears itself.

Who sets what
    voice capture ............ LISTENING
    orchestrator classify .... UNDERSTANDING
    orchestrator plan/route .. THINKING   (and while the model generates)
    knowledge / RAG tools .... RETRIEVING
    every other tool step .... EXECUTING
    approval gateway ......... WAITING_FOR_APPROVAL
    speech output ............ SPEAKING
    session lock ............. LOCKED
    a failed run ............. ERROR (transient)
"""

from __future__ import annotations

import threading
import time
from enum import Enum
from typing import Callable, Dict, List, Optional


class State(str, Enum):
    IDLE = "IDLE"
    LISTENING = "LISTENING"
    UNDERSTANDING = "UNDERSTANDING"
    THINKING = "THINKING"
    RETRIEVING = "RETRIEVING"
    EXECUTING = "EXECUTING"
    WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"
    SPEAKING = "SPEAKING"
    ERROR = "ERROR"
    LOCKED = "LOCKED"


PRIORITY = {
    State.LOCKED: 100,
    State.WAITING_FOR_APPROVAL: 90,
    State.ERROR: 80,
    State.LISTENING: 70,
    State.EXECUTING: 60,
    State.RETRIEVING: 55,
    State.THINKING: 50,
    State.UNDERSTANDING: 45,
    State.SPEAKING: 40,
    State.IDLE: 0,
}

ERROR_HOLD_SECONDS = 4.0


class AssistantState:
    def __init__(self):
        self._lock = threading.RLock()
        self._active: Dict[State, List[str]] = {}
        self._error_until = 0.0
        self._error_detail = ""
        self._listeners: List[Callable[[Dict], None]] = []
        self._last: Optional[Dict] = None
        self._since = time.time()

    # -- mutation -----------------------------------------------------------

    def push(self, state: State, detail: str = "") -> None:
        with self._lock:
            self._active.setdefault(state, []).append(detail)
        self._publish()

    def pop(self, state: State) -> None:
        with self._lock:
            stack = self._active.get(state)
            if stack:
                stack.pop()
                if not stack:
                    self._active.pop(state, None)
        self._publish()

    def clear(self, state: State) -> None:
        with self._lock:
            self._active.pop(state, None)
        self._publish()

    def error(self, detail: str) -> None:
        with self._lock:
            self._error_until = time.time() + ERROR_HOLD_SECONDS
            self._error_detail = detail
        self._publish()
        timer = threading.Timer(ERROR_HOLD_SECONDS + 0.05, self._publish)
        timer.daemon = True
        timer.start()

    def reset(self) -> None:
        with self._lock:
            self._active.clear()
            self._error_until = 0.0
        self._publish()

    class _Scope:
        def __init__(self, owner, state, detail):
            self.owner, self.state, self.detail = owner, state, detail

        def __enter__(self):
            self.owner.push(self.state, self.detail)
            return self

        def __exit__(self, *exc):
            self.owner.pop(self.state)
            return False

    def activity(self, state: State, detail: str = ""):
        """`with assistant_state.activity(State.EXECUTING, "write_document"): ...`"""
        return self._Scope(self, state, detail)

    # -- reading ------------------------------------------------------------

    def snapshot(self) -> Dict:
        with self._lock:
            live = [s for s, stack in self._active.items() if stack]
            if time.time() < self._error_until:
                live.append(State.ERROR)
            if not live:
                state, detail = State.IDLE, ""
            else:
                state = max(live, key=lambda s: PRIORITY[s])
                detail = (self._error_detail if state is State.ERROR
                          else (self._active.get(state) or [""])[-1])
            return {"state": state.value, "detail": detail or "",
                    "active": sorted(s.value for s in live),
                    "since": self._since}

    @property
    def current(self) -> State:
        return State(self.snapshot()["state"])

    def subscribe(self, callback: Callable[[Dict], None]) -> None:
        with self._lock:
            if callback not in self._listeners:
                self._listeners.append(callback)

    def unsubscribe(self, callback: Callable[[Dict], None]) -> None:
        with self._lock:
            if callback in self._listeners:
                self._listeners.remove(callback)

    def _publish(self) -> None:
        snap = self.snapshot()
        with self._lock:
            if self._last and self._last["state"] == snap["state"] and \
                    self._last["detail"] == snap["detail"] and self._last["active"] == snap["active"]:
                return
            if not self._last or self._last["state"] != snap["state"]:
                self._since = time.time()
                snap["since"] = self._since
            self._last = snap
            listeners = list(self._listeners)
        for callback in listeners:
            try:
                callback(snap)
            except Exception:
                pass


assistant_state = AssistantState()
