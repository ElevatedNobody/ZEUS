"""
What the model tests learned about each installed model, kept on disk so the
runtime can act on it: database/model_status.json.

The runtime reads one fact from here today - whether a thinking model honours
`think: false`. Found on 2026-09-25: this machine's qwen3:4b build ignores
it. Sent think=false, Ollama switches off its reasoning parser and the model's
entire reasoning ("Hmm, the user wants me to...") arrives as the ANSWER; left
on, the reasoning goes to a separate field and the answer is clean. qwen3:8b
and deepseek-r1:8b honour the switch. The difference is per build, so it is
measured (agni_ops/models.py) rather than guessed from the name.

Written by TEST_AGNI.bat / STEP 1 : INSTALL / REPAIR.bat. Read by
core/ollama_client.py and STEP 2 : START's preflight.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Dict, Iterable, Optional

_cache: Dict = {}
_cache_mtime = -1.0


def path() -> Path:
    from core.paths import data_dir
    return data_dir() / "model_status.json"


def load() -> Dict[str, Dict]:
    """model -> last test record. Re-read only when the file changes."""
    global _cache, _cache_mtime
    target = path()
    try:
        mtime = target.stat().st_mtime
    except OSError:
        return {}
    if mtime != _cache_mtime:
        try:
            _cache = json.loads(target.read_text(encoding="utf-8")).get("models", {})
        except Exception:
            _cache = {}
        _cache_mtime = mtime
    return _cache


def save(records: Iterable[Dict]) -> None:
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    current: Dict[str, Dict] = {}
    if target.is_file():
        try:
            current = json.loads(target.read_text(encoding="utf-8")).get("models", {})
        except Exception:
            current = {}
    for record in records:
        name = record.get("model")
        if name:
            current[name] = {**record, "tested_at": time.time()}
    target.write_text(json.dumps({"models": current}, indent=2), encoding="utf-8")


def record_for(model: str) -> Optional[Dict]:
    records = load()
    if model in records:
        return records[model]
    base = model[:-len(":latest")] if model.endswith(":latest") else model
    for name, record in records.items():
        if name in (base, f"{base}:latest") or record.get("installed_as") == model:
            return record
    return None


def think_false_leaks(model: str) -> bool:
    """True when this model was MEASURED to leak its reasoning into the answer
    when asked not to think."""
    record = record_for(model) or {}
    return bool((record.get("thinking_control") or {}).get("think_false_leaks"))


def think_false_honoured(model: str) -> Optional[bool]:
    """True = measured to answer cleanly with think=false; False = measured
    to leak; None = never measured on this machine."""
    record = record_for(model) or {}
    control = record.get("thinking_control") or {}
    if "think_false_leaks" not in control:
        return None
    return not control["think_false_leaks"]
