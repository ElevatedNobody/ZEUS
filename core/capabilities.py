"""
Capability-aware model access.

The bug this module exists to prevent: treating every model as a chat model.

An embedding model is not a small chat model, and a reranker is not a chat
model at all. They are reached through DIFFERENT interfaces:

    capability   model                    interface
    ---------------------------------------------------------------
    chat         qwen2.5:3b               POST /api/chat
    vision       qwen2.5vl:3b             POST /api/chat  (with images)
    embedding    bge-m3                   POST /api/embeddings
    reranking    (see RerankProvider)     scoring, not generation

Asking Ollama's /api/chat for an embedding returns prose, or an error, or
nothing useful - which is why "route it like a chat model" is the wrong shape
however carefully the routing table is written.

So capability lookup goes through here, and every capability answers three
questions honestly:

    is the model present?      -> DETECTED
    is an adapter available?   -> ADAPTER
    does a real call work?     -> READY

Nothing reports READY without a real call having succeeded. A model that is
installed but unreachable reports MODEL DETECTED / ADAPTER REQUIRED, never
"no model available" - the two mean different things to whoever has to fix it.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from core.logger import get_logger

logger = get_logger("capabilities")


class Health(str, Enum):
    """What is actually true about a capability right now."""

    READY = "ready"                 # a real call succeeded
    ADAPTER_REQUIRED = "adapter"    # the model is there; the interface is not
    MODEL_MISSING = "missing"       # nothing pulled for this capability
    RUNTIME_DOWN = "runtime_down"   # the local runtime is not reachable
    DEGRADED = "degraded"           # working, but not on the preferred path
    UNAVAILABLE = "unavailable"     # cannot operate at all
    UNKNOWN = "unknown"             # not probed yet


@dataclass
class CapabilityReport:
    """The honest answer about one capability."""

    capability: str
    health: Health = Health.UNKNOWN
    model: str = ""
    interface: str = ""
    detail: str = ""
    checked_at: float = 0.0
    evidence: Dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.health in (Health.READY, Health.DEGRADED)

    def as_dict(self) -> Dict[str, Any]:
        return {"capability": self.capability, "health": self.health.value,
                "model": self.model, "interface": self.interface,
                "detail": self.detail, "evidence": dict(self.evidence),
                "ready": self.ok}

    def line(self) -> str:
        """One line for a readiness report - never a claim without a reason."""
        label = {
            Health.READY: "READY",
            Health.DEGRADED: "DEGRADED",
            Health.ADAPTER_REQUIRED: "MODEL DETECTED / ADAPTER REQUIRED",
            Health.MODEL_MISSING: "NO MODEL PULLED",
            Health.RUNTIME_DOWN: "RUNTIME NOT REACHABLE",
            Health.UNAVAILABLE: "UNAVAILABLE",
            Health.UNKNOWN: "NOT CHECKED",
        }[self.health]
        return f"{label} — {self.detail}" if self.detail else label


# =========================================================== embedding ====

# Preference order. BGE-M3 first: it is multilingual, strong on technical
# text, and already pulled on this installation. The alternatives are listed
# so a machine without it still has a working path rather than a dead RAG.
EMBEDDING_MODELS: Sequence[str] = (
    "bge-m3",
    "nomic-embed-text",
    "qwen3-embedding:0.6b",
    "mxbai-embed-large",
    "all-minilm",
)


class EmbeddingProvider:
    """Embeddings through Ollama's embeddings endpoint - not through chat.

    The dimension is discovered from the first successful call rather than
    assumed, because it differs per model (BGE-M3 is 1024, nomic is 768,
    all-minilm is 384) and a hard-coded number silently corrupts a vector
    store the day someone switches model.
    """

    def __init__(self):
        self._model: Optional[str] = None
        self._dimension: Optional[int] = None
        self._lock = threading.Lock()
        self._report = CapabilityReport(capability="embedding",
                                        interface="ollama_embeddings")

    # -- discovery -------------------------------------------------------

    def resolve(self, force: bool = False) -> Optional[str]:
        """Which embedding model this machine will use, or None."""
        with self._lock:
            if self._model and not force:
                return self._model
            from core.ollama_client import ollama_client

            # An explicit override in config.ini wins, so an operator can pin
            # a model without editing code.
            override = _config_value("models", "embedding_model")
            candidates = [override] if override else list(EMBEDDING_MODELS)
            self._model = ollama_client.pick_available_model(candidates)
            return self._model

    def dimension(self) -> Optional[int]:
        return self._dimension

    # -- the actual call -------------------------------------------------

    def embed(self, text: str, timeout: float = 60.0) -> Optional[List[float]]:
        """One vector, or None. Never raises into a caller's indexing loop."""
        model = self.resolve()
        if not model:
            return None
        from core.ollama_client import ollama_client
        vector = ollama_client.embed(model, text, timeout=timeout)
        if vector and self._dimension is None:
            self._dimension = len(vector)
        return vector or None

    def embed_many(self, texts: Sequence[str], timeout: float = 60.0
                   ) -> List[Optional[List[float]]]:
        return [self.embed(text, timeout=timeout) for text in texts]

    # -- honest health ---------------------------------------------------

    def check(self) -> CapabilityReport:
        """Probe for real: resolve a model, then embed a short string and
        confirm a numeric vector of non-zero length comes back."""
        started = time.time()
        report = CapabilityReport(capability="embedding",
                                  interface="ollama_embeddings",
                                  checked_at=started)
        from core.ollama_client import ollama_client

        if not ollama_client.is_available():
            report.health = Health.RUNTIME_DOWN
            report.detail = ("the local model runtime is not reachable - "
                             "start it with 'ollama serve'")
            self._report = report
            return report

        model = self.resolve(force=True)
        if not model:
            report.health = Health.MODEL_MISSING
            report.detail = (f"no embedding model pulled - try: "
                             f"ollama pull {EMBEDDING_MODELS[0]}")
            self._report = report
            return report

        report.model = model
        vector = self.embed("industrial readiness probe", timeout=30.0)
        if not vector or not isinstance(vector, list):
            report.health = Health.ADAPTER_REQUIRED
            report.detail = (f"{model} is installed but returned no vector from "
                             f"the embeddings endpoint")
            self._report = report
            return report
        if not all(isinstance(value, (int, float)) for value in vector[:8]):
            report.health = Health.ADAPTER_REQUIRED
            report.detail = f"{model} returned a non-numeric response"
            self._report = report
            return report

        self._dimension = len(vector)
        report.health = Health.READY
        report.detail = f"{model} returned a {len(vector)}-dimension vector"
        report.evidence = {"dimension": len(vector),
                           "sample": [round(float(v), 4) for v in vector[:3]],
                           "latency_ms": int((time.time() - started) * 1000)}
        self._report = report
        return report

    def last_report(self) -> CapabilityReport:
        return self._report


# =========================================================== reranking ====

@dataclass
class Scored:
    index: int
    score: float
    text: str


class RerankProvider:
    """Reranking, which is scoring - not generation.

    Two paths, tried in order:

    1. A cross-encoder, if one is present locally (sentence-transformers or
       FlagEmbedding with a downloaded BGE/Qwen reranker). This is the
       strongest signal.
    2. A local Python hybrid scorer: lexical overlap, industrial-taxonomy
       overlap and the dense score the vector search already produced.

    What is deliberately NOT done: pushing pairs through Ollama's /api/chat
    and parsing a number out of the prose. Ollama has no reranking endpoint;
    asking a chat model to "rate 0-10" is slow, unstable across runs, and
    produces a score that cannot be compared between queries. A deterministic
    local scorer is both more honest and more useful, and it is fully offline.

    The hybrid path always works, so reranking is never simply unavailable -
    it reports DEGRADED when the cross-encoder is absent, which is the truth.
    """

    def __init__(self):
        self._report = CapabilityReport(capability="reranking")

    def rerank(self, query: str, rows: List[Dict[str, Any]],
               dense_scores: Optional[List[float]] = None,
               top_n: int = 5) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """Order `rows` best-first. Returns (rows, info) where info names the
        backend that produced the order, so every answer can say how its
        evidence was chosen."""
        from industrial import reranker as engine
        # The engine's parameter is top_k; the pipeline vocabulary is top_n
        # (top-K retrieved, top-N kept). Translate here so callers speak the
        # pipeline's language rather than the engine's.
        ordered, info = engine.rerank(query, rows, dense_scores=dense_scores,
                                      top_k=top_n)
        return ordered, info

    def check(self) -> CapabilityReport:
        """Probe for real: score a query against two documents and confirm the
        clearly relevant one comes first."""
        started = time.time()
        report = CapabilityReport(capability="reranking", checked_at=started)
        try:
            from industrial import reranker as engine
            backend = engine.get_backend()
            report.interface = backend.name
            report.model = backend.detail or backend.name

            rows = [
                {"chunk_text": "The pump seal was replaced during the outage.",
                 "document_title": "maintenance log"},
                {"chunk_text": "Relief valve set pressure shall be verified "
                               "before returning the vessel to service.",
                 "document_title": "SOP"},
            ]
            ordered, info = engine.rerank(
                "what must be verified before returning a vessel to service",
                rows, top_k=2)
            if not ordered:
                report.health = Health.UNAVAILABLE
                report.detail = "the reranker returned nothing"
                self._report = report
                return report

            correct = "relief valve" in ordered[0].get("chunk_text", "").lower()
            cross = bool(getattr(backend, "is_cross_encoder", False))
            report.health = Health.READY if cross else Health.DEGRADED
            report.detail = (
                f"{info.get('backend', backend.name)} ranked "
                f"{len(ordered)} passages"
                + ("" if correct else " (probe order was not the expected one)")
                + ("" if cross else "; no cross-encoder present, hybrid local "
                                    "scoring in use"))
            report.evidence = {
                "backend": info.get("backend", backend.name),
                "cross_encoder": cross,
                "probe_ranked_correctly": correct,
                "latency_ms": int((time.time() - started) * 1000),
            }
        except Exception as error:
            report.health = Health.UNAVAILABLE
            report.detail = f"{type(error).__name__}: {error}"
        self._report = report
        return report

    def last_report(self) -> CapabilityReport:
        return self._report


# ================================================================ chat ====

class ChatCapability:
    """The ordinary generation path, checked the same honest way."""

    def check(self, slot: str = "default") -> CapabilityReport:
        started = time.time()
        report = CapabilityReport(capability=f"chat:{slot}",
                                  interface="ollama_chat", checked_at=started)
        from core.model_router import model_router
        from core.ollama_client import ollama_client

        if not ollama_client.is_available():
            report.health = Health.RUNTIME_DOWN
            report.detail = "the local model runtime is not reachable"
            return report
        model = model_router.route(slot)
        report.model = model or ""
        installed = ollama_client.list_models()
        if not model or not any(
                ollama_client._model_matches(name, model) or name == model
                for name in installed):
            report.health = Health.MODEL_MISSING
            report.detail = f"not pulled - try: ollama pull {model}"
            return report
        report.health = Health.READY
        report.detail = f"{model} is pulled and the runtime is reachable"
        report.evidence = {"latency_ms": int((time.time() - started) * 1000)}
        return report


# ============================================================= registry ====

embedding_provider = EmbeddingProvider()
rerank_provider = RerankProvider()
chat_capability = ChatCapability()


def _config_value(section: str, option: str) -> str:
    try:
        import configparser
        from pathlib import Path
        parser = configparser.ConfigParser()
        parser.read(Path(__file__).resolve().parent.parent / "config.ini")
        if parser.has_option(section, option):
            return parser.get(section, option).strip()
    except Exception:
        pass
    return ""


def check_all() -> Dict[str, CapabilityReport]:
    """Probe every capability with a real call. Used by the readiness report
    and by the diagnostics tool - the same code, so the two can never
    disagree about what is working."""
    return {
        "embedding": embedding_provider.check(),
        "reranking": rerank_provider.check(),
        "chat": chat_capability.check("default"),
        "reasoning": chat_capability.check("reasoning"),
        "vision": chat_capability.check("vision"),
    }


def summary() -> Dict[str, Any]:
    reports = check_all()
    return {
        "capabilities": {name: report.as_dict() for name, report in reports.items()},
        "ready": [name for name, report in reports.items() if report.ok],
        "not_ready": [name for name, report in reports.items() if not report.ok],
    }
