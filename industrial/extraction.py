"""
Phase C (second half) - metadata extraction, document normalisation and
provenance-preserving chunking.

The rule this module exists to enforce: nothing is ever flattened into plain
text. Every chunk that reaches the knowledge base still knows its document,
page, section, heading, revision, date, author, equipment, plant/unit,
department, confidentiality, source location, extraction method and OCR
confidence. If a value is unknown it stays None - it is never invented and
never inherited from a different document.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.logger import get_logger
from industrial.doc_pipeline import DocElement, ExtractedDocument, make_document_id

logger = get_logger("industrial.extract")

# --------------------------------------------------------------------- claims

CLAIM_USER_DOCUMENT = "user_document"
CLAIM_HISTORICAL_FACT = "historical_fact"
CLAIM_ENGINEERING_PRINCIPLE = "engineering_principle"
CLAIM_MODERN_PRACTICE = "modern_best_practice"
CLAIM_COMPANY_SPECIFIC = "company_specific"
CLAIM_AI_INFERENCE = "ai_inference"

VALID_CLAIM_TYPES = {CLAIM_USER_DOCUMENT, CLAIM_HISTORICAL_FACT, CLAIM_ENGINEERING_PRINCIPLE,
                     CLAIM_MODERN_PRACTICE, CLAIM_COMPANY_SPECIFIC, CLAIM_AI_INFERENCE}

CLASS_USER_DOCUMENT = "user_document"
CLASS_KNOWLEDGE_CORPUS = "knowledge_corpus"

# ------------------------------------------------------------------- patterns
# Deliberately conservative: a wrong revision or date is worse than none.

_DATE_FORMATS = ["%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%m/%d/%Y", "%d.%m.%Y", "%Y/%m/%d",
                 "%d-%b-%Y", "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y", "%d-%m-%y",
                 "%d/%m/%y"]

_RE_REVISION = re.compile(r"(?i)\brev(?:ision)?\.?\s*[:#-]?\s*([A-Z0-9][A-Z0-9.\-]{0,7})\b")
_RE_DATE_LABELLED = re.compile(
    r"(?i)\b(?:date|dated|issued\s*on|report\s*date|inspection\s*date)\s*[:\-]?\s*"
    r"([0-9]{1,4}[-/. ][0-9A-Za-z]{1,9}[-/. ][0-9]{2,4})")
_RE_DATE_ISO = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_RE_AUTHOR = re.compile(
    r"(?i)\b(?:prepared\s*by|inspected\s*by|inspector|author|reported\s*by|written\s*by|"
    r"engineer)\s*[:\-]\s*([A-Z][A-Za-z.\-]*(?:[ \t]+[A-Z][A-Za-z.\-]*){0,3})")
_RE_PLANT_UNIT = re.compile(
    r"(?i)\b(?:unit|plant|area|block|refinery|complex)\s*[:\-]\s*"
    r"([A-Z0-9][A-Za-z0-9 &/\-]{1,40})")
_RE_DEPARTMENT = re.compile(
    r"(?i)\b(?:department|dept\.?|section|division)\s*[:\-]\s*([A-Za-z][A-Za-z &/\-]{2,40})")
_RE_CONFIDENTIALITY = re.compile(
    r"(?i)\b(public|internal\s*use\s*only|internal|confidential|restricted|secret)\b")
_RE_TITLE_LABEL = re.compile(r"(?i)^\s*(?:title|subject|sub)\s*[:\-]\s*(.{3,120})$", re.M)


def parse_date(value: str) -> Optional[str]:
    """Return ISO yyyy-mm-dd, or None. Ambiguous 2-digit-year day/month order
    is resolved as day-first (the convention in Indian and European plant
    paperwork); anything unparseable returns None rather than a guess."""
    if not value:
        return None
    v = re.sub(r"\s+", " ", value.strip().strip(",.;"))
    for fmt in _DATE_FORMATS:
        try:
            dt = datetime.strptime(v, fmt)
            if dt.year < 1900 or dt.year > 2100:
                continue
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _first(pattern: re.Pattern, text: str) -> Optional[str]:
    m = pattern.search(text)
    return m.group(1).strip() if m else None


# Values that appear in embedded file properties but say nothing about the
# document: converter names, default placeholders, empty markers.
_JUNK_PROPERTIES = {
    "", "-", "n/a", "na", "none", "null", "unknown", "untitled", "anonymous",
    "(anonymous)", "(unspecified)", "unspecified", "user", "admin", "administrator", "owner", "author",
    "python-docx", "python-pptx", "openpyxl", "reportlab", "reportlab pdf library",
    "microsoft word", "microsoft excel", "microsoft powerpoint", "word", "excel",
    "libreoffice", "openoffice", "pdfcreator", "acrobat", "adobe acrobat", "ghostscript",
    "pymupdf", "docx2pdf", "wps office", "canva", "google docs",
}


def _usable_property(value) -> bool:
    if value is None:
        return False
    text = str(value).strip()
    if len(text) < 2 or len(text) > 200:
        return False
    if text.startswith("(") and text.endswith(")") and len(text) < 40:
        return False   # reportlab/converter placeholders like "(anonymous)"
    return text.lower() not in _JUNK_PROPERTIES


def find_equipment_tags(text: str, taxonomy=None, limit: int = 25) -> List[str]:
    """Extract equipment/instrument tag numbers using the taxonomy's tag
    patterns. Returns unique tags in order of appearance."""
    from industrial import taxonomy as taxonomy_module
    tax = taxonomy or taxonomy_module.get_taxonomy()
    found: List[str] = []
    for pattern in tax.tag_patterns():
        for m in pattern["compiled"].finditer(text):
            tag = m.group(0).strip()
            if tag not in found:
                found.append(tag)
                if len(found) >= limit:
                    return found
    return found


def harvest_metadata(doc: ExtractedDocument, max_chars: int = 6000) -> Dict[str, Any]:
    """Pull document-level metadata from the first pages plus the file's own
    embedded properties. Embedded properties win over regex guesses."""
    # Header metadata lives in prose, not in data tables. A spreadsheet's
    # first data column full of dates must not become the document date, so
    # narrative elements are harvested for metadata while equipment tags are
    # still looked for across everything.
    narrative = [e for e in doc.elements[:80] if e.element_type != "table"]
    head = "\n".join(e.text for e in (narrative or doc.elements[:20]))[:max_chars]
    all_text = "\n".join(e.text for e in doc.elements[:80])[:max_chars]
    raw = doc.raw_metadata or {}

    meta: Dict[str, Any] = {
        "title": None, "revision": None, "doc_date": None, "author": None,
        "plant_unit": None, "department": None, "confidentiality": None,
        "equipment": None, "equipment_tags": [],
    }

    # 1. what the DOCUMENT ITSELF says, in its own text. This wins over the
    #    file's embedded properties: a PDF produced by a converter routinely
    #    carries a meaningless author ('anonymous', the library name) and a
    #    creation date that is the conversion date, not the document date.
    meta["revision"] = _first(_RE_REVISION, head)
    labelled = _first(_RE_DATE_LABELLED, head)
    meta["doc_date"] = parse_date(labelled) if labelled else None
    meta["author"] = _first(_RE_AUTHOR, head)

    # 2. embedded file properties, only where the text gave us nothing and
    #    only when the value is not obvious tooling noise.
    if not meta["title"]:
        for key in ("title", "Title"):
            if _usable_property(raw.get(key)):
                meta["title"] = str(raw[key]).strip()[:160]
                break
    if not meta["author"]:
        for key in ("author", "Author", "creator", "Creator", "last_modified_by"):
            if _usable_property(raw.get(key)):
                meta["author"] = str(raw[key]).strip()[:80]
                break
    if not meta["revision"]:
        for key in ("revision", "Revision"):
            # A bare '1' from a Word core property is a save counter, not an
            # engineering revision - only accept it if the text had none and
            # it looks like a real revision label.
            value = str(raw.get(key) or "").strip()
            if value and value not in ("0", "1") and len(value) <= 8:
                meta["revision"] = value
                break
    if not meta["doc_date"]:
        for key in ("created", "CreationDate"):
            if raw.get(key):
                iso = parse_date(str(raw[key])[:10].replace("D:", ""))
                if iso:
                    meta["doc_date"] = iso
                    break
    if not meta["doc_date"] and narrative:
        # Only from narrative text: a bare ISO date inside a data table is a
        # row value, not the document's date.
        meta["doc_date"] = _first(_RE_DATE_ISO, head)
    meta["plant_unit"] = _first(_RE_PLANT_UNIT, head)
    meta["department"] = _first(_RE_DEPARTMENT, head)
    conf = _first(_RE_CONFIDENTIALITY, head)
    meta["confidentiality"] = conf.lower().replace(" use only", "") if conf else None

    if not meta["title"]:
        labelled_title = _RE_TITLE_LABEL.search(head)
        if labelled_title:
            meta["title"] = labelled_title.group(1).strip()[:160]
        else:
            for e in doc.elements[:12]:
                if e.element_type == "heading" and 4 <= len(e.text.strip()) <= 160:
                    meta["title"] = e.text.strip()
                    break
    if not meta["title"]:
        meta["title"] = Path(doc.filename).stem.replace("_", " ").strip()[:160]

    tags = find_equipment_tags(all_text)
    meta["equipment_tags"] = tags
    meta["equipment"] = tags[0] if tags else None
    return meta


# --------------------------------------------------------------- normalisation

@dataclass
class NormalizedDocument:
    document_id: str
    filename: str
    source_path: str
    file_type: str
    document_type: str = "generic_document"
    document_class: str = CLASS_USER_DOCUMENT
    claim_type: str = CLAIM_USER_DOCUMENT
    title: Optional[str] = None
    revision: Optional[str] = None
    doc_date: Optional[str] = None
    author: Optional[str] = None
    equipment: Optional[str] = None
    plant_unit: Optional[str] = None
    department: Optional[str] = None
    confidentiality: Optional[str] = None
    page_count: int = 0
    file_hash: str = ""
    extraction_method: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    elements: List[DocElement] = field(default_factory=list)
    assets: List[Any] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def header_row(self) -> dict:
        """Row shape expected by memory.database.upsert_industrial_document."""
        return {
            "document_id": self.document_id, "filename": self.filename,
            "source_path": self.source_path, "document_type": self.document_type,
            "document_class": self.document_class, "claim_type": self.claim_type,
            "title": self.title, "revision": self.revision, "doc_date": self.doc_date,
            "author": self.author, "equipment": self.equipment, "plant_unit": self.plant_unit,
            "department": self.department, "confidentiality": self.confidentiality,
            "page_count": self.page_count, "file_hash": self.file_hash,
            "extraction_method": self.extraction_method, "metadata": self.metadata,
        }


def normalize_document(doc: ExtractedDocument,
                       document_class: str = CLASS_USER_DOCUMENT,
                       claim_type: Optional[str] = None,
                       document_type: Optional[str] = None,
                       extra_metadata: Optional[dict] = None) -> NormalizedDocument:
    """Turn a raw ExtractedDocument into the normalised record that the
    knowledge base stores, harvesting document-level metadata on the way."""
    meta = harvest_metadata(doc)
    if extra_metadata:
        meta.update({k: v for k, v in extra_metadata.items() if v is not None})

    default_claim = (CLAIM_USER_DOCUMENT if document_class == CLASS_USER_DOCUMENT
                     else CLAIM_HISTORICAL_FACT)
    claim = claim_type or default_claim
    if claim not in VALID_CLAIM_TYPES:
        raise ValueError(f"unknown claim_type '{claim}'")

    return NormalizedDocument(
        document_id=make_document_id(Path(doc.source_path)),
        filename=doc.filename, source_path=doc.source_path, file_type=doc.file_type,
        document_type=document_type or "generic_document",
        document_class=document_class, claim_type=claim,
        title=meta.get("title"), revision=meta.get("revision"), doc_date=meta.get("doc_date"),
        author=meta.get("author"), equipment=meta.get("equipment"),
        plant_unit=meta.get("plant_unit"), department=meta.get("department"),
        confidentiality=meta.get("confidentiality"), page_count=doc.page_count,
        file_hash=doc.file_hash,
        extraction_method="+".join(doc.extraction_methods) or "unknown",
        metadata={
            "equipment_tags": meta.get("equipment_tags", []),
            "raw_file_metadata": doc.raw_metadata,
            "extraction_methods": doc.extraction_methods,
            "scanned_pages": doc.scanned_pages,
            "mean_ocr_confidence": doc.mean_ocr_confidence(),
            "element_counts": _element_counts(doc.elements),
            "assets": [a.__dict__ for a in doc.assets],
            "duration_s": doc.duration_s,
        },
        elements=doc.elements, assets=doc.assets, warnings=list(doc.warnings),
    )


def _element_counts(elements: List[DocElement]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for e in elements:
        counts[e.element_type] = counts.get(e.element_type, 0) + 1
    return counts


# -------------------------------------------------------------------- chunking

DEFAULT_CHUNK_CHARS = 1100
DEFAULT_OVERLAP_CHARS = 180
MAX_TABLE_CHUNK_CHARS = 3200


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _table_chunks(element: DocElement, base: dict, index: int) -> List[dict]:
    """Tables are chunked by whole rows, never mid-row, and every part keeps
    the header line so a retrieved fragment is still readable."""
    text = element.text
    if len(text) <= MAX_TABLE_CHUNK_CHARS:
        return [dict(base, chunk_index=index, chunk_text=text)]
    lines = text.splitlines()
    header = lines[0] if lines else ""
    parts, current, size = [], [header], len(header)
    for line in lines[1:]:
        if size + len(line) > MAX_TABLE_CHUNK_CHARS and len(current) > 1:
            parts.append("\n".join(current))
            current, size = [header], len(header)
        current.append(line)
        size += len(line) + 1
    if len(current) > 1:
        parts.append("\n".join(current))
    return [dict(base, chunk_index=index + i, chunk_text=p) for i, p in enumerate(parts)]


def chunk_document(norm: NormalizedDocument, chunk_chars: int = DEFAULT_CHUNK_CHARS,
                   overlap_chars: int = DEFAULT_OVERLAP_CHARS,
                   include_inferences: bool = False) -> List[dict]:
    """Section- and page-aware chunking.

    * Text elements are merged only while they share the same page AND section,
      so a chunk can never straddle two pages or two sections and lose its
      citation.
    * Tables and figures become their own chunks - splitting a table across a
      text boundary destroys it.
    * Vision-model output is excluded by default: it is an inference, not
      document content, and must not be retrievable as if it were a source.
    """
    chunks: List[dict] = []
    index = 0
    buffer: List[DocElement] = []
    buffer_len = 0

    def base_for(el: DocElement) -> dict:
        loc = el.source_location or (f"page={el.page_number}" if el.page_number else None)
        return {
            "element_type": el.element_type,
            "page_number": el.page_number,
            "section": el.section,
            "heading": el.heading or el.section,
            "revision": norm.revision,
            "chunk_date": norm.doc_date,
            "author": norm.author,
            "equipment": norm.equipment,
            "plant_unit": norm.plant_unit,
            "department": norm.department,
            "confidentiality": norm.confidentiality,
            "source_location": f"{norm.filename}#{loc}" if loc else norm.filename,
            "extraction_method": el.extraction_method,
            "ocr_confidence": el.ocr_confidence,
            "claim_type": CLAIM_AI_INFERENCE if el.is_inference else norm.claim_type,
        }

    def flush():
        nonlocal buffer, buffer_len, index
        if not buffer:
            return
        text = "\n".join(e.text for e in buffer).strip()
        if text:
            base = base_for(buffer[0])
            base["element_type"] = "text"
            confs = [e.ocr_confidence for e in buffer if e.ocr_confidence is not None]
            base["ocr_confidence"] = round(sum(confs) / len(confs), 4) if confs else None
            methods = sorted({e.extraction_method for e in buffer})
            base["extraction_method"] = "+".join(methods)
            chunk = dict(base, chunk_index=index, chunk_text=text,
                         token_estimate=_estimate_tokens(text))
            chunks.append(chunk)
            index += 1
            # carry an overlap tail into the next chunk for continuity
            if overlap_chars > 0 and len(text) > overlap_chars:
                tail = text[-overlap_chars:]
                buffer = [DocElement(element_type="text", text=tail,
                                     page_number=buffer[-1].page_number,
                                     section=buffer[-1].section,
                                     extraction_method=buffer[-1].extraction_method,
                                     ocr_confidence=buffer[-1].ocr_confidence,
                                     source_location=buffer[-1].source_location)]
                buffer_len = len(tail)
                return
        buffer = []
        buffer_len = 0

    last_key = None
    for el in norm.elements:
        if not el.text or not el.text.strip():
            continue
        if el.is_inference and not include_inferences:
            continue

        if el.element_type in ("table", "figure"):
            flush()
            base = base_for(el)
            new_chunks = (_table_chunks(el, base, index) if el.element_type == "table"
                          else [dict(base, chunk_index=index, chunk_text=el.text)])
            for c in new_chunks:
                c.setdefault("token_estimate", _estimate_tokens(c["chunk_text"]))
            chunks.extend(new_chunks)
            index += len(new_chunks)
            last_key = None
            continue

        key = (el.page_number, el.section)
        if last_key is not None and key != last_key:
            flush()
        last_key = key

        if buffer_len + len(el.text) > chunk_chars and buffer:
            flush()
        buffer.append(el)
        buffer_len += len(el.text) + 1

    flush()
    return [c for c in chunks if c.get("chunk_text", "").strip()]


def extract_and_chunk(path: str | Path, document_class: str = CLASS_USER_DOCUMENT,
                      claim_type: Optional[str] = None, options=None,
                      classify: bool = True) -> tuple:
    """Convenience wrapper used by the ingestion tool and the industrial tools:
    extract -> classify against the document schemas -> normalise -> chunk.

    Returns (NormalizedDocument, chunks, ExtractedDocument)."""
    from industrial.doc_pipeline import extract_document
    from industrial import schemas as schema_module

    raw = extract_document(path, options)
    if raw.error and not raw.elements:
        return None, [], raw

    doc_type, schema_fields = "generic_document", {}
    if classify:
        match = schema_module.classify_document(raw)
        doc_type = match.schema_id
        schema_fields = {"schema_match": match.to_dict()}

    norm = normalize_document(raw, document_class=document_class, claim_type=claim_type,
                              document_type=doc_type)
    if classify:
        extracted = schema_module.extract_fields(raw, doc_type, norm)
        norm.metadata.update(schema_fields)
        norm.metadata["schema_fields"] = extracted.get("fields", {})
        norm.metadata["schema_validation"] = extracted.get("validation", [])

    chunks = chunk_document(norm)
    return norm, chunks, raw
