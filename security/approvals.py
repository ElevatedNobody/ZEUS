"""
The human approval gateway.

Before this existed, "ask the user first" was `input()` on a console - from a
worker thread, behind the desktop window. Launched from START.bat the tool
simply hung waiting for an answer nobody could see; launched without a console
it was refused silently. Neither is an approval.

Now a request that needs a human is turned into an `ApprovalRequest` and handed
to whichever interface is running:

  * the ZEUS interface shows it as a dialog and calls `resolve()`;
  * the native Command Center shows a message box;
  * the CLI asks on the terminal;
  * with nothing attached, the answer is NO.

The worker that asked waits (with a timeout - silence is a refusal). While it
waits the assistant state is WAITING_FOR_APPROVAL, so the interface shows the
truth. Every decision, including a timeout, is audited.
"""

from __future__ import annotations

import itertools
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from core.logger import get_logger

logger = get_logger("security.approvals")

DEFAULT_TIMEOUT = 180.0
_ids = itertools.count(1)


@dataclass
class ApprovalRequest:
    id: int
    tool: str
    description: str
    risk: str
    origin: str = "command"
    user: str = ""
    created_at: float = field(default_factory=time.time)
    timeout: float = DEFAULT_TIMEOUT
    decision: Optional[bool] = None
    decided_by: str = ""
    _event: threading.Event = field(default_factory=threading.Event, repr=False)

    def as_dict(self) -> Dict:
        # Not dataclasses.asdict(): that deep-copies the threading.Event.
        return {"id": self.id, "tool": self.tool, "description": self.description,
                "risk": self.risk, "origin": self.origin, "user": self.user,
                "created_at": self.created_at, "timeout": self.timeout,
                "decision": self.decision, "decided_by": self.decided_by}


class ApprovalGateway:
    def __init__(self):
        self._async_handler: Optional[Callable[[ApprovalRequest], None]] = None
        self._sync_handler: Optional[Callable[[ApprovalRequest], bool]] = None
        self._pending: Dict[int, ApprovalRequest] = {}
        self._lock = threading.Lock()
        self._history: List[Dict] = []

    # -- wiring -------------------------------------------------------------

    def attach(self, async_handler: Optional[Callable[[ApprovalRequest], None]] = None,
               sync_handler: Optional[Callable[[ApprovalRequest], bool]] = None) -> None:
        """`async_handler` is called from a worker thread and must show the
        request and later call `resolve()`; `sync_handler` is used when the
        request is raised ON the GUI thread (it may show a modal dialog)."""
        self._async_handler = async_handler
        self._sync_handler = sync_handler

    def detach(self) -> None:
        self._async_handler = None
        self._sync_handler = None
        self.cancel_all("interface closed")

    @property
    def attached(self) -> bool:
        return self._async_handler is not None or self._sync_handler is not None

    # -- requesting ---------------------------------------------------------

    def request(self, tool: str, description: str, risk: str, origin: str = "command",
                timeout: float = DEFAULT_TIMEOUT,
                fallback: Optional[Callable[[str], bool]] = None) -> bool:
        user = ""
        try:
            from auth.session_manager import session_manager
            user = session_manager.username or ""
        except Exception:
            pass
        request = ApprovalRequest(id=next(_ids), tool=tool, description=description,
                                  risk=risk, origin=origin, user=user, timeout=timeout)

        if not self.attached:
            approved = False
            if fallback is not None:
                try:
                    approved = bool(fallback(f"[{risk}] {description}"))
                except Exception:
                    approved = False
            self._finish(request, approved, "terminal" if fallback else "no approver attached")
            return approved

        on_gui_thread = threading.current_thread() is threading.main_thread()
        if on_gui_thread:
            if self._sync_handler is None:
                self._finish(request, False, "refused: raised on the interface thread")
                return False
            try:
                approved = bool(self._sync_handler(request))
            except Exception as error:
                logger.warning(f"approval dialog failed: {error}")
                approved = False
            self._finish(request, approved, request.decided_by or "operator")
            return approved

        with self._lock:
            self._pending[request.id] = request
        self._set_state(True)
        try:
            handler = self._async_handler
            if handler is None:
                self._finish(request, False, "no approver attached")
                return False
            handler(request)
            if not request._event.wait(timeout):
                self._finish(request, False, f"timed out after {int(timeout)}s")
                return False
            return bool(request.decision)
        finally:
            with self._lock:
                self._pending.pop(request.id, None)
            self._set_state(bool(self._pending))

    def resolve(self, request_id: int, approved: bool, by: str = "operator") -> bool:
        with self._lock:
            request = self._pending.get(int(request_id))
        if request is None or request.decision is not None:
            return False
        self._finish(request, bool(approved), by)
        request._event.set()
        return True

    def cancel_all(self, reason: str = "cancelled") -> int:
        with self._lock:
            pending = list(self._pending.values())
        for request in pending:
            if request.decision is None:
                self._finish(request, False, reason)
                request._event.set()
        return len(pending)

    def pending(self) -> List[Dict]:
        with self._lock:
            return [r.as_dict() for r in self._pending.values()]

    def history(self, limit: int = 30) -> List[Dict]:
        return list(self._history[-limit:])[::-1]

    # -- internals ----------------------------------------------------------

    def _finish(self, request: ApprovalRequest, approved: bool, by: str) -> None:
        request.decision = approved
        request.decided_by = by
        record = request.as_dict()
        self._history.append(record)
        del self._history[:-200]
        try:
            from security.audit_chain import audit_chain
            audit_chain.append("approval", f"{request.tool}: {request.description[:300]}",
                               actor=request.user, approved=approved, decided_by=by,
                               risk=request.risk, origin=request.origin)
        except Exception:
            pass
        if not approved:
            try:
                from security import events
                events.record("approval_refused", f"{request.tool} ({by})", severity="notice")
            except Exception:
                pass

    @staticmethod
    def _set_state(waiting: bool) -> None:
        try:
            from core.assistant_state import assistant_state, State
            # Exactly one WAITING entry however many requests are open.
            assistant_state.clear(State.WAITING_FOR_APPROVAL)
            if waiting:
                assistant_state.push(State.WAITING_FOR_APPROVAL, "waiting for your approval")
        except Exception:
            pass


approval_gateway = ApprovalGateway()
