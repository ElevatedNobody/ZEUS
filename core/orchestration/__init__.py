"""
ZEUS orchestration layer.

    request -> classify -> memory -> plan -> route -> execute
            -> validate -> auto-fix -> deliver -> audit

The layer is additive. It wraps the existing DecisionEngine route table, the
existing tool registry, the existing model router, the existing memory and the
existing security manager - it replaces none of them. Anything that worked
before this package existed still runs through exactly the same code; it now
runs inside a pipeline that can explain the choice of model, check the result,
and repair it when the check fails.
"""

from core.orchestration.contracts import (          # noqa: F401
    Complexity, MedhaError, ModelChoice, OrchestrationResult, PlanStep, Stage,
    StageRecord, Status, TaskKind, ValidationReport,
)

__all__ = [
    "Complexity", "MedhaError", "ModelChoice", "OrchestrationResult", "PlanStep",
    "Stage", "StageRecord", "Status", "TaskKind", "ValidationReport",
    "get_orchestrator", "classify", "validate", "capability_router",
]


def __getattr__(name):
    """Lazy re-exports: importing the package must not drag in the tool
    registry (and therefore every tool module) before the caller is ready."""
    if name == "get_orchestrator":
        from core.orchestration.orchestrator import get_orchestrator
        return get_orchestrator
    if name == "classify":
        from core.orchestration.classifier import classify
        return classify
    if name == "validate":
        from core.orchestration.validators import validate
        return validate
    if name == "capability_router":
        from core.orchestration.router import capability_router
        return capability_router
    raise AttributeError(name)
