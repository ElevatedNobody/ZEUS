"""
Trusted model registry: a model is used only if it is the model that was
installed and approved.

At setup (or when an admin presses "pin models") the digest Ollama reports for
every installed model is written to `config/trusted_models.json`, signed with
the same local secret that keys the audit chain. At routing time the model's
CURRENT digest (from Ollama's /api/tags) is compared with the pinned one:

    pinned + matches    -> TRUSTED
    pinned + different  -> TAMPERED   (never routed; security event)
    not pinned          -> UNPINNED   (routed only if [security]
                                       require_trusted_models = false)
    registry unsigned / signature wrong -> the whole registry is untrusted

This proves a model has not been swapped or modified since it was pinned. It
does not prove where the model originally came from - that is established at
setup, by pulling from the Ollama library over the setup network.
"""

from __future__ import annotations

import configparser
import hashlib
import hmac
import json
import threading
import time
from pathlib import Path
from typing import Dict, Optional

from core.logger import get_logger

logger = get_logger("security.integrity")

TRUSTED, TAMPERED, UNPINNED, UNKNOWN = "trusted", "tampered", "unpinned", "unknown"


def registry_path() -> Path:
    from core.paths import ROOT
    return ROOT / "config" / "trusted_models.json"


def _key() -> bytes:
    from security.audit_chain import audit_chain
    return audit_chain._get_key()


def _sign(models: Dict[str, str]) -> str:
    body = json.dumps(models, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hmac.new(_key(), body, hashlib.sha256).hexdigest()


class ModelIntegrity:
    def __init__(self):
        self._lock = threading.Lock()
        self._digests: Dict[str, str] = {}
        self._fetched_at = 0.0
        self._registry: Optional[Dict] = None

    # -- live digests from Ollama ---------------------------------------------

    def live_digests(self, force: bool = False) -> Dict[str, str]:
        with self._lock:
            if not force and time.time() - self._fetched_at < 30 and self._digests:
                return dict(self._digests)
        try:
            from core.ollama_client import _get, ollama_client
            response = _get(f"{ollama_client.host}/api/tags", timeout=5.0)
            response.raise_for_status()
            digests = {m["name"]: m.get("digest", "") for m in response.json().get("models", [])}
        except Exception as error:
            logger.debug(f"could not read model digests: {error}")
            digests = {}
        with self._lock:
            self._digests = digests
            self._fetched_at = time.time()
        return dict(digests)

    # -- the pinned registry -------------------------------------------------------

    def load(self) -> Optional[Dict]:
        path = registry_path()
        if not path.exists():
            self._registry = None
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as error:
            logger.warning(f"trusted model registry unreadable: {error}")
            self._registry = {"invalid": True, "models": {}}
            return self._registry
        models = data.get("models", {})
        signature_ok = hmac.compare_digest(data.get("signature", ""), _sign(models))
        data["signature_valid"] = signature_ok
        if not signature_ok:
            logger.error("trusted model registry signature does not verify")
        self._registry = data
        return data

    def pin(self, names: Optional[list] = None, by: str = "") -> Dict:
        """Pin the current digests. Returns the written registry."""
        live = self.live_digests(force=True)
        if not live:
            raise RuntimeError("Ollama is not reachable - nothing to pin")
        existing = (self.load() or {}).get("models", {}) if registry_path().exists() else {}
        models = dict(existing) if isinstance(existing, dict) else {}
        for name, digest in live.items():
            if names is None or name in names:
                models[name] = digest
        registry = {"version": 1, "pinned_at": time.time(), "pinned_by": by or "setup",
                    "models": models, "signature": _sign(models)}
        path = registry_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(registry, indent=2), encoding="utf-8")
        self._registry = None
        try:
            from security.audit_chain import audit_chain
            audit_chain.append("models_pinned", f"{len(models)} model digest(s) pinned", actor=by)
        except Exception:
            pass
        return registry

    @staticmethod
    def required() -> bool:
        from core.paths import config_path
        parser = configparser.ConfigParser()
        try:
            parser.read(config_path(), encoding="utf-8")
        except Exception:
            return True
        return parser.getboolean("security", "require_trusted_models", fallback=True)

    def status_of(self, model: str) -> str:
        registry = self._registry if self._registry is not None else self.load()
        if registry is None or not registry.get("signature_valid", False):
            return UNPINNED if registry is None else TAMPERED
        pinned = registry.get("models", {})
        live = self.live_digests()
        current = live.get(model, "")
        if model not in pinned:
            return UNPINNED
        if not current:
            return UNKNOWN
        return TRUSTED if hmac.compare_digest(pinned[model], current) else TAMPERED

    def execution_refusal(self, model: str) -> Optional[str]:
        """Checked where a model is RUN (core/ollama_client.py), not only where
        it is chosen: the router's last-resort fallback returns its first
        preference even when routing rejected it, so without this a model whose
        digest no longer matches the pinned one could still be executed.

        Refuses only on the evidence of a VALID registry: a tampered model, or
        an unpinned one while [security] require_trusted_models is on. When the
        registry itself does not verify (restored without its key, damaged),
        routing already reports that and SECURITY_CHECK fails on it; refusing
        every model then would leave the machine without any model at all."""
        registry = self._registry if self._registry is not None else self.load()
        if not registry or not registry.get("signature_valid"):
            return None
        name = model if model in registry.get("models", {}) or ":" in model else f"{model}:latest"
        status = self.status_of(name)
        if status == TAMPERED:
            try:
                from security import events
                events.record("model_integrity_failure", f"{name}: refused at run time - digest differs "
                                                         f"from the pinned one", severity="critical")
            except Exception:
                pass
            return f"{name} was refused: its digest differs from the one pinned in the trusted model registry"
        if status == UNPINNED and self.required():
            return (f"{name} was refused: it is not in the trusted model registry "
                    f"(pin it in Model Center > Integrity only if you trust it)")
        return None

    def may_use(self, model: str) -> bool:
        """The routing rule."""
        status = self.status_of(model)
        if status == TRUSTED:
            return True
        if status == TAMPERED:
            try:
                from security import events
                events.record("model_integrity_failure",
                              f"{model}: digest differs from the pinned one", severity="critical")
            except Exception:
                pass
            return False
        if status in (UNPINNED, UNKNOWN):
            if registry_path().exists() and self.required():
                if status == UNPINNED:
                    try:
                        from security import events
                        events.record("model_untrusted", f"{model} is not in the trusted registry",
                                      severity="warning", persist=False)
                    except Exception:
                        pass
                    return False
            return True
        return False

    def report(self) -> Dict:
        registry = self.load()
        live = self.live_digests(force=True)
        rows = []
        names = sorted(set(live) | set((registry or {}).get("models", {})))
        for name in names:
            rows.append({"model": name, "status": self.status_of(name),
                         "installed": name in live,
                         "digest": (live.get(name) or "")[:12]})
        return {"registry": str(registry_path()),
                "exists": registry is not None,
                "signature_valid": bool(registry and registry.get("signature_valid")),
                "pinned_at": (registry or {}).get("pinned_at"),
                "required": self.required(), "models": rows}


model_integrity = ModelIntegrity()
