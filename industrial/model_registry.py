"""
Phase J - the local model registry.

Model behaviour is declared in one place instead of being scattered through
feature code:

  config/industrial/model_registry.json   capability metadata (what each model
                                          can do, size, quantisation, context,
                                          industrial use, cautions)
  core/model_router.py                    task -> model preference order
                                          (unchanged, still the single source
                                          of truth for ROUTING)

This module joins the two, checks them against what Ollama actually has
locally, and writes health/test status to config/industrial/model_health.json.

It never pulls a model. If a slot has no local model, that is reported as a
gap - ZEUS does not substitute a different model behind the user's back.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.logger import get_logger
from industrial import MODEL_HEALTH_PATH, MODEL_REGISTRY_PATH

logger = get_logger("industrial.models")

_CACHE: Optional["ModelRegistry"] = None


class ModelRegistry:
    def __init__(self, data: dict):
        self.data = data
        self.version = data.get("version", "0")
        self.models: List[dict] = data.get("models", [])
        self.slot_requirements: Dict[str, dict] = data.get("slot_requirements", {})
        self.policy: Dict[str, Any] = data.get("policy", {})
        self._by_name = {m["model"]: m for m in self.models}

    # ------------------------------------------------------------- lookups

    def get(self, model_name: str) -> Optional[dict]:
        if model_name in self._by_name:
            return self._by_name[model_name]
        base = model_name.split(":")[0]
        for name, spec in self._by_name.items():
            if name.split(":")[0] == base:
                return spec
        return None

    def with_capability(self, capability: str) -> List[dict]:
        return [m for m in self.models if m.get("capabilities", {}).get(capability)]

    def for_task(self, task_type: str) -> List[dict]:
        return [m for m in self.models if task_type in m.get("task_types", [])]

    def routing_table(self) -> Dict[str, List[str]]:
        """The live routing preferences from core/model_router.py - not a copy."""
        try:
            from core.model_router import DEFAULT_PREFERENCES
            return {k: list(v) for k, v in DEFAULT_PREFERENCES.items()}
        except Exception as e:
            logger.warning(f"could not read model router preferences: {e}")
            return {}

    # -------------------------------------------------------------- health

    def installed_models(self) -> List[str]:
        try:
            from core.ollama_client import ollama_client
            if not ollama_client.is_available():
                return []
            return sorted(ollama_client.list_models())
        except Exception as e:
            logger.warning(f"could not list local models: {e}")
            return []

    @staticmethod
    def _is_installed(name: str, installed: List[str]) -> Optional[str]:
        for m in installed:
            if m == name or m.startswith(name + "-") or m.startswith(name + "_"):
                return m
            if ":" not in name and m.split(":")[0] == name:
                return m
        return None

    def health_check(self, probe: bool = False, write: bool = True) -> Dict[str, Any]:
        """Check every registered model against the local Ollama library.

        probe=True additionally runs a tiny local inference per text model and
        one embedding call, so 'test_status' means genuinely exercised rather
        than merely present. Probing costs a few seconds per model."""
        installed = self.installed_models()
        daemon_up = bool(installed) or self._daemon_reachable()

        results = []
        for spec in self.models:
            name = spec["model"]
            resolved = self._is_installed(name, installed)
            entry = {
                "model": name,
                "ollama_name": spec.get("ollama_name", name),
                "resolved_name": resolved,
                "parameters": spec.get("parameters"),
                "quantization": spec.get("quantization"),
                "approx_disk_gb": spec.get("approx_disk_gb"),
                "task_types": spec.get("task_types", []),
                "capabilities": spec.get("capabilities", {}),
                "language": spec.get("language", []),
                "local_path": spec.get("local_path"),
                "health": "present" if resolved else ("absent" if daemon_up else "unknown"),
                "test_status": "not_tested",
                "test_detail": None,
            }
            if probe and resolved:
                entry.update(self._probe(spec, resolved))
            results.append(entry)

        slots = {}
        routing = self.routing_table()
        for slot, requirement in self.slot_requirements.items():
            # embedding/reranker deliberately have no chat-routing entry
            # (core/model_router.py); their slot is served by the preferred
            # model named in the registry itself.
            preferences = routing.get(slot) or [p for p in [requirement.get("preferred")] if p]
            chosen = next((self._is_installed(p, installed) for p in preferences
                           if self._is_installed(p, installed)), None)
            slots[slot] = {
                "required": requirement.get("required", False),
                "preferred": requirement.get("preferred"),
                "preference_order": preferences,
                "resolved": chosen,
                "status": "ok" if chosen else ("BLOCKER" if requirement.get("required")
                                               else "degraded"),
                "reason": requirement.get("reason", ""),
            }

        report = {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "registry_version": self.version,
            "ollama_daemon": "reachable" if daemon_up else "not reachable",
            "installed_models": installed,
            "unregistered_models": [m for m in installed if self.get(m) is None],
            "models": results,
            "slots": slots,
            "present": sum(1 for r in results if r["health"] == "present"),
            "absent": sum(1 for r in results if r["health"] == "absent"),
            "probed": probe,
            "policy": self.policy,
        }
        if write:
            try:
                MODEL_HEALTH_PATH.parent.mkdir(parents=True, exist_ok=True)
                MODEL_HEALTH_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")
            except OSError as e:
                logger.warning(f"could not write model health file: {e}")
        return report

    @staticmethod
    def _daemon_reachable() -> bool:
        try:
            from core.ollama_client import ollama_client
            return ollama_client.is_available(force=True)
        except Exception:
            return False

    @staticmethod
    def _probe(spec: dict, resolved: str) -> dict:
        """One tiny local call to confirm the model actually answers."""
        from core.ollama_client import ollama_client
        caps = spec.get("capabilities", {})
        started = time.perf_counter()
        try:
            if caps.get("embedding") and not caps.get("reranking"):
                vec = ollama_client.embed(resolved, "industrial test", timeout=45.0,
                                          purpose="Model registry probe")
                ok = bool(vec)
                detail = f"embedding dim {len(vec)}" if vec else "no vector returned"
            elif caps.get("reranking"):
                # A cross-encoder is not a chat model; presence is all Ollama
                # can confirm here. The real ranking path is reported by
                # industrial.reranker.status().
                return {"test_status": "present_only",
                        "test_detail": "cross-encoder - scored via industrial.reranker, "
                                       "not through a chat/embedding call"}
            elif caps.get("vision"):
                return {"test_status": "present_only",
                        "test_detail": "vision model - exercised by the offline test with a "
                                       "generated image, not probed here"}
            else:
                out = ollama_client.chat(resolved, [{"role": "user", "content": "Reply with OK."}],
                                         retries=0, timeout=60.0, task_type="probe",
                                         purpose="Model registry probe")
                ok = bool(out) and not out.strip().startswith("[")
                detail = (out or "")[:80]
            elapsed = round(time.perf_counter() - started, 2)
            return {"test_status": "pass" if ok else "fail",
                    "test_detail": f"{detail} ({elapsed}s)"}
        except Exception as e:
            return {"test_status": "fail", "test_detail": str(e)[:160]}

    # --------------------------------------------------------------- views

    def summary(self, health: Optional[dict] = None) -> str:
        health = health or self.load_health() or self.health_check(write=False)
        lines = ["MODEL REGISTRY",
                 f"  registry version : {health.get('registry_version')}",
                 f"  ollama daemon    : {health.get('ollama_daemon')}",
                 f"  models present   : {health.get('present')} / {len(self.models)}"]
        for slot, s in (health.get("slots") or {}).items():
            lines.append(f"  slot {slot:<16}: {s['status']:<8} -> {s['resolved'] or 'none'}")
        return "\n".join(lines)

    @staticmethod
    def load_health() -> Optional[dict]:
        if MODEL_HEALTH_PATH.exists():
            try:
                return json.loads(MODEL_HEALTH_PATH.read_text(encoding="utf-8"))
            except ValueError:
                return None
        return None

    def table(self) -> List[dict]:
        """Flat capability table - MODEL / CAPABILITY / TASK / VISION / CODING /
        REASONING / EMBEDDING / RERANKING / LANGUAGE / SIZE / QUANT / PATH."""
        health = {m["model"]: m for m in (self.load_health() or {}).get("models", [])}
        rows = []
        for spec in self.models:
            caps = spec.get("capabilities", {})
            h = health.get(spec["model"], {})
            rows.append({
                "model": spec["model"],
                "task_types": ", ".join(spec.get("task_types", [])),
                "vision": caps.get("vision", False),
                "coding": caps.get("coding", False),
                "reasoning": caps.get("reasoning", False),
                "embedding": caps.get("embedding", False),
                "reranking": caps.get("reranking", False),
                "language": ", ".join(spec.get("language", [])),
                "size": spec.get("parameters"),
                "quantization": spec.get("quantization"),
                "location": spec.get("ollama_name", spec["model"]),
                "health": h.get("health", "unknown"),
                "test_status": h.get("test_status", "not_tested"),
            })
        return rows


def load_registry(path: Optional[Path] = None) -> ModelRegistry:
    path = Path(path or MODEL_REGISTRY_PATH)
    if not path.exists():
        logger.warning(f"model registry not found at {path}")
        return ModelRegistry({"models": []})
    try:
        return ModelRegistry(json.loads(path.read_text(encoding="utf-8")))
    except (ValueError, OSError) as e:
        logger.error(f"could not load model registry: {e}")
        return ModelRegistry({"models": []})


def get_registry() -> ModelRegistry:
    global _CACHE
    if _CACHE is None:
        _CACHE = load_registry()
    return _CACHE
