"""
ZEUS Tool Registry
Every capability ZEUS has is registered here as a Tool with a name,
description, parameter schema, risk level, and execution function.
The decision engine / planner selects tools from this registry - it
never calls raw functions directly.
"""

import concurrent.futures
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from core.logger import get_logger
from core.security_manager import security_manager, RiskLevel, SecurityError

logger = get_logger("tools")

_executor = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="medha-tool")


@dataclass
class ToolResult:
    success: bool
    output: Any = None
    error: Optional[str] = None
    duration_ms: float = 0.0


@dataclass
class Tool:
    name: str
    description: str
    func: Callable
    risk: RiskLevel = RiskLevel.SAFE
    params: Dict[str, str] = field(default_factory=dict)  # param_name -> description
    timeout: float = 15.0

    def required_params(self) -> List[str]:
        """Declared parameters that the function cannot supply itself.

        `params` is documentation for the model, and a tool may document an
        optional argument ("status: open/closed, default open"). Only a
        parameter with NO default in the signature is genuinely required, so
        the signature decides. If the function cannot be introspected, every
        declared parameter is treated as required, which is the old behaviour.
        """
        try:
            import inspect
            signature = inspect.signature(self.func)
        except (TypeError, ValueError):
            return list(self.params)
        required = []
        for name in self.params:
            parameter = signature.parameters.get(name)
            if parameter is None or parameter.default is inspect.Parameter.empty:
                required.append(name)
        return required

    def validate(self, kwargs: dict) -> Optional[str]:
        """Return an error string if kwargs are invalid, else None."""
        missing = [p for p in self.required_params() if p not in kwargs]
        if missing:
            return f"Missing required parameters: {', '.join(missing)}"
        return None


class ToolRegistry:
    def __init__(self):
        self._tools: Dict[str, Tool] = {}

    def register(self, name: str, description: str, risk: RiskLevel = RiskLevel.SAFE,
                 params: Optional[Dict[str, str]] = None, timeout: float = 15.0):
        """Decorator to register a function as a tool."""
        def decorator(func: Callable):
            self._tools[name] = Tool(
                name=name, description=description, func=func,
                risk=risk, params=params or {}, timeout=timeout,
            )
            return func
        return decorator

    def register_direct(self, tool: Tool):
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def list_tools(self) -> List[Tool]:
        return list(self._tools.values())

    def describe_all(self) -> str:
        """Human/LLM-readable catalogue, used when prompting the model about available tools."""
        lines = []
        for t in sorted(self._tools.values(), key=lambda x: x.name):
            param_str = ", ".join(t.params.keys()) if t.params else "none"
            lines.append(f"- {t.name}({param_str}) [{t.risk.name}]: {t.description}")
        return "\n".join(lines)

    def execute(self, tool_name: str, **kwargs) -> ToolResult:
        tool = self.get(tool_name)
        if tool is None:
            return ToolResult(success=False, error=f"Unknown tool '{tool_name}'")

        err = tool.validate(kwargs)
        if err:
            return ToolResult(success=False, error=err)

        # THE policy choke point. Identity, role, data classification, the
        # read/write/execute separation, the vault gate, protected paths and
        # the network policy are all decided here, by code, before anything
        # runs - whatever path the call came in through.
        from security import policy as _policy
        decision = _policy.authorize(tool.name, int(tool.risk), kwargs)
        if not decision.allowed:
            security_manager._audit(f"DENIED {tool.name}: {decision.reason}", tool.risk, False)
            try:
                from security import events
                events.record("tool_denied", f"{tool.name}: {decision.reason} "
                                             f"(origin={decision.origin})", severity="notice")
            except Exception:
                pass
            if decision.profile is not None and decision.profile.vault_gated:
                # A refusal to touch restricted data belongs in the vault's own
                # audit trail too - "who tried to read the employee register
                # while the vault was locked" is exactly the question it answers.
                try:
                    from memory.database import log_vault_event
                    log_vault_event(tool.name, detail=f"refused: {decision.reason}",
                                    succeeded=False,
                                    username=decision.identity.username if decision.identity else "")
                except Exception:
                    pass
            return ToolResult(success=False, error=decision.reason)

        try:
            approved = security_manager.check_and_confirm(
                tool.name, tool.risk, details=str(kwargs) if kwargs else "",
                force=decision.needs_approval, origin=decision.origin,
            )
        except SecurityError as e:
            return ToolResult(success=False, error=str(e))

        if not approved:
            return ToolResult(success=False, error="Not approved - the action was not run")

        start = time.perf_counter()
        # The assistant state shows what is really running: evidence retrieval
        # for knowledge tools, execution for everything else. A read the interface
        # makes to fill a panel (origin 'ui', SAFE) is not the assistant working on
        # a request - it must not set the state, the voice line or the wave moving.
        panel_read = decision.origin == "ui" and tool.risk == RiskLevel.SAFE
        try:
            from core.assistant_state import State, assistant_state
            retrieving = any(word in tool.name for word in (
                "knowledge", "evidence", "industrial_kb", "search_", "ask_", "rag_"))
            activity = None if panel_read else assistant_state.activity(
                State.RETRIEVING if retrieving else State.EXECUTING, tool.name)
        except Exception:                       # pragma: no cover - state is optional
            activity = None
        if activity is not None:
            activity.__enter__()
        # A tool that calls another tool (an agent adding a task) is already
        # running on a pool thread; submitting to the same small pool and
        # waiting could deadlock it, so nested calls run inline - still after
        # the same policy check and approval above.
        import threading as _threading
        if _threading.current_thread().name.startswith("medha-tool"):
            future = concurrent.futures.Future()
            try:
                future.set_result(tool.func(**kwargs))
            except Exception as nested_error:           # surfaced below like any failure
                future.set_exception(nested_error)
        else:
            # The caller's context variables go with the call (the knowledge-store
            # override, render scopes, trace switches): a pool thread starts with
            # EMPTY context, so without the copy a tool silently used the defaults.
            import contextvars as _contextvars
            future = _executor.submit(_contextvars.copy_context().run, tool.func, **kwargs)
        try:
            output = future.result(timeout=tool.timeout)
            duration = (time.perf_counter() - start) * 1000
            logger.info(f"TOOL_COMPLETED {tool_name} ({duration:.0f}ms)")
            try:
                from security.audit_chain import audit_chain
                audit_chain.append("tool", tool_name, actor=decision.identity.username
                                   if decision.identity else "", origin=decision.origin,
                                   access=decision.profile.access if decision.profile else "",
                                   ok=not (isinstance(output, dict) and output.get("error")),
                                   ms=int(duration))
            except Exception:
                pass
            return ToolResult(success=True, output=output, duration_ms=duration)
        except concurrent.futures.TimeoutError:
            # A Python thread cannot be killed; cancel() only helps if it had
            # not started. Say so rather than implying the work was stopped.
            cancelled = future.cancel()
            logger.error(f"TOOL_FAILED {tool_name}: timeout after {tool.timeout}s"
                         + ("" if cancelled else " (still running in the background)"))
            return ToolResult(success=False, error=f"Tool '{tool_name}' timed out after "
                                                   f"{tool.timeout}s")
        except Exception as e:
            logger.error(f"TOOL_FAILED {tool_name}: {e}\n{traceback.format_exc()}")
            return ToolResult(success=False, error=str(e))
        finally:
            if activity is not None:
                activity.__exit__(None, None, None)


registry = ToolRegistry()

# Importing this module registers all built-in tools against `registry`.
from core import builtin_tools  # noqa: E402,F401

# Industrial workbench tools (SIH26117). Imported after the built-ins so they
# extend the same registry rather than replacing anything. Guarded: if an
# industrial dependency is missing on this machine, ZEUS still starts with
# its full original tool set and the reason is logged.
try:
    from industrial import tools as _industrial_tools  # noqa: E402,F401
except Exception as _industrial_err:  # pragma: no cover - environment dependent
    logger.warning(f"Industrial tools unavailable: {_industrial_err}")

# Plant tools: equipment, maintenance, inventory, alerts, permits, LOTO,
# exports and industrial report generation. Guarded the same way, and imported
# last so it can only ever ADD to the registry.
try:
    from industrial import plant_tools as _plant_tools  # noqa: E402,F401
except Exception as _plant_err:  # pragma: no cover - environment dependent
    logger.warning(f"Industrial plant tools unavailable: {_plant_err}")

# Orchestration introspection: the pipeline, the agent roster, the model
# routing table and the validators, all answerable offline by asking rather
# than by reading source. Guarded like the rest, and imported last.
try:
    from core.orchestration import tools as _orchestration_tools  # noqa: E402,F401
except Exception as _orchestration_err:  # pragma: no cover - environment dependent
    logger.warning(f"Orchestration tools unavailable: {_orchestration_err}")

# The secure file locker and the employee register. Guarded like the rest: a
# machine without the `cryptography` package still starts with every other
# tool, and the vault reports honestly that it cannot store anything safely
# rather than storing it in the clear.
try:
    from core import vault_tools as _vault_tools  # noqa: E402,F401
except Exception as _vault_err:  # pragma: no cover - environment dependent
    logger.warning(f"Vault tools unavailable: {_vault_err}")

# ZEUS: engineering, sandbox, specialist agents, security centre, hardware,
# artifacts, telemetry and meeting intelligence. Guarded like the rest.
try:
    from core import agni_tools as _agni_tools  # noqa: E402,F401
except Exception as _agni_err:  # pragma: no cover - environment dependent
    logger.warning(f"ZEUS tools unavailable: {_agni_err}")

# ZEUS company intelligence (knowledge_fabric): temporal / hybrid / graph /
# structured retrieval, verified analyses and artifacts. Guarded like the rest.
try:
    from knowledge_fabric import tools as _knowledge_fabric_tools  # noqa: E402,F401
except Exception as _kf_err:  # pragma: no cover - environment dependent
    logger.warning(f"Company intelligence tools unavailable: {_kf_err}")

# ZEUS: intelligent data capture and schedule linking (jarvis/) - projects,
# schedules, field evidence, the event store, CPM, forecast explanation,
# chronology, offline sync and learning cards. Guarded like the rest, and only
# ever ADDS to the registry; every call still passes security/policy.py.
try:
    from jarvis import tools as _jarvis_tools  # noqa: E402,F401
except Exception as _jarvis_err:  # pragma: no cover - environment dependent
    logger.warning(f"ZEUS project tools unavailable: {_jarvis_err}")

# The ZEUS NETWORK (crudex_net/tools.py): devices, pairing, permissions,
# messages, files, remote tasks, sharing. Same rule - it only ADDS tools, and
# each one passes security/policy.py. Offline mode never needs any of them.
try:
    from crudex_net import tools as _crudex_net_tools  # noqa: E402,F401
except Exception as _crudex_net_err:  # pragma: no cover - environment dependent
    logger.warning(f"ZEUS NETWORK tools unavailable: {_crudex_net_err}")

