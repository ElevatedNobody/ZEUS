"""
Face verification for ZEUS, built on InsightFace (third_party/insightface).

What it is for - `[face] login` in config.ini chooses:

  off              passwords only (the default when the key is absent)
  second_factor    an account that enrolled a face must pass a face check
                   AFTER its password (auth/session_manager.py enforces this -
                   the policy engine sees an unauthenticated session until it
                   passes)
  face_or_password the access screen offers either: the password alone, or
                   the face alone (identify() below). Chosen by the owner on
                   2026-09-25, knowing the trade-off stated next.

Face-only sign-in is held to a stricter standard than the second factor:
liveness must be `required`, the match must reach `login_threshold` (default
0.50, above the 0.45 second-factor threshold) in at least two live frames, it
must beat every other enrolled account by `login_margin`, and three failed
face attempts switch face sign-in off until a password sign-in succeeds.

How it stays sovereign and honest:

* models load from local folders only (models/insightface/models/<pack>,
  models/insightface/addons/liveness.onnx). Nothing is downloaded at runtime;
  STEP 1 : INSTALL fetches them once and records their SHA-256.
* only a 512-number face template is kept - never an image - and it is
  encrypted with Windows DPAPI (bound to this Windows account) before it is
  written to the database. If DPAPI is unavailable, enrolment is refused.
* RGB liveness (InsightFace's liveness addon) rejects many photo/screen
  replays. It reduces spoofing; it does not make it impossible. That is the
  honest cost of face_or_password: a good enough photo or video of an
  enrolled person is a better attack on an RGB webcam than guessing a strong
  password. second_factor does not have that weakness.
* every enrolment, verification and removal is written to the audit trail.

The pretrained InsightFace models are licensed for non-commercial research use
only (see third_party/README.md).
"""

from __future__ import annotations

import configparser
import hashlib
import json
import os
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.logger import get_logger

logger = get_logger("security.face_auth")

ROOT = Path(__file__).resolve().parent.parent
VENDORED = ROOT / "third_party" / "insightface"
LIVENESS_SHA256 = "87a9ac1dbb16a61eec212957e5095e62a8769c1e188af9b0198f253302c4afdb"
PACK_PREFERENCE = ("buffalo_l", "buffalo_m", "buffalo_s", "buffalo_sc")
TEMPLATE_DIMS = 512
MIN_ENROL_FRAMES = 3


class FaceError(RuntimeError):
    """A refusal with a reason the operator can act on."""


@dataclass
class FaceObservation:
    bbox: List[float]
    det_score: float
    embedding: Any                      # numpy float32 (512,), L2-normalised
    live: Optional[bool]                # None when liveness did not run
    live_score: Optional[float]
    liveness_status: str = "not_run"


# --------------------------------------------------------------- config ----

LOGIN_MODES = ("off", "second_factor", "face_or_password")


def config_file() -> Path:
    return ROOT / "config.ini"


def config() -> Dict[str, Any]:
    parser = configparser.ConfigParser()
    try:
        parser.read(config_file(), encoding="utf-8")
    except Exception:
        pass
    get = lambda key, default: parser.get("face", key, fallback=default).strip()   # noqa: E731

    def number(key: str, default: float) -> float:
        try:
            return float(get(key, str(default)))
        except ValueError:
            return default

    threshold = min(max(number("threshold", 0.45), 0.2), 0.9)
    login = get("login", "off").lower()
    liveness = get("liveness", "required").lower()
    return {"enabled": get("enabled", "true").lower() in ("1", "true", "yes", "on"),
            "login": login if login in LOGIN_MODES else "off",
            "model_pack": get("model_pack", "auto") or "auto",
            "threshold": threshold,
            # Face-only sign-in never accepts less than the second factor does.
            "login_threshold": min(max(number("login_threshold", 0.50), threshold), 0.95),
            "login_margin": min(max(number("login_margin", 0.08), 0.0), 0.5),
            "liveness": liveness if liveness in ("required", "observe", "off") else "required",
            "liveness_threshold": min(max(number("liveness_threshold", 0.80), 0.0), 1.0),
            "camera_index": int(get("camera_index", "0") or 0)}


def models_root() -> Path:
    override = os.environ.get("AGNI_FACE_MODELS_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    try:
        from voice.offline import resolve_models_dir
        return resolve_models_dir() / "insightface"
    except Exception:
        return ROOT / "models" / "insightface"


def pack_dir() -> Optional[Path]:
    """The model pack to use: the configured one, else the most accurate present."""
    root = models_root() / "models"
    wanted = config()["model_pack"]
    names = [wanted] if wanted != "auto" else list(PACK_PREFERENCE)
    for name in names:
        folder = root / name
        if folder.is_dir() and any(folder.glob("*.onnx")):
            return folder
    return None


def liveness_model() -> Optional[Path]:
    path = models_root() / "addons" / "liveness.onnx"
    return path if path.is_file() else None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _import_insightface():
    """The vendored InsightFace source (from the repository the operator
    supplied), unless an installed copy is already importable."""
    import importlib.util
    if importlib.util.find_spec("insightface") is None and str(VENDORED) not in sys.path:
        sys.path.insert(0, str(VENDORED))
    os.environ.setdefault("ORT_DISABLE_TELEMETRY", "1")
    from insightface.app import FaceAnalysis
    return FaceAnalysis


# --------------------------------------------------------------- engine ----

class _Engine:
    """One FaceAnalysis instance, built on first use."""

    def __init__(self):
        self._lock = threading.Lock()
        self._app = None
        self._key = None
        self.pack: Optional[str] = None
        self.liveness_loaded = False

    def get(self):
        cfg = config()
        pack = pack_dir()
        if pack is None:
            raise FaceError("No face model is installed. Run STEP 1 : INSTALL once with "
                            "internet access to download it.")
        live = liveness_model() if cfg["liveness"] != "off" else None
        if cfg["liveness"] == "required" and live is None:
            raise FaceError("The face liveness model is missing, and config.ini requires "
                            "it. Run STEP 1 : INSTALL, or set [face] liveness = off.")
        if live is not None and _sha256(live) != LIVENESS_SHA256:
            raise FaceError("The liveness model does not match its published SHA-256 - "
                            "refusing to use a file that may have been altered.")
        key = (str(pack), str(live), cfg["liveness_threshold"])
        with self._lock:
            if self._app is not None and self._key == key:
                return self._app
            FaceAnalysis = _import_insightface()
            kwargs = {"allowed_modules": ["detection", "recognition"],
                      "providers": ["CPUExecutionProvider"]}
            if live is not None:
                kwargs.update(addons=["liveness"], liveness_mode="observe",
                              liveness_threshold=cfg["liveness_threshold"])
            app = FaceAnalysis(name=str(pack), root=str(models_root()), **kwargs)
            app.prepare(ctx_id=-1, det_size=(640, 640))
            if "recognition" not in app.models:
                raise FaceError(f"The model pack {pack.name} has no recognition model.")
            self._app, self._key = app, key
            self.pack, self.liveness_loaded = pack.name, live is not None
            logger.info(f"face engine ready: {pack.name}, liveness "
                        f"{'on' if live is not None else 'off'}")
            return app

    def analyze(self, image) -> List[FaceObservation]:
        import numpy as np
        app = self.get()
        observations = []
        for face in app.get(image):
            embedding = face.normed_embedding if face.embedding is not None else None
            live = face.liveness
            observations.append(FaceObservation(
                bbox=[float(v) for v in face.bbox],
                det_score=float(face.det_score),
                embedding=None if embedding is None else np.asarray(embedding, dtype="float32"),
                live=None if live is None else live.get("is_live"),
                live_score=None if live is None else live.get("live_score"),
                liveness_status="not_run" if live is None else str(live.get("status"))))
        return observations


engine = _Engine()


# ---------------------------------------------------------------- images ----

def load_image(path: str):
    """BGR image from a file. np.fromfile keeps non-ASCII Windows paths working."""
    import cv2
    import numpy as np
    data = np.fromfile(str(path), dtype=np.uint8)
    image = cv2.imdecode(data, cv2.IMREAD_COLOR) if data.size else None
    if image is None:
        raise FaceError(f"Could not read an image from {path}")
    return image


def capture_frames(count: int = 5, camera_index: Optional[int] = None,
                   interval: float = 0.25, warmup: int = 8) -> list:
    """Frames from the local camera. The camera is released before returning,
    whatever happens, so its light never stays on."""
    import cv2
    index = config()["camera_index"] if camera_index is None else camera_index
    backend = cv2.CAP_DSHOW if sys.platform == "win32" else cv2.CAP_ANY
    capture = cv2.VideoCapture(index, backend)
    frames = []
    try:
        if not capture.isOpened():
            raise FaceError(f"The camera (index {index}) could not be opened.")
        for _ in range(warmup):                 # exposure settles over a few frames
            capture.read()
        for _ in range(count):
            ok, frame = capture.read()
            if ok and frame is not None:
                frames.append(frame)
            time.sleep(interval)
    finally:
        capture.release()
    if not frames:
        raise FaceError("The camera returned no pictures.")
    return frames


# ------------------------------------------------------------- templates ----

def _connect():
    from memory.database import get_connection
    conn = get_connection()
    conn.execute("""CREATE TABLE IF NOT EXISTS face_templates (
                        user_id INTEGER PRIMARY KEY,
                        template BLOB NOT NULL,
                        model TEXT, dims INTEGER, frames INTEGER,
                        created_at REAL, updated_at REAL)""")
    return conn


def _protect(raw: bytes, protect: bool) -> bytes:
    from security.audit_chain import _dpapi
    out = _dpapi(raw, protect)
    if out is None:
        raise FaceError("Windows data protection (DPAPI) is unavailable, so a face "
                        "template cannot be stored or read safely.")
    return out


def has_template(user_id: int) -> bool:
    try:
        conn = _connect()
        try:
            return conn.execute("SELECT 1 FROM face_templates WHERE user_id = ?",
                                (int(user_id),)).fetchone() is not None
        finally:
            conn.close()
    except Exception:
        return False


def enrolled_user_ids() -> List[int]:
    conn = _connect()
    try:
        return [row[0] for row in conn.execute("SELECT user_id FROM face_templates")]
    finally:
        conn.close()


def _load_template(user_id: int):
    import numpy as np
    conn = _connect()
    try:
        row = conn.execute("SELECT template, dims FROM face_templates WHERE user_id = ?",
                           (int(user_id),)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise FaceError("No face is enrolled for this account.")
    vector = np.frombuffer(_protect(bytes(row[0]), False), dtype="float32")
    if vector.size != (row[1] or TEMPLATE_DIMS):
        raise FaceError("The stored face template is damaged - enrol again.")
    return vector


def login_required_for(user_id: int) -> bool:
    """Does this account have to pass a face check after its password?

    Fails CLOSED: if face login is on and the template table cannot be read,
    the answer is yes (the check will then fail and say why) rather than
    silently letting the password alone through."""
    cfg = config()
    if not (cfg["enabled"] and cfg["login"] == "second_factor"):
        return False
    try:
        conn = _connect()
        try:
            return conn.execute("SELECT 1 FROM face_templates WHERE user_id = ?",
                                (int(user_id),)).fetchone() is not None
        finally:
            conn.close()
    except Exception as error:
        logger.error(f"face template lookup failed - requiring the face check: {error}")
        return True


# ------------------------------------------------------------ decisions ----

def _usable(observations: List[FaceObservation], cfg: dict, why: list):
    """The one face in a frame, or None with the reason appended."""
    if not observations:
        why.append("no face")
        return None
    if len(observations) > 1:
        why.append("more than one face")
        return None
    face = observations[0]
    if face.det_score < 0.6 or face.embedding is None:
        why.append("face not clear enough")
        return None
    if cfg["liveness"] == "required" and face.live is not True:
        why.append("liveness check failed" if face.liveness_status == "ok"
                   else f"liveness not evaluated ({face.liveness_status})")
        return None
    return face


def _audit(event: str, username: str, detail: str, severity: str = "info"):
    try:
        from memory.database import log_auth_event
        log_auth_event(event, username, detail)
    except Exception as error:
        logger.warning(f"could not audit {event}: {error}")
    if severity != "info":
        try:
            from security import events
            events.record(event, f"{username}: {detail}", severity=severity)
        except Exception:
            pass


def enroll(user_id: int, username: str, frames: list, by: str = "") -> Dict[str, Any]:
    import numpy as np
    cfg = config()
    if not cfg["enabled"]:
        raise FaceError("Face recognition is switched off in config.ini ([face] enabled).")
    reasons: list = []
    usable = [f for f in (_usable(engine.analyze(frame), cfg, reasons) for frame in frames) if f]
    if len(usable) < MIN_ENROL_FRAMES:
        summary = ", ".join(sorted(set(reasons))) or "too few frames"
        raise FaceError(f"Only {len(usable)} of {len(frames)} pictures were usable "
                        f"({summary}). Face the camera alone, in good light, and try again.")
    stack = np.stack([f.embedding for f in usable])
    template = stack.mean(axis=0)
    template = (template / np.linalg.norm(template)).astype("float32")
    consistency = float(min(stack @ template))
    if consistency < 0.5:
        raise FaceError("The pictures do not agree with each other (were two people "
                        "in view?). Enrolment was not saved.")
    shared_with = _accounts_with_this_face(user_id, template, cfg)
    blob = _protect(template.tobytes(), True)
    now = time.time()
    conn = _connect()
    try:
        conn.execute("""INSERT INTO face_templates (user_id, template, model, dims, frames,
                                                    created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(user_id) DO UPDATE SET template = excluded.template,
                          model = excluded.model, dims = excluded.dims,
                          frames = excluded.frames, updated_at = excluded.updated_at""",
                     (int(user_id), blob, engine.pack, int(template.size), len(usable), now, now))
        conn.commit()
    finally:
        conn.close()
    _audit("face_enrolled", username, f"frames={len(usable)} model={engine.pack} "
                                      f"liveness={'on' if engine.liveness_loaded else 'off'} by={by or username}"
                                      + (f" same_face_as={','.join(shared_with)}" if shared_with else ""))
    result = {"enrolled": True, "user": username, "frames_used": len(usable),
              "frames_offered": len(frames), "model": engine.pack,
              "liveness": "checked" if engine.liveness_loaded else "not checked",
              "consistency": round(consistency, 3)}
    if shared_with:
        result["warning"] = (f"This face is also enrolled on {', '.join(shared_with)}. Face "
                             f"sign-in cannot tell accounts with the same face apart and "
                             f"will refuse them - remove the face from all but one account.")
    return result


def _accounts_with_this_face(user_id: int, template, cfg: dict) -> List[str]:
    """Other accounts whose enrolled face is this same person. One person on
    two accounts (an admin and an everyday account, say) makes face-only
    sign-in ambiguous, so enrolment says so instead of leaving a mystery."""
    names = []
    try:
        others = [uid for uid in enrolled_user_ids() if uid != int(user_id)]
    except Exception:
        return names
    for other in others:
        try:
            if float(_load_template(other) @ template) >= cfg["threshold"]:
                from memory.database import get_user_by_id
                names.append((get_user_by_id(other) or {}).get("username", f"account {other}"))
        except Exception:
            continue
    return names


def verify(user_id: int, username: str, frames: list, purpose: str = "check") -> Dict[str, Any]:
    cfg = config()
    template = _load_template(user_id)
    reasons: list = []
    best = -1.0
    for frame in frames:
        face = _usable(engine.analyze(frame), cfg, reasons)
        if face is not None:
            best = max(best, float(face.embedding @ template))
    ok = best >= cfg["threshold"]
    detail = (f"purpose={purpose} similarity={best:.3f} threshold={cfg['threshold']:.2f} "
              f"model={engine.pack} liveness={'on' if engine.liveness_loaded else 'off'}")
    if not ok and reasons and best < 0:
        detail += f" reasons={','.join(sorted(set(reasons)))}"
    _audit("face_verified" if ok else "face_rejected", username, detail,
           severity="info" if ok else "warning")
    if ok:
        reason = ""
    elif best < 0:
        reason = ", ".join(sorted(set(reasons))) or "no usable face"
    else:
        reason = "face does not match"
    return {"ok": ok, "similarity": round(best, 3) if best >= 0 else None,
            "threshold": cfg["threshold"], "reason": reason,
            "liveness": "checked" if engine.liveness_loaded else "not checked"}


def face_sign_in_available() -> Dict[str, Any]:
    """Can the access screen offer "sign in with face" right now?

    Filesystem and database checks only - no model is loaded and no camera
    is opened, so the screen can ask this while it is being built."""
    cfg = config()
    if not cfg["enabled"] or cfg["login"] != "face_or_password":
        return {"available": False, "reason": "face sign-in is off ([face] login)"}
    if cfg["liveness"] != "required":
        return {"available": False,
                "reason": "face sign-in needs [face] liveness = required"}
    if pack_dir() is None or liveness_model() is None:
        return {"available": False, "reason": "the face models are not installed"}
    try:
        enrolled = enrolled_user_ids()
    except Exception as error:
        return {"available": False, "reason": f"face templates unreadable: {error}"}
    if not enrolled:
        return {"available": False, "reason": "no face is enrolled yet"}
    return {"available": True, "reason": "", "enrolled": len(enrolled)}


def identify(frames: list) -> Dict[str, Any]:
    """Which enrolled account is in front of the camera - or nobody.

    The face-only sign-in decision. Stricter than verify(), because here the
    face is the whole credential:

      * liveness must be `required` (a config that weakens it refuses);
      * every usable frame is compared with EVERY enrolled template, and a
        frame only votes for an account whose score reaches login_threshold
        AND beats the next account by login_margin;
      * the winner needs at least two votes and a majority of usable frames.

    Returns {"ok", "user_id", "similarity", "reason", "frames_used"}. Every
    decision is audited under the matched (or "unknown") name."""
    cfg = config()
    if cfg["liveness"] != "required":
        raise FaceError("Face sign-in needs [face] liveness = required in config.ini.")
    templates = {}
    for user_id in enrolled_user_ids():
        try:
            templates[user_id] = _load_template(user_id)
        except FaceError as error:
            logger.warning(f"face template for account {user_id} skipped: {error}")
    if not templates:
        return {"ok": False, "user_id": None, "similarity": None, "frames_used": 0,
                "reason": "no face is enrolled"}

    reasons: list = []
    votes: Dict[int, int] = {}
    best: Dict[int, float] = {}
    usable = 0
    ambiguous = 0                       # frames that matched two accounts equally well
    for frame in frames:
        face = _usable(engine.analyze(frame), cfg, reasons)
        if face is None:
            continue
        usable += 1
        ranked = sorted(((float(face.embedding @ template), user_id)
                         for user_id, template in templates.items()), reverse=True)
        top_score, top_user = ranked[0]
        runner_up = ranked[1][0] if len(ranked) > 1 else -1.0
        best[top_user] = max(best.get(top_user, -1.0), top_score)
        if top_score >= cfg["login_threshold"]:
            if top_score - runner_up >= cfg["login_margin"]:
                votes[top_user] = votes.get(top_user, 0) + 1
            elif runner_up >= cfg["login_threshold"]:
                ambiguous += 1

    winner, count = max(votes.items(), key=lambda item: item[1]) if votes else (None, 0)
    ok = winner is not None and count >= 2 and count * 2 > usable
    # Nobody in front of the camera at all: not an attempt on any account.
    # (A photo that fails liveness, a second face, a stranger - those are.)
    no_face = usable == 0 and bool(reasons) and set(reasons) == {"no face"}
    # The same person enrolled on two accounts: a set-up problem, not an attack.
    same_face = not ok and ambiguous >= 2 and ambiguous * 2 > usable
    if ok:
        reason = ""
    elif same_face:
        reason = "matches more than one account"
    elif usable == 0:
        reason = ", ".join(sorted(set(reasons))) or "no usable face"
    elif usable < 2:
        reason = "hold still and look at the camera"
    else:
        reason = "face not recognised"
    similarity = round(best[winner], 3) if ok else (round(max(best.values()), 3) if best else None)

    name = "unknown"
    if ok:
        try:
            from memory.database import get_user_by_id
            name = (get_user_by_id(winner) or {}).get("username", str(winner))
        except Exception:
            name = str(winner)
    _audit("face_login_matched" if ok else "face_login_rejected", name,
           f"similarity={similarity} votes={count}/{usable} threshold={cfg['login_threshold']:.2f} "
           f"margin={cfg['login_margin']:.2f} model={engine.pack}"
           + ("" if ok else f" reason={reason}"),
           severity="info" if ok else "warning")
    return {"ok": ok, "user_id": winner if ok else None, "similarity": similarity,
            "reason": reason, "frames_used": usable, "no_face": no_face,
            "same_face_on_two_accounts": same_face}


def remove(user_id: int, username: str, by: str = "") -> Dict[str, Any]:
    conn = _connect()
    try:
        removed = conn.execute("DELETE FROM face_templates WHERE user_id = ?",
                               (int(user_id),)).rowcount
        conn.commit()
    finally:
        conn.close()
    if removed:
        _audit("face_removed", username, f"by={by or username}")
    return {"removed": bool(removed), "user": username}


def status() -> Dict[str, Any]:
    cfg = config()
    pack = pack_dir()
    live = liveness_model()
    try:
        library = bool(_import_insightface())
        library_error = ""
    except Exception as error:
        library, library_error = False, str(error)
    verified_file = models_root() / "agni_verified.json"
    try:
        verified = json.loads(verified_file.read_text(encoding="utf-8")) if verified_file.is_file() else {}
    except Exception:
        verified = {}
    try:
        enrolled = len(enrolled_user_ids())
    except Exception:
        enrolled = 0
    return {"enabled": cfg["enabled"], "login": cfg["login"],
            "library": "InsightFace (vendored from third_party/insightface)" if library
                       else f"unavailable: {library_error}",
            "model_pack": pack.name if pack else None,
            "liveness_model": ("present, SHA-256 verified" if live and _sha256(live) == LIVENESS_SHA256
                               else "present, SHA-256 MISMATCH" if live else "missing"),
            "liveness_policy": cfg["liveness"], "threshold": cfg["threshold"],
            "enrolled_accounts": enrolled, "models_recorded_by_setup": verified,
            "login_threshold": cfg["login_threshold"],
            "note": {"off": "Passwords only.",
                     "second_factor": "Face is a SECOND factor after the password.",
                     "face_or_password": "The access screen offers the password OR the face "
                                         "alone (live face, strict match, 3 tries).",
                     }[cfg["login"]] + " RGB liveness reduces photo/screen replay; it does "
                                       "not make spoofing impossible."}
