"""
ZEUS Memory Manager
Sits above the raw database and provides what the decision engine needs:
recent conversational context (bounded, not the whole history) and durable
memory - "remember I prefer dark mode", "we decided the report uses Q3 data".

Durable memory lives in the Hermes-backed store (memory/hermes_store.py),
which is what the orchestrator RECALLS from. The older `facts` table is kept
in step as a mirror, because tools, the UI and plant workflows read it.

Fixes carried here (2026-09-25):
  * facts were keyed `note_{abs(hash(text)) % 100000}` - Python's str hash is
    randomised per process, so the same fact got a new key every run
    (duplicates that `forget` only half-removed). Keys are now a stable hash.
  * every durable write goes through memory/write_policy.py: credentials and
    secrets are refused, and a changed fact replaces the old version.
  * the conversation context no longer feeds ZEUS's own failure placeholders
    ("[ZEUS could not reach the local AI model ...]") back to the model as
    things it said.
"""

import hashlib
import re
from typing import Dict, List, Optional

from core.logger import get_logger
from memory import database

logger = get_logger("memory")

_REMEMBER_PATTERN = re.compile(r"remember (?:that )?(.+)", re.IGNORECASE)
_DECISION_PATTERN = re.compile(r"^\s*(?:we|the team|management)\s+(?:have\s+)?(?:decided|agreed|"
                               r"chose|approved|settled on)\b", re.IGNORECASE)
_FAILURE_MARKERS = ("[ZEUS could not reach", "[ZEUS could not reach", "[ZEUS model failure",
                    "[Model '", "[Vision model '", "[Could not reach")


def stable_fact_key(text: str) -> str:
    """Same text -> same key, in every process (unlike hash())."""
    digest = hashlib.sha1((text or "").strip().lower().encode("utf-8")).hexdigest()[:12]
    return f"note_{digest}"


def remember_text(text: str, source: str = "user", explicit: bool = True,
                  category: Optional[str] = None) -> Dict:
    """Store one durable memory (policy-checked) and mirror it to `facts`.

    Returns {"stored": bool, "text": ..., "reason": ..., "category": ...,
    "memory_id": ..., "replaced": [...]}."""
    from memory.write_policy import evaluate
    decision = evaluate(text, explicit=explicit, category_hint=category)
    if not decision.allowed:
        return {"stored": False, "text": decision.text, "reason": decision.reason}
    stored, replaced, memory_id, backend_error = None, [], "", ""
    try:
        from memory.hermes_store import get_service
        stored, decision, replaced = get_service().remember(text, explicit=explicit, source=source,
                                                            category=category)
        memory_id = stored.memory_id if stored is not None else ""
    except Exception as error:
        # The mirror below still records it; recall is what is degraded.
        backend_error = str(error)
        logger.error(f"memory store unavailable, fact kept in the facts table only: {error}")
    # Mirror: the facts table readers see the current version only.
    try:
        for old in replaced:
            for key, value in database.get_all_facts().items():
                if value and value.strip().lower() == old.text.strip().lower():
                    database.set_fact(key, "")
        database.set_fact(stable_fact_key(decision.text), decision.text)
    except Exception as error:
        logger.warning(f"facts mirror not updated: {error}")
    return {"stored": True, "text": decision.text, "reason": decision.reason,
            "category": decision.category, "memory_id": memory_id,
            "replaced": [r.text for r in replaced], "backend_error": backend_error}


def forget_text(needle: str) -> List[str]:
    """Remove every memory containing `needle`, from both stores."""
    needle = (needle or "").strip().rstrip(".").lower()
    removed: List[str] = []
    if not needle:
        return removed
    try:
        from memory.hermes_store import get_service
        removed += get_service().forget_matching(needle)
    except Exception as error:
        logger.warning(f"memory store forget skipped: {error}")
    for key, value in database.get_all_facts().items():
        if value and needle in value.lower():
            database.set_fact(key, "")          # the soft delete this table has always used
            if value not in removed:
                removed.append(value)
    return removed


def all_memory_texts() -> List[str]:
    """Everything remembered: the memory store first, then any legacy fact
    not (yet) in it."""
    texts: List[str] = []
    try:
        from memory.hermes_store import get_service
        texts = [m.text for m in get_service().list(limit=500)]
    except Exception as error:
        logger.warning(f"memory store list skipped: {error}")
    known = {t.strip().lower() for t in texts}
    for value in database.get_all_facts().values():
        if value and value.strip().lower() not in known:
            texts.append(value)
            known.add(value.strip().lower())
    return texts


class MemoryManager:
    def __init__(self):
        database.init_db()

    # ------------------------------------------------------------- convo --

    def add_message(self, role: str, content: str):
        database.add_message(role, content)

        if role == "user":
            text = content.strip()
            match = _REMEMBER_PATTERN.match(text)
            if match:
                outcome = remember_text(match.group(1).strip(), source="user", explicit=True)
                if outcome.get("stored"):
                    logger.info(f"Remembered: {outcome['text']} ({outcome['reason']})")
                else:
                    logger.info(f"Not remembered: {outcome['reason']}")
            elif _DECISION_PATTERN.match(text):
                # A decision stated in conversation is durable project memory
                # even without "remember" - it is exactly what gets asked
                # about later ("what did we decide about ...").
                remember_text(text, source="conversation", explicit=False)

    def get_context(self, limit: int = 8) -> List[Dict[str, str]]:
        """The last `limit` messages for an LLM chat call - without ZEUS's own
        failure placeholders, which were never things it said."""
        recent = database.get_recent_messages(limit + 4)
        messages = [{"role": role, "content": content} for role, content in recent
                    if not (role == "assistant" and content.strip().startswith(_FAILURE_MARKERS))]
        return messages[-limit:]

    def search(self, query: str, limit: int = 10):
        return database.search_messages(query, limit)

    # -------------------------------------------------------------- facts --

    def remember(self, key: str, value: str):
        database.set_fact(key, value)

    def recall(self, key: str) -> Optional[str]:
        return database.get_fact(key)

    def all_facts(self) -> dict:
        return database.get_all_facts()

    def forget(self, key: str):
        # Overwrite with empty marker rather than a hard delete API for now.
        database.set_fact(key, "")
