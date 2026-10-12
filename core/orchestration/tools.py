"""
The orchestration layer, exposed as tools.

Everything the pipeline knows about itself is reachable by asking, not only by
watching the dashboard: which agents exist, which model each slot resolves to,
how much RAM the choice needs, what the last runs did, and which repair
strategies exist.

That matters for a sovereign system. "Why did it pick that model" and "what
actually ran" should be answerable offline, by the assistant itself, without
reading source code.
"""

from __future__ import annotations

from typing import Any, Dict, List

from core.logger import get_logger
from core.security_manager import RiskLevel
from core.tool_registry import registry

logger = get_logger("orchestration.tools")


@registry.register(
    "pipeline_status",
    "Show what the orchestrator is doing now and what it did recently: task, "
    "agent, model, tools and status",
    RiskLevel.SAFE, params={"limit": "how many recent runs to include"})
def pipeline_status(limit: int = 5) -> Dict[str, Any]:
    from core.orchestration import activity
    try:
        limit = max(1, min(int(limit), 20))
    except (TypeError, ValueError):
        limit = 5
    current = activity.current()
    recent = activity.recent(limit)
    return {
        "running": bool(current),
        "current": current or "nothing running",
        "recent_runs": [
            {"task": entry["task"], "kind": entry.get("kind", ""),
             "agent": entry.get("agent", ""), "model": entry.get("model", ""),
             "tools": entry.get("tools", []), "status": entry.get("status", ""),
             "ok": entry.get("ok")}
            for entry in recent],
        "summary": activity.status_line() or "idle",
    }


@registry.register(
    "list_specialists",
    "List the specialist agents, what each is responsible for, which model slot "
    "it needs and how many tools it owns",
    RiskLevel.SAFE)
def list_specialists() -> Dict[str, Any]:
    from core.orchestration import agents as agent_registry
    rows = agent_registry.describe_agents()
    deterministic = [row for row in rows if row["deterministic"]]
    return {
        "agent_count": len(rows),
        "deterministic_agents": len(deterministic),
        "note": ("Agents share one tool registry, one database, one memory and "
                 "one document generator - an agent is a responsibility "
                 "boundary, not a separate assistant."),
        "agents": rows,
    }


@registry.register(
    "model_routing",
    "Show the model router: which model each task slot resolves to, its "
    "estimated memory need, free RAM, and which task kinds need no model at all",
    RiskLevel.SAFE)
def model_routing() -> Dict[str, Any]:
    from core.orchestration.router import capability_router
    table = capability_router.describe()
    missing = [f"{row['slot']} (pull {row['model']})"
               for row in table["slots"] if not row.get("pulled")]
    return {
        "available_ram_gb": table["available_ram_gb"],
        "resident_models": table["resident_models"] or "none held",
        "slots": table["slots"],
        "handled_without_a_model": table["deterministic_kinds"],
        "slots_with_no_model_pulled": missing or "none",
        "policy": ("The smallest capable model wins. If it will not fit in "
                   "free RAM the router steps down to a smaller one rather "
                   "than swapping."),
    }


@registry.register(
    "release_models",
    "Ask Ollama to unload the models ZEUS is holding, freeing RAM",
    RiskLevel.LOW_RISK)
def release_models() -> Dict[str, Any]:
    from core.orchestration.router import capability_router
    held = list(capability_router.loaded_models())
    released = capability_router.release_all()
    return {"released": released, "models": held or "none were held",
            "note": "Ollama reloads a model on the next request that needs it."}


@registry.register(
    "validation_report",
    "Show which output validators exist and which repair strategies the "
    "auto-fix layer can apply",
    RiskLevel.SAFE)
def validation_report() -> Dict[str, Any]:
    from core.orchestration import autofix
    from core.orchestration.validators import registered_kinds
    strategies = autofix.known_strategies()
    return {
        "validators": registered_kinds(),
        "repair_strategies": sorted(strategies),
        "max_repair_attempts": autofix.MAX_ATTEMPTS,
        "rule": ("Nothing is reported as finished until its validator passes. "
                 "A failed check is repaired with a specific change, not a "
                 "blind retry, and never more than "
                 f"{autofix.MAX_ATTEMPTS} times."),
    }


@registry.register(
    "explain_last_run",
    "Explain the last request end to end: how it was classified, which agent "
    "and model handled it, which tools ran and what was verified",
    RiskLevel.SAFE)
def explain_last_run() -> Dict[str, Any]:
    from core.orchestration import activity
    recent = activity.recent(2)
    # recent[0] is this very question; the run being asked about is the one before.
    entry = next((item for item in recent
                  if not item["task"].lower().startswith(("explain the last",
                                                          "how did you do"))), None)
    if entry is None:
        return {"error": "No completed run to explain yet."}
    return {
        "request": entry["task"],
        "classified_as": entry.get("kind") or "unclassified",
        "agent": entry.get("agent") or "none",
        "model": entry.get("model") or "none - handled by Python tools",
        "tools_run": entry.get("tools") or "none",
        "status": entry.get("status"),
        "succeeded": entry.get("ok"),
        "detail": entry.get("detail", ""),
    }


@registry.register(
    "capability_report",
    "Report what this machine can actually do right now: which local models "
    "are pulled, whether embedding, reranking and voice really work, and what "
    "to install for anything that does not",
    RiskLevel.SAFE)
def capability_report() -> Dict[str, Any]:
    """Every line here is the result of a real call, not a configuration read.

    This is the tool to reach for when something reports as unavailable: it
    distinguishes MODEL DETECTED / ADAPTER REQUIRED from NO MODEL PULLED from
    RUNTIME NOT REACHABLE, which are three different problems with three
    different fixes and were previously all reported as the same message.
    """
    from core.capabilities import check_all
    from core.ollama_client import ollama_client

    runtime_up = ollama_client.is_available()
    capabilities = {}
    for name, report in check_all().items():
        capabilities[name] = {
            "status": report.line(),
            "ready": report.ok,
            "model": report.model or "none",
            "interface": report.interface or "n/a",
            "evidence": report.evidence or "no probe evidence",
        }

    try:
        from voice.providers import voice_manager
        voice = voice_manager.report()
        voice_summary = {
            "state": voice["state"],
            "detail": voice["detail"],
            "speaking_with": voice["output"]["provider"] or "nothing (text replies)",
            "listening_with": voice["input"]["provider"] or "nothing (typed input)",
            "providers": {name: item["state"]
                          for name, item in voice["providers"].items()},
        }
    except Exception as error:
        voice_summary = {"state": "error", "detail": str(error)}

    return {
        "local_runtime": ("reachable" if runtime_up
                          else "not reachable - start it with 'ollama serve'"),
        "models_pulled": ollama_client.list_models() if runtime_up else [],
        "capabilities": capabilities,
        "voice": voice_summary,
        "sovereignty": {
            "cloud_services": "none - every model runs on this machine",
            "runtime_downloads": "disabled",
        },
    }
