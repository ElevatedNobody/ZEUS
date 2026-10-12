"""
Source authority and document typing.

Authority ladder (1 = most authoritative):

    1  approved SOP / approved policy
    2  official audited report (annual report, audited financial statements, audit report)
    3  official document (manual, specification, datasheet, inspection report, contract, title record)
    4  internal report (maintenance / production / MIS / project report, spreadsheet)
    5  correspondence (letter, e-mail, memo, minutes)
    6  draft
    7  unverified (unknown provenance, low-confidence OCR, AI inference)

A draft or an e-mail must never quietly outrank an approved procedure or an
audited figure: retrieval weights by this rank and contradictions are resolved
towards it (knowledge_fabric/retrieval.py, contradictions.py).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, Optional

LADDER = {1: "approved SOP / policy", 2: "official audited report", 3: "official document (engineering / legal)",
          4: "internal report", 5: "correspondence", 6: "draft", 7: "unverified"}

# weight used in ranking: 1.0 for rank 1 ... 0.25 for rank 7
WEIGHT = {1: 1.0, 2: 0.92, 3: 0.84, 4: 0.7, 5: 0.52, 6: 0.36, 7: 0.25}

DOCUMENT_TYPES = ("annual_report", "financial_record", "maintenance_report", "inspection_report",
                  "sop", "policy", "manual", "engineering_document", "project_record", "correspondence",
                  "production_record", "operational_record", "safety_record", "incident_report",
                  "presentation", "spreadsheet", "scanned_document", "image", "handwritten_document",
                  "transcript", "audit_report", "meeting_minutes", "general_knowledge", "contract",
                  "legal_record", "register", "records_pack", "other")

_RULES = [
    ("annual_report", r"\bannual\s+report\b|\bdirectors'?\s+report\b|\breport\s+of\s+the\s+board\b"),
    ("audit_report", r"\bauditors?'?\s+report\b|\binternal\s+audit\s+report\b|\bstatutory\s+audit\b"),
    ("financial_record", r"\bprofit\s+(?:and|&)\s+loss\b|\bincome\s+statement\b|\bbalance\s+sheet\b|"
                         r"\bfinancial\s+(?:statements?|results|highlights)\b|\bcash\s+flow\s+statement\b|\bbudget\b"),
    ("incident_report", r"\bincident\s+(?:report|investigation)\b|\bnear[\s-]?miss\b|\broot\s+cause\s+analysis\b"),
    ("safety_record", r"\bsafety\s+(?:record|statistics|performance|audit|brief)\b|\bhse\s+report\b|\bpermit\s+to\s+work\b|"
                      r"\bEHS\b|\bgolden\s+rules\b"),
    ("sop", r"\bstandard\s+operating\s+procedure\b|\bSOP[\s\-]?[A-Z]{0,4}[\s\-]?\d|\bSOP(?:-[A-Z0-9]+)+|\bSOP\b"),
    ("contract", r"\blease\s+agreement\b|\b(?:master\s+)?service\s+agreement\b|\bstatement\s+of\s+work\b|"
                 r"\bcontract\s+(?:type|id|no|value)\b|\bindemnif\w+\b|\bgoverning\s+law\b|\blessor\b|\blessee\b"),
    ("legal_record", r"\btitle\s+(?:deed|chain)\b|\bsale\s+deed\b|\bencumbrance\s+certificate\b|\bregistration\s+no\b"),
    ("presentation", r"^\s*#*\s*slide\s+deck\b|^\s*#*\s*slide\s+\d+\b"),
    ("policy", r"\bpolicy\s+(?:document|statement)\b|^\s*[A-Z][\w ]{2,40}\s+policy\s*$"),
    ("manual", r"\b(?:operating|maintenance|instruction|user)\s+manual\b|\bhandbook\b"),
    ("inspection_report", r"\binspection\s+report\b|\bthickness\s+survey\b|\bNDT\s+report\b|\bRBI\s+assessment\b"),
    ("maintenance_report", r"\bmaintenance\s+(?:report|record|history|log)\b|\bwork\s+order\b|\bbreakdown\s+report\b|\bturnaround\s+report\b"),
    ("production_record", r"\bproduction\s+(?:report|record|summary)\b|\bdaily\s+production\b|\bthroughput\s+report\b|\bmonthly\s+production\b"),
    ("operational_record", r"\bshift\s+log\b|\boperations?\s+(?:log|report)\b|\blog\s*book\b"),
    ("project_record", r"\bproject\s+(?:report|status|charter|plan|closure)\b|\bdetailed\s+project\s+report\b|\bmilestone\b"),
    ("meeting_minutes", r"\b(?:board|committee|management)\s+minutes\b|\bminutes\s+of\s+(?:the\s+)?meeting\b|\bMOM\b"),
    ("engineering_document", r"\bdata\s*sheet\b|\bdesign\s+basis\b|\bspecification\b|\bengineering\s+standard\b|\bP&ID\b|\bprocess\s+flow\s+diagram\b"),
    ("correspondence", r"^\s*(?:from|to|subject|cc)\s*:|\bdear\s+(?:sir|madam|mr|ms|dr)\b|\byours\s+(?:faithfully|sincerely)\b"),
    ("transcript", r"\btranscript\b|\[\d{1,2}:\d{2}(?::\d{2})?\]"),
]
_COMPILED = [(name, re.compile(pattern, re.IGNORECASE | re.MULTILINE)) for name, pattern in _RULES]

_BASE_RANK = {"sop": 3, "policy": 3, "annual_report": 2, "audit_report": 2, "financial_record": 4,
              "manual": 3, "engineering_document": 3, "inspection_report": 3, "maintenance_report": 4,
              "production_record": 4, "operational_record": 4, "project_record": 4, "incident_report": 4,
              "safety_record": 4, "presentation": 4, "spreadsheet": 4, "meeting_minutes": 5,
              "contract": 3, "legal_record": 3, "register": 4, "records_pack": 3,
              "correspondence": 5, "transcript": 5, "scanned_document": 4, "image": 6,
              "handwritten_document": 6, "general_knowledge": 4, "other": 5}


_TYPE_TOKENS = (r"ANNUAL_REPORT|FINANCIAL_RECORD|PRODUCTION_RECORD|MAINTENANCE_REPORT|INSPECTION_REPORT|"
                r"SAFETY_RECORD|INCIDENT_REPORT|PROJECT_RECORD|AUDIT_REPORT|OPERATIONAL_RECORD|MEETING_MINUTES|"
                r"ENGINEERING_DOCUMENT|SOP")
_DECLARED_TOKEN = re.compile(r"(?<![A-Za-z])(" + _TYPE_TOKENS + r")(?![A-Za-z])")
_TYPE_HEADING = re.compile(r"^\s*#*\s*(" + _TYPE_TOKENS + r")\s*$", re.M)
_DECLARED_FIELD = re.compile(r"\*\*(?:Type|Doc(?:ument)?[_ ]Type)\s*:\*\*\s*([A-Za-z_ ]{2,40}?)\s*(?:\||$)", re.I | re.M)


def declared_type(text: str) -> Optional[str]:
    """A type the document or section states itself: a heading 'FINANCIAL_RECORD',
    a file named 'FINANCIAL_RECORD_1990_2026', a line '**Type:** SOP'."""
    text = (text or "").strip()
    field = _DECLARED_FIELD.search(text)
    if field:
        value = field.group(1).strip().replace(" ", "_").lower()
        if value in DOCUMENT_TYPES:
            return value
    m = _DECLARED_TOKEN.search(text)
    return m.group(1).lower() if m else None


def section_types(text: str) -> List[str]:
    """Headings that are themselves a document type ('## ANNUAL_REPORT', '## SOP')."""
    return sorted({m.group(1).lower() for m in _TYPE_HEADING.finditer(text or "")})


def classify_type(text: str, filename: str = "", file_type: str = "") -> str:
    head = (text or "")[:8000]
    stem = Path(filename or "").stem
    # A records pack: one file holding sections of several declared types.
    if len(section_types(head)) >= 3:
        return "records_pack"
    # Declared by the file name, the title line or a '**Type:**' field - a type merely
    # MENTIONED in the text ('all types present: ANNUAL_REPORT, ...') declares nothing.
    title_line = next((line for line in head.splitlines() if line.strip()), "")
    field = _DECLARED_FIELD.search(head[:1500])
    declared = declared_type(stem) or (declared_type(field.group(0)) if field else None) or declared_type(title_line)
    if declared:
        return declared
    name = stem.replace("_", " ").replace("-", " ")
    scores: Dict[str, int] = {}
    for doc_type, pattern in _COMPILED:
        hits = len(pattern.findall(head)) + 3 * len(pattern.findall(name))
        if hits:
            scores[doc_type] = hits
    if scores:
        return max(scores.items(), key=lambda kv: kv[1])[0]
    if file_type in ("pptx", "ppt"):
        return "presentation"
    if file_type in ("xlsx", "xlsm", "csv"):
        return "spreadsheet"
    if file_type in ("png", "jpeg", "jpg", "tiff", "bmp", "image"):
        return "image"
    return "other"


def detect_status(text: str, filename: str = "") -> str:
    head = (text or "")[:6000]
    low = head.lower()
    name = (filename or "").lower()
    if re.search(r"\bdraft\b", name) or re.search(r"^\s*draft\b|\bdraft\s+(?:copy|version|for\s+review)\b|\bnot\s+for\s+circulation\b", low, re.M):
        return "draft"
    if re.search(r"\bsuperseded\b|\bobsolete\b|\bwithdrawn\b", low):
        return "superseded"
    if re.search(r"\baudited\b|\bindependent\s+auditors?\b", low):
        return "audited"
    if re.search(r"\bapproved\s+by\s*[:\-]|\bapproval\s*[:\-]\s*\w|\bapproved\s+on\b|\bstatus\s*[:\-]\s*approved\b", low):
        return "approved"
    if re.search(r"\bunverified\b|\bunconfirmed\b|\bpreliminary\b", low):
        return "unverified"
    return "issued"


def rank(document_type: str, status: str, origin: str = "company", mean_ocr_confidence: Optional[float] = None) -> int:
    if status == "draft":
        return 6
    if status in ("unverified", "superseded"):
        return 7
    base = _BASE_RANK.get(document_type, 5)
    if document_type in ("sop", "policy") and status == "approved":
        base = 1
    elif document_type in ("annual_report", "financial_record", "audit_report") and status == "audited":
        base = 2
    elif status == "approved" and base > 3:
        base = 3
    if mean_ocr_confidence is not None and mean_ocr_confidence < 0.6:
        base = max(base, 6)
    if origin in ("sample", "demo"):
        base = max(base, 4)
    return base


def weight(rank_value: int) -> float:
    return WEIGHT.get(int(rank_value or 5), 0.5)


def describe(rank_value: int) -> str:
    return LADDER.get(int(rank_value or 5), "unknown")
