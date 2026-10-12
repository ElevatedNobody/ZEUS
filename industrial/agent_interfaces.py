"""
Phase K - the industrial agent layer: contracts only, no agent logic yet.

This module prepares the environment and interfaces for six agents:

    Inspection Agent      (FIRST to be implemented)
    Document Agent
    Engineering Agent
    Coding Agent
    Calculation Agent
    Report/Approval Agent

Nothing here implements an agent's reasoning. What it does provide is the
contract every one of them will be built against, and a working readiness
check so an agent can report exactly what is missing before it is asked to do
anything: models, OCR engines, schemas, knowledge base content and tools.

Deliberate design choices:

* An agent is NOT a model. Each agent declares the model SLOTS it needs
  (reasoning / vision / embedding / json_structured / code) and resolves them
  through the existing core/model_router.py at run time. No agent hard-codes
  a model name and no agent needs its own model.
* Agents act through the existing tool registry, so security confirmation,
  timeouts and the audit log apply to them exactly as they do to every other
  ZEUS capability.
* Every agent output carries evidence. AgentResult cannot represent a finding
  without the evidence ids behind it, which is what stops an agent from
  producing an unsupported recommendation.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.logger import get_logger

logger = get_logger("industrial.agents")

# Model slots resolved through core/model_router.py - never model names here.
SLOT_REASONING = "reasoning"
SLOT_JSON = "json_structured"
SLOT_VISION = "vision"
SLOT_EMBEDDING = "embedding"
SLOT_RERANKER = "reranker"
SLOT_CODE = "code"
SLOT_CLASSIFY = "classify"


# --------------------------------------------------------------------- data

@dataclass
class Finding:
    """One conclusion an agent reached, with the evidence that supports it.
    `evidence_ids` is mandatory in practice: a finding with none is marked
    unsupported and must be shown as an inference, never as a fact."""
    statement: str
    severity: str = "info"                    # info|low|medium|high|critical
    category: str = "observation"             # observation|defect|deviation|risk|compliance
    evidence_ids: List[str] = field(default_factory=list)
    equipment: Optional[str] = None
    confidence: float = 0.0
    is_inference: bool = False

    def __post_init__(self):
        if not self.evidence_ids:
            self.is_inference = True

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Recommendation:
    action: str
    priority: str = "medium"                  # immediate|high|medium|low
    rationale: str = ""
    evidence_ids: List[str] = field(default_factory=list)
    supporting_reference: Optional[str] = None   # SOP / standard / manual cited
    target_date: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class AgentRequest:
    task: str                                  # what the user asked for
    document_paths: List[str] = field(default_factory=list)
    equipment: Optional[str] = None
    document_type: Optional[str] = None
    output_format: str = "text"                # text|docx|xlsx|pptx|json
    output_path: Optional[str] = None
    options: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentResult:
    agent_id: str
    status: str                                # ok|partial|blocked|failed|not_implemented
    summary: str = ""
    findings: List[Finding] = field(default_factory=list)
    recommendations: List[Recommendation] = field(default_factory=list)
    evidence: List[dict] = field(default_factory=list)      # provenance.evidence_table()
    sources: Dict[str, Any] = field(default_factory=dict)   # provenance.answer_sources()
    artifacts: List[str] = field(default_factory=list)      # generated files
    warnings: List[str] = field(default_factory=list)
    detail: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["findings"] = [f.to_dict() for f in self.findings]
        d["recommendations"] = [r.to_dict() for r in self.recommendations]
        return d

    def unsupported_findings(self) -> List[Finding]:
        return [f for f in self.findings if not f.evidence_ids]


@dataclass
class AgentContext:
    """Everything an agent is allowed to reach. Constructed once per run so an
    agent never imports global state directly and stays testable."""
    knowledge_base: Any = None
    taxonomy: Any = None
    schemas: Any = None
    tool_registry: Any = None
    model_router: Any = None
    workspace: Optional[Path] = None
    evidence_budget: int = 8
    allow_vision: bool = True
    allow_generation: bool = True

    @classmethod
    def build(cls, workspace: Optional[Path] = None, evidence_budget: int = 8) -> "AgentContext":
        from core.model_router import model_router
        from core.tool_registry import registry
        from industrial import schemas as schema_module
        from industrial.knowledge_base import knowledge_base
        from industrial.taxonomy import get_taxonomy
        return cls(knowledge_base=knowledge_base, taxonomy=get_taxonomy(),
                   schemas=schema_module.get_registry(), tool_registry=registry,
                   model_router=model_router,
                   workspace=workspace or __import__("core.paths", fromlist=["documents_root"]).documents_root(),
                   evidence_budget=evidence_budget)


# Slot names that are NOT chat models. An agent may declare `embedding` in
# its required slots, but that slot is satisfied by the embeddings endpoint,
# not by a chat model, and its readiness is answered by a real probe. The
# providers are imported lazily so this module stays cheap to import.
def _embedding_provider():
    from core.capabilities import embedding_provider
    return embedding_provider


def _rerank_provider():
    from core.capabilities import rerank_provider
    return rerank_provider


_NON_CHAT_SLOTS = {
    "embedding": _embedding_provider,
    "reranker": _rerank_provider,
    "reranking": _rerank_provider,
}


# -------------------------------------------------------------------- base

class IndustrialAgent(ABC):
    """Contract every industrial agent implements."""

    agent_id: str = "industrial"
    name: str = "Industrial Agent"
    purpose: str = ""
    required_slots: List[str] = []
    optional_slots: List[str] = []
    required_tools: List[str] = []
    required_schemas: List[str] = []
    requires_ocr: bool = False
    requires_knowledge_base: bool = False
    workflow: List[str] = []
    implemented: bool = False

    # ------------------------------------------------------------ readiness

    def preflight(self, context: Optional[AgentContext] = None) -> Dict[str, Any]:
        """Report whether this agent could run right now, and what is missing.
        Implemented in the base class so every agent gets the same honest
        readiness answer without writing it again."""
        context = context or AgentContext.build()
        missing: List[str] = []
        detail: Dict[str, Any] = {}

        # model slots
        slots = {}
        try:
            from core.ollama_client import ollama_client
            installed = ollama_client.list_models() if ollama_client.is_available() else []
        except Exception:
            installed = []
        for slot in self.required_slots + self.optional_slots:
            required = slot in self.required_slots
            # Embedding and reranking are not chat slots: they have their own
            # interfaces and their own health probe. Asking the chat router
            # for them is an error, and answering from the installed-model
            # list would be a guess about an interface rather than a check of
            # it - which is exactly how a working BGE-M3 came to be reported
            # as unavailable.
            capability = _NON_CHAT_SLOTS.get(slot)
            if capability is not None:
                # One real probe per capability per roster check, not one per
                # agent: the embedding probe loads a model, and ten identical
                # probes made the agent roster take over a minute to open.
                cache = context.__dict__.setdefault("_capability_cache", {})
                if slot not in cache:
                    cache[slot] = capability().check()
                report = cache[slot]
                slots[slot] = {"model": report.model or "none", "available": report.ok,
                               "required": required, "detail": report.detail}
                if not report.ok and required:
                    missing.append(f"{slot} is not available: {report.detail}")
                continue

            model = context.model_router.route(slot)
            from core.ollama_client import OllamaClient
            present = any(OllamaClient._model_matches(m, model) for m in installed)
            slots[slot] = {"model": model, "available": present, "required": required}
            if not present and required:
                missing.append(f"model slot '{slot}' ({model}) not available locally")
        detail["model_slots"] = slots

        # tools
        tool_status = {}
        for tool in self.required_tools:
            present = context.tool_registry.get(tool) is not None
            tool_status[tool] = present
            if not present:
                missing.append(f"tool '{tool}' is not registered")
        detail["tools"] = tool_status

        # schemas
        schema_status = {}
        for sid in self.required_schemas:
            present = sid in context.schemas.schemas
            schema_status[sid] = present
            if not present:
                missing.append(f"document schema '{sid}' is not defined")
        detail["schemas"] = schema_status

        # OCR
        if self.requires_ocr:
            from industrial import ocr_engines
            engines = ocr_engines.available_engines()
            detail["ocr_engines"] = engines
            if not engines:
                missing.append("no OCR engine available")

        # knowledge base
        if self.requires_knowledge_base:
            if "_kb_stats" not in context.__dict__:
                context.__dict__["_kb_stats"] = context.knowledge_base.stats()
            stats = context.__dict__["_kb_stats"]
            detail["knowledge_base"] = {"documents": stats.get("documents", 0),
                                        "chunks": stats.get("chunks", 0),
                                        "embedded": stats.get("embedded_chunks", 0),
                                        "embedding_ready": stats.get("embedding_ready")}
            if not stats.get("embedding_ready"):
                missing.append(f"embedding model unavailable: {stats.get('embedding_detail')}")
            if not stats.get("embedded_chunks"):
                missing.append("industrial knowledge base has no embedded content - "
                               "run scripts/ingest_industrial_knowledge.py")

        return {"agent_id": self.agent_id, "name": self.name,
                "implemented": self.implemented,
                "ready": not missing and self.implemented,
                "environment_ready": not missing,
                "missing": missing, "workflow": self.workflow, "detail": detail}

    def describe(self) -> Dict[str, Any]:
        return {"agent_id": self.agent_id, "name": self.name, "purpose": self.purpose,
                "implemented": self.implemented, "workflow": self.workflow,
                "required_slots": self.required_slots, "optional_slots": self.optional_slots,
                "required_tools": self.required_tools, "required_schemas": self.required_schemas,
                "requires_ocr": self.requires_ocr,
                "requires_knowledge_base": self.requires_knowledge_base}

    # ------------------------------------------------------------ execution

    @abstractmethod
    def run(self, request: AgentRequest, context: Optional[AgentContext] = None) -> AgentResult:
        """Execute the agent. Implementations must return an AgentResult and
        must not raise for ordinary failures."""

    def _not_implemented(self, note: str = "") -> AgentResult:
        return AgentResult(
            agent_id=self.agent_id, status="not_implemented",
            summary=(f"{self.name} is prepared but not implemented yet. "
                     f"Its environment, interfaces and workflow contract are in place."),
            warnings=[note] if note else [],
            detail={"workflow": self.workflow, "preflight": self.preflight()})


# ------------------------------------------------------- the six agent stubs

class InspectionAgent(IndustrialAgent):
    """FIRST agent to be implemented.

    Target workflow (each step maps to a component that already exists):

        Inspection Report        doc_pipeline.extract_document
        -> OCR / Vision          ocr_engines.run_ocr + doc_pipeline.describe_with_vision
        -> Information Extraction schemas.extract_fields('inspection_report')
        -> Industrial Knowledge   knowledge_base.search(document_class=knowledge_corpus)
        -> SOP / Manual Retrieval knowledge_base.search(document_type='sop'|'vendor_document')
        -> Evidence Ranking       reranker.rerank
        -> Reasoning              model_router.route('reasoning') over ranked evidence
        -> Findings               Finding objects, each with evidence ids
        -> Recommendation         Recommendation objects, each with evidence ids
        -> Approval Note          schemas 'approval_note' field set
        -> Verification           provenance.CitationValidator + schema validation
        -> DOCX                   existing document generation tooling
    """
    agent_id = "inspection"
    name = "Inspection Agent"
    purpose = ("Read an inspection report, extract its findings against the inspection "
               "schema, ground them in plant documents, SOPs and industrial knowledge, "
               "rank the evidence, and produce a verified findings + recommendation + "
               "approval note package as a DOCX.")
    required_slots = [SLOT_REASONING, SLOT_JSON, SLOT_EMBEDDING]
    optional_slots = [SLOT_VISION, SLOT_RERANKER]
    required_tools = ["write_document", "read_file"]
    required_schemas = ["inspection_report", "sop", "approval_note", "equipment_history"]
    requires_ocr = True
    requires_knowledge_base = True
    workflow = [
        "ingest_inspection_report", "ocr_or_vision_pages", "extract_schema_fields",
        "retrieve_industrial_knowledge", "retrieve_sop_and_manuals", "rank_evidence",
        "reason_over_evidence", "produce_findings", "produce_recommendations",
        "draft_approval_note", "verify_citations_and_schema", "generate_docx",
    ]
    implemented = True

    def run(self, request: AgentRequest, context: Optional[AgentContext] = None) -> AgentResult:
        from industrial.agent_workflows import run_inspection
        return _guarded_run(self, run_inspection, request, context)


class DocumentAgent(IndustrialAgent):
    agent_id = "industrial_document"
    name = "Document Agent"
    purpose = ("Ingest, classify, search and summarise industrial documents of any "
               "supported format, always answering with document, page, section and "
               "revision.")
    required_slots = [SLOT_EMBEDDING, SLOT_REASONING]
    optional_slots = [SLOT_JSON, SLOT_VISION, SLOT_RERANKER]
    required_tools = ["read_file", "search_files"]
    required_schemas = []
    requires_ocr = True
    requires_knowledge_base = True
    workflow = ["discover_documents", "classify", "extract", "chunk_with_provenance",
                "embed", "answer_with_citations"]
    implemented = True

    def run(self, request, context=None) -> AgentResult:
        from industrial.agent_workflows import run_document
        return _guarded_run(self, run_document, request, context)


class EngineeringAgent(IndustrialAgent):
    agent_id = "engineering"
    name = "Engineering Agent"
    purpose = ("Answer equipment and process engineering questions by combining the "
               "user's plant documents with the local industrial knowledge corpus, "
               "keeping the two clearly separated.")
    required_slots = [SLOT_REASONING, SLOT_EMBEDDING]
    optional_slots = [SLOT_RERANKER, SLOT_JSON]
    required_tools = []
    required_schemas = ["technical_datasheet", "engineering_drawing", "equipment_history"]
    requires_ocr = False
    requires_knowledge_base = True
    workflow = ["understand_question", "expand_with_taxonomy", "retrieve_plant_documents",
                "retrieve_corpus_background", "rank_evidence", "answer_with_claim_separation"]
    implemented = True

    def run(self, request, context=None) -> AgentResult:
        from industrial.agent_workflows import run_engineering
        return _guarded_run(self, run_engineering, request, context)


class CodingAgent(IndustrialAgent):
    agent_id = "industrial_coding"
    name = "Coding Agent"
    purpose = ("Write and explain local scripts for industrial data work - parsing logs, "
               "trending readings, building spreadsheets - using the dedicated coding "
               "model. Generated code is never executed without the existing security "
               "confirmation path.")
    required_slots = [SLOT_CODE]
    optional_slots = [SLOT_REASONING]
    required_tools = ["run_python_sandboxed"]
    required_schemas = []
    requires_ocr = False
    requires_knowledge_base = False
    workflow = ["understand_requirement", "generate_code", "run_in_sandbox",
                "read_error", "fix_and_rerun", "deliver_code_output_and_attempts"]
    implemented = True

    def run(self, request, context=None) -> AgentResult:
        from industrial.agent_workflows import run_coding
        return _guarded_run(self, run_coding, request, context)


class CalculationAgent(IndustrialAgent):
    agent_id = "calculation"
    name = "Calculation Agent"
    purpose = ("Perform engineering calculations deterministically in Python - corrosion "
               "rate, remaining life, minimum required thickness, heat duty, pump head - "
               "and show the formula, inputs, units and source of every input value. "
               "The model proposes; Python computes.")
    required_slots = []
    optional_slots = [SLOT_JSON, SLOT_CODE, SLOT_REASONING]
    required_tools = ["calculate"]
    required_schemas = ["inspection_report", "technical_datasheet"]
    requires_ocr = False
    requires_knowledge_base = False
    workflow = ["identify_calculation", "collect_inputs_with_provenance", "check_units",
                "compute_deterministically", "state_formula_and_assumptions",
                "flag_inputs_that_were_inferred"]
    implemented = True

    def run(self, request, context=None) -> AgentResult:
        from industrial.agent_workflows import run_calculation
        return _guarded_run(self, run_calculation, request, context)


class ReportApprovalAgent(IndustrialAgent):
    agent_id = "report_approval"
    name = "Report/Approval Agent"
    purpose = ("Assemble verified findings, recommendations and evidence into an "
               "inspection report, approval note or summary document (DOCX/XLSX/PPTX), "
               "with a citation table and no unsupported statement.")
    required_slots = [SLOT_REASONING]
    optional_slots = [SLOT_JSON]
    required_tools = ["write_document", "create_spreadsheet", "create_presentation"]
    required_schemas = ["approval_note", "inspection_report", "maintenance_report"]
    requires_ocr = False
    requires_knowledge_base = True
    workflow = ["collect_findings", "verify_every_claim_has_evidence", "draft_document",
                "attach_evidence_table", "validate_against_schema", "export_file"]
    implemented = True

    def run(self, request, context=None) -> AgentResult:
        from industrial.agent_workflows import run_report_approval
        return _guarded_run(self, run_report_approval, request, context)


def _guarded_run(agent: "IndustrialAgent", function, request, context) -> AgentResult:
    """Run an agent workflow; an unexpected exception becomes a FAILED result
    with the reason, never a crash and never a silent success."""
    if request is None:
        return AgentResult(agent_id=agent.agent_id, status="blocked",
                           summary=f"{agent.name}: no request was given, so nothing was done.")
    try:
        return function(request, context)
    except Exception as error:
        import traceback
        logger.error(f"{agent.name} failed: {error} | {traceback.format_exc()}")
        return AgentResult(agent_id=agent.agent_id, status="failed",
                           summary=f"{agent.name} failed: {error}")


class ResearchAgent(IndustrialAgent):
    agent_id = "research"
    name = "Research Agent"
    purpose = ("Answer questions from the local knowledge base and plant documents with "
               "cited evidence, and say what is missing instead of guessing.")
    required_slots = [SLOT_REASONING, SLOT_EMBEDDING]
    optional_slots = [SLOT_RERANKER]
    requires_knowledge_base = True
    workflow = ["understand_question", "expand_with_taxonomy", "retrieve", "rerank",
                "answer_with_citations", "validate_citations"]
    implemented = True

    def run(self, request, context=None) -> AgentResult:
        from industrial.agent_workflows import run_research
        return _guarded_run(self, run_research, request, context)


class PlanningAgent(IndustrialAgent):
    agent_id = "planning"
    name = "Planning Agent"
    purpose = "Turn a goal into a dated, validated task plan and, on request, the task list."
    required_slots = [SLOT_JSON]
    required_tools = ["add_todo"]
    workflow = ["understand_goal", "decompose", "validate_dates_and_owners", "create_tasks"]
    implemented = True

    def run(self, request, context=None) -> AgentResult:
        from industrial.agent_workflows import run_planning
        return _guarded_run(self, run_planning, request, context)


class SafetyAgent(IndustrialAgent):
    agent_id = "safety"
    name = "Safety Agent"
    purpose = ("Report the safety position - permits, isolations, safety alerts and the "
               "governing procedures. Never approves anything.")
    optional_slots = [SLOT_REASONING, SLOT_EMBEDDING]
    required_tools = ["safety_status", "list_permits", "list_isolations"]
    workflow = ["collect_permits_and_isolations", "retrieve_procedures", "report"]
    implemented = True

    def run(self, request, context=None) -> AgentResult:
        from industrial.agent_workflows import run_safety
        return _guarded_run(self, run_safety, request, context)


class OperationsAgent(IndustrialAgent):
    agent_id = "operations"
    name = "Industrial Operations Agent"
    purpose = "Plant status, open alerts, maintenance due and telemetry, labelled by source."
    required_tools = ["plant_status", "list_plant_alerts", "maintenance_due"]
    workflow = ["collect_status", "collect_alerts", "collect_maintenance_due",
                "collect_telemetry", "report"]
    implemented = True

    def run(self, request, context=None) -> AgentResult:
        from industrial.agent_workflows import run_operations
        return _guarded_run(self, run_operations, request, context)


INDUSTRIAL_AGENTS: Dict[str, IndustrialAgent] = {
    a.agent_id: a for a in (
        InspectionAgent(), DocumentAgent(), EngineeringAgent(),
        CodingAgent(), CalculationAgent(), ReportApprovalAgent(),
        ResearchAgent(), PlanningAgent(), SafetyAgent(), OperationsAgent(),
    )
}

FIRST_AGENT = "inspection"


def get_agent(agent_id: str) -> Optional[IndustrialAgent]:
    return INDUSTRIAL_AGENTS.get(agent_id)


def list_industrial_agents() -> List[dict]:
    return [a.describe() for a in INDUSTRIAL_AGENTS.values()]


def preflight_all(context: Optional[AgentContext] = None) -> Dict[str, Any]:
    context = context or AgentContext.build()
    results = {aid: agent.preflight(context) for aid, agent in INDUSTRIAL_AGENTS.items()}
    return {
        "agents": results,
        "environment_ready": [aid for aid, r in results.items() if r["environment_ready"]],
        "blocked": {aid: r["missing"] for aid, r in results.items() if r["missing"]},
        "first_agent": FIRST_AGENT,
    }


def register_with_core_registry() -> List[str]:
    """Add the industrial agents to the existing core/agents.py registry so
    'list agents' shows them alongside the agents ZEUS already had. Purely
    additive - no existing entry is modified or removed."""
    from core.agents import AGENT_REGISTRY, AgentSpec

    added = []
    for agent in INDUSTRIAL_AGENTS.values():
        key = f"industrial_{agent.agent_id}" if agent.agent_id in AGENT_REGISTRY else agent.agent_id
        if key in AGENT_REGISTRY:
            continue
        AGENT_REGISTRY[key] = AgentSpec(
            id=key, name=agent.name, purpose=agent.purpose,
            backend=("local model slots: " + ", ".join(agent.required_slots)
                     + (f" (optional: {', '.join(agent.optional_slots)})"
                        if agent.optional_slots else "")),
            owned_tools=list(agent.required_tools), offline=True,
            status="prepared" if not agent.implemented else "active",
            implemented=agent.implemented,
        )
        added.append(key)
    return added
