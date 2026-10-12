"""
Face login (InsightFace) - every state tested and reported separately.

    MODEL_PRESENT -> CHECKSUM_VALID -> MODEL_LOADED -> MODEL_FUNCTIONAL
    CAMERA_AVAILABLE -> FRAME_RECEIVED -> FACE_DETECTED -> QUALITY -> LIVENESS_TESTED
    -> IDENTITY_MATCHED -> RBAC -> ACCESS DECISION

"No face detected" is not evidence that a model is broken: the setup run of
2026-09-24 printed "the test detection found no face" when its probe
subprocess had in fact FAILED (the machine was out of memory) and the failure
was read as zero faces. Here the model is proven on InsightFace's own sample
photograph first; an empty camera frame is then INPUT_REQUIRED.

The access rules are checked against the REAL decision code
(security/face_auth._usable and the verify threshold): a different person is
DENIED, a face that fails liveness is DENIED. There is no bypass switch.

Runs in a subprocess (`python -m agni_ops.face --probe`), so a native crash in
ONNX Runtime is a reported result, not the end of the test. Camera frames are
analysed in memory and never written anywhere.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Dict, List

from agni_ops.status import (FAIL, INPUT_REQUIRED, NOT_TESTED, PASS, UNAVAILABLE, WARN, Result)

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "third_party" / "insightface" / "insightface" / "data" / "images" / "t1.jpg"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _probe(camera: bool, user_id: int = 0) -> Dict:
    """Runs INSIDE the subprocess. Returns plain data only."""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from security import face_auth as fa
    out: Dict = {"states": {}}
    cfg = fa.config()
    out["config"] = {k: cfg[k] for k in ("enabled", "login", "liveness", "threshold", "liveness_threshold")}

    pack, live = fa.pack_dir(), fa.liveness_model()
    out["states"]["MODEL_PRESENT"] = bool(pack) and bool(live)
    out["pack"] = pack.name if pack else None
    if not pack:
        out["error"] = "no InsightFace model pack in models/insightface/models"
        return out

    # CHECKSUM: the liveness model has a published SHA-256; the pack files are
    # pinned from the SHA-256-verified release archive on first check and
    # compared on every check after that (tamper detection).
    record_path = fa.models_root() / "agni_verified.json"
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except Exception:
        record = {}
    files = {p.name: _sha256(p) for p in sorted(pack.glob("*.onnx"))}
    pinned = record.get("pinned_files")
    changed = [n for n, h in files.items() if pinned and pinned.get(n) not in (None, h)]
    if not pinned:
        record["pinned_files"] = files
        try:
            record_path.write_text(json.dumps(record, indent=2), encoding="utf-8")
        except OSError:
            pass
    live_ok = bool(live) and _sha256(live) == fa.LIVENESS_SHA256
    out["states"]["CHECKSUM_VALID"] = live_ok and not changed
    out["checksum"] = {"liveness_published_sha256": live_ok, "pack_files_changed": changed,
                       "pack_files_pinned_now": not pinned}

    try:
        fa.engine.get()
        out["states"]["MODEL_LOADED"] = True
    except Exception as error:
        out["states"]["MODEL_LOADED"] = False
        out["error"] = f"{type(error).__name__}: {error}"
        return out

    # MODEL_FUNCTIONAL on a known photograph.
    sample = fa.load_image(str(SAMPLE))
    faces = fa.engine.analyze(sample)
    out["sample_faces"] = len(faces)
    out["states"]["MODEL_FUNCTIONAL"] = len(faces) > 0 and all(f.embedding is not None for f in faces)
    out["sample_liveness"] = [f.liveness_status for f in faces][:6]

    # DECISION RULES on real embeddings (in memory, nothing stored):
    if len(faces) >= 2:
        a, b = faces[0], faces[1]
        same = float(a.embedding @ a.embedding)
        other = float(a.embedding @ b.embedding)
        out["decision"] = {
            "same_face_similarity": round(same, 3), "other_person_similarity": round(other, 3),
            "threshold": cfg["threshold"],
            "other_person_denied": other < cfg["threshold"],
        }
        # A face that FAILS liveness must be unusable, whatever it matches.
        spoof = fa.FaceObservation(bbox=a.bbox, det_score=a.det_score, embedding=a.embedding,
                                   live=False, live_score=0.01, liveness_status="ok")
        reasons: List[str] = []
        required = dict(cfg, liveness="required")
        out["decision"]["failed_liveness_denied"] = fa._usable([spoof], required, reasons) is None
        out["decision"]["failed_liveness_reason"] = ", ".join(reasons)
        crowd: List[str] = []
        out["decision"]["two_faces_denied"] = fa._usable([a, b], required, crowd) is None
        empty: List[str] = []
        out["decision"]["no_face_denied"] = fa._usable([], required, empty) is None

    if not camera:
        return out
    # CAMERA - frames analysed in memory only.
    try:
        frames = fa.capture_frames(count=5)
        out["states"]["CAMERA_AVAILABLE"] = True
        out["states"]["FRAME_RECEIVED"] = len(frames) > 0
    except Exception as error:
        out["states"]["CAMERA_AVAILABLE"] = False
        out["camera_error"] = str(error)
        return out
    observations = [fa.engine.analyze(frame) for frame in frames]
    counts = [len(o) for o in observations]
    out["camera_face_counts"] = counts
    out["states"]["FACE_DETECTED"] = any(c > 0 for c in counts)
    usable_reasons: List[str] = []
    usable = [fa._usable(o, cfg, usable_reasons) for o in observations]
    out["camera_usable_frames"] = sum(1 for u in usable if u is not None)
    out["camera_rejections"] = sorted(set(usable_reasons))
    singles = [o[0] for o in observations if len(o) == 1]
    out["states"]["LIVENESS_TESTED"] = any(f.liveness_status == "ok" for f in singles)
    out["camera_liveness"] = [(f.liveness_status, f.live, round(f.live_score or 0, 3)) for f in singles][:5]
    if user_id and fa.has_template(user_id):
        template = fa._load_template(user_id)
        best = max((float(f.embedding @ template) for f in (u for u in usable if u is not None)), default=-1.0)
        out["identity"] = {"user_id": user_id, "best_similarity": round(best, 3),
                           "matched": best >= cfg["threshold"]}
        out["states"]["IDENTITY_MATCHED"] = best >= cfg["threshold"]
    else:
        out["identity"] = {"enrolled_accounts": len(fa.enrolled_user_ids())}
    return out


def suite(camera: bool = True, user_id: int = 0, timeout: float = 600) -> List[Result]:
    group = "face"
    try:
        run = subprocess.run([sys.executable, "-m", "agni_ops.face", "--probe",
                              json.dumps({"camera": camera, "user_id": user_id})],
                             capture_output=True, text=True, timeout=timeout, cwd=str(ROOT),
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return [Result("face engine", FAIL, group=group, reason=f"probe did not finish in {timeout} s")]
    lines = [l for l in (run.stdout or "").splitlines() if l.startswith("{")]
    if run.returncode != 0 or not lines:
        from voice.stt_manager import classify_failure
        kind, why = classify_failure(run.returncode, run.stderr or "")
        return [Result("face engine", FAIL, group=group,
                       reason=f"the face probe process failed: {why}",
                       evidence=(run.stderr or "")[-1500:], where="agni_ops.face --probe")]
    data = json.loads(lines[-1])
    states = data.get("states", {})
    results: List[Result] = []

    def add(name, ok, good, bad, status_if_bad=FAIL, **extra):
        results.append(Result(name, PASS if ok else status_if_bad, group=group,
                              reason=good if ok else bad, **extra))

    add("MODEL_PRESENT", states.get("MODEL_PRESENT"), f"{data.get('pack')} + liveness model",
        data.get("error", "model files missing"), UNAVAILABLE)
    if not states.get("MODEL_PRESENT"):
        return results
    checksum = data.get("checksum", {})
    add("CHECKSUM_VALID", states.get("CHECKSUM_VALID"),
        "liveness matches its published SHA-256; pack files "
        + ("pinned now (from the SHA-256-verified release archive)" if checksum.get("pack_files_pinned_now")
           else "match the pinned SHA-256"),
        f"checksum problem: {checksum}")
    add("MODEL_LOADED", states.get("MODEL_LOADED"), "FaceAnalysis prepared (CPU)",
        data.get("error", "did not load"))
    if not states.get("MODEL_LOADED"):
        return results
    add("MODEL_FUNCTIONAL (sample photo)", states.get("MODEL_FUNCTIONAL"),
        f"detected {data.get('sample_faces')} faces with embeddings in InsightFace's sample",
        "no face found in the reference sample - the detector is not working")
    decision = data.get("decision") or {}
    if decision:
        add("DENY: different person", decision.get("other_person_denied"),
            f"similarity {decision.get('other_person_similarity')} < threshold {decision.get('threshold')} "
            f"(same face: {decision.get('same_face_similarity')})",
            f"a different person scored {decision.get('other_person_similarity')} >= threshold")
        add("DENY: failed liveness", decision.get("failed_liveness_denied"),
            f"rejected ({decision.get('failed_liveness_reason')})", "a non-live face was accepted")
        add("DENY: two faces / no face", decision.get("two_faces_denied") and decision.get("no_face_denied"),
            "both rejected", "accepted an ambiguous frame")
    if not camera:
        results.append(Result("CAMERA", NOT_TESTED, group=group, reason="--no-camera"))
        return results
    add("CAMERA_AVAILABLE", states.get("CAMERA_AVAILABLE"), "camera opened",
        f"{data.get('camera_error', 'camera not available')} - secure fallback: password login only "
        f"(face is a second factor, never a replacement)", UNAVAILABLE)
    if not states.get("CAMERA_AVAILABLE"):
        return results
    add("FRAME_RECEIVED", states.get("FRAME_RECEIVED"), "frames received", "camera returned no frames")
    if states.get("FACE_DETECTED"):
        results.append(Result("FACE_DETECTED", PASS, group=group,
                              reason=f"faces per frame {data.get('camera_face_counts')}; usable "
                                     f"{data.get('camera_usable_frames')}/5"
                                     + (f"; rejected: {', '.join(data['camera_rejections'])}"
                                        if data.get("camera_rejections") else "")))
        results.append(Result("LIVENESS_TESTED", PASS if states.get("LIVENESS_TESTED") else WARN, group=group,
                              reason=f"(status, live, score): {data.get('camera_liveness')}"))
    else:
        results.append(Result("FACE_DETECTED", INPUT_REQUIRED, group=group,
                              reason=f"NO FACE DETECTED in the camera frames {data.get('camera_face_counts')} - "
                                     "sit in front of the camera and run TEST_AGNI.bat face again "
                                     "(the model itself is verified above)"))
    identity = data.get("identity") or {}
    if "matched" in identity:
        results.append(Result("IDENTITY_MATCHED", PASS if identity["matched"] else FAIL, group=group,
                              reason=f"best similarity {identity['best_similarity']} for account "
                                     f"{identity['user_id']} -> ACCESS "
                                     + ("ALLOW (with password)" if identity["matched"] else "DENY")))
    else:
        results.append(Result("IDENTITY_MATCHED", NOT_TESTED, group=group,
                              reason=f"no --user given or not enrolled ({identity.get('enrolled_accounts', 0)} "
                                     f"enrolled account(s)); enrol in ZEUS: Security > Face login"))
    return results


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--probe":
        arguments = json.loads(sys.argv[2])
        import os
        if os.name == "nt":
            import ctypes
            ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002 | 0x8000)
        print(json.dumps(_probe(bool(arguments.get("camera")), int(arguments.get("user_id") or 0)),
                         default=str))
