"""
Face login (InsightFace) and the WFP firewall helper (tailscale/wf).

The model-backed tests run only when InsightFace models are present - in
models/insightface after STEP 1 : INSTALL, or wherever AGNI_TEST_FACE_MODELS
points. Everything else runs everywhere and needs no camera, no network and
no administrator rights: nothing here applies a firewall rule.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SAMPLE = ROOT / "third_party" / "insightface" / "insightface" / "data" / "images" / "t1.jpg"


def _face_models() -> Path:
    for candidate in (os.environ.get("AGNI_TEST_FACE_MODELS", ""),
                      str(ROOT / "models" / "insightface"),
                      str(Path(os.environ.get("TEMP", "/tmp")) / "agni_face_models")):
        if candidate and any(Path(candidate).glob("models/*/*.onnx")) and \
                (Path(candidate) / "addons" / "liveness.onnx").is_file():
            return Path(candidate)
    return Path()


FACE_MODELS = _face_models()
needs_models = pytest.mark.skipif(not FACE_MODELS.parts, reason="InsightFace models not installed")


# ======================================================== face: real models ==

@pytest.fixture()
def face(monkeypatch):
    from security import face_auth
    monkeypatch.setenv("AGNI_FACE_MODELS_DIR", str(FACE_MODELS))
    monkeypatch.setattr(face_auth, "engine", face_auth._Engine())
    from memory.database import init_db
    init_db()
    return face_auth


def _crops():
    import cv2
    from security import face_auth
    image = cv2.imread(str(SAMPLE))
    faces = face_auth.engine.analyze(image)
    out = []
    for obs in faces[:2]:
        x1, y1, x2, y2 = obs.bbox
        w, h = x2 - x1, y2 - y1
        crop = image[int(max(0, y1 - .8 * h)):int(y2 + .8 * h), int(max(0, x1 - .8 * w)):int(x2 + .8 * w)]
        out.append(cv2.resize(crop, None, fx=3, fy=3))
    return image, out


def _variants(person):
    """Five pictures of one person, as a camera would give (light, pose, focus)."""
    import cv2
    return [person, cv2.convertScaleAbs(person, alpha=1.1, beta=8), cv2.flip(person, 1),
            cv2.GaussianBlur(person, (3, 3), 0), cv2.convertScaleAbs(person, alpha=0.95, beta=4)]


@needs_models
def test_enrol_then_the_same_face_matches_and_another_does_not(face):
    import cv2
    image, (person, other) = _crops()
    enrolled = face.enroll(9001, "face.tester", _variants(person))
    assert enrolled["enrolled"] and enrolled["frames_used"] >= 3
    same = face.verify(9001, "face.tester", [cv2.convertScaleAbs(person, alpha=0.9, beta=-5)])
    assert same["ok"] and same["similarity"] > 0.8
    impostor = face.verify(9001, "face.tester", [other])
    assert not impostor["ok"] and impostor["reason"] == "face does not match"
    face.remove(9001, "face.tester")


@needs_models
def test_a_group_photo_or_an_empty_frame_never_verifies(face):
    import numpy as np
    image, (person, _other) = _crops()
    face.enroll(9002, "face.tester2", _variants(person))
    assert face.verify(9002, "face.tester2", [image])["reason"] == "more than one face"
    assert face.verify(9002, "face.tester2", [np.zeros((240, 240, 3), np.uint8)])["reason"] == "no face"
    face.remove(9002, "face.tester2")


@needs_models
def test_the_stored_template_is_encrypted_not_raw_floats(face):
    _image, (person, _other) = _crops()
    face.enroll(9003, "face.tester3", _variants(person))
    conn = face._connect()
    blob = bytes(conn.execute("SELECT template FROM face_templates WHERE user_id = 9003").fetchone()[0])
    conn.close()
    template = face._load_template(9003)
    assert template.size == 512
    assert template.tobytes() not in blob, "the face template is stored in the clear"
    face.remove(9003, "face.tester3")


@needs_models
def test_face_sign_in_identifies_the_right_account_with_the_real_models(face, monkeypatch):
    """identify() end to end on the real InsightFace models: two enrolled
    people, each recognised as themselves and never as the other."""
    import cv2
    _image, (person, other) = _crops()
    settings = {**face.config(), "login": "face_or_password", "liveness": "required"}
    monkeypatch.setattr(face, "config", lambda: settings)
    monkeypatch.setattr(face, "enrolled_user_ids", lambda: [9101, 9102])
    face.enroll(9101, "face.signin.a", _variants(person))
    face.enroll(9102, "face.signin.b", _variants(other))
    try:
        seen_a = face.identify([cv2.convertScaleAbs(person, alpha=0.92, beta=-4)] * 3)
        assert seen_a["ok"] and seen_a["user_id"] == 9101, seen_a
        seen_b = face.identify([cv2.convertScaleAbs(other, alpha=1.05, beta=3)] * 3)
        assert seen_b["ok"] and seen_b["user_id"] == 9102, seen_b
        import numpy as np
        nobody = face.identify([np.zeros((240, 240, 3), np.uint8)] * 3)
        assert not nobody["ok"] and nobody["no_face"]
    finally:
        face.remove(9101, "face.signin.a")
        face.remove(9102, "face.signin.b")


@needs_models
def test_the_engine_makes_no_network_connection(face):
    import cv2
    from security.egress import egress_guard
    before = egress_guard.status()["stats"]["attempts"]
    face.engine.analyze(cv2.imread(str(SAMPLE)))
    assert egress_guard.status()["stats"]["attempts"] == before


# ================================================ face: rules without models ==

def test_a_tampered_liveness_model_is_refused(tmp_path, monkeypatch):
    from security import face_auth
    (tmp_path / "models" / "buffalo_sc").mkdir(parents=True)
    (tmp_path / "models" / "buffalo_sc" / "det.onnx").write_bytes(b"x")
    (tmp_path / "addons").mkdir()
    (tmp_path / "addons" / "liveness.onnx").write_bytes(b"not the published file")
    monkeypatch.setenv("AGNI_FACE_MODELS_DIR", str(tmp_path))
    with pytest.raises(face_auth.FaceError, match="SHA-256"):
        face_auth._Engine().get()


def test_missing_liveness_model_fails_closed_when_required(tmp_path, monkeypatch):
    from security import face_auth
    (tmp_path / "models" / "buffalo_sc").mkdir(parents=True)
    (tmp_path / "models" / "buffalo_sc" / "det.onnx").write_bytes(b"x")
    monkeypatch.setenv("AGNI_FACE_MODELS_DIR", str(tmp_path))
    with pytest.raises(face_auth.FaceError, match="liveness"):
        face_auth._Engine().get()


def test_face_login_requirement_fails_closed_when_templates_cannot_be_read(monkeypatch):
    from security import face_auth
    settings = {**face_auth.config(), "login": "second_factor"}
    monkeypatch.setattr(face_auth, "config", lambda: settings)

    def broken():
        raise RuntimeError("database unreadable")

    monkeypatch.setattr(face_auth, "_connect", broken)
    assert face_auth.login_required_for(1) is True


def test_face_login_is_off_by_default(tmp_path, monkeypatch):
    """The CODE default. The operator's config.ini may choose a mode (the
    repair machine uses face_or_password since 2026-09-25); a config that
    says nothing must mean passwords only."""
    from security import face_auth
    silent = tmp_path / "config.ini"
    silent.write_text("[face]\nenabled = true\n", encoding="utf-8")
    monkeypatch.setattr(face_auth, "config_file", lambda: silent)
    assert face_auth.config()["login"] == "off"
    assert face_auth.login_required_for(1) is False


# ============================================ the session enforces the factor ==

@pytest.fixture()
def sessions(monkeypatch):
    from auth.auth_manager import auth_manager
    from auth.session_manager import SessionManager
    from memory import database
    from security import face_auth
    database.init_db()
    if not database.get_user("factor.user"):
        auth_manager.create_account("factor.user", "Factor-Passw0rd-123")
    monkeypatch.setattr(face_auth, "login_required_for", lambda user_id: True)
    outcomes = []
    monkeypatch.setattr(face_auth, "verify",
                        lambda *a, **k: outcomes.pop(0) if outcomes else {"ok": False, "reason": "no match"})
    return SessionManager(), outcomes


def test_a_correct_password_alone_is_not_a_session_when_a_face_is_enrolled(sessions):
    manager, outcomes = sessions
    result = manager.login("factor.user", "Factor-Passw0rd-123")
    assert result.ok and result.pending_factor == "face"
    assert manager.is_authenticated is False
    with pytest.raises(PermissionError):
        manager.require_authenticated()
    outcomes.append({"ok": True, "similarity": 0.9})
    assert manager.complete_face_factor([object()])["ok"]
    assert manager.is_authenticated is True
    manager.logout()


def test_three_failed_face_checks_end_the_attempt(sessions):
    manager, _outcomes = sessions
    manager.login("factor.user", "Factor-Passw0rd-123")
    for left in (2, 1):
        outcome = manager.complete_face_factor([object()])
        assert not outcome["ok"] and outcome["attempts_left"] == left
    final = manager.complete_face_factor([object()])
    assert final.get("ended") is True
    assert manager.session is None and not manager.is_authenticated


def test_unlock_also_needs_the_face(sessions):
    manager, outcomes = sessions
    manager.login("factor.user", "Factor-Passw0rd-123")
    outcomes.append({"ok": True})
    manager.complete_face_factor([object()])
    manager.lock()
    result = manager.unlock("Factor-Passw0rd-123")
    assert result.ok and result.pending_factor == "face" and not manager.is_authenticated
    outcomes.append({"ok": True})
    manager.complete_face_factor([object()])
    assert manager.is_authenticated
    manager.logout()


def test_cancelling_the_face_check_signs_out(sessions):
    manager, _outcomes = sessions
    manager.login("factor.user", "Factor-Passw0rd-123")
    manager.cancel_pending_factor("test")
    assert manager.session is None


# ========================================================== WFP (tailscale/wf) ==

def test_the_wfp_helper_is_shipped_and_answers(tmp_path):
    exe = ROOT / "bin" / "agni-wfp.exe"
    assert exe.is_file(), "bin/agni-wfp.exe is missing"
    assert (ROOT / "tools" / "agni-wfp" / "main.go").is_file()
    assert (ROOT / "third_party" / "wf" / "LICENSE").is_file()
    if sys.platform == "win32":
        import subprocess
        out = subprocess.run([str(exe), "version"], capture_output=True, text=True, timeout=30)
        assert json.loads(out.stdout)["ok"] is True
        bad = subprocess.run([str(exe), "nonsense"], capture_output=True, text=True, timeout=30)
        assert bad.returncode == 2 and json.loads(bad.stdout)["ok"] is False


def test_wfp_status_never_prompts_and_says_when_it_cannot_read(monkeypatch):
    from security import wfp
    monkeypatch.setattr(wfp, "available", lambda: True)
    monkeypatch.setattr(wfp, "_run_elevated", lambda *a, **k: pytest.fail("status raised a UAC prompt"))
    monkeypatch.setattr(wfp, "is_elevated", lambda: False)
    monkeypatch.setattr(wfp, "_run_direct", lambda args, **k: {
        "ok": False, "error": "access denied - run this from an administrator (elevated) prompt"})
    status = wfp.status()
    assert status["installed"] is None and "administrator" in status["detail"]


def test_wfp_apply_confines_the_real_interpreter(monkeypatch):
    from security import wfp
    seen = {}
    monkeypatch.setattr(wfp, "run", lambda args, elevate=False: seen.update(args=args, elevate=elevate) or {"ok": True})
    wfp.apply(allow_lan=True)
    apps = [seen["args"][i + 1] for i, a in enumerate(seen["args"]) if a == "-app"]
    base = Path(getattr(sys, "_base_executable", sys.executable)).parent
    assert seen["elevate"] is True and "-allow-lan" in seen["args"]
    assert any(Path(a).parent == base for a in apps) or \
        (ROOT / ".venv" / "pyvenv.cfg").is_file(), apps


def test_firewall_rules_target_the_base_interpreter_not_the_venv_launcher(tmp_path):
    """On Windows .venv\\Scripts\\python.exe only launches the base python.exe,
    and the BASE process is what connects - a rule on the launcher blocks nothing."""
    from security.firewall import agni_programs, apply_script
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "pyvenv.cfg").write_text("home = C:\\Python312\n", encoding="utf-8")
    programs = agni_programs(tmp_path)
    assert "C:\\Python312\\python.exe" in programs and "C:\\Python312\\pythonw.exe" in programs
    assert "C:\\Python312\\python.exe" in apply_script(tmp_path)


def test_changing_the_firewall_is_admin_only_and_needs_approval():
    from security import policy
    for tool in ("apply_wfp_firewall", "remove_wfp_firewall", "wfp_blocked_connections"):
        profile = policy.profile_for(tool, 3)
        assert profile.min_role == "admin" and profile.access == policy.EXECUTE, tool
    assert policy.profile_for("remove_face", 3).classification == policy.RESTRICTED


@pytest.mark.parametrize("phrase, tool", [
    ("face status", "face_status"), ("enroll my face", "register_face"),
    ("test my face", "verify_my_face"), ("remove my face", "remove_face"),
    ("wfp status", "wfp_firewall_status"), ("apply the firewall", "apply_wfp_firewall"),
    ("remove the wfp firewall", "remove_wfp_firewall"), ("show blocked connections", "wfp_blocked_connections"),
])
def test_face_and_firewall_phrases_reach_their_tools(phrase, tool):
    from core.decision_engine import _ROUTES
    assert next((name for pattern, name, _ in _ROUTES if pattern.match(phrase)), None) == tool


# ================================================================ the setup ==

def _installer():
    spec = importlib.util.spec_from_file_location("install_everything",
                                                  ROOT / "scripts" / "install_everything.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_setup_pins_the_published_face_model_digests():
    installer = _installer()
    from security.face_auth import LIVENESS_SHA256
    assert installer.FACE_LIVENESS[1] == LIVENESS_SHA256
    assert installer.FACE_PACKS["buffalo_l"][1] == \
        "80ffe37d8a5940d59a7384c201a2a38d4741f2f3c51eef46ebb28218a7b0ca2f"


def test_a_download_with_the_wrong_digest_is_refused_and_removed(tmp_path, monkeypatch):
    installer = _installer()
    import io
    import urllib.request

    class _Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Response(b"evil bytes"))
    target = tmp_path / "liveness.onnx"
    assert installer._download_verified("https://example.invalid/x", target, "0" * 64, 10) is False
    assert not target.exists() and not (tmp_path / "liveness.onnx.part").exists()
