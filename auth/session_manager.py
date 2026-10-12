"""
Session state for ZEUS: who is logged in, whether the screen is locked,
and which user's memory is active.

One process, one active session - this is a desktop application, not a
server. The session row is persisted so the audit trail survives a crash,
but authority lives in this object: no session token is ever accepted from
outside the process.

Crucially, this is also the ONLY place that tells the memory layer which
user is active (`database.set_active_user`). That is what makes user A's
conversation history, facts, notes, to-dos and calendar invisible to user B
without any second memory system existing.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from core.logger import get_logger
from memory import database
import auth.password_manager as pm
from auth.auth_manager import AuthManager, AuthResult, auth_manager

logger = get_logger("session")


@dataclass
class Session:
    session_id: str
    user_id: int
    username: str
    display_name: str
    is_admin: bool
    started_at: float
    locked: bool = False
    last_activity: float = field(default_factory=time.time)
    role: str = "operator"
    # Password accepted, second factor (face) not yet passed. Such a session
    # is NOT authenticated: every protected action is refused until it clears.
    factor_pending: bool = False
    # Why the session was last locked ("idle timeout", "user request",
    # "lockdown" ...) - lets the unlock screen say SESSION EXPIRED only when
    # the backend actually expired it.
    lock_reason: str = ""

    def to_dict(self) -> dict:
        return {"session_id": self.session_id, "user_id": self.user_id,
                "username": self.username, "display_name": self.display_name,
                "is_admin": self.is_admin, "role": self.role,
                "started_at": self.started_at, "factor_pending": self.factor_pending,
                "locked": self.locked, "last_activity": self.last_activity,
                "lock_reason": self.lock_reason}


class SessionManager:
    """Holds the authenticated session for this process."""

    def __init__(self, manager: Optional[AuthManager] = None):
        self.auth = manager or auth_manager
        self._session: Optional[Session] = None
        self._listeners: List[Callable[[str, Optional[Session]], None]] = []
        self._pending_kind = ""              # "login" | "unlock" while a face check is due
        self._factor_failures = 0

    # ------------------------------------------------------------- events --

    def on_change(self, callback: Callable[[str, Optional[Session]], None]):
        """Register a callback fired on 'login' | 'logout' | 'lock' | 'unlock'."""
        self._listeners.append(callback)

    def _emit(self, event: str):
        for callback in list(self._listeners):
            try:
                callback(event, self._session)
            except Exception as e:      # a bad listener must not break auth
                logger.warning(f"Session listener failed on '{event}': {e}")

    # -------------------------------------------------------------- state --

    @property
    def session(self) -> Optional[Session]:
        return self._session

    @property
    def is_authenticated(self) -> bool:
        """True only when a session exists AND is not locked AND has not
        idled out. Everything protected in ZEUS gates on this."""
        if self._session is None or self._session.locked or self._session.factor_pending:
            return False
        idle_limit = self.auth.session_idle_seconds
        if idle_limit and (time.time() - self._session.last_activity) > idle_limit:
            logger.info("Session idle timeout - locking")
            self.lock(reason="idle timeout")
            return False
        return True

    @property
    def current_user(self) -> Optional[dict]:
        if self._session is None:
            return None
        return {"id": self._session.user_id, "username": self._session.username,
                "display_name": self._session.display_name,
                "is_admin": self._session.is_admin, "role": self._session.role}

    @property
    def username(self) -> str:
        return self._session.username if self._session else ""

    def touch(self):
        if self._session is not None:
            self._session.last_activity = time.time()
            database.touch_session(self._session.session_id)

    # -------------------------------------------------------------- login --

    def login(self, username: str, password: str, client: str = "desktop") -> AuthResult:
        """Authenticate and start a session. Returns the AuthResult either way."""
        result = self.auth.authenticate(username, password)
        if not result.ok or not result.user:
            return result

        user = result.user
        session_id = pm.generate_token(24)
        database.create_session(session_id, user["id"], client)
        self._session = Session(
            session_id=session_id, user_id=user["id"], username=user["username"],
            display_name=user["display_name"] or user["username"],
            is_admin=bool(user["is_admin"]), started_at=time.time(),
            role=database.user_role(user))

        if self._face_required(user["id"]):
            self._session.factor_pending = True
            self._pending_kind, self._factor_failures = "login", 0
            database.log_auth_event("second_factor_required", user["username"], "face")
            logger.info(f"Password accepted for '{user['username']}' - face check pending")
            result.pending_factor = "face"
            return result

        self._reset_face_sign_in()              # a password proves who is at the keyboard
        self._finish_login()
        return result

    # ------------------------------------------------- sign in with a face --
    #
    # [face] login = face_or_password: the face ALONE is a credential. Three
    # failed face attempts in a row switch face sign-in off until a password
    # sign-in succeeds; the count is kept on disk so restarting ZEUS does not
    # reset it.

    FACE_ATTEMPTS = 3

    @staticmethod
    def _face_state_path():
        from core.paths import data_dir
        return data_dir() / "face_sign_in.json"

    def _face_state(self) -> dict:
        import json
        try:
            return json.loads(self._face_state_path().read_text(encoding="utf-8"))
        except Exception:
            return {"failures": 0}

    def _save_face_state(self, state: dict) -> None:
        import json
        try:
            path = self._face_state_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(state), encoding="utf-8")
        except Exception as error:
            logger.warning(f"face sign-in state not saved: {error}")

    def _reset_face_sign_in(self) -> None:
        if self._face_state().get("failures"):
            self._save_face_state({"failures": 0})

    def face_sign_in_status(self) -> dict:
        """Should the access screen offer the face, and if not, why not."""
        try:
            from security import face_auth
            status = dict(face_auth.face_sign_in_available())
        except Exception as error:
            return {"available": False, "reason": f"face sign-in unavailable: {error}"}
        failures = int(self._face_state().get("failures", 0))
        status["attempts_left"] = max(0, self.FACE_ATTEMPTS - failures)
        if status.get("available") and failures >= self.FACE_ATTEMPTS:
            status.update(available=False, blocked=True,
                          reason="face sign-in is paused after 3 failed attempts - "
                                 "sign in with your password once to turn it back on")
        return status

    # What the operator is told for each refusal - plain, and never a score.
    _FACE_WORDS = {
        "face not recognised": "Face not recognised.",
        "liveness check failed": "The live-face check failed.",
        "more than one face": "More than one face in view - sign in alone.",
        "face not clear enough": "Face not clear enough - face the camera in good light.",
        "hold still and look at the camera": "Hold still and look straight at the camera.",
        "not the locked user": "This is not the face of the locked session's user.",
    }

    def _face_failed(self, reason: str, username: str = "unknown") -> AuthResult:
        failures = int(self._face_state().get("failures", 0)) + 1
        self._save_face_state({"failures": failures})
        left = max(0, self.FACE_ATTEMPTS - failures)
        database.log_auth_event("face_login_failed", username, f"{reason}; {left} left")
        said = next((words for key, words in self._FACE_WORDS.items() if key in reason),
                    "Face not verified.")
        if left == 0:
            note = f"{said} Face sign-in is paused - use your password."
        else:
            note = f"{said} {left} attempt(s) left."
        return AuthResult(False, reason="face_rejected", attempts_left=left,
                          method="face", note=note)

    @staticmethod
    def _nobody_there() -> AuthResult:
        return AuthResult(False, reason="face_absent", method="face",
                          note="No face in view - look at the camera and try again.")

    @staticmethod
    def _same_face_twice() -> AuthResult:
        """One person enrolled on two accounts: face sign-in cannot choose, and
        it must not guess. A set-up problem, so it does not use up an attempt."""
        database.log_auth_event("face_login_failed", "unknown", "face enrolled on two accounts")
        return AuthResult(False, reason="face_ambiguous", method="face",
                          note="This face is enrolled on more than one account, so face "
                               "sign-in cannot tell which - use your password, then remove "
                               "the face from all but one account (Security > Face login).")

    def login_with_face(self, frames: list, client: str = "desktop") -> AuthResult:
        """Start a session from a face alone ([face] login = face_or_password)."""
        status = self.face_sign_in_status()
        if not status.get("available"):
            return AuthResult(False, reason="face_unavailable", method="face",
                              note=status.get("reason", "Face sign-in is not available."))
        from security import face_auth
        try:
            outcome = face_auth.identify(frames)
        except Exception as error:
            logger.warning(f"face sign-in failed: {error}")
            return self._face_failed(str(error))
        if outcome.get("no_face"):
            return self._nobody_there()
        if outcome.get("same_face_on_two_accounts"):
            return self._same_face_twice()
        if not outcome.get("ok"):
            return self._face_failed(outcome.get("reason") or "no match")

        user = database.get_user_by_id(outcome["user_id"])
        if not user:
            return self._face_failed("the matched account no longer exists")
        # The account's own state still rules: a disabled or locked account
        # is not opened by a face any more than by a password.
        if not user["is_active"]:
            database.log_auth_event("login_failed", user["username"], "inactive account (face)")
            return AuthResult(False, reason="inactive", method="face")
        if user["locked_until"] and user["locked_until"] > time.time():
            database.log_auth_event("login_blocked", user["username"], "locked (face)")
            return AuthResult(False, reason="locked", locked_until=user["locked_until"],
                              method="face")

        database.record_login_success(user["id"])
        database.log_auth_event("login_success", user["username"],
                                f"method=face similarity={outcome.get('similarity')}")
        session_id = pm.generate_token(24)
        database.create_session(session_id, user["id"], client)
        self._session = Session(
            session_id=session_id, user_id=user["id"], username=user["username"],
            display_name=user["display_name"] or user["username"],
            is_admin=bool(user["is_admin"]), started_at=time.time(),
            role=database.user_role(user))
        self._reset_face_sign_in()
        self._finish_login()
        return AuthResult(True, user=database.get_user_by_id(user["id"]), method="face")

    def unlock_with_face(self, frames: list) -> AuthResult:
        """Lift a lock with the LOCKED user's face (1:1, never "whoever
        matches"): another enrolled person cannot take over the session."""
        if self._session is None:
            return AuthResult(False, reason="no_session", method="face")
        status = self.face_sign_in_status()
        if not status.get("available"):
            return AuthResult(False, reason="face_unavailable", method="face",
                              note=status.get("reason", "Face sign-in is not available."))
        from security import face_auth
        try:
            outcome = face_auth.identify(frames)
        except Exception as error:
            return self._face_failed(str(error), self._session.username)
        if outcome.get("no_face"):
            return self._nobody_there()
        if not outcome.get("ok") or outcome.get("user_id") != self._session.user_id:
            return self._face_failed(outcome.get("reason") or "not the locked user",
                                     self._session.username)
        database.log_auth_event("session_unlocked", self._session.username, "method=face")
        self._reset_face_sign_in()
        self._finish_unlock()
        return AuthResult(True, method="face")

    def _finish_login(self):
        user_id, username = self._session.user_id, self._session.username
        # From here on, every memory read/write belongs to this user.
        database.set_active_user(user_id)
        self._adopt_pre_login_data(user_id)
        logger.info(f"Session started for '{username}' "
                    f"(session {self._session.session_id[:8]}...)")
        self._restore_access()
        self._emit("login")

    @staticmethod
    def _face_required(user_id: int) -> bool:
        try:
            from security import face_auth
            return face_auth.login_required_for(user_id)
        except Exception as error:
            logger.error(f"face policy unavailable - requiring the face check: {error}")
            try:
                from security.face_auth import config as face_config
                cfg = face_config()
                return cfg["enabled"] and cfg["login"] == "second_factor"
            except Exception:
                return False

    @property
    def pending_factor(self) -> str:
        return "face" if self._session is not None and self._session.factor_pending else ""

    def complete_face_factor(self, frames: list) -> dict:
        """Run the face check for a session whose password was accepted.
        Three failures end the attempt: the session is signed out (or, for an
        unlock, stays locked) and the password has to be entered again."""
        if self._session is None or not self._session.factor_pending:
            return {"ok": False, "reason": "no face check is pending"}
        from security import face_auth
        try:
            outcome = face_auth.verify(self._session.user_id, self._session.username,
                                       frames, purpose=self._pending_kind or "login")
        except Exception as error:
            outcome = {"ok": False, "reason": str(error)}
        if outcome.get("ok"):
            kind = self._pending_kind
            self._session.factor_pending = False
            self._pending_kind, self._factor_failures = "", 0
            if kind == "unlock":
                self._finish_unlock()
            else:
                self._finish_login()
            return outcome
        self._factor_failures += 1
        outcome["attempts_left"] = max(0, 3 - self._factor_failures)
        if self._factor_failures >= 3:
            self.cancel_pending_factor("three failed face checks")
            outcome["ended"] = True
        return outcome

    def cancel_pending_factor(self, reason: str = "cancelled") -> None:
        if self._session is None or not self._session.factor_pending:
            return
        kind = self._pending_kind
        database.log_auth_event("second_factor_failed", self._session.username, reason)
        self._pending_kind, self._factor_failures = "", 0
        if kind == "unlock":
            self._session.factor_pending = False      # stays locked; password again
            self._session.locked = True
        else:
            self._session.factor_pending = False
            self.logout()

    @staticmethod
    def _adopt_pre_login_data(user_id: int):
        """A database created before login existed has rows with no owner.
        The first account to log in adopts them, so upgrading ZEUS never
        looks like losing your history. With more than one account present
        this does nothing - ownership would be a guess."""
        if database.count_users() != 1:
            return
        orphans = database.orphan_row_counts()
        if not any(orphans.values()):
            return
        adopted = database.adopt_orphan_rows(user_id)
        moved = {k: v for k, v in adopted.items() if v}
        if moved:
            logger.info(f"Adopted pre-login data for the first account: {moved}")
            database.log_auth_event("data_adopted", "", str(moved))

    # ------------------------------------------------------- lock / logout --

    def lock(self, reason: str = "user request") -> bool:
        """Keep the session but require the password again. The application
        stays running; only access is withdrawn."""
        if self._session is None:
            return False
        self._session.locked = True
        self._session.lock_reason = reason
        database.set_session_locked(self._session.session_id, True)
        database.log_auth_event("session_locked", self._session.username, reason)
        logger.info(f"Session locked ({reason})")
        self._withdraw_access("lock")
        self._emit("lock")
        return True

    def unlock(self, password: str) -> AuthResult:
        """Re-authenticate the SAME user to lift a lock."""
        if self._session is None:
            return AuthResult(False, reason="no_session")
        result = self.auth.authenticate(self._session.username, password)
        if result.ok:
            if self._face_required(self._session.user_id):
                self._session.factor_pending = True        # still locked as well
                self._pending_kind, self._factor_failures = "unlock", 0
                database.log_auth_event("second_factor_required", self._session.username,
                                        "face (unlock)")
                result.pending_factor = "face"
                return result
            self._reset_face_sign_in()
            self._finish_unlock()
        return result

    def _finish_unlock(self):
        self._session.locked = False
        self._session.lock_reason = ""
        self._session.last_activity = time.time()
        database.set_session_locked(self._session.session_id, False)
        database.set_active_user(self._session.user_id)
        logger.info(f"Session unlocked for '{self._session.username}'")
        self._restore_access()
        self._emit("unlock")

    def logout(self) -> bool:
        """End the session and detach the user's memory. The application may
        keep running - the caller decides whether to show the login screen
        again or exit."""
        if self._session is None:
            return False
        username = self._session.username
        database.close_session(self._session.session_id)
        database.log_auth_event("logout", username)
        self._session = None
        database.set_active_user(None)
        logger.info(f"Session ended for '{username}'")
        self._withdraw_access("logout")
        self._emit("logout")
        return True

    @staticmethod
    def _withdraw_access(event: str):
        """LOCK and LOGOUT withdraw access to everything protected, not only
        the window: the vault key is wiped (it used to survive both, and the
        next operator inherited an unlocked vault), pending approvals are
        refused, and the assistant state shows LOCKED."""
        try:
            from core.vault import vault
            if vault.is_unlocked:
                vault.lock()
                database.log_vault_event("lock", detail=f"automatic on session {event}")
        except Exception as error:
            logger.warning(f"could not lock the vault on {event}: {error}")
        try:
            from security.approvals import approval_gateway
            approval_gateway.cancel_all(f"session {event}")
        except Exception:
            pass
        try:
            from core.assistant_state import State, assistant_state
            assistant_state.reset()
            assistant_state.push(State.LOCKED, "locked" if event == "lock" else "signed out")
        except Exception:
            pass

    @staticmethod
    def _restore_access():
        try:
            from core.assistant_state import State, assistant_state
            assistant_state.clear(State.LOCKED)
        except Exception:
            pass

    # --------------------------------------------------------------- info --

    def status(self) -> dict:
        return {
            "authenticated": self.is_authenticated,
            "locked": bool(self._session and self._session.locked),
            "pending_factor": self.pending_factor,
            "user": self.current_user,
            "started_at": self._session.started_at if self._session else None,
            "auth": self.auth.status(),
        }

    def require_authenticated(self):
        """Guard for protected entry points. Raises PermissionError rather
        than returning a value, so a caller cannot forget to check."""
        if not self.is_authenticated:
            raise PermissionError("ZEUS is locked - authentication required")


session_manager = SessionManager()
