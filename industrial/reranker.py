"""
Phase D (ranking half) - evidence reranking with BGE-Reranker-v2-M3.

Honest engineering note, because this matters for the demo:

  BGE-Reranker-v2-M3 is a CROSS-ENCODER. It scores a (query, passage) pair
  directly - that is what makes it better than cosine similarity. Ollama's
  API surface is built for generation and embeddings, not pair scoring, so
  the reranker is used through a local cross-encoder runtime when one is
  installed (FlagEmbedding or sentence-transformers, pointed at a LOCAL model
  directory), and otherwise a deterministic hybrid scorer takes over.

The hybrid scorer is not a stub: it combines the dense retrieval score with
lexical overlap, taxonomy-term overlap, exact equipment-tag matches, element
type and OCR confidence. It is measurably better than raw cosine ordering and
needs nothing beyond the standard library and numpy.

Whichever path runs, the backend name is recorded on every result so a report
can state how its evidence was ranked. Nothing here reaches the network: the
cross-encoder is loaded with local_files_only, and a missing local model
means the fallback, never a download.
"""

from __future__ import annotations

import math
import os
import re
import threading
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.logger import get_logger
from industrial import ROOT

logger = get_logger("industrial.rerank")

LOCAL_RERANKER_DIRS = [
    ROOT / "models" / "reranker",
    ROOT / "models" / "bge-reranker-v2-m3",
    ROOT / "models" / "qwen3-reranker",
]

# Qwen3-Reranker does not use a sequence-classification head, so it cannot be
# loaded as a CrossEncoder. It is a causal LM asked a yes/no question, and the
# score is the relative probability of "yes" against "no" in the next token.
# These are the strings from the model card; changing them changes the scores.
QWEN3_PREFIX = ("<|im_start|>system\nJudge whether the Document meets the "
                "requirements based on the Query and the Instruct provided. "
                "Note that the answer can only be \"yes\" or \"no\"."
                "<|im_end|>\n<|im_start|>user\n")
QWEN3_SUFFIX = ("<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n")
QWEN3_INSTRUCT = ("Given a plant engineering question, decide whether the "
                  "document passage contains information that answers it.")

_LOCK = threading.Lock()
_BACKEND: Optional["RerankBackend"] = None

_STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "is", "are", "was", "were",
    "be", "been", "with", "as", "at", "by", "from", "that", "this", "it", "its", "which",
    "what", "how", "when", "where", "who", "why", "do", "does", "did", "has", "have", "had",
    "not", "no", "any", "all", "can", "could", "should", "would", "we", "you", "i", "our",
}


def _tokens(text: str) -> List[str]:
    return [t for t in re.findall(r"[a-z0-9]+(?:[./-][a-z0-9]+)*", (text or "").lower())
            if t not in _STOPWORDS and len(t) > 1]


@dataclass
class RerankBackend:
    name: str      # cross_encoder_flagembedding | cross_encoder_sentence_transformers
                   # | qwen3_local_adapter | hybrid_local
    detail: str
    model: Any = None
    tokenizer: Any = None

    @property
    def is_cross_encoder(self) -> bool:
        """Does this backend score the (query, passage) PAIR directly?

        True for the two cross-encoders and for the Qwen3 adapter: all three
        read the query and the passage together, which is the property that
        makes reranking better than cosine similarity. The hybrid scorer does
        not, and says so.
        """
        return self.name.startswith("cross_encoder") or self.name == "qwen3_local_adapter"


def _find_local_model_dir() -> Optional[Path]:
    for d in LOCAL_RERANKER_DIRS:
        try:
            if d.is_dir() and any(d.glob("*.json")):
                return d
        except OSError:
            continue
    return None


def _looks_like_qwen3_reranker(directory: Path) -> bool:
    """Qwen3-Reranker is a causal LM, not a classifier.

    Loading it as a CrossEncoder produces a randomly initialised
    classification head on top of the LM - which "works" in the sense that it
    returns numbers, and those numbers are noise. Detecting it by its
    architecture is what stops that silent failure.
    """
    config = directory / "config.json"
    if not config.is_file():
        return False
    try:
        import json
        data = json.loads(config.read_text(encoding="utf-8"))
    except Exception:
        return False
    architectures = " ".join(data.get("architectures") or []).lower()
    model_type = str(data.get("model_type", "")).lower()
    return "qwen3" in model_type and "causallm" in architectures


def _load_qwen3_adapter(directory: Path) -> Optional[RerankBackend]:
    """The local Python adapter for Qwen3-Reranker.

    Ollama exposes no reranking endpoint, and this model is not a
    cross-encoder, so neither of the usual paths can drive it. It is scored
    here directly: the pair is put in the model's prompt format and the score
    is softmax(logits["yes"], logits["no"])["yes"] for the next token.

    Cost, honestly: this needs torch and transformers (~2 GB installed) and
    holds 0.6B parameters resident. On the CPU-only target hardware that is
    real memory competing with the chat model, which is why it is used only
    when its weights have been deliberately placed on disk, and why the
    hybrid scorer remains the default.
    """
    try:
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer
    except Exception as error:
        logger.debug(f"Qwen3 reranker adapter unavailable (no torch/transformers): {error}")
        return None
    try:
        tokenizer = AutoTokenizer.from_pretrained(str(directory), local_files_only=True,
                                                  padding_side="left")
        model = AutoModelForCausalLM.from_pretrained(
            str(directory), local_files_only=True, torch_dtype=torch.float32).eval()
        # Resolve the two token ids ONCE. If the tokenizer does not produce
        # single tokens for these, the protocol does not apply to this model
        # and pretending otherwise would produce meaningless scores.
        yes_id = tokenizer.convert_tokens_to_ids("yes")
        no_id = tokenizer.convert_tokens_to_ids("no")
        if yes_id is None or no_id is None or yes_id == no_id:
            logger.warning("Qwen3 reranker: the yes/no protocol does not apply to "
                           "this tokenizer; not using it")
            return None
        logger.info(f"reranker: Qwen3 local adapter from {directory}")
        return RerankBackend(
            "qwen3_local_adapter",
            f"Qwen3-Reranker({directory.name}) scored locally through a yes/no "
            f"logit adapter, local files only",
            model={"model": model, "yes": yes_id, "no": no_id}, tokenizer=tokenizer)
    except Exception as error:
        logger.warning(f"Qwen3 reranker could not be loaded: {error}")
        return None


def _load_backend() -> RerankBackend:
    """Try a real pair scorer first, strictly offline, then fall back."""
    local_dir = _find_local_model_dir()
    if local_dir:
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

        if _looks_like_qwen3_reranker(local_dir):
            backend = _load_qwen3_adapter(local_dir)
            if backend is not None:
                return backend
            # Deliberately do NOT fall through to CrossEncoder here: it would
            # load this model with an untrained head and return noise.
            return RerankBackend(
                "hybrid_local",
                f"a Qwen3 reranker is present at {local_dir.name} but torch/transformers "
                f"are not installed, so it cannot be scored; using the deterministic "
                f"hybrid scorer")

        try:
            from FlagEmbedding import FlagReranker
            model = FlagReranker(str(local_dir), use_fp16=False)
            logger.info(f"reranker: FlagEmbedding cross-encoder from {local_dir}")
            return RerankBackend("cross_encoder_flagembedding",
                                 f"FlagReranker({local_dir.name}), local files only", model)
        except Exception as e:
            logger.debug(f"FlagEmbedding reranker unavailable: {e}")
        try:
            from sentence_transformers import CrossEncoder
            model = CrossEncoder(str(local_dir), local_files_only=True)
            logger.info(f"reranker: sentence-transformers cross-encoder from {local_dir}")
            return RerankBackend("cross_encoder_sentence_transformers",
                                 f"CrossEncoder({local_dir.name}), local files only", model)
        except Exception as e:
            logger.debug(f"sentence-transformers reranker unavailable: {e}")

    return RerankBackend(
        "hybrid_local",
        "deterministic hybrid scorer (dense score + lexical overlap + taxonomy terms + "
        "equipment tag match + element type + OCR confidence). No cross-encoder model "
        "directory found at models/reranker.")


def _qwen3_scores(backend: RerankBackend, query: str, passages: List[str]) -> List[float]:
    """Score pairs with the Qwen3 yes/no protocol. One passage at a time:
    batching 0.6B activations on a CPU-only machine is how this runs out of
    memory mid-answer."""
    import torch

    model = backend.model["model"]
    yes_id, no_id = backend.model["yes"], backend.model["no"]
    tokenizer = backend.tokenizer
    scores: List[float] = []
    with torch.no_grad():
        for passage in passages:
            prompt = (f"{QWEN3_PREFIX}<Instruct>: {QWEN3_INSTRUCT}\n"
                      f"<Query>: {query}\n<Document>: {passage}{QWEN3_SUFFIX}")
            tokens = tokenizer(prompt, return_tensors="pt", truncation=True,
                               max_length=2048)
            logits = model(**tokens).logits[0, -1]
            pair = torch.stack([logits[no_id], logits[yes_id]])
            scores.append(float(torch.softmax(pair, dim=0)[1]))
    return scores


def get_backend() -> RerankBackend:
    global _BACKEND
    if _BACKEND is None:
        with _LOCK:
            if _BACKEND is None:
                _BACKEND = _load_backend()
    return _BACKEND


def reset_backend() -> None:
    global _BACKEND
    with _LOCK:
        _BACKEND = None


# ------------------------------------------------------------------- hybrid

def _lexical_score(query_tokens: List[str], text: str) -> float:
    """BM25-lite overlap on a single document: saturating term frequency with
    a length penalty. Range roughly 0..1."""
    if not query_tokens:
        return 0.0
    doc_tokens = _tokens(text)
    if not doc_tokens:
        return 0.0
    counts = Counter(doc_tokens)
    length_norm = 1.0 / (1.0 + math.log1p(len(doc_tokens) / 120.0))
    hits = 0.0
    for t in set(query_tokens):
        tf = counts.get(t, 0)
        if tf:
            hits += (tf / (tf + 1.2))
        elif len(t) > 4:
            # partial credit for a prefix match ("corrosion" vs "corroded")
            if any(d.startswith(t[:5]) for d in counts):
                hits += 0.25
    return min(1.0, (hits / len(set(query_tokens))) * length_norm * 1.6)


def _taxonomy_overlap(query: str, text: str) -> Tuple[float, List[str]]:
    try:
        from industrial.taxonomy import get_taxonomy
        tax = get_taxonomy()
    except Exception:
        return 0.0, []
    q_terms = {t["term"] for t in tax.detect_terms(query)}
    if not q_terms:
        return 0.0, []
    d_terms = {t["term"] for t in tax.detect_terms(text[:4000])}
    shared = sorted(q_terms & d_terms)
    return (len(shared) / len(q_terms)), shared


def _tag_overlap(query: str, row: Dict[str, Any]) -> float:
    try:
        from industrial.taxonomy import get_taxonomy
        tags = {t["tag"].upper().replace("-", "")
                for t in get_taxonomy().detect_equipment_tags(query)}
    except Exception:
        return 0.0
    if not tags:
        return 0.0
    haystack = " ".join(str(row.get(k) or "") for k in ("equipment", "chunk_text", "section"))
    haystack = haystack.upper().replace("-", "")
    return 1.0 if any(t in haystack for t in tags) else 0.0


_ELEMENT_WEIGHT = {"table": 1.06, "heading": 1.02, "text": 1.0, "list": 1.0,
                   "figure": 0.92, "header": 0.9, "footer": 0.85}


def hybrid_score(query: str, row: Dict[str, Any], dense_score: float) -> Dict[str, Any]:
    q_tokens = _tokens(query)
    text = row.get("chunk_text", "")
    lexical = _lexical_score(q_tokens, text)
    tax_overlap, shared_terms = _taxonomy_overlap(query, text)
    tag = _tag_overlap(query, row)

    score = (0.52 * max(0.0, dense_score)
             + 0.26 * lexical
             + 0.12 * tax_overlap
             + 0.10 * tag)
    score *= _ELEMENT_WEIGHT.get(row.get("element_type", "text"), 1.0)

    # An OCR'd chunk read with low confidence is still usable evidence, but it
    # should not outrank a clean native-text chunk on a tie.
    conf = row.get("ocr_confidence")
    if conf is not None:
        try:
            score *= 0.85 + 0.15 * float(conf)
        except (TypeError, ValueError):
            pass

    # A user's own document outranks general corpus background at equal
    # relevance - the corpus must never speak over the plant's own records.
    if row.get("document_class") == "user_document":
        score *= 1.05

    return {"score": round(float(score), 6),
            "components": {"dense": round(float(dense_score), 4), "lexical": round(lexical, 4),
                           "taxonomy_overlap": round(tax_overlap, 4), "equipment_tag": tag,
                           "shared_terms": shared_terms[:6]}}


# ------------------------------------------------------------------- public

def rerank(query: str, rows: List[Dict[str, Any]], dense_scores: Optional[List[float]] = None,
           top_k: Optional[int] = None) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Rerank candidate chunk rows for a query.

    Returns (ordered_rows, info). Each returned row gains 'rerank_score' and
    'rerank_detail'; `info` records the backend so callers can report it.
    """
    if not rows:
        return [], {"backend": get_backend().name, "detail": get_backend().detail, "scored": 0}

    dense = dense_scores or [float(r.get("retrieval_score", 0.0)) for r in rows]
    backend = get_backend()
    info: Dict[str, Any] = {"backend": backend.name, "detail": backend.detail,
                            "scored": len(rows)}

    if backend.is_cross_encoder:
        try:
            passages = [(r.get("chunk_text") or "")[:2000] for r in rows]
            pairs = [[query, passage] for passage in passages]
            if backend.name == "qwen3_local_adapter":
                raw = _qwen3_scores(backend, query, passages)
            elif backend.name == "cross_encoder_flagembedding":
                raw = backend.model.compute_score(pairs, normalize=True)
            else:
                raw = backend.model.predict(pairs)
            scores = [float(s) for s in (raw if isinstance(raw, (list, tuple)) else [raw])]
            for r, s, d in zip(rows, scores, dense):
                r["rerank_score"] = s
                r["retrieval_score"] = d
                r["rerank_detail"] = {"pair_score": round(s, 6), "dense": round(d, 4),
                                      "scored_by": backend.name}
            ordered = sorted(rows, key=lambda r: r["rerank_score"], reverse=True)
            return (ordered[:top_k] if top_k else ordered), info
        except Exception as e:
            logger.warning(f"cross-encoder rerank failed, falling back to hybrid: {e}")
            info["fallback_reason"] = str(e)
            info["backend"] = "hybrid_local"

    for r, d in zip(rows, dense):
        result = hybrid_score(query, r, d)
        r["rerank_score"] = result["score"]
        r["retrieval_score"] = d
        r["rerank_detail"] = result["components"]
    ordered = sorted(rows, key=lambda r: r["rerank_score"], reverse=True)
    return (ordered[:top_k] if top_k else ordered), info


def status() -> dict:
    backend = get_backend()
    local_dir = _find_local_model_dir()
    return {
        "backend": backend.name,
        "detail": backend.detail,
        "cross_encoder_active": backend.is_cross_encoder,
        "local_model_dir": str(local_dir) if local_dir else None,
        "searched_dirs": [str(d) for d in LOCAL_RERANKER_DIRS],
        "pair_scoring": backend.is_cross_encoder,
        "note": ("Place a local copy of BGE-Reranker-v2-M3 (a cross-encoder) or "
                 "Qwen3-Reranker-0.6B (scored through the local yes/no adapter, needs "
                 "torch) in models/reranker to enable true pair ranking. Without one, "
                 "the deterministic hybrid scorer is used and every result says so."),
    }
