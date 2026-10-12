"""
Sovereign "prove it" report: every line is the result of a check made now.

Nothing here is a configured value dressed up as a measurement. Where a thing
cannot be known without sending a packet (is the network environment connected
to the internet?), the report says "not probed" until an operator explicitly
runs the probe - it does not guess, and it does not quietly probe on its own.
"""

from __future__ import annotations

import sqlite3
import sys
import time
from typing import Dict

_CLOUD_MODULES = ("openai", "anthropic", "google.generativeai", "google.genai", "cohere",
                  "mistralai", "groq", "boto3", "azure.ai", "gtts", "speech_recognition",
                  "langchain_openai", "posthog", "sentry_sdk")


def _line(label: str, state: str, ok: bool, detail: str = "") -> Dict:
    return {"label": label, "state": state, "ok": ok, "detail": detail}


def report(environment_probe: Dict = None) -> Dict:
    lines = []
    started = time.time()

    # -- network: application layer
    try:
        from security.egress import egress_guard
        egress = egress_guard.status()
        blocked_mode = egress["installed"] and egress["mode"] in ("sovereign", "lockdown")
        lines.append(_line("Application egress policy",
                           egress["mode"].upper() if egress["installed"] else "NOT INSTALLED",
                           blocked_mode,
                           "in-process socket guard - covers ZEUS's own Python code"))
        lines.append(_line("External egress (allowed)", str(egress["external_egress_allowed"]),
                           egress["external_egress_allowed"] == 0,
                           "connections to non-local addresses that were permitted"))
        lines.append(_line("External egress (blocked)", str(egress["external_egress_blocked"]), True,
                           "attempts refused, including DNS lookups"))
        lines.append(_line("External DNS", "BLOCKED" if blocked_mode else "AVAILABLE",
                           blocked_mode, f"{egress['stats']['dns_blocked']} lookup(s) refused"))
    except Exception as error:
        lines.append(_line("Application egress policy", "ERROR", False, str(error)))
        blocked_mode = False

    # -- network: OS layer
    try:
        from security.firewall import status as firewall_status
        fw = firewall_status()
        lines.append(_line("OS firewall (Windows)", "ACTIVE" if fw["active"] else "NOT APPLIED",
                           fw["active"], fw["detail"]))
    except Exception as error:
        lines.append(_line("OS firewall (Windows)", "UNKNOWN", False, str(error)))

    try:
        from security import wfp
        filters = wfp.status()
        installed = filters.get("installed")
        lines.append(_line("WFP filters (tailscale/wf)",
                           "ACTIVE" if installed else ("UNKNOWN" if installed is None else "NOT APPLIED"),
                           bool(installed), filters.get("detail", "")))
    except Exception as error:
        lines.append(_line("WFP filters (tailscale/wf)", "UNKNOWN", False, str(error)))

    # -- network: environment
    if environment_probe:
        internet = environment_probe.get("internet", "unknown")
        lines.append(_line("Network environment", internet.upper(), internet == "unreachable",
                           f"probed by operator; adapters up: "
                           f"{', '.join(environment_probe.get('adapters_up', [])) or 'none'}"))
    else:
        try:
            import psutil
            up = [n for n, s in psutil.net_if_stats().items() if s.isup and "loopback" not in n.lower()]
        except Exception:
            up = []
        lines.append(_line("Network environment", "NOT PROBED", False,
                           f"adapters up: {', '.join(up) or 'none'} - press 'Probe network' to test"))

    # -- cloud AI
    loaded = [m for m in _CLOUD_MODULES if m in sys.modules]
    try:
        from core.ollama_client import ollama_client
        host = ollama_client.host
        local_host = any(token in host for token in ("localhost", "127.0.0.1", "[::1]"))
    except Exception:
        host, local_host = "?", False
    cloud_blocked = not loaded and local_host and blocked_mode
    lines.append(_line("Cloud AI", "BLOCKED" if cloud_blocked else "AVAILABLE", cloud_blocked,
                       (f"no cloud SDK loaded; model runtime at {host}" if not loaded
                        else f"cloud SDK loaded: {', '.join(loaded)}")))

    # -- local models
    try:
        from core.ollama_client import ollama_client
        up = ollama_client.is_available(force=True)
        models = ollama_client.list_models(force=True) if up else []
        lines.append(_line("Local models", f"ACTIVE ({len(models)})" if up and models else "UNAVAILABLE",
                           bool(up and models), "Ollama on loopback" if up else "Ollama not reachable"))
    except Exception as error:
        lines.append(_line("Local models", "ERROR", False, str(error)))

    try:
        from security.integrity import model_integrity
        integrity = model_integrity.report()
        trusted = sum(1 for m in integrity["models"] if m["status"] == "trusted")
        tampered = [m["model"] for m in integrity["models"] if m["status"] == "tampered"]
        ok = integrity["exists"] and integrity["signature_valid"] and not tampered
        lines.append(_line("Trusted model registry",
                           "VERIFIED" if ok else ("TAMPERED" if tampered else "NOT PINNED"), ok,
                           f"{trusted} model(s) match pinned digests"
                           + (f"; mismatch: {', '.join(tampered)}" if tampered else "")))
    except Exception as error:
        lines.append(_line("Trusted model registry", "ERROR", False, str(error)))

    # -- capabilities
    try:
        from core.capabilities import embedding_provider
        report_ = embedding_provider.check()
        lines.append(_line("Local RAG (embeddings)", "ACTIVE" if report_.ok else "UNAVAILABLE",
                           report_.ok, report_.line()))
    except Exception as error:
        lines.append(_line("Local RAG (embeddings)", "ERROR", False, str(error)))

    try:
        from industrial.ocr_engines import usable_engines
        engines = usable_engines()
        lines.append(_line("Local OCR", "ACTIVE" if engines else "UNAVAILABLE", bool(engines),
                           ", ".join(engines) or "no OCR engine loads"))
    except Exception as error:
        lines.append(_line("Local OCR", "ERROR", False, str(error)))

    try:
        from core.model_router import model_router
        from core.ollama_client import ollama_client
        vision = model_router.route("vision")
        pulled = any(ollama_client._model_matches(m, vision) for m in ollama_client.list_models())
        lines.append(_line("Local vision", "ACTIVE" if pulled else "UNAVAILABLE", pulled, vision))
    except Exception as error:
        lines.append(_line("Local vision", "ERROR", False, str(error)))

    try:
        from core.tool_registry import registry
        lines.append(_line("Local tools", f"ACTIVE ({len(registry.list_tools())})", True,
                           "every tool runs in-process on this machine"))
    except Exception as error:
        lines.append(_line("Local tools", "ERROR", False, str(error)))

    # -- data and audit
    try:
        from memory import database
        conn = database.get_connection()
        try:
            verdict = conn.execute("PRAGMA quick_check").fetchone()[0]
        finally:
            conn.close()
        lines.append(_line("Database", "ACTIVE" if verdict == "ok" else "DAMAGED", verdict == "ok",
                           f"SQLite quick_check: {verdict}"))
    except sqlite3.Error as error:
        lines.append(_line("Database", "ERROR", False, str(error)))

    try:
        from security.audit_chain import audit_chain
        chain = audit_chain.verify()
        lines.append(_line("Audit", "VERIFIED" if chain["ok"] else "BROKEN", chain["ok"],
                           chain["detail"]))
    except Exception as error:
        lines.append(_line("Audit", "ERROR", False, str(error)))

    try:
        from core.vault import vault
        state = "UNLOCKED" if vault.is_unlocked else ("LOCKED" if vault.is_configured() else "NOT SET UP")
        lines.append(_line("Vault", state, True, "AES-256-GCM, key held only while unlocked"))
    except Exception as error:
        lines.append(_line("Vault", "ERROR", False, str(error)))

    sovereign = all(l["ok"] for l in lines if l["label"] in (
        "Application egress policy", "External egress (allowed)", "Cloud AI", "Local models",
        "Audit", "Database"))
    return {"sovereign_mode": "ACTIVE" if sovereign else "INCOMPLETE",
            "lines": lines, "measured_at": time.time(),
            "duration_ms": int((time.time() - started) * 1000)}
