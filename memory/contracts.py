"""
ZEUS's memory contract - the only shape memory ever has above the store.

The orchestrator, the context builder and the tools see MemoryResult objects,
never a backend's own rows and never text parsed from a CLI. Swapping the
backend (Hermes or ZEUS's native store, config.ini [memory] backend) changes
nothing above this line.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict

USER_MEMORY = "USER_MEMORY"                    # stable preferences, who the user is
PROJECT_MEMORY = "PROJECT_MEMORY"              # facts about the work
TASK_MEMORY = "TASK_MEMORY"                    # outcomes and follow-ups
DECISION_MEMORY = "DECISION_MEMORY"            # confirmed decisions
INDUSTRIAL_KNOWLEDGE = "INDUSTRIAL_KNOWLEDGE"  # plant/equipment facts told to ZEUS
SYSTEM_FACT = "SYSTEM_FACT"                    # verified environment facts
TOOL_RESULT = "TOOL_RESULT"                    # what a tool produced (files, results)

CATEGORIES = (USER_MEMORY, PROJECT_MEMORY, TASK_MEMORY, DECISION_MEMORY,
              INDUSTRIAL_KNOWLEDGE, SYSTEM_FACT, TOOL_RESULT)


@dataclass(frozen=True)
class MemoryResult:
    memory_id: str                  # "<backend>:<id>", stable for update/forget
    text: str
    score: float                    # relevance to the query (0..1); 1.0 for listings
    timestamp: float                # when this version was written
    category: str
    source: str                     # who/what wrote it: user | tool:<name> | migration | system
    metadata: Dict[str, Any] = field(default_factory=dict)
    provenance: Dict[str, Any] = field(default_factory=dict)   # backend, supersedes, first_recorded

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
