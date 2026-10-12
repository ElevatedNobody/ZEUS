"""
Model roles - `select(role, requirements)` beside `model_router.route(task_type)`.

A role names what a caller NEEDS (general reasoning, a fast reply, code,
vision, structured extraction, long documents, a small local model,
embeddings, reranking); the answer says which installed, trusted model serves
it on THIS machine, whether it fits in the memory free right now, and what the
hardware plan would choose.

Rules kept from the rest of ZEUS:
  * hardware detection is mandatory - every choice is made against
    core.hardware.profile() and the memory free at the moment of asking;
  * nothing is downloaded - a missing model is reported with the command
    that installs it (STEP 1 : INSTALL), never pulled from here;
  * a model the trusted-model registry rejects is never chosen
    (model_router._pick_trusted);
  * a status is only as strong as its evidence: INSTALLED is not VERIFIED,
    and a real call is made only when `probe=True` is asked for.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from core.logger import get_logger

logger = get_logger("model_roles")

ROLES: Dict[str, Dict[str, str]] = {
    "GENERAL_REASONING": {"kind": "chat", "slot": "reasoning",
                          "purpose": "analysis, root cause, company intelligence summaries"},
    "FAST": {"kind": "chat", "slot": "fast", "purpose": "quick conversational replies"},
    "CODING": {"kind": "chat", "slot": "code", "purpose": "writing and explaining code"},
    "VISION": {"kind": "chat", "slot": "vision", "purpose": "photographs, scans, drawings, handwriting"},
    "EXTRACTION": {"kind": "chat", "slot": "json_structured", "purpose": "structured JSON extraction"},
    "DOCUMENT": {"kind": "chat", "slot": "default", "purpose": "long-form document writing"},
    "SMALL_LOCAL": {"kind": "chat", "slot": "classify", "purpose": "tiny yes/no and routing decisions"},
    "EMBEDDING": {"kind": "embedding", "slot": "embedding", "purpose": "vector search"},
    "RERANKER": {"kind": "reranker", "slot": "reranker", "purpose": "ordering retrieved passages"},
    "SPEECH_TO_TEXT": {"kind": "voice", "slot": "stt", "purpose": "recognising the operator's speech (Whisper, local)"},
    "TEXT_TO_SPEECH": {"kind": "voice", "slot": "tts", "purpose": "spoken replies (Chatterbox, local; Windows voice fallback)"},
}

# States, strongest last. Only a real call earns VERIFIED. HYBRID is the reranker
# when no cross-encoder is installed: a designed ranker, named for what it is.
NOT_INSTALLED, RUNTIME_DOWN, UNTRUSTED, INSTALLED, VERIFIED, FALLBACK, HYBRID = (
    "NOT_INSTALLED", "RUNTIME_DOWN", "UNTRUSTED", "INSTALLED", "VERIFIED", "FALLBACK", "HYBRID")

# What the hybrid reranker weighs (knowledge_fabric/retrieval.py + industrial/reranker.py).
HYBRID_SIGNALS = ("lexical (full-text rank and term overlap)", "dense (bge-m3 vector rank)",
                  "entity (entity-mention leg, named identifiers)", "temporal (period filter, dated-event leg)",
                  "metadata (document type matched to the question's intent, element type)",
                  "authority (document authority rank, origin weight)")


def _trusted(model: str) -> bool:
    try:
        from security.integrity import model_integrity
    except Exception:
        return True                       # no registry module: nothing to check against
    try:
        return bool(model_integrity.may_use(model))
    except Exception as error:
        logger.warning(f"trust check failed for {model}: {error}")
        return False


@dataclass
class RoleChoice:
    role: str
    kind: str
    purpose: str
    model: str = ""
    state: str = NOT_INSTALLED
    planned_model: str = ""                 # what core.hardware.model_plan picks for this machine
    hardware_tier: str = ""
    estimated_gb: Optional[float] = None
    free_gb: Optional[float] = None
    fits_now: Optional[bool] = None
    meets_requirements: bool = True
    reason: str = ""
    fallback: str = ""
    install_hint: str = ""
    evidence: Dict[str, Any] = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        return self.state in (INSTALLED, VERIFIED, FALLBACK, HYBRID) and self.meets_requirements

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["usable"] = self.usable
        return data


def _hardware():
    from core.hardware import available_model_memory_gb, model_plan, profile
    hw = profile()
    return hw, model_plan(hw), available_model_memory_gb()


def _installed() -> List[str]:
    try:
        from core.ollama_client import ollama_client
        return ollama_client.list_models()
    except Exception as error:
        logger.debug(f"model list unavailable: {error}")
        return []


def _matches(installed: List[str], wanted: str) -> Optional[str]:
    from core.ollama_client import ollama_client
    return next((name for name in installed if ollama_client._model_matches(name, wanted)), None)


def select(role: str, requirements: Optional[Dict[str, Any]] = None, probe: bool = False) -> RoleChoice:
    """The model for `role` on this machine now.

    requirements: {"max_gb": float}  refuse a model estimated larger than this
                  {"must_fit_now": True}  refuse one that does not fit in free memory
    """
    role = (role or "").upper()
    if role not in ROLES:
        raise ValueError(f"unknown role '{role}' - one of {', '.join(ROLES)}")
    spec = ROLES[role]
    requirements = dict(requirements or {})
    choice = RoleChoice(role=role, kind=spec["kind"], purpose=spec["purpose"])
    try:
        hw, plan, free = _hardware()
        choice.hardware_tier = hw.tier
        choice.planned_model = plan["slots"].get(spec["slot"], "")
        choice.free_gb = free
    except Exception as error:                      # hardware detection failed - say so
        choice.reason = f"hardware detection failed: {error}"
        choice.meets_requirements = False
        return choice
    installed = _installed()
    choice.install_hint = (f"install with STEP 1 : INSTALL (plans {choice.planned_model} for the {hw.tier} tier)"
                           if choice.planned_model else "")

    if spec["kind"] == "chat":
        if not installed:
            choice.state, choice.reason = RUNTIME_DOWN, "the local model runtime (Ollama) is not answering"
            return choice
        from core.model_router import model_router
        model = model_router.route(spec["slot"])
        actual = _matches(installed, model)
        if not actual:
            choice.model, choice.state = model, NOT_INSTALLED
            choice.reason = f"no trusted model for '{spec['slot']}' is installed"
            return choice
        if not _trusted(actual):
            # route() falls back to its first preference when nothing passes
            # the registry - that is a model to report, not to use.
            choice.model, choice.state = actual, UNTRUSTED
            choice.reason = "installed, but the trusted-model registry does not accept it (digest or signature)"
            choice.fallback = "re-pin the models (Model Center > Integrity) only if you trust what is installed"
            return choice
        choice.model, choice.state = actual, INSTALLED
        choice.reason = "installed and trusted"
        if choice.planned_model and not _matches([actual], choice.planned_model):
            planned_present = _matches(installed, choice.planned_model)
            rejected = _measured_rejection(planned_present, spec["slot"]) if planned_present else ""
            if rejected:
                # The router measured the planned model unsuitable for this slot and chose
                # another on purpose: that is the right model, not a fallback.
                choice.reason = f"measured choice - {rejected}; {actual} answers directly"
                choice.evidence["planned_model_rejected"] = {"model": planned_present, "why": rejected}
            else:
                choice.state = FALLBACK
                choice.fallback = (f"{choice.planned_model} is installed but was not chosen (not trusted) - using {actual}"
                                   if planned_present else f"the planned {choice.planned_model} is not installed - using {actual}")
        from core.orchestration.router import estimate_ram_gb
        choice.estimated_gb = estimate_ram_gb(actual)
        _apply_recorded_probe(choice, actual)
        if probe:
            _probe_chat(choice)
    elif spec["kind"] == "embedding":
        from core.capabilities import EMBEDDING_MODELS
        model = next((m for m in (_matches(installed, name) for name in EMBEDDING_MODELS) if m), None)
        if not installed:
            choice.state, choice.reason = RUNTIME_DOWN, "the local model runtime (Ollama) is not answering"
        elif not model:
            choice.reason = "no embedding model installed - vector search is off (full-text, entity, graph and " \
                            "table search still work)"
        else:
            choice.model, choice.state, choice.reason = model, INSTALLED, "installed"
            choice.estimated_gb = 1.2
            _apply_recorded_probe(choice, model)
            if probe:
                from core.capabilities import embedding_provider
                report = embedding_provider.check()
                choice.evidence = report.as_dict()
                choice.state = VERIFIED if report.ok else INSTALLED
                choice.reason = report.detail or choice.reason
    elif spec["kind"] == "voice":
        _voice_role(choice, spec["slot"])
    else:                                                # reranker
        try:
            from industrial.reranker import status
            info = status()
        except Exception as error:
            info = {"backend": "unavailable", "detail": str(error), "cross_encoder_active": False}
        choice.model = info.get("backend", "")
        choice.evidence = {k: info.get(k) for k in ("backend", "detail", "local_model_dir", "cross_encoder_active")}
        choice.evidence["searched_dirs"] = info.get("searched_dirs")
        if info.get("cross_encoder_active"):
            choice.state, choice.reason = INSTALLED, f"local cross-encoder loaded ({info.get('backend')})"
            if probe:
                _probe_reranker(choice)
        else:
            choice.state = HYBRID
            choice.reason = ("no local cross-encoder is installed (models/reranker, models/bge-reranker-v2-m3 and "
                             "models/qwen3-reranker are empty) - passages are ordered by the deterministic hybrid "
                             "ranker: " + "; ".join(HYBRID_SIGNALS))
            choice.install_hint = ("to rank (question, passage) pairs directly, put a local BGE-Reranker-v2-M3 or "
                                   "Qwen3-Reranker-0.6B folder in models/reranker (needs its local runtime)")
            if probe:
                _probe_reranker(choice)

    if choice.estimated_gb is not None and choice.free_gb is not None:
        choice.fits_now = choice.estimated_gb <= choice.free_gb
    if requirements.get("max_gb") is not None and choice.estimated_gb is not None \
            and choice.estimated_gb > float(requirements["max_gb"]):
        choice.meets_requirements = False
        choice.reason += f"; estimated {choice.estimated_gb} GB exceeds the {requirements['max_gb']} GB allowed"
    if requirements.get("must_fit_now") and choice.fits_now is False:
        choice.meets_requirements = False
        choice.reason += f"; needs about {choice.estimated_gb} GB, {choice.free_gb} GB free now"
    return choice


def _probe_chat(choice: RoleChoice) -> None:
    try:
        from core.ollama_client import ollama_client
        reply = ollama_client.chat(choice.model, [{"role": "user", "content": "Reply with the single word: ready"}],
                                   retries=0, timeout=120, task_type="classify", purpose="model role check")
        ok = "ready" in (reply or "").lower()
        choice.state = VERIFIED if ok else choice.state
        choice.evidence = {"probe_reply": (reply or "")[:80]}
        if not ok:
            choice.reason += "; the probe reply did not contain the expected word"
    except Exception as error:
        choice.evidence = {"probe_error": str(error)[:200]}
        choice.reason += f"; probe failed: {error}"


def _voice_role(choice: RoleChoice, slot: str) -> None:
    """Speech models are files on disk, loaded on demand. Present files are
    INSTALLED - never VERIFIED from a listing: STT_CHECK.bat transcribes known
    speech and a live listen to verify recognition."""
    try:
        from voice.offline import check_voice_models
        report = check_voice_models()
    except Exception as error:
        choice.reason = f"voice models not checked: {error}"
        return
    key = "whisper" if slot == "stt" else "chatterbox"
    info = report.get(key) or {}
    choice.evidence = {"directory": info.get("directory"), "missing": info.get("missing")}
    if not info.get("present"):
        choice.state = NOT_INSTALLED
        choice.reason = f"{key} files missing: {', '.join(info.get('missing') or [])}"
        choice.install_hint = "install with SETUP_VOICE.bat"
        if slot == "tts":
            choice.fallback = "Windows voice (SAPI) speaks the replies"
        return
    choice.state = INSTALLED
    if slot == "stt":
        choice.model = "whisper large-v3-turbo (models/whisper)"
        choice.estimated_gb = 4.8           # measured peak, CPU int8 (voice/stt_manager.py); CUDA float16 4.1
        choice.reason = "files present - loaded on the first listen; STT_CHECK.bat verifies recognition"
    else:
        choice.model = "chatterbox (models/chatterbox)"
        try:
            from voice import providers
            need = next((getattr(cls, "SYNTHESIS_NEED_GB") for cls in (providers.ChatterboxBridgeProvider,
                         providers.ChatterboxVoiceProvider) if getattr(cls, "SYNTHESIS_NEED_GB", None)), None)
            choice.estimated_gb = float(need) if need else None
        except Exception:
            choice.estimated_gb = None
        choice.reason = "files present - loaded for the first spoken reply"
        choice.fallback = "Windows voice (SAPI) when Chatterbox cannot load"


def _apply_recorded_probe(choice: RoleChoice, model: str) -> None:
    """A model whose last probe (MODEL_PROBE.bat / Model Center) passed all eight
    checks was VERIFIED by a real call - say so, with when. Fewer than eight, or a
    record for another digest, leaves it INSTALLED."""
    try:
        from core.model_calibration import record_for
        from agni_ops.models import verification_points
        record = record_for(model) or {}
    except Exception:
        return
    if not record or record.get("status") != "PASS":
        return
    points = verification_points(record)
    passed = sum(1 for p in points if p["status"] == "PASS")
    when = record.get("tested_at")
    stamp = __import__("time").strftime("%Y-%m-%d %H:%M", __import__("time").localtime(when)) if when else "unknown time"
    choice.evidence["recorded_probe"] = {"checks_passed": passed, "of": len(points), "tested_at": when}
    if passed == len(points):
        choice.state = VERIFIED
        choice.reason += f"; verified by the probe of {stamp} ({passed}/{len(points)} checks)"
    else:
        choice.reason += f"; last probe {stamp}: {passed}/{len(points)} checks passed - probe again to verify"


def _measured_rejection(model: str, slot: str) -> str:
    """Why the router set `model` aside for `slot`, when it was MEASURED - '' otherwise."""
    try:
        from core.model_router import model_router
        from core.model_calibration import think_false_leaks
    except Exception:
        return ""
    if slot in model_router.LATENCY_SLOTS and think_false_leaks(model):
        return f"{model} leaks its reasoning when told not to think (measured, database/model_status.json)"
    return ""


def _probe_reranker(choice: RoleChoice) -> None:
    """A real ranking: the passage that answers the question must come first."""
    rows = [{"chunk_text": "Canteen menu for the week: rice, dal and seasonal vegetables.", "retrieval_score": 0.5},
            {"chunk_text": "The relief valve set pressure on V-101 is 12 bar g; test it every 12 months.", "retrieval_score": 0.5},
            {"chunk_text": "Parking permits are renewed each January at the security office.", "retrieval_score": 0.5}]
    try:
        from industrial.reranker import rerank
        ranked, info = rerank("What is the relief valve set pressure of V-101?", rows, dense_scores=[0.5, 0.5, 0.5])
        first = (ranked[0].get("chunk_text") or "") if ranked else ""
        ok = "relief valve" in first
        choice.evidence["self_test"] = {"passed": ok, "backend": info.get("backend"), "first": first[:80]}
        choice.reason += "; ranking self-test " + ("passed" if ok else "FAILED - the answering passage was not first")
        if ok and choice.state == INSTALLED:
            choice.state = VERIFIED
    except Exception as error:
        choice.evidence["self_test"] = {"passed": False, "error": str(error)[:200]}
        choice.reason += f"; ranking self-test failed: {error}"


def roster(probe: bool = False) -> List[Dict[str, Any]]:
    """Every role on this machine - for the Model Center."""
    return [select(role, probe=probe).to_dict() for role in ROLES]
