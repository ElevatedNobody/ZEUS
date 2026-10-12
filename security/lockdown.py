"""
Emergency lockdown - one action that closes everything that can be closed.

    engage(reason)
        network  -> lockdown mode (loopback only; LAN peers cut off)
        vault    -> locked, key wiped
        approvals-> every pending approval refused
        tasks    -> running multi-step tasks cancelled
        models   -> ZEUS's resident models released from memory
        policy   -> only status and recovery tools may run
        session  -> the interface locks the operator session (it listens here)

    disengage(by)
        admin only; network returns to the configured mode.

The state is written to disk, so a restart during a lockdown comes back
locked down.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List

from core.logger import get_logger

logger = get_logger("security.lockdown")


def _state_path() -> Path:
    from core.paths import data_dir
    return data_dir() / "lockdown.json"


class Lockdown:
    def __init__(self):
        self._lock = threading.Lock()
        self._listeners: List[Callable[[Dict], None]] = []
        self.engaged = False
        self.reason = ""
        self.since = 0.0
        self.by = ""
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(_state_path().read_text(encoding="utf-8"))
            self.engaged = bool(data.get("engaged"))
            self.reason = data.get("reason", "")
            self.since = float(data.get("since", 0))
            self.by = data.get("by", "")
        except Exception:
            pass

    def _save(self) -> None:
        try:
            path = _state_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"engaged": self.engaged, "reason": self.reason,
                                        "since": self.since, "by": self.by}), encoding="utf-8")
        except Exception as error:
            logger.error(f"lockdown state not saved: {error}")

    def subscribe(self, callback: Callable[[Dict], None]) -> None:
        self._listeners.append(callback)

    def status(self) -> Dict:
        return {"engaged": self.engaged, "reason": self.reason, "since": self.since, "by": self.by}

    def engage(self, reason: str = "operator request", by: str = "") -> Dict:
        steps = {}
        with self._lock:
            self.engaged, self.reason, self.since, self.by = True, reason, time.time(), by
            self._save()
        try:
            from security.egress import egress_guard
            egress_guard.configure(mode="lockdown")
            steps["network"] = "loopback only"
        except Exception as error:
            steps["network"] = f"failed: {error}"
        try:
            from core.vault import vault
            vault.lock()
            steps["vault"] = "locked"
        except Exception as error:
            steps["vault"] = f"failed: {error}"
        try:
            from security.approvals import approval_gateway
            steps["approvals"] = f"{approval_gateway.cancel_all('lockdown')} refused"
        except Exception as error:
            steps["approvals"] = f"failed: {error}"
        try:
            from core.task_manager import task_manager
            cancelled = 0
            for task in list(getattr(task_manager, "_tasks", {}).values()):
                if task.status.value in ("QUEUED", "PLANNING", "RUNNING", "WAITING_FOR_USER"):
                    task._cancel_flag.set()
                    cancelled += 1
            steps["tasks"] = f"{cancelled} cancelled"
        except Exception as error:
            steps["tasks"] = f"failed: {error}"
        try:
            from core.orchestration.router import capability_router
            steps["models"] = f"{capability_router.release_all()} released"
        except Exception as error:
            steps["models"] = f"failed: {error}"
        try:
            from security import events
            events.record("lockdown_engaged", f"{reason} ({by or 'unknown'})", severity="critical")
        except Exception:
            pass
        for callback in list(self._listeners):
            try:
                callback(self.status())
            except Exception:
                pass
        logger.critical(f"EMERGENCY LOCKDOWN engaged: {reason}")
        return {"engaged": True, "steps": steps}

    def disengage(self, by: str = "") -> Dict:
        with self._lock:
            was = self.engaged
            self.engaged, self.reason, self.since, self.by = False, "", 0.0, by
            self._save()
        try:
            from security.egress import configure_from_config
            configure_from_config()
        except Exception:
            pass
        try:
            from security import events
            events.record("lockdown_released", f"released by {by or 'unknown'}", severity="notice")
        except Exception:
            pass
        for callback in list(self._listeners):
            try:
                callback(self.status())
            except Exception:
                pass
        return {"engaged": False, "was_engaged": was}

    def reload(self) -> None:
        """For tests / after the data directory changes."""
        self.engaged = False
        self._load()


lockdown = Lockdown()
