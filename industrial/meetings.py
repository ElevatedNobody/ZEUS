"""
Meeting intelligence, entirely on this machine.

    meeting audio -> local Whisper transcript (timestamped)
                  -> local model: summary, decisions, action items (JSON, validated)
                  -> tasks created in the existing to-do list (one list, not a second)
                  -> minutes written as a verified .docx

The transcript is always kept, even if the model is unavailable - analysis is
reported as not done rather than guessed.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, Dict, List

from core.logger import get_logger

logger = get_logger("industrial.meetings")

AUDIO_TYPES = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".webm", ".mp4", ".aac"}


def transcribe(audio_path: str) -> Dict[str, Any]:
    from voice.offline import enforce_offline, resolve_whisper_dir
    enforce_offline()
    path = Path(audio_path)
    if not path.is_file():
        return {"error": f"'{audio_path}' does not exist"}
    if path.suffix.lower() not in AUDIO_TYPES:
        return {"error": f"unsupported audio type '{path.suffix}' - use one of "
                         f"{', '.join(sorted(AUDIO_TYPES))}"}
    model_dir = resolve_whisper_dir()
    if not (model_dir / "model.bin").is_file():
        return {"error": f"no local Whisper model at {model_dir} - run SETUP_VOICE.bat"}
    try:
        from faster_whisper import WhisperModel
        from voice.speech_to_text import _verified_whisper_choice
    except Exception as error:
        return {"error": f"speech recognition is not installed: {error}"}
    choice = _verified_whisper_choice()
    device = choice.get("device", "cpu")
    compute = choice.get("compute_type", "int8" if device == "cpu" else "float16")
    started = time.time()
    try:
        model = WhisperModel(str(model_dir), device=device, compute_type=compute,
                             local_files_only=True)
    except Exception:
        model = WhisperModel(str(model_dir), device="cpu", compute_type="int8",
                             local_files_only=True)
        device = "cpu"
    segments, info = model.transcribe(str(path), vad_filter=True, beam_size=5)
    rows = [{"start": round(s.start, 1), "end": round(s.end, 1), "text": s.text.strip()}
            for s in segments]
    return {"segments": rows, "text": " ".join(r["text"] for r in rows),
            "language": getattr(info, "language", ""), "duration_s": getattr(info, "duration", None),
            "device": device, "seconds": round(time.time() - started, 1)}


def analyse(transcript: str, title: str = "") -> Dict[str, Any]:
    from industrial.agent_workflows import _json, _llm
    if not transcript.strip():
        return {"error": "the transcript is empty"}
    excerpt = transcript[:24000]
    text, model = _llm(
        "json_structured",
        "You turn meeting transcripts into minutes. Return ONLY JSON: {\"summary\": str, "
        "\"decisions\": [str], \"action_items\": [{\"task\": str, \"owner\": str, "
        "\"due\": \"YYYY-MM-DD or empty\"}], \"open_questions\": [str]}. Use only what was "
        "said; if an owner or date was not said, leave it empty. Never invent names.",
        f"Meeting: {title or 'untitled'}\nTranscript:\n{excerpt}", json_format=True, timeout=300)
    data = _json(text) if text else None
    if not isinstance(data, dict):
        return {"error": "no local model produced minutes (is Ollama running?)" if text is None
                else "the model's minutes were not valid JSON", "model": model}
    items = []
    for item in data.get("action_items") or []:
        if not isinstance(item, dict) or not str(item.get("task", "")).strip():
            continue
        due = str(item.get("due", "")).strip()
        items.append({"task": str(item["task"]).strip()[:200],
                      "owner": str(item.get("owner", "")).strip()[:80],
                      "due": due if re.match(r"^\d{4}-\d{2}-\d{2}$", due) else ""})
    # Names the model reports must actually occur in the transcript.
    lowered = transcript.lower()
    for item in items:
        if item["owner"] and item["owner"].lower() not in lowered:
            item["owner_note"] = "owner not found in transcript - removed"
            item["owner"] = ""
    return {"summary": str(data.get("summary", "")).strip(),
            "decisions": [str(d).strip() for d in data.get("decisions") or [] if str(d).strip()],
            "action_items": items,
            "open_questions": [str(q).strip() for q in data.get("open_questions") or [] if str(q).strip()],
            "model": model}


def _finish(transcript: Dict[str, Any], title: str, create_tasks: bool, source: str) -> Dict[str, Any]:
    from industrial.agent_workflows import _record_artifact, _save_docx
    minutes = analyse(transcript["text"], title)
    created = []
    if create_tasks and not minutes.get("error"):
        from core.tool_registry import registry
        from security.policy import origin_scope
        with origin_scope("agent"):
            for item in minutes["action_items"]:
                outcome = registry.execute("add_todo", text=item["task"],
                                           due_date=item["due"] or "", assignee=item["owner"])
                if outcome.success:
                    created.append(item["task"])
    sections = [("Summary", minutes.get("summary") or f"Not generated - {minutes.get('error')}"),
                ("Decisions", minutes.get("decisions") or ["None recorded"]),
                ("Action items", {"columns": ["Task", "Owner", "Due"],
                                  "rows": [[i["task"], i["owner"] or "-", i["due"] or "-"]
                                           for i in minutes.get("action_items", [])]
                                  or [["None recorded", "", ""]]}),
                ("Open questions", minutes.get("open_questions") or ["None"]),
                ("Transcript", "\n\n".join(f"[{s['start']:>7.1f}s] {s['text']}"
                                           for s in transcript.get("segments", []))
                 or transcript["text"])]
    saved = _save_docx(f"Minutes - {title or Path(source).stem}",
                       f"Transcribed and analysed locally by ZEUS on {time.strftime('%Y-%m-%d %H:%M')}",
                       sections, folder="Meetings")
    if saved.get("saved"):
        _record_artifact(saved, "meetings", f"minutes for {source}", minutes.get("model", ""), [source])
    return {"title": title, "transcript_chars": len(transcript["text"]),
            "segments": len(transcript.get("segments", [])),
            "transcription": {k: transcript.get(k) for k in ("device", "seconds", "language", "duration_s")},
            "minutes": minutes, "tasks_created": created,
            "saved": saved.get("saved"), "error": minutes.get("error") if not saved.get("saved") else None}


def process_meeting(audio_path: str, title: str = "", create_tasks: bool = True) -> Dict[str, Any]:
    from core.assistant_state import State, assistant_state
    with assistant_state.activity(State.EXECUTING, "transcribing the meeting"):
        transcript = transcribe(audio_path)
    if transcript.get("error"):
        return {"error": transcript["error"]}
    return _finish(transcript, title or Path(audio_path).stem, create_tasks, audio_path)


def process_transcript_file(path: str, title: str = "", create_tasks: bool = True) -> Dict[str, Any]:
    target = Path(path)
    if not target.is_file():
        return {"error": f"'{path}' does not exist"}
    text = target.read_text(encoding="utf-8", errors="replace")
    return _finish({"text": text, "segments": []}, title or target.stem, create_tasks, path)
