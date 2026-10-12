"""
Intelligent data capture: evidence in, structured and linked events out.

    FILE / IMAGE / VOICE MEMO / TYPED OR SPOKEN NOTE
      -> evidence (content-addressed copy, SHA-256, who / which device / when)
      -> text: document pipeline + OCR (ZEUS's industrial pipeline), local
         Whisper for voice, the vision model's description kept as INFERENCE
      -> vernacular reading (original kept) [+ multilingual model when unsure]
      -> extraction (rules [+ EXTRACTION model, verified against the text])
      -> activity resolution with confidence
      -> field observations + SUBMITTED state events (approval comes later)
      -> correspondence, notices, chronology, search index, audit

Every step's output is stored with its method and model, so any conclusion
can be traced back to the file it came from. Nothing is overwritten.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import mimetypes
import re
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from jarvis import dates, events, extraction, linking, search, store, vernacular
from core.logger import get_logger

logger = get_logger("jarvis.capture")

IMAGE = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}
AUDIO = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".webm", ".mp4", ".aac", ".amr"}
SHEET = {".xlsx", ".xlsm", ".csv"}
DOCS = {".pdf", ".docx", ".pptx", ".txt", ".md", ".rtf", ".html", ".htm", ".eml"}

_EVENT_FOR_KIND = {"completed": "activity_completed", "started": "activity_started", "progress": "progress_update",
                   "blocker": "blocker_reported", "blocker_cleared": "blocker_cleared", "delay": "delay_reported",
                   "inspection": "inspection_completed"}


def kind_of(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in IMAGE:
        return "image"
    if ext in AUDIO:
        return "voice"
    if ext in SHEET:
        return "spreadsheet"
    return "document"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _keep_copy(project_id: str, path: Path, sha: str) -> Path:
    """A content-addressed copy inside ZEUS's protected data folder: the
    original evidence survives whatever happens to the file it came from."""
    target = store.evidence_root() / project_id / sha[:2] / f"{sha}{path.suffix.lower()}"
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    return target


# ------------------------------------------------------------ text sources --

def _document_text(path: Path) -> Dict[str, Any]:
    from industrial.doc_pipeline import ExtractionOptions, extract_document
    doc = extract_document(path, ExtractionOptions(ocr=True, layout=False, images=False, vision=False,
                                                   save_assets=False, max_pages=60))
    return {"text": doc.text, "pages": doc.page_count, "methods": doc.extraction_methods,
            "ocr_confidence": doc.mean_ocr_confidence(), "error": doc.error, "warnings": doc.warnings,
            "metadata": doc.raw_metadata}


def _image_text(path: Path) -> Dict[str, Any]:
    info: Dict[str, Any] = {"text": "", "engine": None, "confidence": None, "width": None, "height": None,
                            "exif_datetime": None}
    try:
        from PIL import Image
        image = Image.open(path)
        image.load()
        info["width"], info["height"] = image.size
        exif = image.getexif() if hasattr(image, "getexif") else {}
        stamp = exif.get(36867) or exif.get(306) if exif else None
        if stamp:
            info["exif_datetime"] = str(stamp).replace(":", "-", 2).replace(" ", "T")
    except Exception as error:
        info["error"] = f"could not open the image: {error}"
        return info
    try:
        from industrial.ocr_engines import run_ocr
        result = run_ocr(image)
        info["text"] = result.text if result.ok else ""
        info["engine"] = result.engine
        info["confidence"] = round(result.confidence, 4) if result.confidence else None
        if not result.ok:
            info["error"] = result.error or "no text found"
    except Exception as error:
        info["error"] = f"OCR unavailable: {error}"
    return info


def _vision_description(path: Path) -> Dict[str, Any]:
    """What the local vision model sees in a site photo - stored as INFERENCE."""
    from jarvis import models
    route = models.route("image_evidence")
    if not route.get("usable"):
        return {"text": "", "model": None, "reason": route.get("reason")}
    try:
        import base64
        import io
        from PIL import Image
        from core.ollama_client import ollama_client
        image = Image.open(path).convert("RGB")
        image.thumbnail((1280, 1280))
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=85)
        reply = ollama_client.chat_vision(
            route["model"], "This is a construction site photograph. Describe only what is visibly present: the "
                            "structure or element, the stage of work (for example excavation, reinforcement, "
                            "formwork, concreting, curing, erection), equipment, and any text or labels you can read "
                            "exactly. Do not guess what you cannot see.",
            base64.b64encode(buffer.getvalue()).decode("ascii"), timeout=240, purpose="ZEUS site photo")
        if reply and not reply.startswith("["):
            return {"text": reply.strip(), "model": route["model"]}
        return {"text": "", "model": route["model"], "reason": (reply or "")[:200]}
    except Exception as error:
        return {"text": "", "model": route.get("model"), "reason": str(error)}


def _transcribe(path: Path) -> Dict[str, Any]:
    """Local Whisper - never a cloud recogniser. Unavailable is said, not hidden."""
    try:
        from voice.providers import voice_manager
        provider = getattr(voice_manager, "input", None)
        stt = getattr(provider, "_stt", None) if provider is not None else None
        if stt is None or not hasattr(stt, "transcribe_file"):
            from voice.speech_to_text import SpeechToText
            stt = SpeechToText()
        if not getattr(stt, "is_available", False):
            return {"text": "", "status": "unavailable",
                    "detail": getattr(stt, "last_error", "") or "no local Whisper model is installed"}
        text = stt.transcribe_file(str(path))
        return {"text": text, "status": "transcribed" if text else "empty",
                "detail": getattr(stt, "last_error", "") or "", "engine": "faster-whisper (local)"}
    except Exception as error:
        return {"text": "", "status": "unavailable", "detail": str(error)}


# -------------------------------------------------------- correspondence --

_REF = re.compile(r"\b(?:ref(?:erence)?\.?\s*(?:no\.?)?|letter no\.?|our ref\.?)\s*[:#-]?\s*([A-Z0-9][A-Z0-9/\-_.]{3,40})",
                  re.I)
_SUBJECT = re.compile(r"^\s*(?:sub(?:ject)?|re)\s*[:.-]\s*(.+)$", re.I | re.M)
_FROM = re.compile(r"^\s*from\s*[:.-]\s*(.+)$", re.I | re.M)
_TO = re.compile(r"^\s*to\s*[:.-]\s*(.+)$", re.I | re.M)


def looks_like_letter(text: str) -> bool:
    low = (text or "").lower()
    return bool(_SUBJECT.search(text or "")) and ("dear" in low or _REF.search(text or "") is not None)


def parse_letter(text: str, reference: _dt.date) -> Dict[str, Any]:
    subject = _SUBJECT.search(text or "")
    ref = _REF.search(text or "")
    sender = _FROM.search(text or "")
    to = _TO.search(text or "")
    head = (text or "")[:600]
    found = dates.find_dates(head, reference, relative=False)
    return {"reference": ref.group(1).rstrip(".") if ref else None,
            "subject": subject.group(1).strip()[:300] if subject else None,
            "from_party": sender.group(1).strip()[:200] if sender else None,
            "to_party": to.group(1).strip()[:200] if to else None,
            "letter_date": found[0][0].isoformat() if found else None,
            "is_notice": bool(re.search(r"\bnotice\b|\bnotif(y|ied|ication)\b", text or "", re.I))}


# -------------------------------------------------------------- the pipeline --

def capture(project_id: str, *, text: str = "", path: Optional[str] = None, source_type: str = "field",
            occurred_at: Optional[str] = None, captured_at: Optional[str] = None, device: Optional[str] = None,
            actor: Optional[str] = None, title: str = "", kind: Optional[str] = None, origin: Optional[str] = None,
            use_model: bool = False, use_vision: bool = False, correspondence: Optional[bool] = None,
            use_embeddings: bool = False) -> Dict[str, Any]:
    started = time.time()
    with store.connection() as conn:
        project = conn.execute("SELECT * FROM jv_projects WHERE project_id = ?", (project_id,)).fetchone()
        if project is None:
            raise ValueError(f"unknown project {project_id}")
        origin = origin or project["origin"]
    captured = dates.iso_dt(captured_at) if captured_at else store.now_iso()
    reference = dates.from_iso(occurred_at) or dates.from_iso(captured) or dates.today()
    warnings: List[str] = []
    source_file = Path(path).expanduser() if path else None
    evidence_kind = kind or (kind_of(source_file) if source_file else "text")
    text_info: Dict[str, Any] = {}
    original_text = text or ""
    image_info: Dict[str, Any] = {}
    vision: Dict[str, Any] = {}
    voice_info: Dict[str, Any] = {}
    doc_info: Dict[str, Any] = {}

    if source_file is not None:
        if not source_file.is_file():
            raise FileNotFoundError(f"{source_file} does not exist")
        if evidence_kind == "image":
            image_info = _image_text(source_file)
            if image_info.get("error"):
                warnings.append(image_info["error"])
            if use_vision:
                vision = _vision_description(source_file)
                if not vision.get("text") and vision.get("reason"):
                    warnings.append(f"vision model: {vision['reason']}")
            # a signboard or a slate in a photo is one statement; its line breaks are layout
            extracted = " ".join((image_info.get("text") or "").split())
            original_text = "\n".join(x for x in (text, extracted) if x)
            if not occurred_at and image_info.get("exif_datetime"):
                reference = dates.from_iso(image_info["exif_datetime"]) or reference
        elif evidence_kind == "voice":
            voice_info = _transcribe(source_file)
            if voice_info["status"] != "transcribed":
                warnings.append(f"voice memo not transcribed: {voice_info.get('detail') or voice_info['status']}")
            original_text = "\n".join(x for x in (text, voice_info.get("text", "")) if x)
        else:
            doc_info = _document_text(source_file)
            if doc_info.get("error"):
                warnings.append(doc_info["error"])
            original_text = "\n".join(x for x in (text, doc_info.get("text", "")) if x)

    reading = vernacular.normalize(original_text)
    if use_model and reading.vernacular and reading.coverage < 0.85:
        reading = vernacular.model_translate(reading)
    basis = reading.translation or reading.normalized

    with store.connection() as conn:
        contractors = [r[0] for r in conn.execute("SELECT DISTINCT contractor FROM jv_activities WHERE project_id = ? "
                                                  "AND contractor IS NOT NULL AND contractor != ''", (project_id,))]
        candidates = linking.candidates_for(conn, project_id)
    codes = [c["code"] for c in candidates]
    rule_obs = extraction.extract_rules(original_text, basis, reference, codes, contractors)
    model_result: Dict[str, Any] = {"status": "not requested"}
    if use_model and original_text.strip():
        model_result = extraction.extract_model(original_text, basis, reference,
                                                [{"code": c["code"], "name": c["name"]} for c in candidates])
        if model_result.get("status") == "ok":
            rule_obs = extraction.merge(rule_obs, model_result)
        elif model_result.get("reason"):
            warnings.append(f"extraction model: {model_result['reason']}")

    is_letter = correspondence if correspondence is not None else looks_like_letter(original_text)
    if is_letter:
        evidence_kind = "correspondence" if evidence_kind in ("text", "document") else evidence_kind

    with store.connection() as conn:
        evidence_id = store.new_id("EVD")
        sha = size = mime = stored = None
        if source_file is not None:
            sha = _sha256(source_file)
            duplicate = conn.execute("SELECT evidence_id FROM jv_evidence WHERE project_id = ? AND sha256 = ?",
                                     (project_id, sha)).fetchone()
            if duplicate:
                warnings.append(f"this file was already captured as {duplicate['evidence_id']} - linked, not duplicated")
            stored = str(_keep_copy(project_id, source_file, sha))
            size = source_file.stat().st_size
            mime = mimetypes.guess_type(source_file.name)[0]
        else:
            sha = hashlib.sha256(original_text.encode("utf-8")).hexdigest()
        occurred = dates.iso_dt(occurred_at) if occurred_at else (
            f"{rule_obs[0].occurred_on}T00:00:00" if rule_obs and rule_obs[0].occurred_on else
            (image_info.get("exif_datetime") or captured))
        store.insert(conn, "jv_evidence", {
            "evidence_id": evidence_id, "project_id": project_id, "kind": evidence_kind,
            "title": title or (source_file.name if source_file else original_text[:80]),
            "filename": source_file.name if source_file else None, "stored_path": stored, "sha256": sha,
            "bytes": size, "mime": mime, "occurred_at": occurred, "captured_at": captured, "received_at": store.now(),
            "captured_by": actor if actor is not None else store.current_user(),
            "device_id": device or events.device_id(conn), "source_type": source_type,
            "original_text": original_text, "language": reading.language, "scripts": store.dumps(reading.scripts),
            "origin": origin, "metadata": store.dumps({"warnings": warnings, "normalization": reading.as_dict(),
                                                       "source_path": str(source_file) if source_file else None})})
        if evidence_kind == "image" or image_info:
            store.insert(conn, "jv_image_evidence", {
                "evidence_id": evidence_id, "width": image_info.get("width"), "height": image_info.get("height"),
                "exif_datetime": image_info.get("exif_datetime"), "ocr_text": image_info.get("text"),
                "ocr_engine": image_info.get("engine"), "ocr_confidence": image_info.get("confidence"),
                "vision_description": vision.get("text"), "vision_model": vision.get("model")})
        if voice_info:
            store.insert(conn, "jv_voice_evidence", {
                "evidence_id": evidence_id, "duration_s": None, "transcript": voice_info.get("text"),
                "stt_engine": voice_info.get("engine"), "status": voice_info["status"],
                "detail": voice_info.get("detail")})
        if doc_info or evidence_kind in ("document", "correspondence", "spreadsheet"):
            store.insert(conn, "jv_documents", {
                "evidence_id": evidence_id, "doc_type": _doc_type(original_text, evidence_kind),
                "title": title or (source_file.name if source_file else None),
                "doc_date": rule_obs[0].occurred_on if rule_obs else None, "author": None, "reference": None,
                "pages": doc_info.get("pages"), "extraction_methods": store.dumps(doc_info.get("methods") or []),
                "ocr_confidence": doc_info.get("ocr_confidence")})

        # every extraction step is its own record, with its method and model
        for method, model, output, status in (
                ("document_pipeline", None, {k: v for k, v in doc_info.items() if k != "text"}, "ok" if doc_info and not doc_info.get("error") else None),
                ("ocr", image_info.get("engine"), {"text": image_info.get("text"), "confidence": image_info.get("confidence")},
                 "ok" if image_info.get("text") else ("failed" if image_info else None)),
                ("vision", vision.get("model"), {"description": vision.get("text"), "note": "AI inference, not a measurement"},
                 "ok" if vision.get("text") else ("unavailable" if use_vision and evidence_kind == "image" else None)),
                ("stt", voice_info.get("engine"), {"transcript": voice_info.get("text"), "detail": voice_info.get("detail")},
                 voice_info.get("status")),
                ("vernacular", reading.model or None, reading.as_dict(), "ok"),
                ("rules", None, {"observations": [o.as_dict() for o in rule_obs if o.method != "model"]}, "ok"),
                ("model", model_result.get("model"), model_result, model_result.get("status")
                 if use_model else None)):
            if status is None:
                continue
            store.insert(conn, "jv_extractions", {
                "extraction_id": store.new_id("EXT"), "evidence_id": evidence_id, "project_id": project_id,
                "method": method, "model": model, "created_at": store.now(),
                "duration_ms": (output or {}).get("duration_ms") if isinstance(output, dict) else None,
                "status": status, "confidence": reading.confidence if method == "vernacular" else None,
                "output": store.dumps(output), "error": None})

        events.append(conn, project_id, "evidence_captured", occurred_at=occurred, captured_at=captured, device=device,
                      actor=actor, source=source_type, evidence_ids=[evidence_id], origin=origin, base_head=None,
                      payload={"kind": evidence_kind, "title": title or (source_file.name if source_file else ""),
                               "sha256": sha, "language": reading.language, "note": original_text[:200]})

        observations = []
        created_events = []
        seen: Dict[tuple, List[Dict[str, Any]]] = {}
        for obs in rule_obs:
            link = linking.resolve(conn, project_id, obs, reference, use_embeddings=use_embeddings)
            observation_id = store.new_id("OBS")
            obs_occurred = f"{obs.occurred_on}T00:00:00" if obs.occurred_on else occurred
            store.insert(conn, "jv_field_observations", {
                "observation_id": observation_id, "project_id": project_id, "evidence_id": evidence_id,
                "extraction_id": None, "observed_at": obs_occurred, "original_text": obs.text,
                "normalized_text": obs.reading, "language": reading.language,
                "activity_ref": ", ".join(obs.refs) or None, "activity_id": link["activity_id"],
                "link_confidence": link["confidence"], "link_status": link["status"],
                "link_reasons": store.dumps({"reason": link["reason"], "candidates": link["candidates"]}),
                "kind": obs.kind, "percent_complete": obs.percent_complete, "quantity": obs.quantity, "unit": obs.unit,
                "status_word": obs.kind, "blocker_text": obs.reading if obs.kind in ("blocker", "delay") else None,
                "blocker_category": obs.blocker_category, "expected_date": obs.expected_date,
                "remaining_days": obs.remaining_days, "contractor": obs.contractor, "location_text": obs.location,
                "confidence": obs.confidence, "event_id": None, "created_at": store.now()})
            item = {"observation_id": observation_id, "kind": obs.kind, "text": obs.text, "reading": obs.reading,
                    "refs": obs.refs, "work_types": obs.work_types, "percent_complete": obs.percent_complete,
                    "quantity": obs.quantity, "unit": obs.unit, "occurred_on": obs.occurred_on,
                    "expected_date": obs.expected_date, "blocker_category": obs.blocker_category,
                    "method": obs.method, "confidence": obs.confidence, "link": link, "events": []}
            if link["status"] == "linked" and obs.kind != "note":
                key = (link["activity_id"], _EVENT_FOR_KIND.get(obs.kind))
                if is_letter:
                    # A letter is a party's statement, not a field observation: it is
                    # recorded as correspondence and linked, but it changes no state.
                    made = []
                elif key in seen:
                    made = seen[key]                  # the same fact twice in one record: one event
                    if made:
                        conn.execute("UPDATE jv_field_observations SET event_id = ? WHERE observation_id = ?",
                                     (made[0]["event_id"], observation_id))
                else:
                    made = observation_events(conn, project_id, observation_id, obs, link["activity_id"], evidence_id,
                                              obs_occurred, captured, device, actor, source_type, origin,
                                              link["confidence"])
                    seen[key] = made
                    created_events.extend(made)
                item["events"] = made
                store.insert(conn, "jv_links", {"project_id": project_id, "from_kind": "evidence", "from_id": evidence_id,
                                                "to_kind": "activity", "to_id": link["activity_id"],
                                                "relation": "evidences", "confidence": link["confidence"],
                                                "method": "linking", "created_by": store.current_user(),
                                                "created_at": store.now()}, or_ignore=True)
            observations.append(item)

        letter = None
        if is_letter:
            letter = record_letter(conn, project_id, evidence_id, original_text, reference, observations, occurred,
                                   captured, device, actor, source_type, origin)
        search.index_item(conn, f"evidence:{evidence_id}", "evidence", evidence_id, project_id, None, occurred, captured,
                          source_type, search.authority(source_type, evidence_kind), origin,
                          title or (source_file.name if source_file else "field note"),
                          "\n".join(x for x in (original_text, reading.normalized if reading.vernacular else "",
                                                reading.translation, vision.get("text", "")) if x))
        store.audit("capture", "evidence", evidence_id, project_id,
                    {"kind": evidence_kind, "observations": len(observations), "events": len(created_events),
                     "sha256": sha}, conn=conn)
    return {"ok": True, "evidence_id": evidence_id, "kind": evidence_kind, "sha256": sha,
            "captured_at": captured, "occurred_at": occurred, "language": reading.language,
            "original_text": original_text[:4000], "normalized_text": reading.normalized[:4000],
            "translation": reading.translation, "vernacular": reading.vernacular, "terms": reading.terms[:60],
            "coverage": reading.coverage, "ocr": {k: image_info.get(k) for k in ("text", "engine", "confidence")}
            if image_info else None, "vision": vision or None, "voice": voice_info or None,
            "observations": observations, "events": created_events, "correspondence": letter,
            "model_extraction": {k: model_result.get(k) for k in ("status", "model", "reason", "rejected")},
            "warnings": warnings, "duration_ms": int((time.time() - started) * 1000)}


def _doc_type(text: str, kind: str) -> str:
    low = (text or "").lower()
    if kind == "correspondence" or looks_like_letter(text):
        return "letter"
    for word, label in (("daily progress report", "dpr"), ("dpr", "dpr"), ("site diary", "site_diary"),
                        ("inspection", "inspection"), ("progress report", "progress_report"), ("hindrance", "hindrance_register"),
                        ("minutes", "minutes"), ("rfi", "rfi")):
        if word in low:
            return label
    return "document" if kind != "spreadsheet" else "spreadsheet"


def observation_events(conn: sqlite3.Connection, project_id: str, observation_id: str, obs, activity_id: str,
                       evidence_id: str, occurred: str, captured: str, device: Optional[str], actor: Optional[str],
                       source_type: str, origin: str, confidence: float) -> List[Dict[str, Any]]:
    """The submitted event(s) an observation implies, once it is linked."""
    made = []
    event_type = _EVENT_FOR_KIND.get(obs.kind)
    if not event_type:
        return made
    payload: Dict[str, Any] = {"note": obs.reading[:500], "original_text": obs.text[:500],
                               "observation_id": observation_id, "method": obs.method}
    if event_type == "progress_update":
        payload.update(percent_complete=obs.percent_complete, quantity_done=obs.quantity, unit=obs.unit)
    elif event_type == "activity_completed":
        payload.update(actual_finish=dates.iso(occurred))
    elif event_type == "activity_started":
        payload.update(actual_start=dates.iso(occurred))
    elif event_type == "inspection_completed":
        payload.update(result="passed", description=obs.reading[:300])
    elif event_type in ("blocker_reported", "delay_reported"):
        payload.update(description=obs.reading[:300], category=obs.blocker_category,
                       expected_clear=obs.expected_date)
        if event_type == "blocker_reported":
            payload["blocker_id"] = store.new_id("BLK")
    event = events.append(conn, project_id, event_type, activity_id=activity_id, occurred_at=occurred,
                          captured_at=captured, device=device, actor=actor, source=source_type, payload=payload,
                          evidence_ids=[evidence_id], observation_id=observation_id, confidence=confidence,
                          origin=origin)
    made.append({"event_id": event["event_id"], "event_type": event_type, "status": event["status"]})
    if obs.remaining_days is not None and event_type in ("progress_update", "activity_started", "blocker_reported"):
        extra = events.append(conn, project_id, "remaining_update", activity_id=activity_id, occurred_at=occurred,
                              captured_at=captured, device=device, actor=actor, source=source_type,
                              payload={"remaining_days": obs.remaining_days, "note": obs.reading[:300]},
                              evidence_ids=[evidence_id], observation_id=observation_id, confidence=confidence,
                              origin=origin)
        made.append({"event_id": extra["event_id"], "event_type": "remaining_update", "status": extra["status"]})
    conn.execute("UPDATE jv_field_observations SET event_id = ? WHERE observation_id = ?",
                 (event["event_id"], observation_id))
    return made


def record_letter(conn: sqlite3.Connection, project_id: str, evidence_id: str, text: str, reference: _dt.date,
                  observations: List[Dict[str, Any]], occurred: str, captured: str, device, actor, source_type,
                  origin) -> Dict[str, Any]:
    letter = parse_letter(text, reference)
    correspondence_id = store.new_id("COR")
    # the activities the letter is about: those its statements linked to clearly,
    # most-mentioned first (a suggestion alone does not make it about an activity)
    counts: Dict[str, int] = {}
    for item in observations:
        if item["link"].get("activity_id"):
            counts[item["link"]["activity_id"]] = counts.get(item["link"]["activity_id"], 0) + 1
    activity_ids = sorted(counts, key=lambda a: -counts[a])
    store.insert(conn, "jv_correspondence", {
        "correspondence_id": correspondence_id, "project_id": project_id, "evidence_id": evidence_id,
        "reference": letter["reference"], "direction": "incoming" if source_type in ("contractor", "client") else "recorded",
        "from_party": letter["from_party"], "to_party": letter["to_party"], "subject": letter["subject"],
        "letter_date": letter["letter_date"], "received_date": dates.iso(captured), "summary": text[:600],
        "created_at": store.now()})
    conn.execute("UPDATE jv_documents SET reference = ?, doc_date = COALESCE(?, doc_date) WHERE evidence_id = ?",
                 (letter["reference"], letter["letter_date"], evidence_id))
    for aid in activity_ids:
        store.insert(conn, "jv_links", {"project_id": project_id, "from_kind": "correspondence",
                                        "from_id": correspondence_id, "to_kind": "activity", "to_id": aid,
                                        "relation": "relates_to", "confidence": None, "method": "linking",
                                        "created_by": store.current_user(), "created_at": store.now()}, or_ignore=True)
    when = f"{letter['letter_date']}T00:00:00" if letter["letter_date"] else occurred
    primary = activity_ids[0] if activity_ids else None
    payload = {"reference": letter["reference"], "subject": letter["subject"], "from_party": letter["from_party"],
               "to_party": letter["to_party"], "correspondence_id": correspondence_id,
               "activities": activity_ids}
    events.append(conn, project_id, "correspondence_received", activity_id=primary, occurred_at=when,
                  captured_at=captured, device=device, actor=actor, source=source_type, payload=payload,
                  evidence_ids=[evidence_id], origin=origin, base_head=None)
    if letter["is_notice"]:
        events.append(conn, project_id, "notice_issued", activity_id=primary, occurred_at=when, captured_at=captured,
                      device=device, actor=actor, source=source_type,
                      payload=dict(payload, notice_type="delay", issued_on=letter["letter_date"] or dates.iso(when)),
                      evidence_ids=[evidence_id], origin=origin, base_head=None)
    search.index_item(conn, f"correspondence:{correspondence_id}", "correspondence", correspondence_id, project_id,
                      primary, when, captured, source_type, 8, origin, letter["subject"] or "letter", text)
    return dict(letter, correspondence_id=correspondence_id, activity_ids=activity_ids)
