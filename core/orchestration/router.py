"""
Capability router: pick the SMALLEST model that can actually do the job.

The existing `core.model_router.ModelRouter` maps a task-type slot to a
concrete pulled model. This layer sits on top of it and adds the three things
a 16 GB / Iris Xe machine actually needs:

1. **Deterministic-first.** Many tasks need no model at all. Reading a
   spreadsheet, totalling a column, converting a file, listing equipment -
   those are Python. The router says so, and the orchestrator skips the LLM
   entirely rather than paying seconds of latency to be told what pandas
   already knows.

2. **Smallest capable model.** A task kind plus a complexity gives a ladder
   of slots (e.g. simple chat -> "fast" before "default" before "reasoning").
   The first slot with a pulled model wins.

3. **RAM awareness.** Every candidate carries an estimated resident size,
   parsed from its own name (parameter count x bytes-per-parameter for the
   quantisation). If the machine does not have room, the router steps DOWN the
   ladder instead of thrashing swap. Nothing is loaded speculatively, and
   `release()` asks Ollama to evict a model when a heavy step is finished.

This module never replaces the old router - it delegates to it, so every
existing call to `model_router.route("reasoning")` behaves exactly as before.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from core.logger import get_logger
from core.model_router import DEFAULT_PREFERENCES, model_router
from core.ollama_client import ollama_client
from core.orchestration.contracts import Complexity, ModelChoice, TaskKind

logger = get_logger("orchestration.router")

# Bytes per parameter by quantisation tag. q4 is what this project pulls.
_BYTES_PER_PARAM = {
    "q2": 0.40, "q3": 0.50, "q4": 0.60, "q5": 0.72, "q6": 0.85, "q8": 1.10,
    "f16": 2.05, "fp16": 2.05,
}
_DEFAULT_BYTES_PER_PARAM = 0.60          # assume q4 when the tag says nothing
_RUNTIME_OVERHEAD_GB = 0.7               # KV cache + server, measured rough

# Slot ladders per task kind and complexity: first entry is tried first, and
# each later entry is a genuinely smaller or cheaper fallback.
_LADDERS: Dict[Tuple[TaskKind, Complexity], List[str]] = {
    (TaskKind.CHAT, Complexity.SIMPLE): ["fast", "classify", "default"],
    (TaskKind.CHAT, Complexity.MODERATE): ["default", "fast"],
    (TaskKind.CHAT, Complexity.HARD): ["reasoning", "default", "fast"],

    (TaskKind.REASONING, Complexity.SIMPLE): ["default", "fast"],
    (TaskKind.REASONING, Complexity.MODERATE): ["reasoning", "default"],
    (TaskKind.REASONING, Complexity.HARD): ["reasoning", "default"],

    (TaskKind.CODE, Complexity.SIMPLE): ["code", "json_structured", "default"],
    (TaskKind.CODE, Complexity.MODERATE): ["code", "json_structured"],
    (TaskKind.CODE, Complexity.HARD): ["code", "reasoning"],

    (TaskKind.VISION, Complexity.SIMPLE): ["vision"],
    (TaskKind.VISION, Complexity.MODERATE): ["vision"],
    (TaskKind.VISION, Complexity.HARD): ["vision"],

    (TaskKind.RAG, Complexity.SIMPLE): ["default", "fast"],
    (TaskKind.RAG, Complexity.MODERATE): ["reasoning", "default"],
    (TaskKind.RAG, Complexity.HARD): ["reasoning", "default"],

    (TaskKind.DOCUMENT_READ, Complexity.SIMPLE): ["default", "fast"],
    (TaskKind.DOCUMENT_READ, Complexity.MODERATE): ["reasoning", "default"],
    (TaskKind.DOCUMENT_READ, Complexity.HARD): ["reasoning", "default"],

    # Document generation: the STRUCTURE is a schema (strict JSON), the prose
    # is narrative. Structured slot first - it is smaller and more reliable.
    (TaskKind.WORD, Complexity.SIMPLE): ["json_structured", "default"],
    (TaskKind.WORD, Complexity.MODERATE): ["json_structured", "reasoning"],
    (TaskKind.WORD, Complexity.HARD): ["reasoning", "json_structured"],
    (TaskKind.EXCEL, Complexity.SIMPLE): ["json_structured", "default"],
    (TaskKind.EXCEL, Complexity.MODERATE): ["json_structured", "reasoning"],
    (TaskKind.EXCEL, Complexity.HARD): ["json_structured", "reasoning"],
    (TaskKind.POWERPOINT, Complexity.SIMPLE): ["json_structured", "default"],
    (TaskKind.POWERPOINT, Complexity.MODERATE): ["json_structured", "reasoning"],
    (TaskKind.POWERPOINT, Complexity.HARD): ["reasoning", "json_structured"],

    (TaskKind.MULTI_STEP, Complexity.SIMPLE): ["json_structured", "default"],
    (TaskKind.MULTI_STEP, Complexity.MODERATE): ["reasoning", "json_structured"],
    (TaskKind.MULTI_STEP, Complexity.HARD): ["reasoning", "json_structured"],
}

# Kinds whose work is done by Python, not by a model. Listing these explicitly
# is the point: it is the difference between "an assistant that calls an LLM
# for everything" and one that only calls it when judgement is required.
DETERMINISTIC_KINDS = {
    TaskKind.OCR,               # OCR engines extract text; no LLM needed
    TaskKind.PDF,               # parse/merge/split are library calls
    TaskKind.CONVERSION,        # binary conversion is never a model's job
    TaskKind.FILE_OPS,          # filesystem operations
    TaskKind.INDUSTRIAL_DATA,   # SQL against the local plant database
    TaskKind.AUTOMATION,        # desktop control
    TaskKind.MEMORY,            # store/recall
}

_PARAM_PATTERN = re.compile(r"[:\-](\d+(?:\.\d+)?)b\b", re.IGNORECASE)


def estimate_ram_gb(model_name: str) -> float:
    """Estimate resident size from the model's own tag.

    'qwen2.5:3b-instruct-q4_K_M' -> 3 x 0.60 + overhead. This is an estimate,
    not a measurement, and it is used only to order candidates and to refuse
    something that clearly will not fit - never to report a precise figure.
    """
    if not model_name:
        return 0.0
    lowered = model_name.lower()
    match = _PARAM_PATTERN.search(lowered)
    if not match:
        # Embedding/reranker models carry no size tag and are small.
        return 1.2 if ("bge" in lowered or "embed" in lowered) else 4.5
    billions = float(match.group(1))
    bytes_per_param = _DEFAULT_BYTES_PER_PARAM
    for tag, value in _BYTES_PER_PARAM.items():
        if tag in lowered:
            bytes_per_param = value
            break
    return round(billions * bytes_per_param + _RUNTIME_OVERHEAD_GB, 2)


def available_ram_gb() -> float:
    """Memory a model could use right now: free system RAM plus free VRAM on
    an NVIDIA GPU (Ollama places layers on the GPU first and spills the rest
    to system RAM). A conservative assumption if nothing can be measured."""
    try:
        import psutil
        ram = psutil.virtual_memory().available / (1024 ** 3)
    except Exception:
        return 8.0
    vram = 0.0
    try:
        from core.hardware import profile
        gpu = profile().nvidia
        vram = gpu.vram_free_gb if gpu else 0.0
    except Exception:
        vram = 0.0
    return round(ram + vram, 2)


class CapabilityRouter:
    """Chooses a model for a task, or declares that no model is required."""

    def __init__(self):
        self._loaded: Dict[str, float] = {}      # model -> estimated GB

    # ------------------------------------------------------------ routing --

    def route_for(self, kind: TaskKind, complexity: Complexity = Complexity.SIMPLE,
                  context_tokens: int = 0, allow_deterministic: bool = True,
                  prefer_slot: str = "") -> ModelChoice:
        """Return the model to use, with the reason it was chosen.

        `prefer_slot` lets the agent that will actually do the work say what it
        needs - a report agent wants the small structured-output model, and
        should not be handed a 7B reasoning model just because the sentence
        was long.
        """
        if allow_deterministic and kind in DETERMINISTIC_KINDS:
            return ModelChoice(
                model="", task_type="deterministic", deterministic=True,
                reason=f"{kind.value} is handled by Python tools - no model needed")

        if complexity == Complexity.TRIVIAL:
            return ModelChoice(model="", task_type="deterministic", deterministic=True,
                               reason="deterministic path - no model needed")

        ladder = _LADDERS.get((kind, complexity))
        if ladder is None:
            ladder = _LADDERS.get((TaskKind.CHAT, complexity), ["default", "fast"])

        if prefer_slot:
            ladder = [prefer_slot] + [slot for slot in ladder if slot != prefer_slot]

        # A long context genuinely needs a bigger model; a short one does not.
        if context_tokens > 6000 and "reasoning" not in ladder:
            ladder = ["reasoning"] + ladder

        budget = available_ram_gb()
        first_choice: Optional[ModelChoice] = None
        for slot in ladder:
            model = self._resolve_slot(slot)
            if not model:
                continue
            size = estimate_ram_gb(model)
            candidate = ModelChoice(
                model=model, task_type=slot, estimated_ram_gb=size,
                reason=f"{kind.value}/{complexity.value} -> '{slot}' slot")
            if first_choice is None:
                first_choice = candidate
            if self._fits(model, size, budget):
                if candidate is not first_choice:
                    candidate.reason += (f"; stepped down from "
                                         f"{first_choice.model} to fit "
                                         f"{budget:.1f} GB free")
                return candidate
            logger.info(f"{model} (~{size} GB) does not fit in {budget} GB free; "
                        f"trying a smaller slot")

        if first_choice is not None:
            first_choice.reason += "; no smaller model available, using it anyway"
            return first_choice

        # Nothing pulled at all: name what SHOULD be there rather than
        # silently substituting an unrelated model.
        slot = ladder[0]
        wanted = DEFAULT_PREFERENCES.get(slot, DEFAULT_PREFERENCES["default"])[0]
        return ModelChoice(model=wanted, task_type=slot,
                           estimated_ram_gb=estimate_ram_gb(wanted),
                           reason=f"no model pulled for '{slot}' - "
                                  f"try: ollama pull {wanted}")

    def route_slot(self, slot: str) -> str:
        """Direct slot access for CHAT slots, delegating to the existing router.

        Embedding and reranking are not chat slots and raise here, by design:
        they have their own interfaces in core.capabilities.
        """
        return model_router.route(slot)

    def _resolve_slot(self, slot: str) -> Optional[str]:
        try:
            model = model_router.route(slot)
        except Exception as error:
            logger.warning(f"slot '{slot}' could not be resolved: {error}")
            return None
        return model or None

    def _fits(self, model: str, size_gb: float, budget_gb: float) -> bool:
        if model in self._loaded:       # already resident - no new allocation
            return True
        return size_gb <= max(1.0, budget_gb - 0.8)   # leave headroom for the OS

    # ------------------------------------------------------- bookkeeping --

    def note_loaded(self, model: str):
        if model:
            self._loaded[model] = estimate_ram_gb(model)

    def loaded_models(self) -> Dict[str, float]:
        return dict(self._loaded)

    def release(self, model: str) -> bool:
        """Ask Ollama to evict a model now instead of waiting out its idle
        timer. Used after a heavy step so the next step has room."""
        self._loaded.pop(model, None)
        if not model or not ollama_client.is_available():
            return False
        try:
            from core.ollama_client import _post
            _post(f"{ollama_client.host}/api/generate",
                  json={"model": model, "prompt": "", "keep_alive": 0},
                  timeout=8.0)
            logger.info(f"released {model} from memory")
            return True
        except Exception as error:
            logger.debug(f"could not release {model}: {error}")
            return False

    def release_all(self) -> int:
        released = 0
        for model in list(self._loaded):
            if self.release(model):
                released += 1
        return released

    # ---------------------------------------------------------- reporting --

    def describe(self) -> Dict[str, object]:
        """What the router would do right now - used by diagnostics and the
        'system information' tool, so the routing table is inspectable."""
        # A slot always resolves to SOMETHING - the first preference is
        # returned even when nothing is pulled, so the caller can be told what
        # to install. `pulled` is the honest field: it says whether that model
        # is actually on this machine.
        try:
            installed = set(ollama_client.list_models())
        except Exception:
            installed = set()
        rows = []
        # CHAT slots only. Embedding and reranking are separate capabilities
        # with their own interfaces, reported by core.capabilities - listing
        # them here would resolve them through the chat router, which is the
        # mistake this separation exists to prevent.
        for slot in ("fast", "classify", "default", "reasoning", "json_structured",
                     "code", "vision"):
            model = self._resolve_slot(slot) or ""
            pulled = any(ollama_client._model_matches(name, model)
                         for name in installed) if model else False
            rows.append({"slot": slot, "model": model, "pulled": pulled,
                         "estimated_ram_gb": estimate_ram_gb(model)})

        from core.capabilities import embedding_provider, rerank_provider
        capabilities = {}
        for name, provider in (("embedding", embedding_provider),
                               ("reranking", rerank_provider)):
            report = provider.last_report()
            capabilities[name] = {"health": report.health.value,
                                  "model": report.model,
                                  "interface": report.interface,
                                  "detail": report.detail}
        return {
            "available_ram_gb": available_ram_gb(),
            "resident_models": self.loaded_models(),
            "slots": rows,
            "capabilities": capabilities,
            "deterministic_kinds": sorted(k.value for k in DETERMINISTIC_KINDS),
        }


capability_router = CapabilityRouter()
