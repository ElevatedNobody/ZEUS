"""
Phase G - industrial document schemas.

Loads config/industrial/document_schemas.json and provides three things:

  classify_document()  which of the 14 industrial document types this file is
                       (deterministic keyword + filename scoring; an optional
                       local model tie-break is available but never required)
  extract_fields()     schema-driven field extraction WITH evidence - every
                       extracted value carries the page, section and element
                       it came from, plus the method used
  validate_fields()    schema validation rules, reported as findings rather
                       than silently "fixed"

Nothing here invents a value. A field that cannot be found stays absent, and
a validation rule that cannot be evaluated locally is reported as
'not_evaluated' instead of passing quietly.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.logger import get_logger
from industrial import SCHEMAS_PATH

logger = get_logger("industrial.schemas")

_LOCK = threading.Lock()
_REGISTRY: Optional["SchemaRegistry"] = None

_UNIT_RE = re.compile(r"(?i)\b(mm|cm|m|in|inch|bar|barg|kg/cm2|kpa|mpa|psi|psig|deg\s*c|°c|°f|"
                      r"c|f|hrs?|hours?|days?|months?|years?|mm/s|micron|%|rpm|kw|hp)\b")
_NUMBER_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")


@dataclass
class FieldValue:
    name: str
    value: Any
    method: str                      # regex|table|assets|taxonomy|fallback|numbered_list
    page_number: Optional[int] = None
    section: Optional[str] = None
    source_location: Optional[str] = None
    extraction_method: Optional[str] = None
    ocr_confidence: Optional[float] = None
    pattern: Optional[str] = None

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class SchemaMatch:
    schema_id: str
    name: str
    score: float
    confidence: str                  # high|medium|low|fallback
    matched_keywords: List[str] = field(default_factory=list)
    filename_hits: List[str] = field(default_factory=list)
    runner_up: Optional[str] = None
    method: str = "deterministic_keyword_scoring"

    def to_dict(self) -> dict:
        return dict(self.__dict__)


class SchemaRegistry:
    def __init__(self, data: dict):
        self.data = data
        self.version = data.get("version", "0")
        self.common = data.get("common", {})
        self.schemas: Dict[str, dict] = data.get("schemas", {})
        self.fallback = data.get("fallback_schema", {"id": "generic_document",
                                                     "name": "Generic Industrial Document",
                                                     "extraction_fields": [], "validation": []})
        self.generic_patterns = {
            k: [re.compile(p) for p in v]
            for k, v in self.common.get("generic_patterns", {}).items()
        }

    # ------------------------------------------------------------- catalogue

    def list_schemas(self) -> List[dict]:
        out = [{"id": sid, "name": s.get("name", sid), "description": s.get("description", ""),
                "expected_fields": s.get("expected_fields", []),
                "validation_rules": len(s.get("validation", []))}
               for sid, s in self.schemas.items()]
        out.append({"id": self.fallback.get("id", "generic_document"),
                    "name": self.fallback.get("name", "Generic"),
                    "description": self.fallback.get("description", ""),
                    "expected_fields": self.fallback.get("expected_fields", []),
                    "validation_rules": 0})
        return out

    def get(self, schema_id: str) -> dict:
        return self.schemas.get(schema_id) or self.fallback

    # -------------------------------------------------------- classification

    def classify(self, text: str, filename: str = "") -> SchemaMatch:
        lowered = (text or "").lower()[:20000]
        fname = (filename or "").lower()
        scored: List[tuple] = []

        for sid, schema in self.schemas.items():
            spec = schema.get("classification", {})
            hits, fhits = [], []
            score = 0.0
            for kw in spec.get("keywords", []):
                if kw.lower() in lowered:
                    hits.append(kw)
                    # a keyword in the first 1500 characters (title block /
                    # header area) is worth more than one buried on page 30
                    score += 2.0 if kw.lower() in lowered[:1500] else 1.0
            for hint in spec.get("filename_hints", []):
                if hint.lower() in fname:
                    fhits.append(hint)
                    score += 3.0
            if score:
                scored.append((score, sid, hits, fhits, spec.get("min_score", 2)))

        if not scored:
            return SchemaMatch(schema_id=self.fallback.get("id", "generic_document"),
                               name=self.fallback.get("name", "Generic Industrial Document"),
                               score=0.0, confidence="fallback")

        scored.sort(key=lambda x: x[0], reverse=True)
        score, sid, hits, fhits, min_score = scored[0]
        runner_up = scored[1][1] if len(scored) > 1 else None

        if score < min_score:
            return SchemaMatch(schema_id=self.fallback.get("id", "generic_document"),
                               name=self.fallback.get("name", "Generic Industrial Document"),
                               score=score, confidence="fallback",
                               matched_keywords=hits, filename_hits=fhits, runner_up=sid)

        gap = score - (scored[1][0] if len(scored) > 1 else 0.0)
        confidence = "high" if (score >= 8 and gap >= 3) else ("medium" if score >= 4 else "low")
        return SchemaMatch(schema_id=sid, name=self.schemas[sid].get("name", sid), score=score,
                           confidence=confidence, matched_keywords=hits[:12],
                           filename_hits=fhits, runner_up=runner_up)

    # ------------------------------------------------------------ extraction

    def extract(self, doc, schema_id: str, norm=None) -> Dict[str, Any]:
        """Run a schema's extraction fields against an ExtractedDocument.
        Returns {'schema_id', 'fields': {name: FieldValue-dict}, 'missing',
        'validation': [...]}"""
        schema = self.get(schema_id)
        fields: Dict[str, dict] = {}

        text_elements = [e for e in getattr(doc, "elements", []) if e.text and not e.is_inference]
        table_elements = [e for e in text_elements if e.element_type == "table"]
        full_text = "\n".join(e.text for e in text_elements)

        for spec in schema.get("extraction_fields", []):
            name = spec["name"]
            ftype = spec.get("type", "text")
            value: Optional[FieldValue] = None

            if ftype == "table" or spec.get("source") == "tables":
                if table_elements:
                    picked = table_elements[:5]
                    value = FieldValue(
                        name=name,
                        value=[{"page": t.page_number, "text": t.text[:4000],
                                "rows": t.extra.get("rows"), "columns": t.extra.get("columns")}
                               for t in picked],
                        method="table", page_number=picked[0].page_number,
                        section=picked[0].section, source_location=picked[0].source_location,
                        extraction_method=picked[0].extraction_method)
            elif spec.get("source") == "assets":
                assets = getattr(doc, "assets", [])
                if assets:
                    value = FieldValue(name=name,
                                       value=[{"path": a.path, "page": a.page_number}
                                              for a in assets[:20]],
                                       method="assets")
            elif spec.get("source") == "numbered_paragraphs" or ftype == "ordered_list":
                steps = _numbered_steps(text_elements)
                if steps:
                    value = FieldValue(name=name, value=steps[:200], method="numbered_list",
                                       page_number=steps[0].get("page"))
            elif spec.get("source") == "taxonomy_tag_patterns" or ftype == "tag_list":
                value = _tag_list_field(name, spec, text_elements)
            elif ftype == "taxonomy_term":
                value = _taxonomy_term_field(name, spec, full_text)
            else:
                value = _regex_field(name, spec, text_elements)

            if value is None and spec.get("fallback"):
                value = self._generic_fallback(name, spec["fallback"], text_elements, norm)

            if value is not None:
                fields[name] = value.to_dict()

        expected = schema.get("expected_fields", [])
        required = [s["name"] for s in schema.get("extraction_fields", []) if s.get("required")]
        missing_required = [n for n in required if n not in fields]

        return {
            "schema_id": schema_id,
            "schema_name": schema.get("name", schema_id),
            "fields": fields,
            "expected_fields": expected,
            "missing_required": missing_required,
            "evidence_fields": self.common.get("evidence_fields", []),
            "validation": self.validate(schema_id, fields),
        }

    def _generic_fallback(self, name: str, fallback_key: str, elements,
                          norm=None) -> Optional[FieldValue]:
        # normalised document-level metadata wins if we already harvested it
        if norm is not None:
            mapped = {"doc_date": getattr(norm, "doc_date", None),
                      "author": getattr(norm, "author", None),
                      "plant_unit": getattr(norm, "plant_unit", None),
                      "document_number": None, "approved_by": None}
            if mapped.get(fallback_key):
                return FieldValue(name=name, value=mapped[fallback_key], method="fallback",
                                  pattern=f"document metadata: {fallback_key}")
        for pattern in self.generic_patterns.get(fallback_key, []):
            for el in elements[:120]:
                m = pattern.search(el.text)
                if m:
                    return FieldValue(name=name, value=m.group(1).strip(), method="fallback",
                                      page_number=el.page_number, section=el.section,
                                      source_location=el.source_location,
                                      extraction_method=el.extraction_method,
                                      ocr_confidence=el.ocr_confidence,
                                      pattern=pattern.pattern)
        return None

    # ------------------------------------------------------------ validation

    def validate(self, schema_id: str, fields: Dict[str, dict]) -> List[dict]:
        from industrial.extraction import parse_date

        schema = self.get(schema_id)
        results: List[dict] = []

        def add(rule, field_name, status, detail):
            results.append({"field": field_name, "rule": rule, "status": status,
                            "detail": detail})

        for rule in schema.get("validation", []):
            fname = rule.get("field")
            rname = rule.get("rule")
            detail = rule.get("detail", "")
            present = fname in fields
            value = fields.get(fname, {}).get("value")

            if rname == "required":
                add(rname, fname, "pass" if present else "fail",
                    detail if not present else "present")
            elif rname == "non_empty":
                ok = present and bool(value)
                add(rname, fname, "pass" if ok else ("fail" if present else "missing"), detail)
            elif rname == "date_parseable":
                if not present:
                    add(rname, fname, "missing", detail)
                else:
                    add(rname, fname, "pass" if parse_date(str(value)) else "fail",
                        f"value '{value}' " + ("parsed" if parse_date(str(value)) else "not a date"))
            elif rname == "date_after":
                other = rule.get("compare_to")
                a, b = fields.get(fname, {}).get("value"), fields.get(other, {}).get("value")
                da, db = parse_date(str(a)) if a else None, parse_date(str(b)) if b else None
                if not (da and db):
                    add(rname, fname, "not_evaluated", f"needs both {fname} and {other}")
                else:
                    add(rname, fname, "pass" if da >= db else "fail",
                        f"{fname}={da} vs {other}={db}")
            elif rname == "in_enum":
                spec = _field_spec(schema, fname)
                allowed = [str(v).lower() for v in (spec.get("enum") if spec else []) or []]
                if not present:
                    add(rname, fname, "missing", detail)
                elif not allowed:
                    add(rname, fname, "not_evaluated", "no enum declared")
                else:
                    add(rname, fname, "pass" if str(value).lower() in allowed else "fail",
                        f"value '{value}'; allowed: {', '.join(allowed)}")
            elif rname == "non_negative":
                nums = _NUMBER_RE.findall(str(value)) if present else []
                if not nums:
                    add(rname, fname, "missing" if not present else "not_evaluated", detail)
                else:
                    add(rname, fname, "pass" if float(nums[0]) >= 0 else "fail", f"value {nums[0]}")
            elif rname in ("has_unit", "numeric_units"):
                if not present:
                    add(rname, fname, "missing", detail)
                else:
                    text = json.dumps(value) if not isinstance(value, str) else value
                    add(rname, fname, "pass" if _UNIT_RE.search(text) else "fail",
                        "unit found" if _UNIT_RE.search(text) else detail)
            elif rname == "not_greater_than":
                other = rule.get("compare_to")
                a = _first_number(fields.get(fname, {}).get("value"))
                b = _first_number(fields.get(other, {}).get("value"))
                if a is None or b is None:
                    add(rname, fname, "not_evaluated", f"needs numeric {fname} and {other}")
                else:
                    add(rname, fname, "pass" if a <= b else "fail",
                        f"{fname}={a} vs {other}={b} - {detail}" if a > b else f"{a} <= {b}")
            elif rname == "distinct_from":
                other = rule.get("compare_to")
                a, b = fields.get(fname, {}).get("value"), fields.get(other, {}).get("value")
                if not (a and b):
                    add(rname, fname, "not_evaluated", f"needs both {fname} and {other}")
                else:
                    same = str(a).strip().lower() == str(b).strip().lower()
                    add(rname, fname, "fail" if same else "pass", detail if same else "distinct")
            elif rname == "chronological":
                add(rname, fname, "not_evaluated",
                    "sequence ordering is checked by the agent that consumes the table")
            elif rname in ("requires_evidence", "never_autofill", "no_inference", "vendor_claim",
                           "present_or_flag", "taxonomy_known", "within_tolerance_check",
                           "compare_to_criteria", "time_parseable"):
                # Policy rules: enforced at answer/report time by
                # provenance.CitationValidator and the agent layer, not by
                # pattern matching here. Reported so they stay visible.
                add(rname, fname, "policy", detail)
            else:
                add(rname or "unknown", fname, "not_evaluated", f"no local check for rule '{rname}'")

        return results


def _field_spec(schema: dict, name: str) -> Optional[dict]:
    for s in schema.get("extraction_fields", []):
        if s.get("name") == name:
            return s
    return None


def _first_number(value) -> Optional[float]:
    if value is None:
        return None
    nums = _NUMBER_RE.findall(str(value))
    return float(nums[0]) if nums else None


# A captured multi-line value stops when a new labelled field starts
# ("Severity:", "Required action:") or a blank line separates sections.
_NEXT_LABEL_RE = re.compile(r"^\s*[A-Z][A-Za-z /&()-]{2,40}\s*:", re.M)


def _trim_value(value: str, multiline: bool) -> str:
    value = value.strip()
    if not multiline:
        return value
    cut = value.find("\n\n")
    if cut > 0:
        value = value[:cut]
    lines = value.splitlines()
    kept = [lines[0]] if lines else []
    for line in lines[1:]:
        if _NEXT_LABEL_RE.match(line):
            break
        kept.append(line)
    return " ".join(l.strip() for l in kept).strip()


def _regex_field(name: str, spec: dict, elements) -> Optional[FieldValue]:
    """Patterns are tried in the order the schema declares them, each across
    the WHOLE document, before the next pattern is tried. Declaration order is
    therefore precedence: 'Equipment ID: P-101A' must win over a bare tag-like
    string such as a document number that happens to appear on page 1 first."""
    patterns = spec.get("patterns") or []
    enum = [str(v).lower() for v in spec.get("enum", [])]
    multiline = spec.get("type") == "text"
    flags = re.S if multiline else 0
    compiled = []
    for p in patterns:
        try:
            compiled.append(re.compile(p, flags))
        except re.error as e:
            logger.warning(f"bad pattern for field '{name}': {e}")

    if not compiled and enum:
        # enum-only field: look for any allowed value as a whole word
        compiled = [re.compile(r"(?i)\b(" + "|".join(re.escape(v) for v in enum) + r")\b")]

    is_list = spec.get("type") in ("list", "timestamped_list")
    collected: List[str] = []
    first_hit: Optional[FieldValue] = None

    for pattern in compiled:
        for el in elements:
            for m in pattern.finditer(el.text):
                value = _trim_value(m.group(1) if m.groups() else m.group(0), multiline)
                if not value:
                    continue
                if enum and value.lower() not in enum and spec.get("type") == "enum":
                    continue
                if is_list:
                    if value not in collected:
                        collected.append(value)
                    if first_hit is None:
                        first_hit = FieldValue(name=name, value=None, method="regex",
                                               page_number=el.page_number, section=el.section,
                                               source_location=el.source_location,
                                               extraction_method=el.extraction_method,
                                               ocr_confidence=el.ocr_confidence,
                                               pattern=pattern.pattern)
                else:
                    return FieldValue(name=name, value=value, method="regex",
                                      page_number=el.page_number, section=el.section,
                                      source_location=el.source_location,
                                      extraction_method=el.extraction_method,
                                      ocr_confidence=el.ocr_confidence,
                                      pattern=pattern.pattern)
        if is_list and collected:
            break
    if is_list and collected and first_hit is not None:
        first_hit.value = collected[:40]
        return first_hit
    return None


def _tag_list_field(name: str, spec: dict, elements) -> Optional[FieldValue]:
    from industrial.taxonomy import get_taxonomy
    tax = get_taxonomy()
    tags: List[str] = []
    first_el = None
    patterns = [re.compile(p) for p in spec.get("patterns", [])] or None
    for el in elements:
        hits = ([m.group(0) for p in patterns for m in p.finditer(el.text)] if patterns
                else [t["tag"] for t in tax.detect_equipment_tags(el.text)])
        for tag in hits:
            if tag not in tags:
                tags.append(tag)
                first_el = first_el or el
        if len(tags) >= 60:
            break
    if not tags:
        return None
    return FieldValue(name=name, value=tags, method="taxonomy",
                      page_number=getattr(first_el, "page_number", None),
                      section=getattr(first_el, "section", None),
                      source_location=getattr(first_el, "source_location", None),
                      extraction_method=getattr(first_el, "extraction_method", None))


def _taxonomy_term_field(name: str, spec: dict, text: str) -> Optional[FieldValue]:
    from industrial.taxonomy import get_taxonomy
    tax = get_taxonomy()
    categories = set(spec.get("taxonomy_categories", []))
    for hit in tax.detect_terms(text):
        if not categories or hit["category"] in categories:
            return FieldValue(name=name, value=hit["term"], method="taxonomy",
                              pattern=f"taxonomy category {hit['category']}")
    return None


def _numbered_steps(elements) -> List[dict]:
    step_re = re.compile(r"^\s*(\d{1,3})[.)]\s+(.{3,})$")
    steps = []
    for el in elements:
        for line in el.text.splitlines():
            m = step_re.match(line.strip())
            if m:
                steps.append({"number": int(m.group(1)), "text": m.group(2).strip(),
                              "page": el.page_number, "section": el.section})
    return steps


# ------------------------------------------------------------------- module API

def load_registry(path: Optional[Path] = None) -> SchemaRegistry:
    path = Path(path or SCHEMAS_PATH)
    if not path.exists():
        logger.warning(f"document schemas not found at {path}")
        return SchemaRegistry({"schemas": {}})
    try:
        return SchemaRegistry(json.loads(path.read_text(encoding="utf-8")))
    except (ValueError, OSError) as e:
        logger.error(f"could not load document schemas: {e}")
        return SchemaRegistry({"schemas": {}})


def get_registry() -> SchemaRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        with _LOCK:
            if _REGISTRY is None:
                _REGISTRY = load_registry()
    return _REGISTRY


def reload_registry() -> SchemaRegistry:
    global _REGISTRY
    with _LOCK:
        _REGISTRY = load_registry()
    return _REGISTRY


def classify_document(doc) -> SchemaMatch:
    """Classify an ExtractedDocument (or raw text + filename)."""
    if hasattr(doc, "elements"):
        text = "\n".join(e.text for e in doc.elements[:200] if e.text and not e.is_inference)
        filename = doc.filename
    else:
        text, filename = str(doc), ""
    return get_registry().classify(text, filename)


def extract_fields(doc, schema_id: str, norm=None) -> Dict[str, Any]:
    return get_registry().extract(doc, schema_id, norm)


def validate_fields(schema_id: str, fields: Dict[str, dict]) -> List[dict]:
    return get_registry().validate(schema_id, fields)


def list_schemas() -> List[dict]:
    return get_registry().list_schemas()
