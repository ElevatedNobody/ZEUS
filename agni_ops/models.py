"""
Real, typed tests for the Ollama models ZEUS is configured to use.

Every model goes through the same phases, but the INFERENCE phase depends on
what the model is for - one generic "say OK" test for everything is how the
setup run of 2026-09-24 got two models wrong:

    bge-m3       reported "NOT WORKING - 0-dim vector". The model was fine.
                 Ollama had answered HTTP 500 (its runner crashed allocating
                 host memory - GGML_ASSERT(ctx->mem_buffer != NULL), exit
                 0xc0000409) and the test read `embeddings` out of the error
                 body, found none, and printed "0-dim".
    qwen3-vl:8b  reported "NOT WORKING". It is a thinking model that ignores
                 think=false; with num_predict=24 every token went into the
                 reasoning trace and the answer was empty. It was also never
                 shown an image, so its actual function was never tested.

Phases, per model:
    1 PRESENCE    installed in the local daemon (DOWNLOADED + INSTALLED)
    2 CHECKSUM    digest matches the signed trusted registry (CHECKSUM_VALID)
    3 LOAD        room is made (core/model_lifecycle.py), the model is loaded
                  and confirmed resident in /api/ps (LOAD_TEST_PASSED)
    4 INFERENCE   by type - EMBEDDING: embed, validate, store, index, query,
                  rank; CHAT: a reply that contains the requested word;
                  VISION: a generated test image, and the colour named back
    5 VALIDATION  the output is checked, not just "non-empty"
    6 METRICS     load/inference latency, resident size, VRAM, commit delta
    then the model is UNLOADED, so the next test starts from the same state.
"""

from __future__ import annotations

import base64
import io
import json
import math
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional

from agni_ops.status import (CHECKSUM_VALID, DEGRADED, DOWNLOADED, FAIL, INFERENCE_TEST_PASSED,
                             INSTALLED, LOAD_TEST_PASSED, NOT_TESTED, PASS, UNAVAILABLE, WARN,
                             Result)
from core import model_lifecycle as lifecycle

EMBEDDING, CHAT, VISION = "EMBEDDING", "CHAT", "VISION"

# Output of a load that failed because the runner could not get memory. These
# are the strings Ollama 0.34 / llama.cpp actually produce (see its server.log).
_MEMORY_MARKERS = ("memory", "mem_buffer", "0xc0000409", "0xc0000005", "alloc",
                   "terminated", "paging file", "cudaMalloc", "out of memory")

# The three documents and the query of the embedding functional test. The
# query shares almost no words with the right answer, so a lexical fluke
# cannot pass it - the vectors have to carry meaning.
_EMBED_DOCS = ["The centrifugal pump discharge pressure rose to 12 bar after the impeller change.",
               "Boiler feedwater chemistry: dissolved oxygen and pH control for the deaerator.",
               "Quarterly budget review of the canteen and office stationery spending."]
_EMBED_QUERY = "What is the outlet pressure of the pump?"


@dataclass
class ModelReport:
    model: str
    type: str = ""
    installed_as: str = ""
    capabilities: List[str] = field(default_factory=list)
    present: bool = False
    digest: str = ""
    checksum: str = "unknown"            # trusted | tampered | unpinned | unknown
    load: Optional[bool] = None
    inference: Optional[bool] = None
    function: Optional[bool] = None
    function_detail: str = ""
    load_s: Optional[float] = None
    inference_s: Optional[float] = None
    resident_gb: Optional[float] = None
    vram_gb: Optional[float] = None
    commit_delta_gb: Optional[float] = None
    status: str = NOT_TESTED
    error: str = ""
    cause: str = ""
    fallback: str = ""
    states: List[str] = field(default_factory=list)
    evidence: str = ""
    slot: str = ""
    thinking_control: Dict = field(default_factory=dict)
    unloaded: Optional[bool] = None      # confirmed gone from /api/ps after the test
    tested_at: Optional[float] = None

    def to_dict(self) -> Dict:
        data = asdict(self)
        data["points"] = verification_points(data)
        return data

    def to_result(self) -> Result:
        yes = lambda v: "-" if v is None else ("YES" if v else "NO")   # noqa: E731
        reason = self.cause or self.function_detail or self.error
        return Result(
            component=f"model {self.model}", status=self.status, reason=reason,
            where="Ollama " + lifecycle.host(), evidence=self.evidence[:1500],
            fallback=self.fallback, group="models",
            current_state=" > ".join(self.states) or "not installed",
            details={"type": self.type, "slot": self.slot, "present": yes(self.present),
                     "checksum": self.checksum, "load": yes(self.load),
                     "inference": yes(self.inference), "function": yes(self.function),
                     "latency_load_s": self.load_s, "latency_inference_s": self.inference_s,
                     "resident_gb": self.resident_gb, "vram_gb": self.vram_gb,
                     "commit_delta_gb": self.commit_delta_gb})


# ------------------------------------------------------------- helpers ----

def installed_models() -> List[str]:
    status, data, _raw = lifecycle.request("/api/tags", timeout=8)
    if status != 200:
        return []
    return [m.get("name", "") for m in (data or {}).get("models", [])]


def resolve_installed(name: str, models: List[str]) -> str:
    try:
        from core.ollama_client import OllamaClient
        return next((m for m in models if OllamaClient._model_matches(m, name)), "")
    except Exception:
        return next((m for m in models if m in (name, f"{name}:latest")), "")


def capabilities(name: str) -> List[str]:
    status, data, _raw = lifecycle.request("/api/show", {"model": name}, timeout=20)
    if status != 200 or not data:
        return []
    return [str(c) for c in (data.get("capabilities") or [])]


def classify(caps: List[str], name: str = "") -> str:
    """The model's job, from what the runtime says it can do - not its name."""
    if "embedding" in caps:
        return EMBEDDING
    if "vision" in caps:
        return VISION
    if caps:
        return CHAT
    lowered = name.lower()
    if "embed" in lowered or lowered.startswith("bge"):
        return EMBEDDING
    if "-vl" in lowered or "vision" in lowered or "llava" in lowered:
        return VISION
    return CHAT


def _is_memory_failure(text: str) -> bool:
    lowered = (text or "").lower()
    return any(marker.lower() in lowered for marker in _MEMORY_MARKERS)


def _error_of(status: int, data: Optional[dict], raw: str) -> str:
    if data and data.get("error"):
        return f"HTTP {status}: {data['error']}"
    if status == 0:
        return f"no response: {raw}"
    return f"HTTP {status}: {raw[:300]}"


def _cosine(a: List[float], b: List[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


def test_image_png() -> bytes:
    """A 256x256 PNG of a solid red circle on white - generated, never
    downloaded, so the answer is known exactly."""
    from PIL import Image, ImageDraw
    image = Image.new("RGB", (256, 256), "white")
    ImageDraw.Draw(image).ellipse((48, 48, 208, 208), fill=(220, 20, 20))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


# ---------------------------------------------------------- functional ----

def _embedding_function(report: ModelReport, name: str) -> None:
    """TEXT -> EMBEDDING -> VECTOR -> STORE -> QUERY -> SIMILARITY -> TOP RESULT."""
    from core.model_output import validate_vector
    started = time.time()
    status, data, raw = lifecycle.request(
        "/api/embed", {"model": name, "input": _EMBED_DOCS + [_EMBED_QUERY],
                       "keep_alive": "2m"}, timeout=300)
    report.inference_s = round(time.time() - started, 2)
    if status != 200:
        report.inference = False
        report.error = _error_of(status, data, raw)
        report.evidence = raw[:1500]
        return
    vectors = (data or {}).get("embeddings") or []
    report.inference = True
    if len(vectors) != len(_EMBED_DOCS) + 1:
        report.function = False
        report.function_detail = (f"expected {len(_EMBED_DOCS) + 1} vectors, got {len(vectors)}"
                                  if vectors else "0-dimensional embedding output")
        report.evidence = raw[:600]
        return
    for vector in vectors:
        ok, why = validate_vector(vector)
        if not ok:
            report.function = False
            report.function_detail = why
            return
    dims = {len(v) for v in vectors}
    if len(dims) != 1:
        report.function = False
        report.function_detail = f"inconsistent vector dimensions {sorted(dims)}"
        return
    # STORE and INDEX exactly the way ZEUS's RAG tables do: JSON in SQLite.
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE chunks (id INTEGER PRIMARY KEY, text TEXT, embedding TEXT)")
    for text, vector in zip(_EMBED_DOCS, vectors):
        conn.execute("INSERT INTO chunks (text, embedding) VALUES (?, ?)", (text, json.dumps(vector)))
    query = vectors[-1]
    ranked = sorted(((_cosine(query, json.loads(row[1])), row[0])
                     for row in conn.execute("SELECT text, embedding FROM chunks")), reverse=True)
    conn.close()
    top_score, top_text = ranked[0]
    report.function = top_text == _EMBED_DOCS[0] and top_score > ranked[1][0]
    report.function_detail = (f"{dims.pop()}-dim vectors; query ranked the pump document first "
                              f"(cosine {top_score:.3f} vs {ranked[1][0]:.3f})"
                              if report.function else
                              f"wrong top result: '{top_text[:50]}' (cosine {top_score:.3f})")


def _ask(name: str, think: Optional[bool]) -> tuple:
    """One 'reply READY' call. Returns (status, data, raw, seconds)."""
    body = {"model": name, "stream": False, "keep_alive": "2m",
            "messages": [{"role": "user", "content": "Reply with exactly one word: READY"}],
            # Enough room even for a model that reasons although asked not to.
            "options": {"num_predict": 768, "temperature": 0}}
    if think is not None:
        body["think"] = think
    started = time.time()
    status, data, raw = lifecycle.request("/api/chat", body, timeout=600)
    if status == 400 and "think" in body:
        body.pop("think")
        status, data, raw = lifecycle.request("/api/chat", body, timeout=600)
    return status, data, raw, round(time.time() - started, 2)


def _clean_ready(data: Optional[dict]) -> tuple:
    """(answer, is_clean). Clean = the requested word and (almost) nothing
    else. Checking only that 'ready' appears passed a model whose leaked
    reasoning QUOTED the prompt - 'the user wants me to reply ... READY'."""
    from core.model_output import strip_thinking
    message = (data or {}).get("message") or {}
    answer = strip_thinking(message.get("content") or "").strip()
    words = answer.replace('"', " ").replace(".", " ").split()
    return answer, bool(answer) and len(words) <= 4 and "ready" in answer.lower()


def _chat_function(report: ModelReport, name: str, thinking: bool) -> None:
    status, data, raw, seconds = _ask(name, False if thinking else None)
    report.inference_s = seconds
    if status != 200:
        report.inference = False
        report.error = _error_of(status, data, raw)
        report.evidence = raw[:1500]
        return
    report.inference = True
    answer, clean = _clean_ready(data)
    control = {"think_false_leaks": False}
    if thinking and not clean and answer:
        # Asked not to think, it put its reasoning in the answer. Does it
        # answer cleanly when allowed to think? If so ZEUS asks it to think
        # and keeps only the answer (core/model_calibration.py).
        status2, data2, raw2, seconds2 = _ask(name, True)
        answer2, clean2 = _clean_ready(data2) if status2 == 200 else ("", False)
        control = {"think_false_leaks": True, "think_true_clean": clean2,
                   "leaked_sample": answer[:120]}
        if clean2:
            report.function = True
            report.inference_s = seconds2
            report.function_detail = (f"answered '{answer2[:20]}' - this build ignores think=false "
                                      f"(its reasoning leaked into the answer), so ZEUS lets it "
                                      f"think and returns only the answer")
            report.thinking_control = control
            return
    report.thinking_control = control
    if not answer:
        message = (data or {}).get("message") or {}
        report.function = False
        report.function_detail = ("empty answer - the token budget was spent reasoning"
                                  if (data or {}).get("done_reason") == "length" or message.get("thinking")
                                  else "empty answer")
        return
    report.function = clean
    report.function_detail = (f"answered '{answer[:40]}'" if clean
                              else f"did not give the one-word answer: '{answer[:80]}'")


def _vision_function(report: ModelReport, name: str) -> None:
    """IMAGE -> model -> inference -> response -> validated text."""
    image = base64.b64encode(test_image_png()).decode("ascii")
    body = {"model": name, "stream": False, "keep_alive": "2m", "images": [image],
            "prompt": "What colour is the circle in this picture? Answer with one word.",
            "options": {"num_predict": 2048, "temperature": 0}}
    started = time.time()
    status, data, raw = lifecycle.request("/api/generate", body, timeout=600)
    report.inference_s = round(time.time() - started, 2)
    if status != 200:
        report.inference = False
        report.error = _error_of(status, data, raw)
        report.evidence = raw[:1500]
        return
    from core.model_output import strip_thinking
    answer = strip_thinking((data or {}).get("response") or "").strip()
    report.inference = True
    if not answer:
        report.function = False
        report.function_detail = ("empty answer - the token budget was spent reasoning"
                                  if (data or {}).get("done_reason") == "length"
                                  else "empty answer to the image")
        return
    report.function = "red" in answer.lower()
    report.function_detail = (f"named the test circle's colour: '{answer[:40]}'" if report.function
                              else f"could not name the colour of a red circle: '{answer[:80]}'")


# ----------------------------------------------------------------- test ----

def test_model(name: str, slot: str = "", unload_after: bool = True,
               installed: Optional[List[str]] = None) -> ModelReport:
    from core.sysmem import commit_snapshot
    report = ModelReport(model=name, slot=slot)
    installed = installed if installed is not None else installed_models()

    # 1 PRESENCE
    actual = resolve_installed(name, installed)
    if not actual:
        report.status = UNAVAILABLE
        report.cause = f"not installed - run: ollama pull {name}"
        return report
    report.present, report.installed_as = True, actual
    report.states += [DOWNLOADED, INSTALLED]
    report.capabilities = capabilities(actual)
    report.type = classify(report.capabilities, actual)
    status, tags, _raw = lifecycle.request("/api/tags", timeout=8)
    for entry in (tags or {}).get("models", []):
        if entry.get("name") == actual:
            report.digest = entry.get("digest", "")

    # 2 CHECKSUM
    try:
        from security.integrity import model_integrity
        report.checksum = model_integrity.status_of(actual)
    except Exception as error:
        report.checksum = f"unknown ({error})"
    if report.checksum == "trusted":
        report.states.append(CHECKSUM_VALID)

    # 3 LOAD - make room first, then load and confirm residency.
    room_ok, room_reason, unloaded = lifecycle.ensure_room(actual)
    if unloaded:
        report.evidence += f"unloaded before test: {', '.join(unloaded)}\n"
    if not room_ok:
        report.load = False
        report.status = DEGRADED
        report.cause = f"insufficient runtime memory - {room_reason}"
        return report
    before = commit_snapshot().get("commit_available_gb")
    started = time.time()
    if report.type == EMBEDDING:
        status, data, raw = lifecycle.request("/api/embed", {"model": actual, "input": "load",
                                                             "keep_alive": "2m"}, timeout=300)
    else:
        status, data, raw = lifecycle.request("/api/generate", {"model": actual, "keep_alive": "2m"},
                                              timeout=300)
    report.load_s = round(time.time() - started, 2)
    loaded = next((m for m in lifecycle.resident() if m["name"] == actual), None)
    if status != 200 or loaded is None:
        report.load = False
        report.error = _error_of(status, data, raw) if status != 200 else "not resident after load"
        report.evidence += raw[:1500]
        if _is_memory_failure(report.error):
            report.status = DEGRADED
            report.cause = ("insufficient runtime memory - the model runner crashed while "
                            f"allocating ({report.error[:200]})")
        else:
            report.status = FAIL
            report.cause = f"load failed - {report.error[:240]}"
        return report
    report.load = True
    report.states.append(LOAD_TEST_PASSED)
    report.resident_gb, report.vram_gb = loaded["size_gb"], loaded["vram_gb"]
    after = commit_snapshot().get("commit_available_gb")
    if before is not None and after is not None:
        report.commit_delta_gb = round(before - after, 2)

    # 4 + 5 INFERENCE and VALIDATION, by type
    try:
        if report.type == EMBEDDING:
            _embedding_function(report, actual)
        elif report.type == VISION:
            _vision_function(report, actual)
        else:
            _chat_function(report, actual, thinking="thinking" in report.capabilities)
    finally:
        if unload_after:
            report.unloaded = bool(lifecycle.unload(actual))
        report.tested_at = time.time()

    if report.inference is False:
        report.status = DEGRADED if _is_memory_failure(report.error) else FAIL
        report.cause = (("insufficient runtime memory - " if report.status == DEGRADED
                         else "inference request failed - ") + report.error[:240])
    elif report.function:
        report.states.append(INFERENCE_TEST_PASSED)
        if report.checksum == "trusted":
            report.status = PASS
        elif report.checksum == "tampered":
            # It runs, but the router refuses a model whose digest changed
            # (require_trusted_models) - so for ZEUS it is not usable.
            report.status = FAIL
            report.cause = ("digest differs from the signed trusted registry - re-pin only if "
                            "you pulled this model yourself (admin, in ZEUS: the "
                            "pin_trusted_models tool; or STEP 1 : INSTALL, which pins after "
                            "verifying)")
        else:
            report.status = WARN
            report.cause = "works; its digest is not pinned in the trusted registry yet"
    else:
        report.status = FAIL
        report.cause = f"{report.type.lower()} output failed validation - {report.function_detail}"
    return report


POINTS = ("INSTALLED", "DIGEST TRUSTED", "MEMORY ROOM", "LOADED + RESIDENT", "ANSWERED", "OUTPUT VALIDATED",
          "BEHAVIOUR CALIBRATED", "UNLOADED + METRICS")


def verification_points(r: Dict) -> List[Dict]:
    """The eight checks of one model, read from its recorded test - each PASS,
    FAIL or NOT RUN (a later check is not run when an earlier one failed)."""
    def point(name, passed, detail=""):
        return {"point": name, "status": "NOT RUN" if passed is None else ("PASS" if passed else "FAIL"), "detail": detail}
    cause = r.get("cause") or ""
    no_room = cause.startswith("insufficient runtime memory") and r.get("load") is False and not r.get("load_s")
    caps = r.get("capabilities") or []
    control = r.get("thinking_control") or {}
    if not r.get("present"):
        return [point(POINTS[0], False, cause or "not installed")] + [point(n, None) for n in POINTS[1:]]
    out = [point(POINTS[0], True, r.get("installed_as") or r.get("model")),
           point(POINTS[1], r.get("checksum") == "trusted", r.get("checksum") or "unknown"),
           point(POINTS[2], not no_room, cause if no_room else "room made (other models unloaded first)"),
           point(POINTS[3], r.get("load"), (f"{r.get('load_s')} s, {r.get('resident_gb')} GB resident"
                                          if r.get("load") else (r.get("error") or cause)[:200]))]
    answered = r.get("inference") if r.get("load") else None
    out.append(point(POINTS[4], answered, f"{r.get('inference_s')} s" if answered else (r.get("error") or "")[:200]))
    out.append(point(POINTS[5], r.get("function") if answered else None, (r.get("function_detail") or "")[:200]))
    if not answered:
        calibrated, detail = None, ""
    elif r.get("type") == CHAT and "thinking" in caps:
        calibrated = bool(control)
        detail = ("think=false is ignored - it is allowed to think and only the answer is kept" if control.get("think_false_leaks")
                  else "honours think=false" if control else "thinking control not measured")
    else:
        calibrated, detail = bool(r.get("function")), {EMBEDDING: "vectors rank the right passage first",
                                                        VISION: "names the colour of a generated image"}.get(r.get("type"), "not a thinking model")
    out.append(point(POINTS[6], calibrated, detail))
    metrics = r.get("unloaded") is not None or r.get("tested_at") is not None
    out.append(point(POINTS[7], (bool(r.get("unloaded")) if r.get("unloaded") is not None else None) if metrics and r.get("load") else None,
                     f"VRAM {r.get('vram_gb')} GB, commit +{r.get('commit_delta_gb')} GB, "
                     f"{'unloaded after the test' if r.get('unloaded') else 'unload not confirmed'}" if r.get("load") else ""))
    return out


def chat_models() -> Dict[str, str]:
    """The configured models that answer in words (chat and vision) - 'Probe every chat model'."""
    installed = installed_models()
    out = {}
    for name, slot in configured_models().items():
        actual = resolve_installed(name, installed)
        if actual and classify(capabilities(actual), actual) in (CHAT, VISION):
            out[name] = slot
        elif not actual and "embedding" not in slot:
            out[name] = slot                                  # missing: reported as not installed
    return out


def configured_models() -> Dict[str, str]:
    """model -> slot(s) it serves: this machine's plan (core/hardware.py),
    plus whatever the router ACTUALLY picks for each chat slot right now -
    it may differ (a config.ini override, an untrusted digest skipped, or a
    latency slot moved off a model measured to leak its reasoning), and the
    model that really answers is the one that has to be tested."""
    from core.hardware import model_plan
    slots: Dict[str, List[str]] = {}
    for slot, model in model_plan().get("slots", {}).items():
        slots.setdefault(model, []).append(slot)
    try:
        from core.model_router import model_router
        model_router.invalidate()
        for slot in ("fast", "classify", "default", "reasoning", "json_structured", "code", "vision"):
            actual = model_router.route(slot)
            planned = next((m for m, names in slots.items() if slot in names), None)
            if actual and actual != planned and not any(
                    resolve_installed(m, [actual]) for m in slots):
                slots.setdefault(actual, []).append(f"{slot} (routed)")
    except Exception:
        pass
    return {model: ",".join(names) for model, names in slots.items()}


def fallback_candidates(report: ModelReport, installed: List[str]) -> List[str]:
    """Installed models that can do the same job, smallest first."""
    wanted = {EMBEDDING: "embedding", VISION: "vision"}.get(report.type)
    candidates = []
    for name in installed:
        if name == report.installed_as:
            continue
        caps = capabilities(name)
        if wanted and wanted not in caps:
            continue
        if not wanted and ("completion" not in caps or "embedding" in caps):
            continue
        candidates.append((lifecycle.model_size_gb(name), name))
    return [name for _size, name in sorted(candidates)]


def run_matrix(models: Optional[Dict[str, str]] = None, with_fallbacks: bool = True,
               log=None) -> List[ModelReport]:
    """Test every configured model; for each one that does not PASS, find the
    smallest installed model of the same type that does, and name it as the
    verified fallback."""
    models = models if models is not None else configured_models()
    installed = installed_models()
    reports: List[ModelReport] = []
    for name, slot in models.items():
        if log:
            log.say(f"  testing {name} ({slot}) ...")
        report = test_model(name, slot=slot, installed=installed)
        if with_fallbacks and report.status in (FAIL, DEGRADED, UNAVAILABLE) and report.type:
            for candidate in fallback_candidates(report, installed)[:3]:
                if log:
                    log.say(f"    trying fallback {candidate} ...")
                trial = test_model(candidate, slot=f"fallback for {name}", installed=installed)
                if trial.status in (PASS, WARN):
                    report.fallback = f"{candidate} (verified: {trial.function_detail})"
                    break
            else:
                report.fallback = report.fallback or "none verified"
        reports.append(report)
        if log:
            log.result(report.to_result())
    save_health(reports)
    return reports


def save_health(reports: List[ModelReport]) -> None:
    """The last verified state of every model (core/model_calibration.py):
    STEP 2 : START's preflight reports it without re-running inference, and
    the runtime reads the thinking-control calibration from it."""
    try:
        from core.model_calibration import save
        save(report.to_dict() for report in reports)
    except Exception:
        pass


def load_health() -> Dict[str, Dict]:
    try:
        from core.model_calibration import load
        return load()
    except Exception:
        return {}


def matrix_table(reports: List[ModelReport]) -> str:
    """The MODEL | TYPE | PRESENT | CHECKSUM | LOAD | INFERENCE | FUNCTION |
    LATENCY | RESOURCE USE | STATUS | ERROR | FALLBACK table."""
    yes = lambda v: "-" if v is None else ("YES" if v else "NO")   # noqa: E731
    lines = [f"{'MODEL':<22}{'TYPE':<11}{'PRESENT':<9}{'CHECKSUM':<10}{'LOAD':<6}{'INFER':<7}"
             f"{'FUNCTION':<10}{'LATENCY(load/inf s)':<21}{'RESIDENT/VRAM GB':<18}{'STATUS':<12}"]
    for r in reports:
        latency = f"{r.load_s if r.load_s is not None else '-'}/{r.inference_s if r.inference_s is not None else '-'}"
        resources = (f"{r.resident_gb}/{r.vram_gb}" if r.resident_gb is not None else "-")
        lines.append(f"{r.model[:21]:<22}{r.type or '-':<11}{yes(r.present):<9}{r.checksum[:9]:<10}"
                     f"{yes(r.load):<6}{yes(r.inference):<7}{yes(r.function):<10}{latency:<21}"
                     f"{resources:<18}{r.status:<12}")
        if r.status != PASS:
            lines.append(f"{'':<4}CAUSE: {r.cause or r.error or '-'}")
            lines.append(f"{'':<4}FALLBACK: {r.fallback or '-'}")
        else:
            lines.append(f"{'':<4}{r.function_detail}")
    return "\n".join(lines)
