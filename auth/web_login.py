"""
The sign-in controller behind the ZEUS interface's login screen.

The page renders; this decides. Every method returns what the security core
(auth_manager / session_manager / face_auth) actually concluded - the page
cannot mark itself signed in, and nothing here adds a path around the core:

  * password sign-in, first-run setup and unlock call the same
    session_manager / auth_manager methods the terminal login uses (Argon2id,
    lockout, generic wording, audit);
  * face sign-in is offered only when [face] login = face_or_password and the
    engine is ready; face as a second factor runs after the password; the
    matched account's own state still applies;
  * one attempt at a time: a second attempt while one is running is refused;
  * the camera is opened here, never by the page. The page receives small
    JPEG previews to show; recognition uses the full frames, which stay in
    this process and are never written anywhere.

Face progress is reported as the states of docs/AUTH_ARCHITECTURE.md, each
emitted only when the fact it names is true:
    CAMERA READY -> SCANNING -> VERIFYING IDENTITY -> FACE DETECTED ->
    LIVENESS CHECK -> IDENTITY VERIFIED -> ACCESS GRANTED | ACCESS DENIED,
    CAMERA ERROR, AUTHENTICATION TIMEOUT.
FACE DETECTED and LIVENESS CHECK are reported from the recognition result
(the detector and the liveness model run inside it), so they follow
VERIFYING IDENTITY rather than pretending to be separate live steps.
"""

from __future__ import annotations

import base64
import sys
import threading
import time
from typing import Any, Callable, Dict, Optional

from core.logger import get_logger

logger = get_logger("auth.web_login")

Emit = Callable[[str, Dict[str, Any]], None]

FACE_FRAMES = 5
FACE_INTERVAL = 0.22
FACE_WARMUP = 0.8
FACE_TIMEOUT = 10.0
PREVIEW_WIDTH = 220
PREVIEW_EVERY = 0.08


def _lockdown() -> Dict[str, Any]:
    try:
        from security.lockdown import lockdown
        return {"engaged": bool(lockdown.engaged), **(lockdown.status() if hasattr(lockdown, "status") else {})}
    except Exception as error:
        return {"engaged": False, "detail": f"lockdown state unknown: {error}"}


class AuthController:
    def __init__(self, session_manager=None, auth_manager=None):
        if session_manager is None:
            from auth.session_manager import session_manager as default_session
            session_manager = default_session
        if auth_manager is None:
            from auth.auth_manager import auth_manager as default_auth
            auth_manager = default_auth
        self.sessions = session_manager
        self.accounts = auth_manager
        self._busy = threading.Lock()
        self._face_thread: Optional[threading.Thread] = None
        self._face_stop = threading.Event()

    # ------------------------------------------------------------- state ----

    def state(self) -> Dict[str, Any]:
        """Everything the login screen needs to draw itself - all measured."""
        session = self.sessions.session
        lockdown = _lockdown()
        login_off = session is None and not getattr(self.accounts, "require_login", True)
        if self.sessions.is_authenticated or login_off:
            phase = "signed_in"
        elif session is not None and self.sessions.pending_factor:
            phase = "second_factor"
        elif session is not None and session.locked:
            phase = "unlock"
        elif self.accounts.first_run_required():
            phase = "setup"
        else:
            phase = "login"
        try:
            face = dict(self.sessions.face_sign_in_status())
        except Exception as error:
            face = {"available": False, "reason": f"face sign-in unavailable: {error}"}
        try:
            from security.face_auth import config as face_config
            face["mode"] = face_config().get("login", "off")
        except Exception:
            face["mode"] = "unknown"
        try:
            from auth import password_manager as pm
            min_length = self.accounts._int("min_password_length", pm.MIN_PASSWORD_LENGTH)
        except Exception:
            min_length = 8
        return {
            "phase": phase,
            "user": session.username if session is not None and phase in ("unlock", "second_factor", "signed_in") else "",
            "display_name": session.display_name if session is not None and phase in ("unlock", "signed_in") else "",
            "role": session.role if session is not None and phase == "signed_in" else "",
            "session_expired": bool(session is not None and session.locked and
                                    "idle" in (getattr(session, "lock_reason", "") or "")),
            "pending_factor": self.sessions.pending_factor,
            "face": face,
            "lockdown": lockdown,
            "password_policy": {"min_length": min_length},
            "methods": {"password": True, "face": bool(face.get("available")),
                        "pin": False, "passkey": False,
                        "unavailable_note": "PIN and passkey sign-in are not available: ZEUS has no PIN or "
                                            "passkey store. Use your password or your face."},
            "busy": self._busy.locked(),
            "login_switched_off": login_off,
            "remembered": remembered_method(),
        }

    # ------------------------------------------------------------ password ---

    def _attempt(self, fn: Callable[[], Dict[str, Any]]) -> Dict[str, Any]:
        if not self._busy.acquire(blocking=False):
            return {"ok": False, "message": "A sign-in is already in progress."}
        try:
            return fn()
        finally:
            self._busy.release()

    @staticmethod
    def _result(result, **extra) -> Dict[str, Any]:
        out = {"ok": bool(result.ok), "message": result.message, "pending_factor": result.pending_factor,
               "method": result.method, "attempts_left": result.attempts_left,
               "locked_until": result.locked_until}
        out.update(extra)
        return out

    def setup(self, username: str, password: str, confirm: str) -> Dict[str, Any]:
        def run():
            if not self.accounts.first_run_required():
                return {"ok": False, "message": "Setup is only possible before the first account exists."}
            if password != confirm:
                return {"ok": False, "message": "The passwords do not match."}
            try:
                user = self.accounts.create_account(username.strip(), password, is_admin=True)
            except ValueError as error:
                return {"ok": False, "message": str(error)}
            try:
                from memory import database
                database.set_user_role(user["id"], "admin")
            except Exception as error:
                logger.warning(f"role not recorded for the first account: {error}")
            result = self.sessions.login(username.strip(), password, client="desktop")
            return self._result(result, created=True)
        return self._attempt(run)

    def login(self, username: str, password: str) -> Dict[str, Any]:
        def run():
            if self.sessions.session is not None:
                return {"ok": False, "message": "A session already exists - unlock it or switch user."}
            result = self.sessions.login(username, password, client="desktop")
            if result.ok and not result.pending_factor:
                _remember_method("password")
            return self._result(result)
        return self._attempt(run)

    def unlock(self, password: str) -> Dict[str, Any]:
        def run():
            session = self.sessions.session
            if session is None or not session.locked:
                return {"ok": False, "message": "Nothing to unlock."}
            return self._result(self.sessions.unlock(password))
        return self._attempt(run)

    def cancel_factor(self) -> Dict[str, Any]:
        self.face_cancel()
        self.sessions.cancel_pending_factor("cancelled at the login screen")
        return {"ok": True}

    def switch_user(self) -> Dict[str, Any]:
        self.face_cancel()
        if self.sessions.session is not None:
            self.sessions.logout()
        return {"ok": True}

    # ---------------------------------------------------------------- face ---

    def face_start(self, purpose: str, emit: Emit) -> Dict[str, Any]:
        """purpose: login | unlock | second_factor. Progress arrives through
        `emit("auth_face", {...})`; this returns at once."""
        if purpose not in ("login", "unlock", "second_factor"):
            return {"ok": False, "message": f"unknown face purpose '{purpose}'"}
        if purpose == "login":
            status = self.sessions.face_sign_in_status()
            if not status.get("available"):
                return {"ok": False, "message": status.get("reason", "Face sign-in is not available.")}
        if purpose == "unlock":
            session = self.sessions.session
            if session is None or not session.locked:
                return {"ok": False, "message": "Nothing to unlock."}
            if not self.sessions.face_sign_in_status().get("available"):
                return {"ok": False, "message": self.sessions.face_sign_in_status().get(
                    "reason", "Face unlock is not available.")}
        if purpose == "second_factor" and not self.sessions.pending_factor:
            return {"ok": False, "message": "No face check is pending."}
        if not self._busy.acquire(blocking=False):
            return {"ok": False, "message": "A sign-in is already in progress."}
        self._face_stop.clear()
        self._face_thread = threading.Thread(target=self._face_run, args=(purpose, emit),
                                             name="medha-face-sign-in", daemon=True)
        self._face_thread.start()
        return {"ok": True, "started": True}

    def face_cancel(self) -> Dict[str, Any]:
        self._face_stop.set()
        thread = self._face_thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=3.0)
        return {"ok": True}

    def _face_run(self, purpose: str, emit: Emit) -> None:
        def say(state: str, **data):
            try:
                emit("auth_face", {"state": state, "purpose": purpose, **data})
            except Exception:
                pass
        try:
            frames = self._capture(say)
            if frames is None:
                return
            say("VERIFYING IDENTITY")
            if purpose == "login":
                result = self.sessions.login_with_face(frames, client="desktop")
                outcome = {"ok": result.ok, "message": result.message, "reason": result.reason,
                           "attempts_left": result.attempts_left}
            elif purpose == "unlock":
                result = self.sessions.unlock_with_face(frames)
                outcome = {"ok": result.ok, "message": result.message, "reason": result.reason,
                           "attempts_left": result.attempts_left}
            else:
                raw = self.sessions.complete_face_factor(frames)
                outcome = {"ok": bool(raw.get("ok")), "reason": raw.get("reason", ""),
                           "message": "" if raw.get("ok") else (raw.get("reason") or "Face not verified."),
                           "attempts_left": raw.get("attempts_left"), "ended": raw.get("ended", False)}
            del frames
            reason = (outcome.get("reason") or "") + " " + (outcome.get("message") or "")
            if "face_absent" in reason or "No face in view" in reason:
                say("ACCESS DENIED", message=outcome["message"], face_detected=False)
                return
            say("FACE DETECTED")
            if "liveness" in reason.lower() or "live-face" in reason.lower():
                say("LIVENESS CHECK", passed=False)
                say("ACCESS DENIED", message=outcome["message"], attempts_left=outcome.get("attempts_left"))
                return
            say("LIVENESS CHECK", passed=True)
            if not outcome["ok"]:
                say("ACCESS DENIED", message=outcome["message"] or "Face not verified.",
                    attempts_left=outcome.get("attempts_left"), ended=outcome.get("ended", False))
                return
            say("IDENTITY VERIFIED")
            if self.sessions.is_authenticated:
                if purpose == "login":
                    _remember_method("face")
                say("ACCESS GRANTED", user=self.sessions.username)
            else:
                say("ACCESS DENIED", message="The session did not open.")
        except Exception as error:
            logger.exception("face sign-in failed")
            say("CAMERA ERROR", message=f"Face sign-in failed: {error}")
        finally:
            self._busy.release()

    def _capture(self, say) -> Optional[list]:
        try:
            import cv2
            from security import face_auth
            index = face_auth.config()["camera_index"]
        except Exception as error:
            say("CAMERA ERROR", message=f"The camera cannot be used here: {error}")
            return None
        backend = cv2.CAP_DSHOW if sys.platform == "win32" else cv2.CAP_ANY
        capture = cv2.VideoCapture(index, backend)
        kept = []
        try:
            if not capture.isOpened():
                say("CAMERA ERROR", message=f"The camera (index {index}) could not be opened - is another "
                                            f"application using it?")
                return None
            started = time.time()
            ready = False
            next_keep, last_preview = started + FACE_WARMUP, 0.0
            while not self._face_stop.is_set() and time.time() - started < FACE_TIMEOUT:
                ok, frame = capture.read()
                if not ok or frame is None:
                    time.sleep(0.02)
                    continue
                now = time.time()
                if not ready:
                    ready = True
                    say("CAMERA READY")
                if now - last_preview >= PREVIEW_EVERY:
                    preview = _preview(frame, cv2)
                    if preview:
                        say("SCANNING", progress=len(kept) / float(FACE_FRAMES), preview=preview)
                    last_preview = now
                if now >= next_keep:
                    kept.append(frame.copy())
                    next_keep = now + FACE_INTERVAL
                    if len(kept) >= FACE_FRAMES:
                        say("SCANNING", progress=1.0)
                        break
        finally:
            capture.release()
        if self._face_stop.is_set():
            say("CANCELLED")
            return None
        if len(kept) < FACE_FRAMES:
            say("AUTHENTICATION TIMEOUT" if ready else "CAMERA ERROR",
                message="The camera returned too few pictures in time." if ready
                else "The camera did not return a picture.")
            return None
        return kept


def _preview(frame, cv2) -> str:
    """A small, mirrored JPEG for display only (never used for recognition)."""
    try:
        height, width = frame.shape[:2]
        scale = PREVIEW_WIDTH / float(width)
        small = cv2.resize(frame, (PREVIEW_WIDTH, max(1, int(height * scale))))
        small = cv2.flip(small, 1)
        ok, data = cv2.imencode(".jpg", small, [int(cv2.IMWRITE_JPEG_QUALITY), 60])
        return "data:image/jpeg;base64," + base64.b64encode(data.tobytes()).decode("ascii") if ok else ""
    except Exception:
        return ""


def _remember_method(method: str) -> None:
    """How this machine last signed in ("face" or "password") - nothing about who."""
    try:
        import json
        from core.paths import data_dir
        path = data_dir() / "access_preferences.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"method": method}), encoding="utf-8")
    except Exception:
        pass


def remembered_method() -> str:
    try:
        import json
        from core.paths import data_dir
        return json.loads((data_dir() / "access_preferences.json").read_text(encoding="utf-8")).get("method", "password")
    except Exception:
        return "password"
