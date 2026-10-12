"""
Task decomposition: one request, an ordered graph of steps with dependencies.

The behaviour this exists for is the request the brief calls out directly:

    "Read this PDF, analyse it, update Word, create Excel and make a PPT."

That is one sentence and six pieces of work, and the pieces are not
independent - the Word, Excel and deck must all come from the SAME analysis or
their numbers will disagree. So the graph carries dependencies, and the
analysis step is shared rather than repeated per deliverable.

Two decomposers, tried in order:

1. **Deterministic.** Named formats and named files are unambiguous. A request
   that says "excel and a deck" gets a plan built by Python, instantly, with
   no model involved and no chance of a hallucinated tool name.
2. **Model-assisted.** Only when the deterministic pass finds no deliverables
   does the existing `core.planner.decompose` get asked - and every tool it
   proposes is checked against the registry before it enters the plan.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from core.logger import get_logger
from core.orchestration.contracts import Complexity, PlanStep, TaskKind
from core.orchestration import agents as agent_registry
from core.orchestration.classifier import Classification

logger = get_logger("orchestration.plan")

# A path mentioned in the request, quoted or bare.
_PATH_PATTERN = re.compile(
    r"""["']([^"']+\.(?:xlsx|xlsm|xls|csv|pdf|docx|pptx|txt|md|json))["']"""
    r"""|([A-Za-z]:[\\/][^\s"']+\.(?:xlsx|xlsm|xls|csv|pdf|docx|pptx|txt|md|json))"""
    r"""|((?:\./|/|~/)[^\s"']+\.(?:xlsx|xlsm|xls|csv|pdf|docx|pptx|txt|md|json))"""
    r"""|(\b[\w\-. ]+\.(?:xlsx|xlsm|xls|csv|pdf|docx|pptx))\b""",
    re.IGNORECASE)

# An equipment tag such as P-204, PSV-2041, E-205.
_TAG_PATTERN = re.compile(r"\b([A-Z]{1,4}-\d{2,5})\b")

# Deliverable kind -> (tool when the plant database is the subject,
#                      tool for a free topic, validator kind)
_DELIVERABLE_TOOLS: Dict[TaskKind, Tuple[str, str, str]] = {
    TaskKind.WORD: ("executive_report", "write_document", "docx"),
    TaskKind.EXCEL: ("executive_workbook", "create_spreadsheet", "xlsx"),
    TaskKind.POWERPOINT: ("executive_presentation", "create_presentation", "pptx"),
    TaskKind.PDF: ("executive_report", "write_document", "docx"),
}

# Source-file readers by extension. These are deterministic: no model reads a
# spreadsheet to find out what is in it.
_READERS: Dict[str, Tuple[str, str]] = {
    "xlsx": ("analyze_spreadsheet", "data"),
    "xlsm": ("analyze_spreadsheet", "data"),
    "xls": ("analyze_spreadsheet", "data"),
    "csv": ("analyze_spreadsheet", "data"),
    "pdf": ("read_file", "data"),
    "docx": ("read_file", "data"),
    "txt": ("read_file", "data"),
    "md": ("read_file", "data"),
    "json": ("read_file", "data"),
}


def extract_path(text: str) -> Optional[str]:
    """The source file the request refers to, if it names one."""
    match = _PATH_PATTERN.search(text)
    if not match:
        return None
    for group in match.groups():
        if group:
            return group.strip()
    return None


def extract_subject(text: str) -> str:
    """An equipment tag makes the plant database the subject; without one the
    subject is the whole plant."""
    match = _TAG_PATTERN.search(text.upper())
    return match.group(1) if match else ""


def _tool_exists(name: str) -> bool:
    from core.tool_registry import registry
    return registry.get(name) is not None


def _step(index: int, tool: str, args: Dict, description: str,
          depends_on: Optional[List[str]] = None, validate_as: str = "",
          optional: bool = False) -> PlanStep:
    return PlanStep(id=f"s{index}", tool=tool, args=dict(args),
                    description=description, agent=agent_registry.agent_for_tool(tool) or "",
                    depends_on=list(depends_on or []), validate_as=validate_as,
                    optional=optional)


def deterministic_plan(text: str, classification: Classification) -> List[PlanStep]:
    """Build the graph from what the sentence actually names.

    Returns an empty list when the request names no deliverable - that is the
    signal for the caller to try the model-assisted decomposer instead.
    """
    deliverables = classification.deliverables or []
    if not deliverables:
        return []

    source = extract_path(text)
    subject = extract_subject(text)
    plant_subject = bool(subject) or _mentions_plant(text)

    steps: List[PlanStep] = []
    index = 1
    read_step_id: Optional[str] = None

    # --- 1. read the source, once, if one was named
    if source:
        extension = source.rsplit(".", 1)[-1].lower()
        reader, validate_as = _READERS.get(extension, ("read_file", "data"))
        if not _tool_exists(reader):
            reader = "read_file"
        step = _step(index, reader, {"path": source},
                     f"Read {source}", validate_as=validate_as)
        steps.append(step)
        read_step_id = step.id
        index += 1

    # --- 2. one step per deliverable, all depending on the single read
    for kind in deliverables:
        plant_tool, topic_tool, validate_as = _DELIVERABLE_TOOLS[kind]

        if source and kind in (TaskKind.POWERPOINT, TaskKind.WORD):
            # A spreadsheet in, an executive story out - analysed, not pasted.
            special = ("spreadsheet_to_presentation" if kind == TaskKind.POWERPOINT
                       else "spreadsheet_to_report")
            if source.rsplit(".", 1)[-1].lower() in ("xlsx", "xlsm", "xls", "csv") \
                    and _tool_exists(special):
                steps.append(_step(index, special, {"path": source},
                                   f"Build the {kind.value} from {source}",
                                   depends_on=[read_step_id] if read_step_id else [],
                                   validate_as=validate_as))
                index += 1
                continue

        if plant_subject and _tool_exists(plant_tool):
            args = {"subject": subject} if subject else {}
            steps.append(_step(index, plant_tool, args,
                               f"Build the {kind.value} from plant records"
                               + (f" for {subject}" if subject else ""),
                               depends_on=[read_step_id] if read_step_id else [],
                               validate_as=validate_as))
        else:
            topic = _topic_from(text)
            steps.append(_step(index, topic_tool, {"topic": topic},
                               f"Build the {kind.value} on {topic}",
                               depends_on=[read_step_id] if read_step_id else [],
                               validate_as=validate_as))
        index += 1

    # --- 3. a PDF deliverable is a conversion of the document, not a rewrite
    if TaskKind.PDF in deliverables and _tool_exists("convert_file"):
        producer = next((s for s in steps if s.validate_as == "docx"), None)
        if producer:
            steps.append(_step(index, "convert_file",
                               {"source_path": "", "target_format": "pdf"},
                               "Convert the document to PDF",
                               depends_on=[producer.id], validate_as="pdf",
                               optional=True))
    return steps


def _mentions_plant(text: str) -> bool:
    lowered = text.lower()
    return any(word in lowered for word in (
        "plant", "equipment", "maintenance", "alert", "inventory", "spare",
        "inspection", "safety", "permit", "isolation", "asset", "operations",
        "executive", "management", "board", "briefing"))


def _topic_from(text: str) -> str:
    """The subject of a free-topic document, with the instruction words removed."""
    cleaned = re.sub(
        r"\b(please|can you|could you|make|create|generate|build|write|prepare|"
        r"produce|a|an|the|me|for|about|on|into|and|then|also|new|"
        r"word|docx|document|report|excel|xlsx|spreadsheet|workbook|"
        r"powerpoint|pptx|slides?|deck|presentation|pdf|file)\b",
        " ", text, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,.-")
    return cleaned or "the requested subject"


def model_plan(goal: str) -> List[PlanStep]:
    """Fall back to the existing LLM planner, validated against the registry.

    Every proposed tool is checked before it enters the plan, so a model that
    invents `make_powerpoint` produces a shorter plan, never a broken one.
    """
    try:
        from core.planner import decompose
        raw = decompose(goal)
    except Exception as error:
        logger.warning(f"model planner unavailable: {error}")
        return []
    if not raw:
        return []
    steps: List[PlanStep] = []
    for index, item in enumerate(raw, start=1):
        tool = item.get("tool", "")
        if not _tool_exists(tool):
            continue
        step = _step(index, tool, item.get("args", {}) or {},
                     item.get("description", tool),
                     validate_as=agent_registry.validator_for(
                         agent_registry.agent_for_tool(tool) or "", tool))
        step.origin = "planner"
        steps.append(step)
    return steps


_COMPANY_REFERENCE = re.compile(
    r"\b(?:we|our|us|ours|the company|company'?s?|our company|the refinery|our refinery|our plants?|"
    r"organi[sz]ation'?s?|the business|annual reports?|our records|company records)\b", re.IGNORECASE)
_COMPANY_HISTORY = re.compile(r"\b(?:company history|history of (?:the |our )?company|company timeline|"
                              r"timeline of (?:the |our )?company)\b", re.IGNORECASE)
_FOLLOW_UP = re.compile(r"\b(?:this|that|the same|the previous|the last|your)\s+(?:analysis|answer|findings|results)\b|"
                        r"\bsame analysis\b", re.IGNORECASE)
_ARTIFACT_VALIDATOR = {"xlsx": "xlsx", "action_plan": "xlsx", "docx": "docx", "brief": "docx",
                       "pptx": "pptx", "pdf": "pdf", "md": ""}


# ------------------------------------------------------ knowledge requests --
# "Read the refinery SOP from the knowledge base", "Use the RAG information to answer
# this", "According to the uploaded SOP, what is the procedure?" - the operator asks
# for what the RECORDS say. These used to fall to read_file ("read ..." taken as a
# file name) or to the plain model with no retrieval at all.
_KNOWLEDGE_SOURCE = re.compile(
    r"\b(?:knowledge ?base|rag|enterprise knowledge|company knowledge|knowledge fabric|"
    r"(?:our|company|the company'?s?) (?:records|documents|docs|files|knowledge)|"
    r"(?:uploaded|ingested|indexed) (?:sops?|documents?|docs|files|manuals?|procedures?|policies|records|reports?)|"
    r"according to (?:the |our )?(?:uploaded |latest |current |relevant )?"
    r"(?:sops?|procedures?|manuals?|policy|policies|standards?|documents?|records|reports?)|"
    r"(?:what|how) does (?:the |our )?(?:[\w\-]+ ){0,3}(?:sop|manual|standard|procedure|policy) "
    r"(?:say|state|require|describe|specify)|"
    r"sops?|standard operating procedures?)\b", re.IGNORECASE)
_KB_BOILERPLATE = (
    re.compile(r"\b(?:please\s+)?(?:use|using|with|read|search|check|consult|query|look (?:it )?up in)\s+"
               r"(?:the\s+|this\s+|our\s+|my\s+)?(?:relevant\s+)?(?:rag|knowledge ?base|enterprise knowledge|"
               r"company knowledge|knowledge fabric)(?:\s+(?:information|info|data|context|documents))?", re.I),
    re.compile(r"\b(?:from|in|using|per|via|through)\s+(?:the\s+|our\s+|my\s+)?(?:rag|knowledge ?base|"
               r"enterprise knowledge|company knowledge|knowledge fabric)\b", re.I),
    re.compile(r"\bto answer (?:this|that|it|the question|my question)\b", re.I),
)
_CONTENT_WORD = re.compile(r"[a-z0-9][a-z0-9\-]+", re.I)
_EMPTY_WORDS = {"this", "that", "it", "the", "a", "an", "please", "answer", "question", "information", "info",
                "for", "about", "on", "and", "to", "me", "my", "our", "now", "again", "data", "context"}


def knowledge_request(text: str, classification: Optional[Classification] = None) -> bool:
    """Does the operator ask for the knowledge base / the records rather than the model?"""
    if not text or extract_path(text):
        return False                       # a named file is read by the document tools
    if re.match(r"^\s*(?:please\s+)?(?:write|draft|create|generate|prepare|make|author|compose)\b", text, re.I) \
            and not re.search(r"\b(?:from|using|per|according to|based on) (?:the |our )?(?:knowledge ?base|rag|records|"
                              r"uploaded|existing)\b", text, re.I):
        return False                       # "write an SOP for ..." creates a document; it looks nothing up
    if classification is not None and classification.deliverables and not re.search(
            r"\b(?:from|using|per|according to) (?:the |our )?(?:knowledge ?base|rag|records|sops?)\b", text, re.I):
        return False                       # "write an SOP for ..." is a document to create
    return bool(_KNOWLEDGE_SOURCE.search(text)) or (
        classification is not None and classification.kind == TaskKind.RAG)


def knowledge_question(text: str, history: Optional[List[Dict[str, str]]] = None) -> str:
    """The question to search the records for: the request without its 'use the
    knowledge base' wording - or, when nothing else is left ('Use the RAG
    information to answer this'), the operator's previous question."""
    question = text
    for pattern in _KB_BOILERPLATE:
        question = pattern.sub(" ", question)
    question = re.sub(r"\s+", " ", question).strip(" ,.;:!?-")
    question = re.sub(r"^(?:for|about|on|and|to)\s+", "", question, flags=re.I).strip()
    content = [w for w in _CONTENT_WORD.findall(question.lower()) if w not in _EMPTY_WORDS]
    if len(content) >= 2 or (content and not history):
        return question
    for message in reversed(history or []):
        if message.get("role") == "user" and message.get("content", "").strip():
            earlier = message["content"].strip()
            if knowledge_request(earlier) and len([w for w in _CONTENT_WORD.findall(
                    knowledge_question(earlier).lower()) if w not in _EMPTY_WORDS]) < 2:
                continue                  # an earlier "read the RAG" is not a question either
            return earlier
    return ""


def knowledge_plan(text: str, classification: Classification,
                   history: Optional[List[Dict[str, str]]] = None) -> List[PlanStep]:
    """Retrieval BEFORE the model: the question goes to the knowledge fabric
    (company_intelligence - hybrid search over every store, access-filtered, the
    evidence put into the model's prompt, cited and validated)."""
    if not knowledge_request(text, classification):
        return []
    question = knowledge_question(text, history)
    if not question:
        return [PlanStep(id="s1", tool="knowledge_overview", args={},
                         description="Say what the knowledge base holds (nothing was asked yet)",
                         agent="analysis")]
    logger.info(f"knowledge request -> retrieval: {question[:120]!r}")
    return [PlanStep(id="s1", tool="company_intelligence", args={"question": question},
                     description="Retrieve from the knowledge base, then answer from the evidence (cited)",
                     agent="analysis", validate_as="rag")]


def _names_known_subject(text: str) -> bool:
    """Does the question name a unit, asset or site the company records know?"""
    try:
        from knowledge_fabric import retrieval, store
        with store.connection() as conn:
            if not conn.execute("SELECT 1 FROM kf_documents LIMIT 1").fetchone():
                return False
            found = retrieval.question_entities(conn, text)
        return bool(retrieval.subject_entities(found))
    except Exception as error:
        logger.debug(f"company subject check skipped: {error}")
        return False


def _has_saved_analysis() -> bool:
    try:
        from auth.session_manager import session_manager
        from knowledge_fabric import analysis
        return analysis.latest(session_manager.username or "") is not None
    except Exception:
        return False


def company_follow_up(text: str) -> List[PlanStep]:
    """'Now create a management presentation from this analysis': the SAVED
    analysis, rendered - the same numbers - instead of a new document."""
    if not _FOLLOW_UP.search(text or "") or not _has_saved_analysis():
        return []
    try:
        from knowledge_fabric import planner
    except Exception:
        return []
    formats = planner.plan(text).formats
    if not formats:
        return []
    steps = [PlanStep(id=f"s{index}", tool="create_analysis_artifact", args={"format": fmt},
                      description=f"{fmt.upper()} from the saved analysis", agent="artifact",
                      validate_as=_ARTIFACT_VALIDATOR.get(fmt, ""))
             for index, fmt in enumerate(formats, 1)]
    logger.info(f"company-intelligence follow-up: {len(steps)} file(s) from the saved analysis")
    return steps


def company_plan(text: str, classification: Classification) -> List[PlanStep]:
    """A question about the company's OWN records goes to the company-intelligence
    pipeline (knowledge_fabric) - cited evidence, calculated numbers, and "no
    records" when there are none - instead of a model answering from nothing.

    Deliberately narrow: a measure (profit, cost, downtime ...) together with a
    reference to the company, a period, or a unit the records know; or the
    company's history. General questions ("what is EBITDA?") do not qualify.
    Requested files become separate steps (creating a file needs its own role)."""
    if extract_path(text):
        return []                              # a named file is read by the document tools
    try:
        from knowledge_fabric import planner, temporal
    except Exception:
        return []
    plan = planner.plan(text)
    if plan.formats and _FOLLOW_UP.search(text):
        return company_follow_up(text) or []
    scope = temporal.parse(text)
    has_period = bool(scope.years() or scope.relation == "relative")
    qualifies = bool(_COMPANY_HISTORY.search(text)) or (
        bool(plan.metrics) and (bool(_COMPANY_REFERENCE.search(text)) or has_period or _names_known_subject(text)))
    if not qualifies:
        return []
    steps = [PlanStep(id="s1", tool="company_intelligence", args={"question": text},
                      description="Answer from the company records (cited, calculated, verified)",
                      agent="analysis", validate_as="rag")]
    for index, fmt in enumerate(plan.formats, 2):
        steps.append(PlanStep(id=f"s{index}", tool="create_analysis_artifact", args={"format": fmt},
                              description=f"{fmt.upper()} from the same analysis", agent="artifact",
                              depends_on=["s1"], validate_as=_ARTIFACT_VALIDATOR.get(fmt, "")))
    logger.info(f"company-intelligence plan: {len(steps)} step(s)")
    return steps


# Routes whose patterns also swallow knowledge requests: "Read the refinery SOP from
# the knowledge base" is not a file called "the refinery SOP from the knowledge base",
# and the old keyword search only reads the retired knowledge-pack index.
_KNOWLEDGE_SHADOWED_ROUTES = ("read_file", "search_knowledge")


def build_plan(text: str, classification: Classification,
               direct: Optional[Tuple[str, Dict]] = None,
               history: Optional[List[Dict[str, str]]] = None) -> List[PlanStep]:
    """The one entry point. Order matters: an exact route beats a guess, a
    deterministic decomposition beats a model, and a model beats nothing -
    except that a request for the knowledge base is never answered by a model
    alone: retrieval runs first."""
    # A matched route is one action. When the sentence asks for SEVERAL
    # deliverables the route table can only see the first of them, so the
    # multi-step plan wins - otherwise "read this and make a deck and a
    # report" would stop after the read.
    multi = len(classification.deliverables) > 1

    # "... from this analysis" refers to a SAVED company analysis: render it,
    # even when a route would otherwise write a new document about the words
    # "the same analysis". Only when such an analysis exists for this user.
    follow_up = company_follow_up(text)
    if follow_up:
        return follow_up

    knowledge = [] if multi else knowledge_plan(text, classification, history)
    if knowledge and (not direct or direct[0] in _KNOWLEDGE_SHADOWED_ROUTES):
        return knowledge

    if direct and not multi:
        tool, args = direct
        agent = agent_registry.agent_for_tool(tool) or agent_registry.agent_for_kind(
            classification.kind)
        return [PlanStep(id="s1", tool=tool, args=dict(args),
                         description=f"Run {tool}", agent=agent,
                         validate_as=agent_registry.validator_for(agent, tool))]

    # The company's own records, before a generic topic document: a verified
    # analysis beats a model writing about "losses" from general knowledge.
    plan = company_plan(text, classification)
    if plan:
        return plan

    plan = deterministic_plan(text, classification)
    if not plan and direct:
        tool, args = direct
        agent = agent_registry.agent_for_tool(tool) or agent_registry.agent_for_kind(
            classification.kind)
        return [PlanStep(id="s1", tool=tool, args=dict(args),
                         description=f"Run {tool}", agent=agent,
                         validate_as=agent_registry.validator_for(agent, tool))]
    if plan:
        logger.info(f"deterministic plan: {len(plan)} step(s)")
        return plan

    if classification.kind == TaskKind.MULTI_STEP or classification.complexity in (
            Complexity.HARD,):
        plan = model_plan(text)
        if plan:
            logger.info(f"model-assisted plan: {len(plan)} step(s)")
            return plan
    return []


def ready_steps(steps: List[PlanStep], done: Dict[str, PlanStep]) -> List[PlanStep]:
    """Steps whose dependencies are all satisfied - the execution order."""
    from core.orchestration.contracts import Status
    ready = []
    for step in steps:
        if step.status is not Status.PENDING:
            continue
        if all(dependency in done for dependency in step.depends_on):
            ready.append(step)
    return ready
