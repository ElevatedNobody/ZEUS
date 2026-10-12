"""
Sign in with a face OR a password ([face] login = face_or_password).

Added 2026-09-25 at the owner's request. The face alone is a credential here,
so these tests pin the rules that make that acceptable:

  * a live face is required; a config that weakens liveness refuses;
  * the match must be strong AND clearly one enrolled person, in 2+ frames;
  * three failed face attempts pause face sign-in until a password is used;
  * a locked or disabled account is not opened by a face either;
  * unlocking a session takes the LOCKED user's face, nobody else's;
  * the ZEUS sign-in screen's behaviour is tested in tests/test_web_login.py.

The recognition engine and the camera are replaced by stand-ins, so this
runs without models, a camera or a person in front of it.
"""

from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PASSWORD = "Face-Or-Passw0rd-2026"


# =============================================================== matching ==

def _unit(values):
    import numpy as np
    vector = np.asarray(values, dtype="float32")
    return vector / np.linalg.norm(vector)


@pytest.fixture()
def engine(monkeypatch):
    """Templates for two accounts and a scripted camera: each frame is a
    tuple (embedding or None, live)."""
    from security import face_auth
    templates = {1: _unit([1, 0, 0, 0]), 2: _unit([0, 1, 0, 0])}
    monkeypatch.setattr(face_auth, "enrolled_user_ids", lambda: list(templates))
    monkeypatch.setattr(face_auth, "_load_template", lambda uid: templates[uid])
    settings = {**face_auth.config(), "enabled": True, "login": "face_or_password",
                "liveness": "required", "login_threshold": 0.50, "login_margin": 0.08}
    monkeypatch.setattr(face_auth, "config", lambda: settings)
    monkeypatch.setattr(face_auth, "_audit", lambda *a, **k: None)

    def analyze(frame):
        if frame is None:
            return []
        vector, live = frame
        return [face_auth.FaceObservation(bbox=[0, 0, 10, 10], det_score=0.9,
                                          embedding=_unit(vector), live=live,
                                          live_score=0.9 if live else 0.1,
                                          liveness_status="ok")]

    monkeypatch.setattr(face_auth.engine, "analyze", analyze)
    return face_auth, templates, settings


def test_a_clear_live_face_is_recognised_as_its_account(engine):
    face_auth, _templates, _ = engine
    outcome = face_auth.identify([([1, 0.1, 0, 0], True)] * 5)
    assert outcome["ok"] and outcome["user_id"] == 1 and outcome["similarity"] > 0.9


def test_a_photo_that_fails_liveness_never_signs_in(engine):
    face_auth, _templates, _ = engine
    outcome = face_auth.identify([([1, 0, 0, 0], False)] * 5)
    assert not outcome["ok"] and "liveness" in outcome["reason"] and not outcome["no_face"]


def test_a_stranger_is_not_matched_to_the_nearest_account(engine):
    face_auth, _templates, _ = engine
    outcome = face_auth.identify([([0.3, 0.3, 1, 0], True)] * 5)
    assert not outcome["ok"] and outcome["reason"] == "face not recognised"


def test_a_face_close_to_two_accounts_is_refused(engine):
    """Look-alikes: the best match must beat the next one by the margin."""
    face_auth, templates, _ = engine
    templates[2] = _unit([1, 0.12, 0, 0])            # nearly the same person
    outcome = face_auth.identify([([1, 0.06, 0, 0], True)] * 5)
    assert not outcome["ok"]


def test_one_good_frame_is_not_enough(engine):
    face_auth, _templates, _ = engine
    outcome = face_auth.identify([([1, 0, 0, 0], True), None, None, None, None])
    assert not outcome["ok"] and outcome["frames_used"] == 1


def test_an_empty_room_is_reported_as_nobody_there(engine):
    face_auth, _templates, _ = engine
    outcome = face_auth.identify([None] * 5)
    assert not outcome["ok"] and outcome["no_face"] is True


def test_one_person_on_two_accounts_is_named_as_the_problem(engine):
    """The owner has an admin and an everyday account. Enrolling the same face
    on both must not look like 'not recognised' forever."""
    face_auth, templates, _ = engine
    templates[2] = _unit([1, 0.02, 0, 0])            # the same person, second account
    outcome = face_auth.identify([([1, 0.01, 0, 0], True)] * 5)
    assert not outcome["ok"] and outcome["same_face_on_two_accounts"]
    assert outcome["reason"] == "matches more than one account"


def test_enrolling_a_face_already_on_another_account_warns(engine, monkeypatch):
    face_auth, templates, _ = engine
    monkeypatch.setattr(face_auth, "_protect", lambda raw, protect: raw)

    class _Conn:
        def execute(self, *a, **k):
            return self

        def commit(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(face_auth, "_connect", lambda: _Conn())
    monkeypatch.setattr("memory.database.get_user_by_id",
                        lambda uid: {"username": {1: "dhruv@1007", 2: "Dhruv1"}[uid]})
    enrolled = face_auth.enroll(2, "Dhruv1", [([1, 0.03, 0, 0], True)] * 4)
    assert enrolled["enrolled"] and "dhruv@1007" in enrolled.get("warning", "")
    clean = face_auth.enroll(2, "Dhruv1", [([0, 1, 0.05, 0], True)] * 4)
    assert "warning" not in clean


def test_face_only_sign_in_refuses_to_run_with_liveness_weakened(engine):
    face_auth, _templates, settings = engine
    settings["liveness"] = "observe"
    with pytest.raises(face_auth.FaceError, match="liveness"):
        face_auth.identify([([1, 0, 0, 0], True)] * 5)


def test_the_code_default_is_passwords_only(tmp_path, monkeypatch):
    """Without [face] login in config.ini, nothing but a password signs in."""
    from security import face_auth
    empty = tmp_path / "config.ini"
    empty.write_text("[face]\nenabled = true\n", encoding="utf-8")
    monkeypatch.setattr(face_auth, "config_file", lambda: empty)
    assert face_auth.config()["login"] == "off"
    assert face_auth.face_sign_in_available()["available"] is False
    assert face_auth.login_required_for(1) is False


def test_face_only_matching_is_never_looser_than_the_second_factor(tmp_path, monkeypatch):
    from security import face_auth
    loose = tmp_path / "config.ini"
    loose.write_text("[face]\nthreshold = 0.60\nlogin_threshold = 0.30\n", encoding="utf-8")
    monkeypatch.setattr(face_auth, "config_file", lambda: loose)
    assert face_auth.config()["login_threshold"] >= 0.60


# ================================================================ session ==

@pytest.fixture()
def sessions(monkeypatch):
    from auth.auth_manager import auth_manager
    from auth.session_manager import SessionManager
    from memory import database
    from security import face_auth
    database.init_db()
    for name in ("face.owner", "face.other"):
        if not database.get_user(name):
            auth_manager.create_account(name, PASSWORD)
        user = database.get_user(name)
        database.set_user_lock(user["id"], None)
        database.record_login_success(user["id"])
    owner, other = database.get_user("face.owner"), database.get_user("face.other")
    monkeypatch.setattr(face_auth, "face_sign_in_available",
                        lambda: {"available": True, "reason": "", "enrolled": 2})
    outcomes = []
    monkeypatch.setattr(face_auth, "identify", lambda frames: outcomes.pop(0))
    manager = SessionManager()
    manager._save_face_state({"failures": 0})
    yield manager, outcomes, owner, other
    manager.logout()


def _match(user):
    return {"ok": True, "user_id": user["id"], "similarity": 0.81, "reason": "", "no_face": False}


def _miss(reason="face not recognised"):
    return {"ok": False, "user_id": None, "similarity": 0.2, "reason": reason, "no_face": False}


def test_a_recognised_face_alone_starts_the_session(sessions):
    from memory import database
    manager, outcomes, owner, _other = sessions
    outcomes.append(_match(owner))
    result = manager.login_with_face(["frame"] * 5)
    assert result.ok and result.method == "face"
    assert manager.is_authenticated and manager.username == "face.owner"
    conn = database.get_connection()
    try:
        rows = conn.execute("SELECT event, username, detail FROM auth_events "
                            "ORDER BY rowid DESC LIMIT 5").fetchall()
    finally:
        conn.close()
    assert any(r[0] == "login_success" and r[1] == "face.owner" and "method=face" in (r[2] or "")
               for r in rows), "a face sign-in must be in the audit trail as one"


def test_a_password_alone_still_works_in_face_or_password_mode(sessions, monkeypatch):
    from security import face_auth
    manager, _outcomes, _owner, _other = sessions
    settings = {**face_auth.config(), "enabled": True, "login": "face_or_password"}
    monkeypatch.setattr(face_auth, "config", lambda: settings)
    assert face_auth.login_required_for(_owner["id"]) is False
    result = manager.login("face.owner", PASSWORD)
    assert result.ok and not result.pending_factor and manager.is_authenticated


def test_three_failed_faces_pause_face_sign_in_until_a_password_is_used(sessions):
    manager, outcomes, owner, _other = sessions
    for left in (2, 1, 0):
        outcomes.append(_miss())
        result = manager.login_with_face(["frame"] * 5)
        assert not result.ok and result.attempts_left == left
    status = manager.face_sign_in_status()
    assert status["available"] is False and status.get("blocked")
    outcomes.append(_match(owner))                   # even the right face is refused now
    assert manager.login_with_face(["frame"] * 5).reason == "face_unavailable"
    assert manager.login("face.owner", PASSWORD).ok  # the password turns it back on
    assert manager.face_sign_in_status()["available"] is True


def test_the_pause_survives_a_restart(sessions):
    from auth.session_manager import SessionManager
    manager, outcomes, _owner, _other = sessions
    for _ in range(3):
        outcomes.append(_miss("liveness check failed"))
        manager.login_with_face(["frame"] * 5)
    assert SessionManager().face_sign_in_status()["available"] is False


def test_nobody_in_front_of_the_camera_does_not_use_up_an_attempt(sessions):
    manager, outcomes, _owner, _other = sessions
    for _ in range(5):
        outcomes.append({"ok": False, "user_id": None, "similarity": None,
                         "reason": "no face", "no_face": True})
        result = manager.login_with_face(["frame"] * 5)
        assert result.reason == "face_absent"
    assert manager.face_sign_in_status()["attempts_left"] == 3


def test_the_same_face_on_two_accounts_does_not_use_up_attempts(sessions):
    manager, outcomes, _owner, _other = sessions
    for _ in range(4):
        outcomes.append({"ok": False, "user_id": None, "similarity": 0.8,
                         "reason": "matches more than one account", "no_face": False,
                         "same_face_on_two_accounts": True})
        result = manager.login_with_face(["frame"] * 5)
        assert result.reason == "face_ambiguous" and "more than one account" in result.message
    assert manager.face_sign_in_status()["attempts_left"] == 3


def test_a_locked_account_is_not_opened_by_its_face(sessions):
    from memory import database
    manager, outcomes, owner, _other = sessions
    database.set_user_lock(owner["id"], time.time() + 300)
    outcomes.append(_match(owner))
    result = manager.login_with_face(["frame"] * 5)
    assert not result.ok and result.reason == "locked" and not manager.is_authenticated


def test_unlock_takes_the_locked_users_face_and_nobody_elses(sessions):
    manager, outcomes, owner, other = sessions
    manager.login("face.owner", PASSWORD)
    manager.lock()
    outcomes.append(_match(other))
    assert not manager.unlock_with_face(["frame"] * 5).ok
    assert not manager.is_authenticated and manager.username == "face.owner"
    outcomes.append(_match(owner))
    assert manager.unlock_with_face(["frame"] * 5).ok and manager.is_authenticated


def test_face_sign_in_is_refused_when_the_mode_is_off(sessions, monkeypatch):
    from security import face_auth
    manager, outcomes, owner, _other = sessions
    monkeypatch.setattr(face_auth, "face_sign_in_available",
                        lambda: {"available": False, "reason": "face sign-in is off ([face] login)"})
    outcomes.append(_match(owner))
    result = manager.login_with_face(["frame"] * 5)
    assert not result.ok and result.reason == "face_unavailable"
    assert "off" in result.message
