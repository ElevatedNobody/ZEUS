"""
Task classification - deterministic first, model only when genuinely unsure.

Classifying "make me a PowerPoint about pump maintenance" does not require a
language model, and using one costs a second of latency and a gigabyte of RAM
to learn what the word "PowerPoint" already said. So this is regex and
keywords, and the LLM is consulted only for free text that matched nothing -
and even then only when Ollama is already up.

The output drives four decisions: which agent runs, which model slot the
router should look in, how hard the task is, and which validator checks the
result.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from core.logger import get_logger
from core.orchestration.contracts import Complexity, TaskKind

logger = get_logger("orchestration.classify")


@dataclass
class Classification:
    kind: TaskKind
    complexity: Complexity
    signals: List[str] = field(default_factory=list)
    deliverables: List[TaskKind] = field(default_factory=list)
    confident: bool = True

    def as_dict(self) -> Dict[str, object]:
        return {"kind": self.kind.value, "complexity": self.complexity.value,
                "signals": list(self.signals),
                "deliverables": [d.value for d in self.deliverables],
                "confident": self.confident}


# Ordered: the FIRST matching kind wins.
#
# File-handling verbs come FIRST, ahead of the format patterns, because
# "copy report.docx to the archive folder" is a filesystem operation that
# happens to mention a format - not a request to write a Word document. The
# format patterns then beat the generic verbs below them.
_KIND_PATTERNS: List[tuple] = [
    (TaskKind.FILE_OPS, re.compile(
        r"^\s*(?:please\s+)?(copy|move|rename|delete|archive|organi[sz]e)\b"
        r"|\b(list files|find files|create (?:a )?folder|delete (?:the )?(?:file|folder))\b")),
    (TaskKind.POWERPOINT, re.compile(
        r"\b(powerpoint|pptx?|slide[s]?|deck|presentation|briefing pack|"
        r"management pack|board pack)\b")),
    (TaskKind.EXCEL, re.compile(
        r"\b(excel|xlsx?|spreadsheet|workbook|worksheet|pivot)\b")),
    (TaskKind.WORD, re.compile(
        r"\b(word document|\.docx|docx|word file|write (?:a |an )?(?:report|memo|"
        r"letter|assignment|essay|note)|approval note)\b")),
    (TaskKind.PDF, re.compile(r"\b(pdf)\b")),
    (TaskKind.CONVERSION, re.compile(
        r"\b(convert|turn (?:this|it|the .+?) into|export (?:as|to)|save as)\b")),
    (TaskKind.OCR, re.compile(r"\b(ocr|scan(?:ned)?|read the text (?:in|from))\b")),
    (TaskKind.VISION, re.compile(
        r"\b(screenshot|screen|image|photo|picture|diagram|what (?:is|do you) see)\b")),
    (TaskKind.RAG, re.compile(
        r"\b(knowledge base|according to (?:the )?(?:sop|manual|document)|"
        r"what does the (?:sop|manual|standard|procedure) say|search (?:the )?(?:docs|documents)|"
        r"index (?:this |the )?folder)\b")),
    (TaskKind.DOCUMENT_READ, re.compile(
        r"\b(read|open|summari[sz]e|analy[sz]e|extract)\b.{0,30}"
        r"\b(file|document|report|pdf|docx|xlsx|csv|spreadsheet)\b")),
    (TaskKind.CODE, re.compile(
        r"\b(code|function|script|python|javascript|bug|traceback|refactor|"
        r"unit test|stack ?trace|compile)\b|```")),
    (TaskKind.INDUSTRIAL_DATA, re.compile(
        r"\b(equipment|maintenance|inspection|alert[s]?|inventory|spare[s]?|permit|"
        r"isolation|lockout|tagout|loto|plant|asset|work order|downtime|"
        r"stock level|supplier|sop)\b")),
    (TaskKind.FILE_OPS, re.compile(
        r"\b(copy|move|rename|delete|organi[sz]e|list files|find files|"
        r"create (?:a )?folder|archive)\b")),
    (TaskKind.AUTOMATION, re.compile(
        r"\b(open (?:chrome|firefox|notepad|explorer|settings|calculator)|"
        r"launch |lock (?:the )?(?:screen|computer|pc)|shutdown|restart|"
        r"take a screenshot|type |press |clipboard|volume|mute)\b")),
    (TaskKind.MEMORY, re.compile(
        r"\b(remember (?:that )?|forget |what do you remember|recall )\b")),
    (TaskKind.REASONING, re.compile(
        r"\b(why|explain|compare|evaluate|trade-?off|pros and cons|"
        r"what would happen|reason(?:ing)? about)\b")),
]

# Deliverable markers, used to spot "word AND excel AND ppt" in one request.
_DELIVERABLE_PATTERNS: List[tuple] = [
    (TaskKind.WORD, re.compile(r"\b(word|docx|report|memo|document)\b")),
    (TaskKind.EXCEL, re.compile(r"\b(excel|xlsx|spreadsheet|workbook)\b")),
    (TaskKind.POWERPOINT, re.compile(r"\b(powerpoint|pptx?|slides?|deck|presentation)\b")),
    (TaskKind.PDF, re.compile(r"\bpdf\b")),
]

_CHAIN_MARKERS = re.compile(
    r"\b(and then|then |after that|followed by|,\s*(?:and\s+)?(?:also\s+)?(?:then\s+)?"
    r"(?:create|make|generate|build|write|export|convert))\b")

_HARD_MARKERS = re.compile(
    r"\b(analy[sz]e|strateg|recommend|root cause|executive|board|"
    r"in depth|comprehensive|detailed|assess|evaluate|implications)\b")

_SIMPLE_MARKERS = re.compile(
    r"^\s*(hi|hello|hey|thanks|thank you|ok|okay|yes|no|what time|"
    r"who are you|what can you do)\b")


# A filename in the sentence names the INPUT. Its extension must not be read
# as a requested output, or "read data.xlsx and make a deck" silently acquires
# a phantom spreadsheet deliverable.
_FILENAME_TOKEN = re.compile(
    r"""(?:["'][^"']+["']|\S+)\.(?:xlsx|xlsm|xls|csv|pdf|docx|pptx|txt|md|json)\b""",
    re.IGNORECASE)


def strip_filenames(text: str) -> str:
    return _FILENAME_TOKEN.sub(" ", text)


_QUESTION_START = re.compile(r"^\s*(what|which|when|who|whom|whose|where|why|how|did|do|does|"
                             r"have|has|had|was|were|is|are|was there)\b")
_REQUEST_FORM = re.compile(r"\b(can|could|would|will) you\b|\bplease\b|\b(i need|i want|i'd like|"
                           r"give me|send me|get me|let'?s|help me (?:make|create|write|build|prepare))\b")


def asks_about_existing(text: str) -> bool:
    """Is this a QUESTION rather than an order to produce something?

    'What decision did we make earlier about the refinery report?' names a
    report but asks for a memory; it was being planned as write_document
    (topic 'What decision did we earlier refinery ?') and would have written
    a Word file instead of answering. A question still produces a deliverable
    when it is phrased as a request: 'can you make a report on P-101?'."""
    lowered = (text or "").strip().lower()
    is_question = lowered.endswith("?") or bool(_QUESTION_START.match(lowered))
    return is_question and not _REQUEST_FORM.search(lowered)


def detect_deliverables(text: str) -> List[TaskKind]:
    """Which output FORMATS the request asks for, in the order named.

    'read this PDF, analyse it, update Word, create Excel and make a PPT'
    is one request with three deliverables, and treating it as three tasks
    from one analysis is what keeps the numbers consistent between them.

    Filenames are removed first: the '.xlsx' in a source path is the input,
    not an order for a workbook.
    """
    lowered = strip_filenames(text.lower())
    found: List[TaskKind] = []
    for kind, pattern in _DELIVERABLE_PATTERNS:
        match = pattern.search(lowered)
        if match and kind not in found:
            found.append(kind)
    # Order them the way the sentence does, so the plan follows the request.
    positions = {}
    for kind in found:
        pattern = dict(_DELIVERABLE_PATTERNS)[kind]
        match = pattern.search(lowered)
        positions[kind] = match.start() if match else 0
    return sorted(found, key=lambda k: positions[k])


def estimate_complexity(text: str, kind: TaskKind) -> Complexity:
    lowered = text.lower().strip()
    if _SIMPLE_MARKERS.match(lowered):
        return Complexity.SIMPLE
    if kind in (TaskKind.OCR, TaskKind.CONVERSION, TaskKind.FILE_OPS,
                TaskKind.AUTOMATION, TaskKind.MEMORY, TaskKind.INDUSTRIAL_DATA):
        return Complexity.TRIVIAL
    words = len(lowered.split())
    if _HARD_MARKERS.search(lowered) or words > 45:
        return Complexity.HARD
    if words > 14 or kind in (TaskKind.REASONING, TaskKind.CODE, TaskKind.RAG,
                              TaskKind.POWERPOINT, TaskKind.EXCEL, TaskKind.WORD):
        return Complexity.MODERATE
    return Complexity.SIMPLE


def classify(text: str, use_model_fallback: bool = False) -> Classification:
    """Deterministic classification. `use_model_fallback` consults the small
    'classify' model ONLY when nothing matched and Ollama is already running."""
    lowered = (text or "").lower().strip()
    signals: List[str] = []

    deliverables = detect_deliverables(lowered)
    if deliverables and asks_about_existing(lowered):
        deliverables = []
        signals.append("a question about something - nothing to produce")
    chained = bool(_CHAIN_MARKERS.search(lowered))

    kind: Optional[TaskKind] = None
    for candidate, pattern in _KIND_PATTERNS:
        match = pattern.search(lowered)
        if match:
            kind = candidate
            signals.append(f"matched '{match.group(0).strip()}' -> {candidate.value}")
            break

    # In a file or conversion request the format words name the OPERAND, not
    # something to be produced: "copy report.docx" asks for no new document.
    if kind in (TaskKind.FILE_OPS, TaskKind.CONVERSION, TaskKind.DOCUMENT_READ):
        deliverables = []
        signals.append("format words name the file being handled, not a deliverable")

    # More than one deliverable, or an explicit chain with a deliverable, is a
    # multi-step task no matter which single kind matched first.
    if len(deliverables) > 1 or (chained and deliverables):
        signals.append(f"{len(deliverables)} deliverable(s) requested"
                       if len(deliverables) > 1 else "chained request")
        return Classification(kind=TaskKind.MULTI_STEP,
                              complexity=Complexity.HARD if len(deliverables) > 2
                              else Complexity.MODERATE,
                              signals=signals, deliverables=deliverables)

    if kind is None and use_model_fallback:
        kind = _model_classify(lowered)
        if kind:
            signals.append(f"small classifier model -> {kind.value}")

    if kind is None:
        kind = TaskKind.CHAT
        signals.append("no specific signal - treating as conversation")
        confident = False
    else:
        confident = True

    return Classification(kind=kind, complexity=estimate_complexity(lowered, kind),
                          signals=signals, deliverables=deliverables,
                          confident=confident)


def _model_classify(text: str) -> Optional[TaskKind]:
    """Last resort, and cheap by design: the smallest pulled model, one word
    of output, no retry. Returns None on any doubt rather than guessing."""
    try:
        from core.ollama_client import ollama_client
        from core.orchestration.router import capability_router
        if not ollama_client.is_available():
            return None
        model = capability_router.route_slot("classify")
        if not model:
            return None
        options = ", ".join(k.value for k in (
            TaskKind.CHAT, TaskKind.REASONING, TaskKind.CODE, TaskKind.RAG,
            TaskKind.INDUSTRIAL_DATA, TaskKind.FILE_OPS, TaskKind.AUTOMATION))
        reply = ollama_client.chat(model, [
            {"role": "system", "content":
                f"Classify the request into exactly one of: {options}. "
                f"Reply with the single word only."},
            {"role": "user", "content": text[:500]},
        ], retries=0, timeout=15.0, task_type="classify",
            purpose="Task classification")
        answer = (reply or "").strip().lower().split()[:1]
        if not answer:
            return None
        for candidate in TaskKind:
            if candidate.value == answer[0]:
                return candidate
    except Exception as error:
        logger.debug(f"model classification skipped: {error}")
    return None
