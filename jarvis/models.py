"""
Which local model does each ZEUS task use - and is it really there?

    ocr                 RapidOCR / PaddleOCR / Tesseract / EasyOCR (ZEUS's layered engines)
    image_evidence      VISION role (qwen3-vl on the RTX 5060 plan)
    vernacular          the router's multilingual slot (Qwen3, Aya as fallback)
    extraction          EXTRACTION role - strict JSON (Qwen3 with thinking off)
    activity_linking    deterministic scoring + EMBEDDING role (bge-m3) when enabled
    schedule_reasoning  deterministic CPM - no model computes a date
    causal_explanation  GENERAL_REASONING role, only to phrase facts already computed
    classification      SMALL_LOCAL role
    retrieval           EMBEDDING + RERANKER roles (hybrid ranker when no cross-encoder)
    speech              SPEECH_TO_TEXT role (local Whisper)

A model is reported usable only when the local runtime lists it AND the
trusted-model registry accepts it (core.model_roles). Nothing is downloaded and
nothing is shown as available because it was configured.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List

TASKS: Dict[str, Dict[str, str]] = {
    "ocr": {"kind": "ocr", "purpose": "typed, scanned and photographed text (site boards, DPR scans, letters)"},
    "image_evidence": {"kind": "role", "role": "VISION", "purpose": "what a site photograph shows (stored as inference)"},
    "vernacular": {"kind": "slot", "slot": "multilingual", "purpose": "regional-language / mixed site notes to English"},
    "extraction": {"kind": "role", "role": "EXTRACTION", "purpose": "structured fields from a note, verified against it"},
    "activity_linking": {"kind": "hybrid", "role": "EMBEDDING",
                         "purpose": "field reference -> schedule activity (ids, aliases, names, dates, embeddings)"},
    "schedule_reasoning": {"kind": "deterministic", "purpose": "CPM dates, float, critical path, propagation"},
    "causal_explanation": {"kind": "role", "role": "GENERAL_REASONING",
                           "purpose": "phrasing the computed causal chain (facts only, dates checked)"},
    "classification": {"kind": "role", "role": "SMALL_LOCAL", "purpose": "small yes/no and routing decisions"},
    "retrieval": {"kind": "role", "role": "EMBEDDING", "purpose": "semantic project search (with FTS5 always on)"},
    "reranking": {"kind": "role", "role": "RERANKER", "purpose": "ordering retrieved evidence"},
    "speech": {"kind": "role", "role": "SPEECH_TO_TEXT", "purpose": "voice memos and spoken field updates"},
}

_cache: Dict[str, Any] = {}
_lock = threading.Lock()
_TTL = 60.0


def _installed() -> List[str]:
    try:
        from core.ollama_client import ollama_client
        return ollama_client.list_models() if ollama_client.is_available() else []
    except Exception:
        return []


def _trusted(model: str) -> bool:
    try:
        from security.integrity import model_integrity
        return bool(model_integrity.may_use(model))
    except Exception:
        return True


def route(task: str, fresh: bool = False) -> Dict[str, Any]:
    spec = TASKS.get(task)
    if spec is None:
        raise ValueError(f"unknown ZEUS task '{task}' - one of {', '.join(TASKS)}")
    with _lock:
        hit = _cache.get(task)
        if hit and not fresh and time.time() - hit["_at"] < _TTL:
            return {k: v for k, v in hit.items() if k != "_at"}
    out: Dict[str, Any] = {"task": task, "purpose": spec["purpose"], "kind": spec["kind"], "model": "",
                           "usable": False, "state": "", "reason": "", "fallback": ""}
    try:
        if spec["kind"] == "deterministic":
            out.update(model="none (deterministic Python)", usable=True, state="DETERMINISTIC",
                       reason="computed by jarvis/cpm.py - a model never computes a schedule date")
        elif spec["kind"] == "ocr":
            from industrial.ocr_engines import usable_engines
            engines = usable_engines()
            out.update(model=", ".join(engines) or "none", usable=bool(engines),
                       state="INSTALLED" if engines else "NOT_INSTALLED",
                       reason="layered OCR engines ready" if engines else "no OCR engine is installed",
                       fallback="typed text is still captured" if not engines else "")
        elif spec["kind"] == "slot":
            installed = _installed()
            if not installed:
                out.update(state="RUNTIME_DOWN", reason="the local model runtime (Ollama) is not answering",
                           fallback="the deterministic vernacular lexicon still reads the note")
            else:
                from core.model_router import model_router
                from core.ollama_client import ollama_client
                wanted = model_router.route(spec["slot"])
                actual = next((m for m in installed if ollama_client._model_matches(m, wanted)), None)
                if actual and _trusted(actual):
                    out.update(model=actual, usable=True, state="INSTALLED", reason="installed and trusted")
                else:
                    out.update(model=wanted, state="NOT_INSTALLED" if not actual else "UNTRUSTED",
                               reason="not installed" if not actual else "not accepted by the trusted-model registry",
                               fallback="the deterministic vernacular lexicon still reads the note")
        else:
            from core import model_roles
            choice = model_roles.select(spec["role"])
            usable = choice.usable and bool(choice.model)
            out.update(model=choice.model or choice.planned_model, usable=usable, state=choice.state,
                       reason=choice.reason, fallback=choice.fallback or _fallback(task))
            if spec["kind"] == "hybrid":
                out["reason"] = ("deterministic scoring always; " + ("embeddings available (" + choice.model + ")"
                                                                     if usable else "embeddings unavailable"))
                out["usable"] = True
    except Exception as error:
        out.update(usable=False, state="ERROR", reason=f"{type(error).__name__}: {error}", fallback=_fallback(task))
    with _lock:
        _cache[task] = dict(out, _at=time.time())
    return out


def _fallback(task: str) -> str:
    return {"image_evidence": "OCR text and the operator's caption are still captured",
            "extraction": "the deterministic rule extractor still runs",
            "causal_explanation": "the deterministic explanation is shown",
            "retrieval": "full-text search still works", "speech": "type the field update instead",
            "classification": "deterministic rules"}.get(task, "")


def routing_report(fresh: bool = False) -> Dict[str, Any]:
    rows = [route(task, fresh=fresh) for task in TASKS]
    return {"ok": True, "routes": rows, "usable": sum(1 for r in rows if r["usable"]), "total": len(rows)}


def clear_cache() -> None:
    with _lock:
        _cache.clear()
