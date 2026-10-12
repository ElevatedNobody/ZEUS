"""
What ZEUS is allowed to remember, and under which category.

Durable information is stored: confirmed decisions, stable preferences,
project and plant facts, task outcomes, verified fixes and environment facts.
Refused, always: passwords, access tokens, API keys, private keys, card
numbers and other credentials - even when the user says "remember". A memory
store is read back into prompts; a secret written here would be handed to a
model on some later, unrelated question.

Refused when captured automatically (not when explicitly asked): greetings,
thanks, questions and other transient chatter.

Also decided here: the SUBJECT of a memory, which is how a changed fact
replaces the old one instead of sitting beside it as a contradiction
("my deadline is Friday" -> "my deadline is Monday").
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Optional

from memory.contracts import (DECISION_MEMORY, INDUSTRIAL_KNOWLEDGE, PROJECT_MEMORY,
                              SYSTEM_FACT, TASK_MEMORY, USER_MEMORY)

# ------------------------------------------------------------- secrets ----

_SECRET_PATTERNS = [
    # Not bare "pass"/"pin": "the shear pin is loose" is plant information.
    ("password", re.compile(r"\b(password|passwd|passcode|passphrase|pwd|pin (?:code|number))\b"
                            r"\s*(is|:|=|was)\s*\S+", re.I)),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("api_key", re.compile(r"\b(sk|pk|rk)-[A-Za-z0-9_\-]{16,}")),
    ("api_key", re.compile(r"\b(AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("api_key", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("api_key", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}\b")),
    ("token", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")),
    # The value must look like a credential (a digit in it, or long), so
    # "the secret is consistency" is not refused.
    ("token", re.compile(r"\b(bearer|token|secret|api[_ -]?key|access[_ -]?key|client[_ -]?secret)\b"
                         r"\s*(is|:|=)\s*(?:(?=\S*\d)\S{8,}|\S{16,})", re.I)),
    ("credential", re.compile(r"\b(username|user id|login)\b.{0,30}\b(password|pwd)\b", re.I)),
]
_CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")
_LONG_RANDOM = re.compile(r"\b[A-Za-z0-9+/_\-]{32,}\b")


def _luhn(digits: str) -> bool:
    total, parity = 0, len(digits) % 2
    for index, char in enumerate(digits):
        value = int(char)
        if index % 2 == parity:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def secret_kind(text: str) -> Optional[str]:
    """The kind of secret in `text`, or None."""
    for kind, pattern in _SECRET_PATTERNS:
        if pattern.search(text or ""):
            return kind
    for match in _CARD.finditer(text or ""):
        digits = re.sub(r"\D", "", match.group(0))
        if 13 <= len(digits) <= 19 and _luhn(digits):
            return "card_number"
    for match in _LONG_RANDOM.finditer(text or ""):
        token = match.group(0)
        classes = sum(bool(re.search(p, token)) for p in (r"[a-z]", r"[A-Z]", r"\d"))
        if classes == 3 and not re.fullmatch(r"[0-9a-f]{32,64}", token.lower()):
            return "high_entropy_token"
    return None


# ----------------------------------------------------------- transient ----

_TRANSIENT = re.compile(
    r"^(hi|hello|hey|thanks|thank you|ok|okay|cool|nice|great|good (morning|evening|night)|"
    r"bye|goodbye|yes|no|sure|hmm+|lol)\b[\s!.]*$", re.I)
_LEAD_INS = re.compile(r"^(please\s+)?(remember|note|keep in mind|don'?t forget|fyi|for the record)"
                       r"(\s+that)?[:,]?\s+", re.I)


def clean(text: str) -> str:
    stripped = _LEAD_INS.sub("", (text or "").strip())
    return re.sub(r"\s+", " ", stripped).strip().rstrip(".")


# ----------------------------------------------------------- categories ---

_EQUIPMENT_TAG = re.compile(r"\b[A-Z]{1,4}-?\d{2,5}[A-Z]?\b")
_CATEGORY_RULES = [
    (DECISION_MEMORY, re.compile(r"\b(we|i|the team|management)\s+(have\s+)?(decided|agreed|chose|approved|"
                                 r"settled on|are going with|will go with)\b|\bdecision\b", re.I)),
    (TASK_MEMORY, re.compile(r"\b(todo|to-do|task|action item|follow[ -]up|need to|must|deadline|due)\b", re.I)),
    (INDUSTRIAL_KNOWLEDGE, re.compile(
        r"\b(pump|compressor|boiler|turbine|valve|heat exchanger|exchanger|reactor|column|vessel|"
        r"pipeline|flare|refinery|plant|unit|psv|prv|pressure|temperature|flow|vibration|bearing|"
        r"impeller|seal|corrosion|inspection|hazop|loto|permit)\b", re.I)),
    (SYSTEM_FACT, re.compile(r"\b(gpu|vram|ram|cpu|python|ollama|driver|windows|installed|version|"
                             r"firewall|model)\b", re.I)),
    (PROJECT_MEMORY, re.compile(r"\b(project|report|milestone|client|proposal|presentation|budget|"
                                r"meeting|submission|hackathon|document)\b", re.I)),
    (USER_MEMORY, re.compile(r"\b(i|my|me|i'm|i am)\b", re.I)),
]


_PERSONAL = re.compile(r"^(my (favou?rite|preferred|name)\b|i (really )?(prefer|like|love|hate|dislike)\b|"
                       r"call me\b|i am called\b|i'm called\b)", re.I)


def categorize(text: str, hint: Optional[str] = None) -> str:
    if hint:
        return hint
    # A preference is about the PERSON, whatever it mentions ("my favourite
    # report format" is not project memory because it says "report").
    if _PERSONAL.match(clean(text)):
        return USER_MEMORY
    for category, pattern in _CATEGORY_RULES:
        if pattern.search(text):
            if category == INDUSTRIAL_KNOWLEDGE or _EQUIPMENT_TAG.search(text) is None:
                return category
            return INDUSTRIAL_KNOWLEDGE
    return INDUSTRIAL_KNOWLEDGE if _EQUIPMENT_TAG.search(text) else USER_MEMORY


# -------------------------------------------------------------- subject ---

_ATTR = re.compile(r"^(?:my|our|the|this|his|her|their)\s+(?P<subject>[a-z0-9' \-]{2,48}?)\s+"
                   r"(?:is|are|was|were|=|will be|has been|should be)\b", re.I)
_PREF = re.compile(r"^i\s+(?:really\s+)?(?:prefer|like|want|use|love|hate|dislike)\s+(?P<object>.+)$", re.I)
_NAME = re.compile(r"^(?:my name is|call me|i am called|i'm called)\b", re.I)
_STOP = {"a", "an", "the", "to", "of", "for", "in", "on", "and", "or", "with", "my", "our"}


def subject_key(text: str) -> str:
    """A short key for WHAT the memory is about, so a newer version of the
    same fact supersedes the older one. Empty when no subject is clear - then
    only near-identical text is treated as the same memory."""
    t = clean(text).lower()
    if _NAME.match(t):
        return "attr:name"
    match = _ATTR.match(t)
    if match:
        words = [w for w in match.group("subject").split() if w not in _STOP]
        return "attr:" + " ".join(words) if words else ""
    match = _PREF.match(t)
    if match:
        words = [w for w in re.findall(r"[a-z0-9]+", match.group("object")) if w not in _STOP]
        return f"pref:{words[-1]}" if words else ""
    return ""


# ------------------------------------------------------------- decision ---

@dataclass
class WriteDecision:
    allowed: bool
    reason: str
    text: str = ""
    category: str = ""
    subject: str = ""
    flags: Dict[str, object] = field(default_factory=dict)


def evaluate(text: str, explicit: bool, category_hint: Optional[str] = None) -> WriteDecision:
    """Should `text` be stored? `explicit` = the user asked ("remember ...")."""
    cleaned = clean(text)
    if not cleaned:
        return WriteDecision(False, "nothing to remember")
    kind = secret_kind(cleaned) or secret_kind(text)
    if kind:
        return WriteDecision(False, f"refused: looks like a {kind.replace('_', ' ')} - "
                                    f"ZEUS never stores credentials or secrets in memory")
    if not explicit:
        if _TRANSIENT.match(cleaned) or cleaned.endswith("?") or len(cleaned.split()) < 4:
            return WriteDecision(False, "transient - not durable information")
    if len(cleaned) > 2000:
        cleaned = cleaned[:2000]
    flags: Dict[str, object] = {}
    try:
        from security.injection import scan
        result = scan(cleaned)
        if result.flagged:
            # Stored (it is information the user gave), but marked: the context
            # builder quotes it as untrusted and it can never act as an order.
            flags["instruction_like"] = result.kinds
    except Exception:
        pass
    category = categorize(cleaned, category_hint)
    return WriteDecision(True, "durable", cleaned, category, subject_key(cleaned), flags)
