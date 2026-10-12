"""
ZEUS Model Router
Chooses which local model to use for a given task type, matching the
"Ollama is the head, models are specialists" design: routine/small
decisions go to tiny fast models, structured generation goes to models
good at strict JSON, prose/reasoning goes to a stronger reasoning model,
and dedicated models handle vision/embeddings/code - never one model
doing everything.
"""

import configparser
from pathlib import Path
from typing import Dict

from core.logger import get_logger
from core.ollama_client import ollama_client

logger = get_logger("router")

ROOT = Path(__file__).resolve().parent.parent

# Task types, each mapped to the actual models pulled for this ZEUS
# install (in preference order - first pulled model wins, later ones are
# fallbacks). Every model below was chosen for a reason tied to the task:
DEFAULT_PREFERENCES: Dict[str, list] = {
    # Order = preference. The first PULLED model wins, so each list starts with
    # the Qwen3-generation model STEP 1 : INSTALL installs for the hardware it
    # finds, and keeps the earlier models as fallbacks so an install that has
    # not been upgraded still works. See core/hardware.py:model_plan.

    # Quick conversational replies.
    "fast": ["qwen3:4b", "phi3.5:3.8b-mini-instruct-q4_K_M", "gemma2:2b-instruct-q4_K_M",
             "qwen2.5:3b-instruct-q4_K_M"],

    # Small yes/no or short-list decisions.
    "classify": ["qwen3:1.7b", "granite3.1-dense:2b-instruct-q4_K_M",
                 "smollm2:1.7b-instruct-q4_K_M", "qwen2.5:0.5b-instruct-q4_K_M", "qwen3:4b"],

    # Long-form reasoning, RAG answers, failure analysis. DeepSeek-R1 8B (the
    # Qwen3-based distill) on an 8 GB GPU; Qwen3 8B is an equally capable
    # fallback with its own thinking mode.
    "reasoning": ["deepseek-r1:8b", "qwen3:8b", "deepseek-r1:14b", "qwen3:14b",
                  "mistral:7b-instruct-q4_K_M", "deepseek-r1:7b-qwen-distill-q4_K_M"],

    # Structured JSON generation (slides, sheets, schedules). Qwen3 with
    # thinking switched off (core/ollama_client.py) respects a strict schema.
    "json_structured": ["qwen3:8b", "qwen3:4b", "qwen2.5:3b-instruct-q4_K_M",
                        "qwen2.5-coder:7b-instruct-q4_K_M"],

    # Code generation/explanation - dedicated coding models meaningfully
    # outperform general models here.
    #
    # DeepSeek-Coder-V2 16B is LAST on purpose and must not be promoted: at q4
    # it needs ~10 GB, more than an 8 GB GPU or a 16 GB laptop can spare.
    "code": ["qwen2.5-coder:7b", "qwen2.5-coder:14b", "qwen2.5-coder:7b-instruct-q4_K_M",
             "codegemma:7b-instruct-q4_K_M", "qwen2.5-coder:3b",
             "deepseek-coder-v2:16b-lite-instruct-q4_K_M"],

    # Real visual understanding (photographs, scanned pages, handwriting).
    "vision": ["qwen3-vl:8b", "qwen3-vl:4b", "qwen2.5vl:3b-q4_K_M", "qwen2.5vl:3b",
               "moondream:1.8b-q4", "moondream"],

    # NOTE: there is deliberately no "embedding" or "reranker" slot here.
    # This router picks CHAT models and reaches them through /api/chat. An
    # embedding model is reached through /api/embeddings and a reranker is
    # not a generation model at all, so routing them here produced a model
    # name that the chat path could not use. See core/capabilities.py.

    # Multilingual - Qwen3 is multilingual; aya stays as a fallback.
    "multilingual": ["qwen3:8b", "aya-expanse:8b-q4_K_M"],

    "default": ["qwen3:8b", "qwen3:4b", "qwen2.5:3b-instruct-q4_K_M",
                "mistral:7b-instruct-q4_K_M"],
}


def _load_overrides() -> Dict[str, str]:
    cfg_path = ROOT / "config.ini"
    overrides = {}
    if cfg_path.exists():
        cfg = configparser.ConfigParser()
        cfg.read(cfg_path)
        if cfg.has_section("models"):
            overrides = dict(cfg.items("models"))
    return overrides


class ModelRouter:
    def __init__(self):
        self._cache: Dict[str, str] = {}

    # Capabilities that are NOT chat, and must not be resolved here. Routing
    # them through this table is the bug that made a working BGE-M3 install
    # report as "No models available for task 'embedding'", so it is now an
    # error at the call site rather than a wrong answer downstream.
    NON_CHAT_CAPABILITIES = {
        "embedding": "core.capabilities.embedding_provider",
        "reranker": "core.capabilities.rerank_provider",
        "reranking": "core.capabilities.rerank_provider",
    }

    def route(self, task_type: str = "default") -> str:
        """Return the best available CHAT model name for a task type."""
        if task_type in self.NON_CHAT_CAPABILITIES:
            raise ValueError(
                f"'{task_type}' is not a chat capability and cannot be routed to a "
                f"chat model - use {self.NON_CHAT_CAPABILITIES[task_type]} instead")
        if task_type in self._cache:
            return self._cache[task_type]

        overrides = _load_overrides()
        key = f"{task_type}_model"
        preferred = DEFAULT_PREFERENCES.get(task_type, DEFAULT_PREFERENCES["default"])
        if key in overrides and overrides[key].strip():
            preferred = [overrides[key].strip()] + [m for m in preferred if m != overrides[key].strip()]
        model = self._pick_trusted(preferred)
        if model is not None and task_type in self.LATENCY_SLOTS:
            model = self._prefer_quick_answering(model, preferred)

        if model is None:
            logger.warning(f"No models available for task '{task_type}'")
            # Fall back to the FIRST preferred model for this task type, not
            # a hardcoded text model - substituting the wrong model for a
            # vision or embedding task would silently produce garbage
            # instead of a clear "model not found" error from Ollama.
            preferred = DEFAULT_PREFERENCES.get(task_type, DEFAULT_PREFERENCES["default"])
            model = preferred[0]

        self._cache[task_type] = model
        return model

    # Slots whose whole point is a quick answer.
    LATENCY_SLOTS = ("fast", "classify")

    def _prefer_quick_answering(self, model: str, preferred: list) -> str:
        """A thinking model MEASURED to ignore think=false must be allowed to
        think (core/ollama_client.py) - correct, but no longer quick: this
        machine's qwen3:4b wrote 17,000 characters of reasoning for a
        one-line JSON reply. For a latency slot, the next installed, trusted
        preference that answers directly is used instead. Only a measured
        leak moves the slot (core/model_calibration.py)."""
        try:
            from core.model_calibration import think_false_leaks
        except Exception:
            return model
        if not think_false_leaks(model):
            return model
        remaining = [m for m in preferred if not ollama_client._model_matches(model, m)]
        while remaining:
            alternative = self._pick_trusted(remaining)
            if alternative is None:
                break
            if not think_false_leaks(alternative):
                logger.info(f"{model} leaks its reasoning when asked not to think (measured) - "
                            f"using {alternative} for quick replies")
                return alternative
            remaining = [m for m in remaining if not ollama_client._model_matches(alternative, m)]
        return model

    @staticmethod
    def _pick_trusted(preferred: list):
        """First preferred model that is pulled AND passes the trusted model
        registry (security/integrity.py). A model whose digest no longer
        matches the pinned one is skipped and reported, never used."""
        installed = ollama_client.list_models()
        if not installed:
            return None
        try:
            from security.integrity import model_integrity
        except Exception:
            model_integrity = None
        for candidate in preferred:
            for name in installed:
                if not ollama_client._model_matches(name, candidate):
                    continue
                if model_integrity is not None and not model_integrity.may_use(name):
                    logger.warning(f"skipping {name}: not trusted by the model registry")
                    continue
                return name
        return None

    def invalidate(self):
        """Forget cached choices (after models are pulled, pinned or removed)."""
        self._cache.clear()

    def classify_task(self, text: str) -> str:
        """Very lightweight heuristic classifier - not an LLM call, keeps latency low."""
        lowered = text.lower()
        code_markers = ("def ", "function", "class ", "```", "traceback", "error:", "bug", "fix this code")
        if any(m in lowered for m in code_markers):
            return "code"
        if len(text) > 400 or "explain" in lowered or "why" in lowered or "compare" in lowered:
            return "reasoning"
        if "screen" in lowered or "screenshot" in lowered:
            return "vision"
        return "fast"


model_router = ModelRouter()
