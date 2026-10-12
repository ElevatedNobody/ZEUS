"""
QueryPlanner: a question -> what kind of question it is, when, about what, and
which retrieval and reasoning steps it needs.

Deterministic (keyword and pattern rules): the plan is explainable, repeatable
and cheap, and it is shown to the user as the pipeline that ran. A question can
carry several intents - "Why did profitability decline after 2017, and what
should we do? Put it in Excel." is ROOT_CAUSE + FINANCIAL + TREND +
RECOMMENDATION + DECISION_SUPPORT + ARTIFACT_GENERATION.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from knowledge_fabric import temporal

INTENTS = ("FACT_LOOKUP", "HISTORICAL", "COMPARISON", "TREND", "ROOT_CAUSE", "MULTI_HOP", "NUMERICAL",
           "FINANCIAL", "OPERATIONAL", "DOCUMENT_SEARCH", "POLICY", "PROCEDURE", "RECOMMENDATION",
           "FORECAST", "SCENARIO", "DECISION_SUPPORT", "ARTIFACT_GENERATION", "MULTIMODAL")

_RULES: Dict[str, str] = {
    "HISTORICAL": r"\bhistory\b|\bhistorical\b|\bover the (?:years|decades)\b|\btimeline\b|\bevolution\b|\bsince (?:19|20)\d\d\b|\bfrom (?:19|20)\d\d\b|\bhow has\b|\bchronolog",
    "COMPARISON": r"\bcompar|\bversus\b|\bvs\.?\b|\bdifference between\b|\bagainst\b|\brelative to\b|\bbetter than\b|\bworse than\b|\bwhat changed between\b",
    "TREND": r"\btrend|\bover time\b|\bgrowth\b|\bgrew\b|\bdeclin|\bincreas|\bdecreas|\bfell\b|\bfall\b|\bdrop|\brise\b|\brose\b|\bcagr\b|\byo[y]\b|year[- ]on[- ]year|\btrajector",
    "ROOT_CAUSE": r"\bwhy\b|\broot cause|\bcause[sd]?\b|\breason|\bdriver|\bexplain why|\bexplain what happened|\bwhat led to\b|\bresponsible for\b|\bcontribut",
    "NUMERICAL": r"\bcalculat|\baverage\b|\bmean\b|\bmedian\b|\btotal\b|\bsum\b|\bhow much\b|\bhow many\b|\bpercentage\b|\bstd|\bstandard deviation|\bvariance\b|\bcorrelat|\bcagr\b|\byo[y]\b|\brolling\b|\bhighest\b|\blowest\b|\bmaximum\b|\bminimum\b",
    "FINANCIAL": r"\bprofit|\bloss(?:es)?\b|\brevenue|\bturnover\b|\bcost|\bexpens|\bmargin|\bebitda\b|\bfinancial|\bbudget|\bcapex\b|\bopex\b|\bdebt\b|\bcash\b|\bearning|\bspend|\bgrm\b|\bsavings\b|\binvestment",
    "OPERATIONAL": r"\bproduction\b|\bthroughput\b|\bdowntime\b|\bavailability\b|\butili[sz]ation\b|\boperations?\b|\bshutdown|\bturnaround|\bfailure|\bbreakdown|\bmaintenance\b|\breliability|\bmtbf\b|\bmttr\b|\boutage|\btrip(?:s|ped)?\b|\byield\b|\binspection|\bequipment|\bunit[\s\-]?\d",
    "DOCUMENT_SEARCH": r"\bfind (?:all )?(?:the )?(?:documents?|reports?|files?|records?)\b|\bshow (?:me )?(?:all )?(?:the )?(?:documents?|reports?|files?)\b|\blist (?:the )?(?:documents?|reports?|files?)\b|\bwhich documents\b|\bdocuments (?:related|about|on|mentioning)\b|\bwhere is\b.*\bdocument",
    "POLICY": r"\bpolic(?:y|ies)\b|\bguideline|\bcompliance\b|\bregulat|\bstandard(?:s)? require",
    "PROCEDURE": r"\bsop\b|\bprocedure|\bsteps? (?:to|for)\b|\bhow (?:do|should|to) (?:we |i )?(?:perform|carry out|start|stop|isolate|operate)\b|\bwork instruction|\bchecklist|\brevision",
    "RECOMMENDATION": r"\bwhat (?:can|should|could) we do\b|\brecommend|\bsuggest|\bhow (?:can|could|do) we (?:improve|reduce|fix|recover|increase)\b|\bsolution|\bresolution|\bremed|\bwhat to do\b|\bimprove\b|\baction plan\b|\bimplementable\b",
    "FORECAST": r"\bforecast|\bpredict|\bprojection|\bproject(?:ed)? (?:to|for)\b|\bnext (?:year|quarter|five years)\b|\bexpected (?:in|by)\b|\bwill (?:be|reach)\b",
    "SCENARIO": r"\bwhat if\b|\bscenario|\bif we (?:reduce|increase|cut|raise|invest|change)\b|\bsensitivity\b|\bsuppose\b|\bassum(?:e|ing) (?:that )?(?:we|the)\b",
    "DECISION_SUPPORT": r"\bshould we\b|\bdecid|\bdecision|\boptions?\b|\btrade-?offs?\b|\bprioriti|\bimplementable\b|\bwhich (?:option|approach)\b|\bgo or no-?go\b|\bbest course\b",
    "ARTIFACT_GENERATION": r"\b(?:create|make|generate|prepare|build|draft|produce|give me|export)\b.{0,40}\b(?:excel|xlsx|workbook|spreadsheet|word|docx|document|report|powerpoint|pptx|presentation|deck|slides?|pdf|brief|action plan|implementation plan)\b|\b(?:excel|word|powerpoint|pdf) (?:file|version|export)\b",
    "MULTIMODAL": r"\bimage|\bphoto|\bpicture|\bscan(?:ned)?\b|\bdrawing|\bdiagram|\bchart|\bfigure|\bhandwrit|\bscreenshot|\bp&id\b",
}
_COMPILED = {intent: re.compile(pattern, re.IGNORECASE) for intent, pattern in _RULES.items()}
_QUESTION_START = re.compile(r"^\s*(what|who|when|where|which|is|are|was|were|does|did|do|how)\b", re.IGNORECASE)
_MULTI_HOP = re.compile(r"\b(?:that|which|whose|who)\b.{3,60}\b(?:failed|supplied|maintained|inspected|caused|affected|operated|located)\b|"
                        r"\b(?:suppliers?|vendors?|units?|equipment|projects?)\s+(?:of|for|behind|linked to|related to)\s+(?:the\s+)?(?:equipment|units?|projects?|incidents?|failures?)\b",
                        re.IGNORECASE)

FORMATS = {"xlsx": r"\bexcel\b|\bxlsx\b|\bworkbook\b|\bspreadsheet\b",
           "pptx": r"\bpowerpoint\b|\bpptx\b|\bpresentation\b|\bdeck\b|\bslides?\b",
           "docx": r"\bword\b|\bdocx\b|\breport\b|\bdocument\b",
           "pdf": r"\bpdf\b",
           "brief": r"\bexecutive brief\b|\bbrief(?:ing)?\b|\bone[- ]pager\b",
           "action_plan": r"\baction plan\b|\bimplementation plan\b"}


@dataclass
class Plan:
    question: str
    intents: List[str]
    primary: str
    complexity: str                         # simple | complex
    scope: Dict[str, Any]
    entities: List[str] = field(default_factory=list)
    metrics: List[str] = field(default_factory=list)
    formats: List[str] = field(default_factory=list)
    sources: List[str] = field(default_factory=list)       # which retrieval legs to run
    steps: List[str] = field(default_factory=list)         # the pipeline, in order
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


_METRIC_WORDS = {
    "profit": "net profit", "profitability": "net profit", "net profit": "net profit", "loss": "net profit",
    "losses": "net profit", "pat": "net profit", "revenue": "revenue", "turnover": "revenue", "sales": "revenue",
    "ebitda": "ebitda", "margin": "ebitda margin", "grm": "grm", "refining margin": "grm",
    "maintenance cost": "maintenance cost", "maintenance costs": "maintenance cost",
    "maintenance expenditure": "maintenance cost", "operating cost": "operating cost", "opex": "operating cost",
    "capex": "capital expenditure", "production": "production", "throughput": "crude throughput",
    "downtime": "downtime", "availability": "availability", "energy": "energy intensity index",
    "crude cost": "crude cost", "interest": "interest cost", "debt": "debt",
    "capacity utilisation": "capacity utilisation", "capacity utilization": "capacity utilisation",
}


def metrics_in(question: str) -> List[str]:
    low = question.lower()
    found = []
    for phrase in sorted(_METRIC_WORDS, key=len, reverse=True):
        if re.search(rf"\b{re.escape(phrase)}\b", low):
            metric = _METRIC_WORDS[phrase]
            if metric not in found:
                found.append(metric)
            low = re.sub(rf"\b{re.escape(phrase)}\b", " ", low)
    return found


def plan(question: str, latest_year: Optional[int] = None, earliest_year: Optional[int] = None,
         entity_ids: Optional[List[str]] = None) -> Plan:
    text = question or ""
    intents = [intent for intent, pattern in _COMPILED.items() if pattern.search(text)]
    if _MULTI_HOP.search(text):
        intents.append("MULTI_HOP")
    scope = temporal.parse(text, latest_year=latest_year, earliest_year=earliest_year)
    if scope.relation not in ("none",) and "HISTORICAL" not in intents and len(scope.years()) > 5:
        intents.append("HISTORICAL")
    if scope.measures and "TREND" not in intents and ("trend" in scope.measures or "yoy" in scope.measures or "cagr" in scope.measures):
        intents.append("TREND")
    if not intents or (_QUESTION_START.search(text) and not set(intents) - {"FINANCIAL", "OPERATIONAL", "MULTIMODAL"}):
        intents.append("FACT_LOOKUP")
    ordered = [i for i in INTENTS if i in intents]

    priority = ("DECISION_SUPPORT", "RECOMMENDATION", "ROOT_CAUSE", "SCENARIO", "FORECAST", "COMPARISON",
                "TREND", "MULTI_HOP", "HISTORICAL", "NUMERICAL", "PROCEDURE", "POLICY", "DOCUMENT_SEARCH",
                "FINANCIAL", "OPERATIONAL", "MULTIMODAL", "ARTIFACT_GENERATION", "FACT_LOOKUP")
    primary = next(i for i in priority if i in ordered)
    simple = set(ordered) <= {"FACT_LOOKUP", "DOCUMENT_SEARCH", "POLICY", "PROCEDURE", "MULTIMODAL",
                              "OPERATIONAL", "FINANCIAL"} and not scope.measures
    formats = [fmt for fmt, pattern in FORMATS.items() if re.search(pattern, text, re.IGNORECASE)] \
        if "ARTIFACT_GENERATION" in ordered else []
    if "docx" in formats and re.search(r"\breport\b", text, re.I) and not re.search(r"\bword\b|\bdocx\b", text, re.I) \
            and ("xlsx" in formats or "pptx" in formats):
        formats.remove("docx")          # "an Excel report" is an Excel file

    sources = ["fulltext", "vector", "entities"]
    if scope.years() or "HISTORICAL" in ordered or "TREND" in ordered:
        sources.append("temporal")
    if ordered and set(ordered) & {"NUMERICAL", "FINANCIAL", "OPERATIONAL", "TREND", "COMPARISON", "ROOT_CAUSE",
                                   "FORECAST", "SCENARIO", "DECISION_SUPPORT", "RECOMMENDATION"}:
        sources.append("structured")
    if set(ordered) & {"ROOT_CAUSE", "MULTI_HOP", "RECOMMENDATION", "DECISION_SUPPORT", "HISTORICAL"}:
        sources.append("graph")
    sources.append("legacy")

    if simple:
        steps = ["classify", "retrieve", "answer"]
    else:
        steps = ["classify", "plan", "retrieve", "rerank"]
        if "structured" in sources:
            steps.append("analyze")
        steps.append("reason")
        steps.append("validate")
        if set(ordered) & {"RECOMMENDATION", "DECISION_SUPPORT", "ROOT_CAUSE"}:
            steps.append("recommend")
        if formats:
            steps.append("generate_artifact")
            steps.append("verify")
        steps.append("deliver")

    notes = list(scope.assumptions)
    if scope.relation == "none" and set(ordered) & {"TREND", "HISTORICAL", "COMPARISON"}:
        notes.append("no period named - every year the records cover is used")
    return Plan(question=text, intents=ordered, primary=primary,
                complexity="simple" if simple else "complex", scope=scope.to_dict(),
                entities=list(entity_ids or []), metrics=metrics_in(text), formats=formats,
                sources=sources, steps=steps, notes=notes)
