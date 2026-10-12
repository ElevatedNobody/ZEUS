"""
ZEUS Planner
Classifies user input into QUESTION / COMMAND / TASK / MULTI_STEP_TASK,
and for multi-step tasks asks the reasoning model to decompose the goal
into an ordered list of tool calls, validated against the tool registry.
"""

import json
import re
from enum import Enum
from typing import List, Optional, TypedDict

from core.logger import get_logger
from core.model_router import model_router
from core.ollama_client import ollama_client
from core.tool_registry import registry

logger = get_logger("planner")

_MULTISTEP_MARKERS = (
    " and then", " then ", " and summarize", " and create", " and save",
    " and report", "find all", "organize", "each ", "summarize and",
)

_COMMAND_VERBS = (
    "open ", "close ", "launch ", "start ", "stop ", "play ", "pause ",
    "take a screenshot", "screenshot", "search for", "delete ", "move ",
    "copy ", "create ", "rename ",
)


class IntentType(str, Enum):
    QUESTION = "QUESTION"
    COMMAND = "COMMAND"
    TASK = "TASK"
    MULTI_STEP_TASK = "MULTI_STEP_TASK"


class PlanStep(TypedDict):
    tool: str
    args: dict
    description: str


def classify_intent(text: str) -> IntentType:
    lowered = text.lower().strip()

    if any(marker in lowered for marker in _MULTISTEP_MARKERS):
        return IntentType.MULTI_STEP_TASK

    if lowered.startswith(("what", "who", "when", "why", "how", "is ", "are ", "can you tell")) and "?" in lowered:
        return IntentType.QUESTION

    if any(lowered.startswith(v) or v in lowered for v in _COMMAND_VERBS):
        return IntentType.COMMAND

    if any(w in lowered for w in ("organize", "clean up", "find my", "generate a report")):
        return IntentType.TASK

    return IntentType.QUESTION


def decompose(goal: str) -> Optional[List[PlanStep]]:
    """
    Ask the reasoning model to break `goal` into tool calls.
    Returns None if the model is unavailable or output can't be parsed -
    callers should fall back to treating it as a plain chat message.
    """
    if not ollama_client.is_available():
        return None

    model = model_router.route("reasoning")
    catalogue = registry.describe_all()

    system_prompt = (
        "You are ZEUS's task planner. Break the user's goal into a short ordered "
        "list of tool calls using ONLY tools from the catalogue below. "
        "Respond with ONLY a JSON array, no prose, no markdown fences. "
        "Each item: {\"tool\": <name>, \"args\": {...}, \"description\": <short human summary>}.\n\n"
        f"AVAILABLE TOOLS:\n{catalogue}"
    )

    raw = ollama_client.chat(model, [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": goal},
    ])

    cleaned = re.sub(r"^```(json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
    try:
        steps = json.loads(cleaned)
    except (ValueError, TypeError):
        logger.warning(f"Planner could not parse model output as JSON: {raw[:200]}")
        return None

    if not isinstance(steps, list):
        return None

    valid_steps: List[PlanStep] = []
    for s in steps:
        if not isinstance(s, dict) or "tool" not in s:
            continue
        if registry.get(s["tool"]) is None:
            logger.warning(f"Planner referenced unknown tool '{s['tool']}' - dropping step")
            continue
        valid_steps.append({
            "tool": s["tool"],
            "args": s.get("args", {}) or {},
            "description": s.get("description", s["tool"]),
        })
    return valid_steps or None
