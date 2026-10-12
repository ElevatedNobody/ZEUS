"""
The ZEUS login screen's controller (auth/web_login.py): the page renders,
this decides - through the same security core as every other sign-in.

The camera and the recognition engine are stand-ins, so this runs without a
camera, models or a person; the password path uses real accounts (Argon2id).
"""

import threading
import time

import numpy as np
import pytest

from auth import web_login

PASSWORD = "Web-Login-Passw0rd-2026"


class FakeCamera:
    opened = True

    def __init__(self, index, backend=None):
        self.frames = 0

    def isOpened(self):
        return FakeCamera.opened

    def read(self):
        self.frames += 1
        return True, np.full((240, 320, 3), 90, dtype=np.uint8)

    def release(self):
        pass


@pytest.fixture
def controller(monkeypatch):
    import cv2
    from auth.auth_manager import auth_manager
    from auth.session_manager import SessionManager
    from memory import database
    from security import face_auth
    database.init_db()
    if not database.get_user("web.owner"):
        auth_manager.create_account("web.owner", PASSWORD)
    owner = database.get_user("web.owner")
    database.set_user_lock(owner["id"], None)
    database.record_login_success(owner["id"])
    monkeypatch.setattr(cv2, "VideoCapture", FakeCamera)
    FakeCamera.opened = True
    monkeypatch.setattr(web_login, "FACE_WARMUP", 0.0)
    monkeypatch.setattr(web_login, "FACE_INTERVAL", 0.01)
    monkeypatch.setattr(web_login, "PREVIEW_EVERY", 0.0)
    monkeypatch.setattr(face_auth, "face_sign_in_available", lambda: {"available": True, "reason": "", "enrolled": 1})
    settings = {**face_auth.config(), "enabled": True, "login": "face_or_password"}
    monkeypatch.setattr(face_auth, "config", lambda: settings)
    outcomes = []
    monkeypatch.setattr(face_auth, "identify", lambda frames: outcomes.pop(0))
    sessions = SessionManager()
    sessions._save_face_state({"failures": 0})
    events = []
    ctl = web_login.AuthController(session_manager=sessions)
    yield ctl, sessions, outcomes, owner, events
    ctl.face_cancel()
    sessions.logout()


def _collect(events):
    return lambda kind, payload: events.append(payload)


def _wait(events, final=("ACCESS GRANTED", "ACCESS DENIED", "CAMERA ERROR", "AUTHENTICATION TIMEOUT",
                         "CANCELLED"), timeout=10.0):
    started = time.time()
    while time.time() - started < timeout:
        if any(e["state"] in final for e in events):
            return [e["state"] for e in events]
        time.sleep(0.02)
    raise AssertionError(f"no final state: {[e['state'] for e in events]}")


# ------------------------------------------------------------------ state --

def test_the_screen_is_told_what_is_really_available(controller):
    ctl, *_ = controller
    state = ctl.state()
    assert state["phase"] == "login"
    assert state["methods"]["password"] is True and state["methods"]["face"] is True
    assert state["methods"]["pin"] is False and state["methods"]["passkey"] is False
    assert "not available" in state["methods"]["unavailable_note"]
    assert state["user"] == ""                               # nobody is named before a sign-in


# --------------------------------------------------------------- password --

def test_a_wrong_password_gets_the_generic_answer(controller):
    ctl, sessions, *_ = controller
    result = ctl.login("web.owner", "wrong-password-123")
    assert not result["ok"] and "Invalid credentials" in result["message"]
    assert not sessions.is_authenticated
    unknown = ctl.login("nobody.here", "wrong-password-123")
    assert "Invalid credentials" in unknown["message"]          # no hint which half was wrong


def test_the_right_password_signs_in(controller):
    ctl, sessions, *_ = controller
    result = ctl.login("web.owner", PASSWORD)
    assert result["ok"] and sessions.is_authenticated
    assert ctl.state()["phase"] == "signed_in" and ctl.state()["user"] == "web.owner"


def test_lock_unlock_and_session_expiry(controller):
    ctl, sessions, *_ = controller
    ctl.login("web.owner", PASSWORD)
    sessions.lock("user request")
    state = ctl.state()
    assert state["phase"] == "unlock" and state["user"] == "web.owner" and not state["session_expired"]
    assert not ctl.unlock("wrong-password-123")["ok"] and not sessions.is_authenticated
    assert ctl.unlock(PASSWORD)["ok"] and sessions.is_authenticated
    sessions.lock("idle timeout")
    assert ctl.state()["session_expired"] is True


def test_one_attempt_at_a_time(controller):
    ctl, sessions, *_ = controller
    ctl._busy.acquire()
    try:
        assert "already in progress" in ctl.login("web.owner", PASSWORD)["message"]
        assert not sessions.is_authenticated
    finally:
        ctl._busy.release()


def test_setup_is_refused_once_an_account_exists(controller):
    ctl, *_ = controller
    assert not ctl.setup("intruder", "Another-Passw0rd-1", "Another-Passw0rd-1")["ok"]


def test_switch_user_signs_out(controller):
    ctl, sessions, *_ = controller
    ctl.login("web.owner", PASSWORD)
    sessions.lock()
    ctl.switch_user()
    assert sessions.session is None and ctl.state()["phase"] == "login"


# ------------------------------------------------------------------- face --

def test_face_sign_in_reports_each_real_state(controller):
    ctl, sessions, outcomes, owner, events = controller
    outcomes.append({"ok": True, "user_id": owner["id"], "similarity": 0.8, "reason": "", "no_face": False})
    assert ctl.face_start("login", _collect(events))["ok"]
    states = _wait(events)
    order = ["CAMERA READY", "SCANNING", "VERIFYING IDENTITY", "FACE DETECTED", "LIVENESS CHECK",
             "IDENTITY VERIFIED", "ACCESS GRANTED"]
    positions = [states.index(s) for s in order]
    assert positions == sorted(positions)
    assert sessions.is_authenticated and sessions.username == "web.owner"
    previews = [e["preview"] for e in events if e.get("preview")]
    assert previews and all(p.startswith("data:image/jpeg;base64,") for p in previews)


def test_a_failed_liveness_check_is_denied(controller):
    ctl, sessions, outcomes, _owner, events = controller
    outcomes.append({"ok": False, "user_id": None, "similarity": 0.8, "reason": "liveness check failed",
                     "no_face": False})
    ctl.face_start("login", _collect(events))
    states = _wait(events)
    liveness = next(e for e in events if e["state"] == "LIVENESS CHECK")
    assert liveness["passed"] is False and states[-1] == "ACCESS DENIED"
    assert not sessions.is_authenticated


def test_no_camera_leaves_the_password_path_open(controller):
    ctl, sessions, _outcomes, _owner, events = controller
    FakeCamera.opened = False
    ctl.face_start("login", _collect(events))
    states = _wait(events)
    assert states[-1] == "CAMERA ERROR" and not sessions.is_authenticated
    ctl._face_thread.join(timeout=3)
    assert ctl.login("web.owner", PASSWORD)["ok"]               # the fallback works at once


def test_face_is_not_offered_when_it_is_not_available(controller, monkeypatch):
    from security import face_auth
    ctl, *_ = controller
    monkeypatch.setattr(face_auth, "face_sign_in_available",
                        lambda: {"available": False, "reason": "no face enrolled"})
    result = ctl.face_start("login", lambda *a: None)
    assert not result["ok"] and "no face enrolled" in result["message"]
    assert ctl.state()["methods"]["face"] is False


def test_the_screen_reopens_on_the_method_used_last(controller):
    ctl, sessions, outcomes, owner, events = controller
    outcomes.append({"ok": True, "user_id": owner["id"], "similarity": 0.8, "reason": "", "no_face": False})
    ctl.face_start("login", _collect(events))
    _wait(events)
    assert ctl.state()["remembered"] == "face"
    sessions.logout()
    ctl.login("web.owner", PASSWORD)
    assert ctl.state()["remembered"] == "password"


def test_stopping_the_scan_releases_the_camera(controller, monkeypatch):
    ctl, sessions, _outcomes, _owner, events = controller
    released = []

    class SlowCamera(FakeCamera):
        def read(self):
            time.sleep(0.05)
            return super().read()

        def release(self):
            released.append(True)

    import cv2
    monkeypatch.setattr(cv2, "VideoCapture", SlowCamera)
    monkeypatch.setattr(web_login, "FACE_INTERVAL", 5.0)       # would take far longer than the test
    ctl.face_start("login", _collect(events))
    time.sleep(0.3)
    ctl.face_cancel()
    assert released and not ctl._face_thread.is_alive()
    assert _wait(events)[-1] == "CANCELLED" and not sessions.is_authenticated
    assert ctl.login("web.owner", PASSWORD)["ok"]                # nothing left holding the sign-in lock


def test_the_page_can_not_start_a_second_factor_that_is_not_pending(controller):
    ctl, *_ = controller
    assert not ctl.face_start("second_factor", lambda *a: None)["ok"]
    assert not ctl.face_start("backdoor", lambda *a: None)["ok"]
