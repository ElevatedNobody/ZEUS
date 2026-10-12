"""
Shared contracts for the ZEUS orchestration layer.

Everything that crosses a module boundary in the pipeline uses these types,
so a stage can never return a bare string that the next stage has to guess at.
The rule the whole layer is built on: **never fail silently**. Every stage
returns a structured result, and every failure carries enough information for
the auto-fix layer to try something specific rather than blindly retry.

    {
        "status": "failed",
        "module": "pptx_renderer",
        "error_type": "layout_overflow",
        "detail": "...",
        "recovery": "retry_with_fewer_rows"
    }

Nothing here imports a model, a database or a tool - these are pure data
structures so every other module can depend on them without a cycle.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class TaskKind(str, Enum):
    """What the user is actually asking for.

    This drives model routing, agent selection and which validator runs, so
    it is deliberately about the WORK, not about the phrasing.
    """

    CHAT = "chat"
    REASONING = "reasoning"
    CODE = "code"
    DOCUMENT_READ = "document_read"
    OCR = "ocr"
    VISION = "vision"
    RAG = "rag"
    WORD = "word"
    EXCEL = "excel"
    POWERPOINT = "powerpoint"
    PDF = "pdf"
    CONVERSION = "conversion"
    FILE_OPS = "file_ops"
    INDUSTRIAL_DATA = "industrial_data"
    AUTOMATION = "automation"
    MEMORY = "memory"
    MULTI_STEP = "multi_step"
    UNKNOWN = "unknown"


class Complexity(str, Enum):
    """How much model capability the task genuinely needs.

    The router uses this to pick the SMALLEST capable model - on a 16 GB
    machine, sending a yes/no classification to a 7B model costs seconds and
    gigabytes for no gain.
    """

    TRIVIAL = "trivial"      # deterministic, no model at all
    SIMPLE = "simple"        # 0.5B - 3B is plenty
    MODERATE = "moderate"    # 3B - 7B
    HARD = "hard"            # 7B reasoning model, longer context


class Stage(str, Enum):
    INTENT = "intent"
    MEMORY = "memory"
    CLASSIFY = "classify"
    PLAN = "plan"
    AUTHORIZE = "authorize"
    ROUTE = "route"
    EXECUTE = "execute"
    VALIDATE = "validate"
    AUTOFIX = "autofix"
    DELIVER = "deliver"


class Status(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    OK = "ok"
    FAILED = "failed"
    SKIPPED = "skipped"
    NEEDS_CONFIRMATION = "needs_confirmation"


@dataclass
class MedhaError:
    """A failure the orchestrator can reason about.

    `error_type` is a stable machine-readable slug (not a message), because
    the auto-fix layer dispatches on it. `recovery` names the strategy that
    should be tried, or is empty when nothing sensible can be attempted.
    """

    module: str
    error_type: str
    detail: str = ""
    recovery: str = ""
    status: str = "failed"

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def __str__(self) -> str:                       # what the user finally sees
        text = self.detail or self.error_type.replace("_", " ")
        return f"{text} [{self.module}/{self.error_type}]"


@dataclass
class ValidationReport:
    """The verdict on one produced artefact."""

    ok: bool
    kind: str
    checks: Dict[str, bool] = field(default_factory=dict)
    detail: str = ""
    error: Optional[MedhaError] = None

    @property
    def failed_checks(self) -> List[str]:
        return [name for name, passed in self.checks.items() if not passed]

    def as_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "kind": self.kind, "checks": dict(self.checks),
                "detail": self.detail,
                "error": self.error.as_dict() if self.error else None}


@dataclass
class ModelChoice:
    """What the router decided, and why - the 'why' is shown in the UI so a
    model choice is never a black box."""

    model: str
    task_type: str
    reason: str = ""
    estimated_ram_gb: float = 0.0
    deterministic: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class PlanStep:
    """One node of the task graph."""

    id: str
    tool: str
    args: Dict[str, Any] = field(default_factory=dict)
    description: str = ""
    agent: str = ""
    depends_on: List[str] = field(default_factory=list)
    validate_as: str = ""            # validator kind, "" means no artefact check
    optional: bool = False
    status: Status = Status.PENDING
    output: Any = None
    error: Optional[MedhaError] = None
    attempts: int = 0
    # Who proposed this step: "command" (a route matched the operator's own
    # words) or "planner" (a MODEL proposed it). security.policy requires a
    # human approval for any side effect a model proposed.
    origin: str = "command"

    def as_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        data["error"] = self.error.as_dict() if self.error else None
        # Tool output can hold anything; keep the transcript printable.
        if not isinstance(self.output, (str, int, float, bool, type(None), dict, list)):
            data["output"] = str(self.output)
        return data


@dataclass
class StageRecord:
    """One line of the pipeline transcript. This is what makes the run
    explainable after the fact without exposing model chain-of-thought."""

    stage: Stage
    status: Status
    detail: str = ""
    started_at: float = field(default_factory=time.time)
    duration_ms: int = 0

    def as_dict(self) -> Dict[str, Any]:
        return {"stage": self.stage.value, "status": self.status.value,
                "detail": self.detail, "duration_ms": self.duration_ms}


@dataclass
class OrchestrationResult:
    """What the orchestrator hands back.

    `response` is the human answer. Everything else is the evidence: which
    agent ran, which model was chosen, which tools fired, what was validated,
    and what (if anything) had to be repaired.
    """

    response: str = ""
    intent: str = ""
    task_kind: TaskKind = TaskKind.UNKNOWN
    complexity: Complexity = Complexity.SIMPLE
    agent: str = ""
    model: Optional[ModelChoice] = None
    tools_used: List[str] = field(default_factory=list)
    steps: List[PlanStep] = field(default_factory=list)
    stages: List[StageRecord] = field(default_factory=list)
    validations: List[ValidationReport] = field(default_factory=list)
    artifacts: List[str] = field(default_factory=list)
    refresh: List[str] = field(default_factory=list)
    ui_action: Optional[Dict[str, Any]] = None
    error: Optional[MedhaError] = None
    repaired: bool = False
    succeeded: bool = True

    def as_dict(self) -> Dict[str, Any]:
        return {
            "response": self.response,
            "intent": self.intent,
            "task_kind": self.task_kind.value,
            "complexity": self.complexity.value,
            "agent": self.agent,
            "model": self.model.as_dict() if self.model else None,
            "tools_used": list(self.tools_used),
            "steps": [s.as_dict() for s in self.steps],
            "stages": [s.as_dict() for s in self.stages],
            "validations": [v.as_dict() for v in self.validations],
            "artifacts": list(self.artifacts),
            "refresh": list(self.refresh),
            "ui_action": self.ui_action,
            "error": self.error.as_dict() if self.error else None,
            "repaired": self.repaired,
            "succeeded": self.succeeded,
        }

    def to_engine_response(self) -> Dict[str, Any]:
        """The exact shape DecisionEngine.process() has always returned, so
        the UI, main.py and every existing test keep working unchanged. The
        pipeline detail rides along as extra keys nobody is forced to read."""
        payload: Dict[str, Any] = {
            "response": self.response,
            "intent": self.intent,
            "refresh": list(self.refresh),
        }
        if self.agent:
            payload["agent"] = self.agent
        if self.ui_action:
            payload["ui_action"] = self.ui_action
        payload["pipeline"] = {
            "task_kind": self.task_kind.value,
            "complexity": self.complexity.value,
            "agent": self.agent,
            "model": self.model.model if self.model else "",
            "model_reason": self.model.reason if self.model else "",
            "tools": list(self.tools_used),
            "stages": [s.as_dict() for s in self.stages],
            "artifacts": list(self.artifacts),
            "validated": all(v.ok for v in self.validations) if self.validations else None,
            "repaired": self.repaired,
            "succeeded": self.succeeded,
            "error": self.error.as_dict() if self.error else None,
        }
        return payload
