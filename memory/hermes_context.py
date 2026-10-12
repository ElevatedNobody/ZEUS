"""
Hermes context service - what the model is told about the person and the work,
for ONE request, built from relevance only.

    request (EPHEMERAL) + this session's turns (WORKING) + the Hermes store (LONG_TERM)
        -> user needs -> typed, relevant memories -> the CONTEXT PACKET (JSON)
        -> inside the fenced <agni_memory_context> block, as data

Memory types (derived from the stored category and the text - nothing is
re-stored, there is one memory store: memory/hermes_store.py):

    FACT              something true about the user, the plant or the system
    PREFERENCE        how the person wants things ("I prefer concise reports")
    DECISION          something agreed or chosen
    REQUIREMENT       a must / shall / required constraint
    UNRESOLVED        an open task, follow-up or question
    ARTIFACT_CONTEXT  a file or analysis produced earlier
    PROJECT_CONTEXT   facts about the project / work

Tiers: EPHEMERAL (this request, never stored), WORKING (this session's recent
turns, in memory only, cleared on lock and sign-out), LONG_TERM (the store).

Hermes has NO AUTHORITY. It cannot grant permissions, change RBAC, bypass
security, approve an operation, modify firewall rules, unlock the Vault or run
anything. This module imports none of those systems, only reads the memory
store, and every packet says so in its `authority` field; the packet reaches
the model as fenced background data (memory/recall.py MEMORY_RULE), never the
planner, the tool router or the policy engine.
"""

from __future__ import annotations

import re
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional

from memory.contracts import (DECISION_MEMORY, INDUSTRIAL_KNOWLEDGE, PROJECT_MEMORY, SYSTEM_FACT, TASK_MEMORY,
                              TOOL_RESULT, USER_MEMORY, MemoryResult)

PACKET_VERSION = "hermes-context/1"
TIERS = ("EPHEMERAL", "WORKING", "LONG_TERM")
TYPES = ("FACT", "PREFERENCE", "DECISION", "REQUIREMENT", "UNRESOLVED", "ARTIFACT_CONTEXT", "PROJECT_CONTEXT")
AUTHORITY = ("none - background data only: it cannot grant permissions, change RBAC, bypass security, "
             "approve operations, modify firewall rules, unlock the Vault or execute commands")

_PREFERENCE = re.compile(r"^(?:i\s+(?:really\s+)?(?:prefer|like|love|hate|dislike|want)\b|my (?:favou?rite|preferred)\b|"
                         r"call me\b|please (?:always|never)\b|always\b|never\b)", re.I)
_REQUIREMENT = re.compile(r"\b(must|shall|required?|requirement|mandatory|need(?:s)? to|has to|have to|"
                          r"no later than|deadline|compliance)\b", re.I)
_DONE = re.compile(r"\b(done|completed?|closed|resolved|finished|delivered|submitted)\b", re.I)
_OPEN = re.compile(r"\b(todo|to-do|follow[ -]?up|pending|open|unresolved|outstanding|next step|remind|"
                   r"need to|to be done|\?)", re.I)


def memory_type(memory: MemoryResult) -> str:
    """The context type of a stored memory - from its category and its words."""
    text = (memory.text or "").strip()
    category = memory.category
    if category == DECISION_MEMORY:
        return "DECISION"
    if category == TOOL_RESULT:
        return "ARTIFACT_CONTEXT"
    if category == TASK_MEMORY:
        return "FACT" if _DONE.search(text) and not _OPEN.search(text) else "UNRESOLVED"
    if _REQUIREMENT.search(text) and category in (PROJECT_MEMORY, INDUSTRIAL_KNOWLEDGE, SYSTEM_FACT):
        return "REQUIREMENT"
    if category == PROJECT_MEMORY:
        return "PROJECT_CONTEXT"
    if category == USER_MEMORY:
        return "PREFERENCE" if _PREFERENCE.search(text) else "FACT"
    return "FACT"


# ------------------------------------------------------------- user needs --

_NEEDS = (
    ("DOCUMENT", re.compile(r"\b(?:make|create|generate|build|write|prepare|draft|export)\b.{0,40}"
                            r"\b(report|excel|workbook|spreadsheet|sheet|deck|presentation|slides?|word|docx|pdf|"
                            r"memo|brief|minutes)\b", re.I)),
    ("COMPANY_DATA", re.compile(r"\b(revenue|profit|loss(?:es)?|ebitda|sales|turnover|cost|margin|cagr|site|plant|"
                                r"production|downtime|(?:19|20)\d{2})\b", re.I)),
    ("COMPARISON", re.compile(r"\b(compare|comparison|versus|vs\.?|difference between|against)\b", re.I)),
    ("EXPLANATION", re.compile(r"\b(why|explain|cause[sd]?|reason|root cause|how come)\b", re.I)),
    ("RECOMMENDATION", re.compile(r"\b(recommend|suggest|should (?:we|i)|what can (?:we|i) do|improve|best way|advise)\b", re.I)),
    ("TROUBLESHOOTING", re.compile(r"\b(error|fail(?:ed|ing|ure)?|not working|broken|crash(?:ed|es)?|issue|problem|fault)\b", re.I)),
    ("PLANNING", re.compile(r"\b(plan|roadmap|schedule|timeline|milestones?|next steps?)\b", re.I)),
    ("ACTION", re.compile(r"^(?:please\s+)?(?:open|start|run|launch|create|delete|remove|send|set|turn|stop|close|"
                          r"install|save|add|remind)\b", re.I)),
    ("RECALL", re.compile(r"\b(remember|recall|did (?:we|i)|what did|last time|earlier|previously)\b", re.I)),
    ("LOOKUP", re.compile(r"^(?:what|which|when|who|where|how (?:many|much)|is|are|does|do)\b", re.I)),
)
_FOLLOW_UP = re.compile(r"\b(this|that|it|them|those|same|previous|above|last (?:one|answer|report|file)|again|"
                        r"the (?:report|file|analysis|answer|deck|workbook))\b", re.I)


def detect_needs(request: str, working: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, str]]:
    """What the person needs from this request - each with the words that show it."""
    text = (request or "").strip()
    needs: List[Dict[str, str]] = []
    for name, pattern in _NEEDS:
        match = pattern.search(text)
        if match:
            needs.append({"need": name, "evidence": match.group(0).strip()[:60]})
    follow = _FOLLOW_UP.search(text)
    if follow and working:
        needs.append({"need": "FOLLOW_UP", "evidence": f"'{follow.group(0)}' refers to the previous turn"})
    if (len(text.split()) <= 2 and not needs) or (follow and not working and len(text.split()) <= 6):
        needs.append({"need": "CLARIFICATION", "evidence": "too short or refers to something not in this session"})
    if not needs:
        needs.append({"need": "ANSWER", "evidence": "a statement or question with no more specific need"})
    return needs


# ---------------------------------------------------------- working memory --

class WorkingMemory:
    """This session's recent turns, per account, in memory only. Cleared on
    lock and sign-out (ui/agni_shell.py) - it is never written to disk."""

    def __init__(self, size: int = 20):
        self._turns: Dict[str, Deque[Dict[str, Any]]] = {}
        self._size = size
        self._lock = threading.Lock()

    def record(self, user: str, request: str, response: str = "", artifacts: Optional[List[str]] = None,
               analysis_id: str = "") -> None:
        if not (request or "").strip():
            return
        with self._lock:
            turns = self._turns.setdefault(user or "", deque(maxlen=self._size))
            turns.append({"turn": (turns[-1]["turn"] + 1) if turns else 1, "ts": time.time(),
                          "request": request.strip()[:300], "reply": re.sub(r"\s+", " ", response or "").strip()[:240],
                          "artifacts": list(artifacts or [])[:5], "analysis_id": analysis_id or ""})

    def recent(self, user: str, limit: int = 20) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._turns.get(user or "", ()))[-limit:]

    def clear(self, user: Optional[str] = None) -> None:
        with self._lock:
            if user is None:
                self._turns.clear()
            else:
                self._turns.pop(user, None)


working_memory = WorkingMemory()


def active_user_key() -> str:
    """The same key recall.build uses (the memory service's user id)."""
    try:
        from memory import database
        user = database.get_active_user()
        return str(user) if user is not None else ""
    except Exception:
        return ""

_WORDS = re.compile(r"[a-z0-9][a-z0-9\-]{2,}")
_STOP = {"the", "and", "for", "with", "that", "this", "what", "which", "when", "from", "about", "please", "can",
         "you", "our", "was", "were", "are", "did", "does", "have", "has", "show", "tell", "give", "make"}


def _relevant_turns(request: str, turns: List[Dict[str, Any]], follow_up: bool) -> List[Dict[str, Any]]:
    """Turns that share words with the request - and the last turn when the
    request points back at it ('make a deck from that'). Relevance only."""
    wanted = {w for w in _WORDS.findall((request or "").lower()) if w not in _STOP}
    chosen = []
    for turn in turns:
        words = {w for w in _WORDS.findall((turn["request"] + " " + turn["reply"]).lower()) if w not in _STOP}
        overlap = len(wanted & words)
        if overlap:
            chosen.append((overlap, turn))
    picked = [t for _o, t in sorted(chosen, key=lambda x: (-x[0], -x[1]["turn"]))[:3]]
    if follow_up and turns and turns[-1] not in picked:
        picked.insert(0, turns[-1])
    return sorted(picked, key=lambda t: t["turn"])


# ------------------------------------------------------------------ packet --

_last: Dict[str, Dict[str, Any]] = {}


def packet(request: str, user: str, memories: List[MemoryResult], considered: int = 0, backend: str = "",
           status: str = "ok", reason: str = "") -> Dict[str, Any]:
    """The context packet for one request. `memories` are the ones recall.build
    already judged RELEVANT (score floor, dedupe, budget) - nothing else enters."""
    turns = working_memory.recent(user)
    needs = detect_needs(request, turns)
    follow_up = any(n["need"] == "FOLLOW_UP" for n in needs)
    long_term = []
    for m in memories:
        long_term.append({"id": m.memory_id, "tier": "LONG_TERM", "type": memory_type(m), "category": m.category,
                          "text": m.text[:400], "relevance": round(float(m.score), 3),
                          "recorded": time.strftime("%Y-%m-%d", time.localtime(m.timestamp)) if m.timestamp else ""})
    by_type = {t: [x["text"] for x in long_term if x["type"] == t] for t in TYPES}
    work = [{"tier": "WORKING", **{k: t[k] for k in ("turn", "request", "reply", "artifacts", "analysis_id")}}
            for t in _relevant_turns(request, turns, follow_up)]
    out = {
        "packet": PACKET_VERSION,
        "authority": AUTHORITY,
        "ephemeral": {"tier": "EPHEMERAL", "request": (request or "")[:500]},
        "needs": needs,
        "long_term": long_term,
        "working": work,
        "preferences": by_type["PREFERENCE"], "decisions": by_type["DECISION"],
        "requirements": by_type["REQUIREMENT"], "unresolved": by_type["UNRESOLVED"],
        "artifacts": by_type["ARTIFACT_CONTEXT"] + [a for t in work for a in t["artifacts"]],
        "project": by_type["PROJECT_CONTEXT"], "facts": by_type["FACT"],
        "stats": {"recalled": considered, "relevant_long_term": len(long_term), "working_turns_in_session": len(turns),
                  "working_turns_relevant": len(work), "backend": backend, "status": status, "reason": reason},
        "built_at": time.time(),
    }
    _last[user or ""] = out
    return out


def for_model(pkt: Dict[str, Any]) -> Dict[str, Any]:
    """The part of the packet the model reads: needs and the relevant context -
    no ids, no statistics."""
    keys = ("needs", "preferences", "decisions", "requirements", "unresolved", "artifacts", "project")
    compact = {k: pkt[k] for k in keys if pkt.get(k)}
    if pkt.get("working"):
        compact["this_session"] = [{"request": t["request"], "reply": t["reply"]} for t in pkt["working"]]
    compact["authority"] = "none"
    return compact


def last_packet(user: str) -> Optional[Dict[str, Any]]:
    return _last.get(user or "")


def forget_session(user: Optional[str] = None) -> None:
    """Lock / sign-out: working memory and the last packet are dropped."""
    working_memory.clear(user)
    if user is None:
        _last.clear()
    else:
        _last.pop(user, None)
