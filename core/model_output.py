"""
Checks on what a model returned - standard library only, so the installer and
the diagnostics can use them before (or without) the application's packages.
core/ollama_client.py re-exports both.
"""

from __future__ import annotations

import math
import re
from typing import Tuple

_THINK_BLOCK = re.compile(r"<think>.*?</think>\s*", re.DOTALL | re.IGNORECASE)


def strip_thinking(text: str) -> str:
    """Remove a <think>...</think> trace, and an unterminated leading one."""
    if not text:
        return text
    cleaned = _THINK_BLOCK.sub("", text)
    if cleaned.lstrip().lower().startswith("<think>") and "</think>" not in cleaned.lower():
        return ""
    return cleaned.strip()


def validate_vector(vector) -> Tuple[bool, str]:
    """Is this a usable embedding? (ok, reason).

    The setup run of 2026-09-24 reported bge-m3 as a '0-dim vector' because
    it read the embedding out of an HTTP 500 error body. A vector is accepted
    only if it exists, has at least one dimension, every value is a finite
    number, and it is not all zeros (a zero vector has no direction, so
    cosine similarity against it is undefined)."""
    if vector is None:
        return False, "no vector returned"
    try:
        values = [float(v) for v in vector]
    except (TypeError, ValueError):
        return False, "vector contains non-numeric values"
    if not values:
        return False, "0-dimensional embedding output"
    if not all(math.isfinite(v) for v in values):
        return False, f"vector contains NaN/Inf ({len(values)}-dim)"
    if not any(values):
        return False, f"all-zero vector ({len(values)}-dim)"
    return True, f"{len(values)}-dim vector"
