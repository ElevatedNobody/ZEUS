"""
The ZEUS orchestrator.

    USER REQUEST -> intent -> memory -> classify -> plan -> route -> execute
                 -> validate -> auto-fix -> validate -> deliver -> memory -> audit

Two design decisions carry the whole thing:

**It wraps; it does not replace.** The DecisionEngine's route table (200+
deterministic patterns) and its chat handler are injected as callables. The
orchestrator does not re-implement either, so every command that worked before
still resolves through exactly the same code - it just now runs inside a
pipeline that classifies it, routes a model for it, validates its output and
repairs it when validation fails.

**The model is the last resort, not the first.** A step that Python can do is
done by Python. `route_for()` is asked for a model only when a step genuinely
needs judgement, and the choice is recorded with the reason so nothing about
model selection is invisible.

Nothing here fails silently: every stage appends a StageRecord, every failure
is a structured MedhaError, and a step that cannot be repaired is reported to
the user in plain words rather than swallowed.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.logger import get_logger
from core.orchestration import activity, agents as agent_registry, autofix, task_graph
from core.orchestration.classifier import Classification, classify
from core.orchestration.contracts import (
    Complexity, MedhaError, ModelChoice, OrchestrationResult, PlanStep, Stage,
    StageRecord, Status, TaskKind, ValidationReport,
)
from core.orchestration.router import capability_router
from core.orchestration.validators import validate

logger = get_logger("orchestration")

# Tools whose output is the answer itself, shown in full in a multi-step reply.
_ANSWER_TOOLS = {"company_intelligence"}

# Refresh hints for the UI, by the kind of work that just happened.
_REFRESH_BY_KIND: Dict[TaskKind, List[str]] = {
    TaskKind.MULTI_STEP: ["tasks"],
    TaskKind.MEMORY: ["todos", "notes"],
    TaskKind.INDUSTRIAL_DATA: [],
}


class Orchestrator:
    """One run of the pipeline per user request."""

    def __init__(self,
                 direct_matcher: Optional[Callable[[str], Optional[Tuple[str, Dict]]]] = None,
                 chat_handler: Optional[Callable[[str, List[Dict[str, str]]], str]] = None,
                 formatter: Optional[Callable[[str, Any], str]] = None):
        from core.tool_registry import registry
        self.registry = registry
        self._direct_matcher = direct_matcher
        self._chat_handler = chat_handler
        self._formatter = formatter or (lambda tool, output: str(output))
        # Filled by the memory stage on every run. Initialised here because
        # the delivery stage reads them, and if the memory stage ever fails -
        # a locked database, a corrupt fact row - delivery would raise
        # AttributeError and the user would get "something went wrong"
        # instead of an answer that simply had no history attached.
        self._facts: Dict[str, Any] = {}
        self._history: List[Dict[str, str]] = []
        self._memory = None                      # memory.recall.MemoryContext
        self._routed: Optional[ModelChoice] = None

    # ------------------------------------------------------------- run ----

    def run(self, text: str, context: Optional[Dict[str, Any]] = None) -> OrchestrationResult:
        context = context or {}
        result = OrchestrationResult()
        board = activity.begin(text, kind="")
        self._board = board

        from core.assistant_state import State, assistant_state
        try:
            with assistant_state.activity(State.UNDERSTANDING, "classifying the request"):
                self._stage_classify(text, result, board)
                self._stage_memory(text, context, result)
            with assistant_state.activity(State.THINKING, "planning"):
                plan = self._stage_plan(text, result, board)
                self._authorize_plan(plan, result)
                self._stage_route(result, board, plan)
            self._stage_execute(plan, result, board)
            self._stage_deliver(text, context, plan, result, board)
            self._remember_outcome(text, result)
            self._record_working_memory(text, result)
        except Exception as error:                # the pipeline itself must not crash
            logger.exception("orchestration failed")
            result.succeeded = False
            result.error = MedhaError(module="orchestrator", error_type="pipeline_error",
                                       detail=str(error), recovery="")
            if not result.response:
                result.response = (f"Something went wrong while handling that: {error}. "
                                   f"Nothing was changed.")
            activity.finish(board, ok=False, detail=str(error))
            assistant_state.error(str(error)[:120])
            self._audit(text, result)
            return result

        if not result.succeeded:
            assistant_state.error((result.error.detail if result.error else "the request failed")[:120])
        activity.finish(board, ok=result.succeeded, detail=result.response[:120])
        self._audit(text, result)
        return result

    def _authorize_plan(self, plan: List[PlanStep], result: OrchestrationResult):
        """The AUTHORIZE stage: check every planned step against the policy
        BEFORE anything runs, so a plan that would stop half-way on a refused
        step is refused up front and the operator is told which step and why.
        (ToolRegistry.execute checks again at run time - the policy is the
        authority, this stage is the early, explainable view of it.)"""
        started = time.time()
        if not plan:
            self._record(result, Stage.AUTHORIZE, Status.OK, "nothing to run", started,
                         note="authorized")
            return
        from security import policy
        refused = []
        for step in plan:
            tool = self.registry.get(step.tool)
            if tool is None:
                continue
            decision = policy.authorize(step.tool, int(tool.risk), step.args,
                                        origin=step.origin or "command")
            if not decision.allowed:
                refused.append((step, decision.reason))
        if refused:
            for step, reason in refused:
                step.status = Status.FAILED
                step.error = MedhaError(module="policy", error_type="not_authorized",
                                        detail=reason, recovery="")
            self._record(result, Stage.AUTHORIZE, Status.FAILED,
                         "; ".join(f"{s.tool} refused ({r})" for s, r in refused),
                         started)
        else:
            self._record(result, Stage.AUTHORIZE, Status.OK,
                         f"{len(plan)} step(s) permitted by policy", started)

    # ---------------------------------------------------------- stages ----

    def _record(self, result: OrchestrationResult, stage: Stage, status: Status,
                detail: str, started: float, note: str = ""):
        elapsed = int((time.time() - started) * 1000)
        result.stages.append(StageRecord(
            stage=stage, status=status, detail=detail, duration_ms=elapsed))
        # Mirror the stage onto the activity board so the interface can draw
        # the pipeline from what actually happened, not from a guess.
        board = getattr(self, "_board", None)
        if board is not None:
            state = {Status.OK: "ok", Status.FAILED: "failed",
                     Status.RUNNING: "running",
                     Status.SKIPPED: "ok"}.get(status, "pending")
            activity.note_stage(board, stage.value, state,
                                note or (f"{elapsed} ms" if elapsed >= 10 else ""))

    def _stage_classify(self, text: str, result: OrchestrationResult, board):
        started = time.time()
        classification: Classification = classify(text)
        result.task_kind = classification.kind
        result.complexity = classification.complexity
        self._classification = classification
        activity.update(board, kind=classification.kind.value, status="classified")
        self._record(result, Stage.CLASSIFY, Status.OK,
                     f"{classification.kind.value} / {classification.complexity.value}"
                     + (f" ({classification.signals[0]})" if classification.signals else ""),
                     started)

    def _stage_memory(self, text: str, context: Dict[str, Any],
                      result: OrchestrationResult):
        """Bounded, relevant context - not the whole transcript.

        History arrives from the caller already trimmed. Long-term memory is
        RECALLED for this request through Hermes (memory/recall.py): only the
        memories relevant to what was asked, deduplicated and within a
        character budget. This replaced injecting the first eight stored
        facts into every prompt, which silently dropped everything remembered
        after the eighth.
        """
        started = time.time()
        history = context.get("history", []) or []
        self._facts = {}              # kept for readers of the old attribute
        self._history = history
        from memory.recall import MemoryContext
        try:
            from memory import recall
            memory = recall.build(text)
        except Exception as error:
            memory = MemoryContext("unavailable", reason=f"{type(error).__name__}: {error}")
        self._memory = memory
        status = Status.OK
        if memory.status == "unavailable":
            status = Status.FAILED
            logger.warning(f"MEMORY UNAVAILABLE for this request - {memory.reason}; "
                           f"policy: {'refuse' if self._memory_refuses() else 'answer without memory'}")
        self._record(result, Stage.MEMORY, status,
                     f"{len(history)} recent message(s); {memory.summary()}", started,
                     note=f"{len(memory.results)} recalled" if memory.results else "")

    @staticmethod
    def _memory_refuses() -> bool:
        try:
            from memory.hermes_store import config
            return config()["on_unavailable"] == "refuse"
        except Exception:
            return False

    def _stage_plan(self, text: str, result: OrchestrationResult, board) -> List[PlanStep]:
        started = time.time()
        direct = None
        if self._direct_matcher:
            try:
                direct = self._direct_matcher(text)
            except Exception as error:
                logger.warning(f"direct route matching failed: {error}")
        plan = task_graph.build_plan(text, self._classification, direct, history=self._history)
        result.steps = plan
        result.intent = self._legacy_intent(text, plan, bool(direct))

        if plan:
            agent = plan[0].agent or agent_registry.agent_for_kind(result.task_kind)
            result.agent = agent
            # A matched route knows more about the work than a keyword scan
            # does: 'show active alerts' is industrial data, not conversation.
            specialist = agent_registry.get(agent)
            if (specialist and specialist.primary_kind
                    and result.task_kind in (TaskKind.CHAT, TaskKind.UNKNOWN)):
                result.task_kind = specialist.primary_kind
            activity.update(board, agent=agent, status="planned")
            detail = (f"{len(plan)} step(s): "
                      + " -> ".join(step.tool for step in plan[:4]))
        else:
            result.agent = agent_registry.agent_for_kind(result.task_kind)
            activity.update(board, agent=result.agent, status="answering")
            detail = "no tool step - answering directly"
        self._record(result, Stage.PLAN, Status.OK, detail, started)
        return plan

    def _stage_route(self, result: OrchestrationResult, board, plan: List[PlanStep]):
        """Choose a model - or establish that none is needed."""
        started = time.time()
        specialists = [agent_registry.get(step.agent or "") for step in plan]
        known = [s for s in specialists if s is not None]
        # A plan whose every step is Python needs no model at all - that is the
        # common case for plant queries, file work and automation, and skipping
        # the model is what keeps those instant on a 16 GB machine.
        all_deterministic = bool(plan) and len(known) == len(plan) and all(
            s.deterministic for s in known)
        # The agent that will do the work names the slot it needs.
        prefer_slot = next((s.model_slot for s in known if s.model_slot), "")

        if all_deterministic:
            choice = ModelChoice(model="", task_type="deterministic", deterministic=True,
                                 reason="every step in this plan is deterministic Python")
        else:
            choice = capability_router.route_for(
                result.task_kind, result.complexity,
                context_tokens=sum(len(m.get("content", "")) for m in self._history) // 4,
                prefer_slot=prefer_slot)
        result.model = choice
        self._routed = choice
        if choice.model:
            capability_router.note_loaded(choice.model)
        activity.update(board, model=choice.model or "none (deterministic)",
                        status="running")
        self._record(result, Stage.ROUTE, Status.OK,
                     (f"{choice.model} — {choice.reason}" if choice.model
                      else choice.reason), started)

    def _stage_execute(self, plan: List[PlanStep], result: OrchestrationResult, board):
        """Run the graph in dependency order, validating and repairing as we go."""
        if not plan:
            self._record(result, Stage.EXECUTE, Status.SKIPPED, "no tool steps", time.time())
            return

        started = time.time()
        done: Dict[str, PlanStep] = {}
        guard = 0
        while len(done) < len(plan) and guard < len(plan) * 4:
            guard += 1
            ready = task_graph.ready_steps(plan, done)
            if not ready:
                break
            for step in ready:
                self._run_step(step, result, board, done)
                done[step.id] = step

        blocked = [s for s in plan if s.status is Status.PENDING]
        for step in blocked:
            step.status = Status.SKIPPED
            step.error = MedhaError(module="orchestrator", error_type="dependency_failed",
                                     detail=f"a step {step.id} depends on did not complete",
                                     recovery="")

        failures = [s for s in plan if s.status is Status.FAILED and not s.optional]
        result.succeeded = not failures
        self._record(result, Stage.EXECUTE,
                     Status.OK if not failures else Status.FAILED,
                     f"{len(done)} step(s) run, {len(failures)} failed", started)

    def _run_step(self, step: PlanStep, result: OrchestrationResult, board,
                  done: Dict[str, PlanStep]):
        """Execute one step, then validate it, then repair it if needed."""
        self._resolve_dependencies(step, done)
        step.status = Status.RUNNING
        activity.note_tool(board, step.tool)
        activity.update(board, status=f"running {step.tool}")

        while True:
            step.attempts += 1
            from security.policy import origin_scope
            with origin_scope(step.origin or "command"):
                outcome = self.registry.execute(step.tool, **step.args)
            if step.tool not in result.tools_used:
                result.tools_used.append(step.tool)

            if not outcome.success:
                step.error = MedhaError(
                    module=step.tool, error_type="tool_reported_error",
                    detail=outcome.error or "the tool reported a failure",
                    recovery="retry")
                step.output = None
                if self._try_repair(step, result, board):
                    continue
                step.status = Status.FAILED
                return

            # A tool that answers with an error dict has failed, whatever its
            # success flag says - and its OWN message is the useful one, so it
            # is carried through instead of being replaced by a validator's
            # more generic complaint.
            if isinstance(outcome.output, dict) and outcome.output.get("error"):
                step.output = outcome.output
                step.error = MedhaError(
                    module=step.tool, error_type="tool_reported_error",
                    detail=str(outcome.output["error"]), recovery="retry")
                if self._try_repair(step, result, board):
                    continue
                step.status = Status.FAILED
                return

            step.output = outcome.output
            step.error = None
            self._collect_artifacts(step, result)

            report = self._validate_step(step, result, board)
            if report is None or report.ok:
                step.status = Status.OK
                return

            step.error = report.error
            if self._try_repair(step, result, board):
                continue
            step.status = Status.FAILED
            return

    def _resolve_dependencies(self, step: PlanStep, done: Dict[str, PlanStep]):
        """Fill in an argument a previous step produced.

        The concrete case: 'convert the document to PDF' cannot know the
        document's path until the step that wrote it has run.
        """
        if not step.depends_on:
            return
        for dependency_id in step.depends_on:
            previous = done.get(dependency_id)
            if previous is None or not isinstance(previous.output, dict):
                continue
            produced = previous.output.get("saved") or previous.output.get("path")
            if not produced:
                continue
            for key in ("source_path", "path", "input_path", "file"):
                if key in step.args and not step.args[key]:
                    step.args[key] = produced
                    break

    _FILE_KINDS = ("docx", "xlsx", "pptx", "pdf", "csv")

    def _validator_kind_for(self, step: PlanStep) -> str:
        """Pick the validator from evidence first, names last.

        Order: what the plan declared; then what the step actually produced
        (a path ending .pptx is a deck, whatever the tool is called); then the
        tool name; then the agent's default.
        """
        if step.validate_as:
            return step.validate_as

        produced: List[str] = []

        def walk(value: Any, depth: int = 0):
            if depth > 4 or len(produced) > 6:
                return
            if isinstance(value, str) and "." in value:
                extension = value.rsplit(".", 1)[-1].lower()
                if extension in self._FILE_KINDS:
                    produced.append(extension)
            elif isinstance(value, dict):
                for key, item in value.items():
                    if key not in ("error", "analysis", "checks"):
                        walk(item, depth + 1)
            elif isinstance(value, (list, tuple)):
                for item in value:
                    walk(item, depth + 1)

        walk(step.output)
        if len(set(produced)) > 1:
            return "bundle"
        if produced:
            return produced[0]
        return agent_registry.validator_for(step.agent, step.tool)

    def _validate_step(self, step: PlanStep, result: OrchestrationResult,
                       board) -> Optional[ValidationReport]:
        kind = self._validator_kind_for(step)
        if not kind:
            return None
        activity.update(board, status=f"validating {step.tool}")
        started = time.time()
        report = validate(kind, step.output)
        result.validations.append(report)
        self._record(result, Stage.VALIDATE,
                     Status.OK if report.ok else Status.FAILED,
                     f"{step.tool} [{kind}]: {report.detail}"
                     + (f" (failed: {', '.join(report.failed_checks)})"
                        if report.failed_checks else ""),
                     started)
        return report

    def _try_repair(self, step: PlanStep, result: OrchestrationResult, board) -> bool:
        """Attempt one specific repair. True means 'try the step again'."""
        repair = autofix.plan_repair(step.error, step)
        if repair is None:
            return False
        started = time.time()
        step.args = repair.args
        result.repaired = True
        activity.update(board, status=f"repairing {step.tool}")
        self._record(result, Stage.AUTOFIX, Status.RUNNING,
                     f"{step.tool}: {repair.strategy} — {repair.note}", started)
        return True

    _ARTIFACT_EXTENSIONS = ("docx", "xlsx", "pptx", "pdf", "csv", "png", "txt", "md")

    def _collect_artifacts(self, step: PlanStep, result: OrchestrationResult):
        """Every file the step produced, however deeply the tool nested it.

        A briefing pack returns three files inside a dict of dicts; a single
        generator returns one path under 'saved'. Walking the structure means
        the user is told about all of them, not just the ones at the top level.
        """
        def walk(value: Any, depth: int = 0):
            if depth > 4:
                return
            if isinstance(value, str):
                if ("." in value
                        and value.rsplit(".", 1)[-1].lower() in self._ARTIFACT_EXTENSIONS
                        and value not in result.artifacts):
                    result.artifacts.append(value)
            elif isinstance(value, dict):
                for key, item in value.items():
                    if key in ("error", "analysis", "checks"):
                        continue
                    walk(item, depth + 1)
            elif isinstance(value, (list, tuple)):
                for item in value:
                    walk(item, depth + 1)

        walk(step.output)

    def _stage_deliver(self, text: str, context: Dict[str, Any], plan: List[PlanStep],
                       result: OrchestrationResult, board):
        started = time.time()
        result.refresh = list(_REFRESH_BY_KIND.get(result.task_kind, []))

        if not plan:
            result.response = self._answer_directly(
                text, spoken=bool(context.get("spoken")))
            report = validate("text", result.response)
            result.validations.append(report)
            if not report.ok and report.error:
                result.succeeded = False
                result.error = report.error
            self._record(result, Stage.DELIVER, Status.OK if report.ok else Status.FAILED,
                         "answered from the model" if report.ok else report.detail, started)
            return

        if len(plan) == 1:
            step = plan[0]
            if step.status is Status.OK:
                result.response = self._formatter(step.tool, step.output)
            else:
                result.response = self._failure_text(step)
                result.error = step.error
        else:
            result.response = self._multi_step_text(plan, result)
            if len(plan) > 1:
                result.refresh = sorted(set(result.refresh + ["tasks"]))

        self._record(result, Stage.DELIVER,
                     Status.OK if result.succeeded else Status.FAILED,
                     f"{len(result.artifacts)} artifact(s)", started)

    # --------------------------------------------------------- helpers ----

    @staticmethod
    def _legacy_intent(text: str, plan: List[PlanStep], direct: bool) -> str:
        """The QUESTION / COMMAND / TASK / MULTI_STEP_TASK label this project
        has always returned. Kept exactly as it was so the UI and every
        existing caller keep reading the field they already read - the richer
        `task_kind` sits alongside it rather than displacing it."""
        from core.planner import IntentType, classify_intent
        if len(plan) > 1:
            return IntentType.MULTI_STEP_TASK.value
        if direct or len(plan) == 1:
            return IntentType.COMMAND.value
        try:
            return classify_intent(text).value
        except Exception:
            return IntentType.QUESTION.value

    def _answer_directly(self, text: str, spoken: bool = False) -> str:
        if self._chat_handler is None:
            return "No conversation handler is connected."
        history = list(self._history)
        memory = getattr(self, "_memory", None)
        if memory is not None and memory.status == "unavailable" and self._memory_refuses():
            return ("My long-term memory is unavailable right now "
                    f"({memory.reason[:120]}), and this installation is configured not to "
                    "answer without it ([memory] on_unavailable = refuse). Run REPAIR.bat.")
        # Relevant memories, fenced as DATA (memory/recall.py). They go in
        # the user turn - never in a system message, which would give a
        # stored sentence the authority of an instruction.
        block = memory.block if memory is not None and memory.status == "ok" else ""
        # `spoken` is passed only to handlers that accept it. Tests and any
        # older injected handler take (text, history), and calling those with
        # a third argument would turn a spoken question into a TypeError -
        # losing the answer over a styling preference.
        # The model the ROUTE stage chose is the model that answers - before,
        # the chat handler re-routed on its own, so the pipeline could report
        # one model while another one replied.
        routed = getattr(self, "_routed", None)
        model = routed.model if routed is not None and routed.model else None
        think = bool(routed is not None and routed.task_type == "reasoning")
        try:
            return self._chat_handler(text, history, spoken=spoken, model=model, think=think,
                                      memory_context=block)
        except TypeError:
            # A handler that predates memory context: the fenced block goes
            # in front of the request, still in the user turn.
            from memory.recall import user_turn
            framed = user_turn(block, text)
            try:
                return self._chat_handler(framed, history, spoken=spoken, model=model, think=think)
            except TypeError:
                try:
                    return self._chat_handler(framed, history, spoken=spoken)
                except TypeError:
                    return self._chat_handler(framed, history)

    @staticmethod
    def _record_working_memory(text: str, result: OrchestrationResult) -> None:
        """This session's turn for Hermes' WORKING tier - in memory only, cleared on lock."""
        try:
            from memory import hermes_context
            hermes_context.working_memory.record(hermes_context.active_user_key(), text, result.response,
                                                 result.artifacts)
        except Exception as error:
            logger.debug(f"working memory not recorded: {error}")

    def _remember_outcome(self, text: str, result: OrchestrationResult):
        """What a successful request PRODUCED is durable: 'which report did we
        make about the refinery?' must be answerable next week. Only files
        that were written, never the request's data."""
        if not (result.succeeded and result.artifacts):
            return
        try:
            # The self-tests run as the read-only diagnostic identity; what
            # they generate is test output, not the operator's history.
            from security.policy import current_identity
            if current_identity().username == "diagnostics":
                return
        except Exception:
            pass
        try:
            import os
            from memory.contracts import TOOL_RESULT
            from memory.memory_manager import remember_text
            names = ", ".join(os.path.basename(path) for path in result.artifacts[:4])
            tools = "+".join(result.tools_used) or "a tool"
            remember_text(f"Created {names} (saved at {os.path.dirname(result.artifacts[0])}) "
                          f"for the request: {text.strip()[:160]}",
                          source=f"tool:{tools}", explicit=True, category=TOOL_RESULT)
        except Exception as error:
            logger.debug(f"outcome not remembered: {error}")

    @staticmethod
    def _failure_text(step: PlanStep) -> str:
        error = step.error
        if error is None:
            return f"Couldn't complete '{step.description}'."
        attempts = (f" after {step.attempts} attempt(s)" if step.attempts > 1 else "")
        recovery = ""
        if error.recovery and step.attempts > 1:
            recovery = f" Tried: {error.recovery.replace('_', ' ')}."
        return (f"Couldn't do that{attempts}: {error.detail or error.error_type}."
                f"{recovery}")

    def _multi_step_text(self, plan: List[PlanStep], result: OrchestrationResult) -> str:
        lines: List[str] = []
        completed = [s for s in plan if s.status is Status.OK]
        failed = [s for s in plan if s.status is Status.FAILED]
        skipped = [s for s in plan if s.status is Status.SKIPPED]

        # A step whose output IS the answer (the company-intelligence analysis)
        # comes first, exactly as it would read on its own - a list of "done"
        # lines must not replace the answer that was asked for.
        for step in plan:
            if step.status is Status.OK and step.tool in _ANSWER_TOOLS:
                lines.append(self._formatter(step.tool, step.output))
                lines.append("")
        lines.append(f"{len(completed)} of {len(plan)} step(s) completed"
                     + (" - every file was verified after writing."
                        if completed and not failed else "."))
        for step in plan:
            mark = {Status.OK: "done", Status.FAILED: "failed",
                    Status.SKIPPED: "skipped"}.get(step.status, step.status.value)
            detail = ""
            if step.status is Status.OK and isinstance(step.output, dict):
                saved = step.output.get("saved") or step.output.get("path")
                if saved:
                    detail = f" -> {saved}"
            elif step.status is Status.FAILED and step.error:
                detail = f" -> {step.error.detail or step.error.error_type}"
            lines.append(f"  - {step.description}: {mark}{detail}")

        if result.repaired:
            lines.append("One or more steps failed validation and were repaired "
                         "before delivery.")
        if failed:
            lines.append(f"{len(failed)} step(s) could not be completed; nothing "
                         f"partial was reported as finished.")
        if skipped:
            lines.append(f"{len(skipped)} step(s) were skipped because a step they "
                         f"depend on did not complete.")
        return "\n".join(lines)

    def _audit(self, text: str, result: OrchestrationResult):
        """One line per run: who asked, what ran, what it produced, whether it
        was validated. Never the content of the request's data, never a prompt.
        """
        try:
            from core.security_manager import security_manager, RiskLevel
            user = ""
            try:
                from memory import database
                user_id = database.get_active_user()
                user = f"user={user_id}" if user_id else "user=none"
            except Exception:
                user = "user=?"
            summary = (f"orchestrator {user} kind={result.task_kind.value} "
                       f"agent={result.agent or '-'} "
                       f"model={result.model.model if result.model else '-'} "
                       f"tools={'+'.join(result.tools_used) or '-'} "
                       f"artifacts={len(result.artifacts)} "
                       f"validated={all(v.ok for v in result.validations) if result.validations else '-'} "
                       f"repaired={result.repaired} ok={result.succeeded}")
            security_manager._audit(summary, RiskLevel.SAFE, True)
        except Exception as error:
            logger.debug(f"audit note skipped: {error}")


_default: Optional[Orchestrator] = None


def get_orchestrator(direct_matcher=None, chat_handler=None, formatter=None) -> Orchestrator:
    """The process-wide orchestrator. The DecisionEngine supplies its route
    matcher and chat handler the first time it is built, so the pipeline uses
    the same command table and the same conversational behaviour as before."""
    global _default
    if _default is None:
        _default = Orchestrator(direct_matcher, chat_handler, formatter)
    else:
        if direct_matcher is not None:
            _default._direct_matcher = direct_matcher
        if chat_handler is not None:
            _default._chat_handler = chat_handler
        if formatter is not None:
            _default._formatter = formatter
    return _default
