"""
Phase E - the local industrial knowledge corpus.

This is NOT fine-tuning. No base model is retrained to hold industrial
history: the corpus is a set of plain markdown files under
knowledge/industrial_history/, ingested into the same local knowledge base as
the user's own documents but tagged with a different claim type so the two can
never be confused.

Corpus file format (deliberately simple, human-editable, diff-friendly):

    ---
    title: History of Petroleum Refining
    domain: petroleum_refining
    default_claim_type: historical_fact
    ---

    ## Thermal cracking {claim: historical_fact}
    Burton's thermal cracking process was commercialised in 1913 ...

    ## Why cracking works {claim: engineering_principle}
    Longer hydrocarbon chains crack at ...

    ## Current practice {claim: modern_best_practice}
    Modern refiners typically ...

Every section carries its own claim type. That claim type travels with the
chunk into the knowledge base and appears on every piece of evidence, so a
retrieved statement always announces itself as general industry background
rather than as something read out of the user's inspection report.
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from core.logger import get_logger
from industrial import KNOWLEDGE_DIR
from industrial.doc_pipeline import file_hash, make_document_id
from industrial.extraction import (CLAIM_ENGINEERING_PRINCIPLE, CLAIM_HISTORICAL_FACT,
                                   CLAIM_MODERN_PRACTICE, CLASS_KNOWLEDGE_CORPUS,
                                   VALID_CLAIM_TYPES, _estimate_tokens)

logger = get_logger("industrial.corpus")

CORPUS_ROOT = KNOWLEDGE_DIR / "industrial_history"

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)
_HEADING_RE = re.compile(r"^(#{1,4})\s+(.*?)\s*(?:\{claim:\s*([a-z_]+)\s*\})?\s*$")

# The 30 knowledge domains the workbench ships with.
DOMAINS = [
    "oil_gas", "petroleum_refining", "petrochemicals", "process_engineering",
    "mechanical_engineering", "electrical_engineering", "instrumentation_control",
    "process_safety", "maintenance", "reliability", "inspection", "corrosion",
    "rotating_equipment", "static_equipment", "piping", "valves", "pumps", "compressors",
    "heat_exchangers", "boilers", "utilities", "hse", "emergency_response", "quality",
    "project_management", "engineering_standards", "industrial_incidents", "lessons_learned",
    "industrial_manufacturing", "automation_digitalization",
]


@dataclass
class CorpusSection:
    heading: str
    text: str
    claim_type: str
    level: int = 2
    order: int = 0


@dataclass
class CorpusDocument:
    path: Path
    title: str
    domain: str
    default_claim_type: str
    sections: List[CorpusSection] = field(default_factory=list)
    meta: Dict[str, str] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return "\n\n".join(f"{s.heading}\n{s.text}" for s in self.sections)


def _parse_frontmatter(content: str) -> tuple:
    m = _FRONTMATTER_RE.match(content)
    if not m:
        return {}, content
    meta: Dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip().lower()] = value.strip()
    return meta, content[m.end():]


def parse_corpus_file(path: Path) -> Optional[CorpusDocument]:
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning(f"could not read corpus file {path}: {e}")
        return None

    meta, body = _parse_frontmatter(content)
    default_claim = meta.get("default_claim_type", CLAIM_HISTORICAL_FACT)
    if default_claim not in VALID_CLAIM_TYPES:
        logger.warning(f"{path.name}: unknown default_claim_type '{default_claim}' - "
                       f"using historical_fact")
        default_claim = CLAIM_HISTORICAL_FACT

    domain = meta.get("domain") or path.parent.name
    title = meta.get("title") or path.stem.replace("_", " ").title()

    sections: List[CorpusSection] = []
    heading, level, claim = title, 1, default_claim
    buffer: List[str] = []
    order = 0

    def flush():
        nonlocal buffer, order
        text = "\n".join(buffer).strip()
        buffer = []
        if text:
            sections.append(CorpusSection(heading=heading, text=text, claim_type=claim,
                                          level=level, order=order))
            order += 1

    for line in body.splitlines():
        m = _HEADING_RE.match(line.rstrip())
        if m:
            flush()
            level = len(m.group(1))
            heading = m.group(2).strip()
            tagged = m.group(3)
            if tagged and tagged in VALID_CLAIM_TYPES:
                claim = tagged
            elif tagged:
                logger.warning(f"{path.name}: unknown claim tag '{tagged}' on '{heading}'")
                claim = default_claim
            else:
                claim = default_claim
        else:
            buffer.append(line)
    flush()

    if not sections:
        return None
    return CorpusDocument(path=path, title=title, domain=domain,
                          default_claim_type=default_claim, sections=sections, meta=meta)


def iter_corpus_files(root: Optional[Path] = None) -> Iterable[Path]:
    root = Path(root or CORPUS_ROOT)
    if not root.is_dir():
        return []
    return sorted(p for p in root.rglob("*.md") if p.is_file() and not p.name.startswith("_"))


def corpus_chunks(doc: CorpusDocument, chunk_chars: int = 1400) -> List[dict]:
    """One chunk per section, split further only if a section is very long.
    A chunk never merges two claim types."""
    chunks: List[dict] = []
    index = 0
    for section in doc.sections:
        source_location = f"{doc.path.name}#{section.heading[:60]}"
        base = {
            "element_type": "text", "page_number": None, "section": section.heading,
            "heading": section.heading, "revision": doc.meta.get("revision"),
            "chunk_date": doc.meta.get("date"), "author": doc.meta.get("author"),
            "equipment": None, "plant_unit": None,
            "department": doc.domain, "confidentiality": "public",
            "source_location": source_location,
            "extraction_method": "knowledge_corpus", "ocr_confidence": None,
            "claim_type": section.claim_type,
        }
        body = f"{section.heading}\n{section.text}".strip()
        if len(body) <= chunk_chars:
            chunks.append(dict(base, chunk_index=index, chunk_text=body,
                               token_estimate=_estimate_tokens(body)))
            index += 1
            continue
        paragraphs = [p.strip() for p in section.text.split("\n\n") if p.strip()]
        current, size = [section.heading], len(section.heading)
        for para in paragraphs:
            if size + len(para) > chunk_chars and len(current) > 1:
                text = "\n".join(current)
                chunks.append(dict(base, chunk_index=index, chunk_text=text,
                                   token_estimate=_estimate_tokens(text)))
                index += 1
                current, size = [section.heading], len(section.heading)
            current.append(para)
            size += len(para) + 2
        if len(current) > 1:
            text = "\n".join(current)
            chunks.append(dict(base, chunk_index=index, chunk_text=text,
                               token_estimate=_estimate_tokens(text)))
            index += 1
    return chunks


def ingest_corpus_document(doc: CorpusDocument, embed: bool = True,
                           run_id: Optional[str] = None) -> Dict[str, Any]:
    """Store one corpus file with per-section claim types."""
    from memory.database import (add_industrial_chunks, delete_industrial_document, init_db,
                                 log_ingestion, upsert_industrial_document)
    from industrial.knowledge_base import knowledge_base

    run_id = run_id or f"corpus-{uuid.uuid4().hex[:8]}"
    document_id = make_document_id(doc.path)
    chunks = corpus_chunks(doc)
    if not chunks:
        log_ingestion(run_id, str(doc.path), "skipped", document_id, 0, "no sections")
        return {"file": str(doc.path), "status": "skipped", "chunks": 0}

    init_db()
    delete_industrial_document(document_id)
    upsert_industrial_document({
        "document_id": document_id, "filename": doc.path.name, "source_path": str(doc.path),
        "document_type": "knowledge_corpus", "document_class": CLASS_KNOWLEDGE_CORPUS,
        "claim_type": doc.default_claim_type, "title": doc.title,
        "revision": doc.meta.get("revision"), "doc_date": doc.meta.get("date"),
        "author": doc.meta.get("author"), "equipment": None, "plant_unit": None,
        "department": doc.domain, "confidentiality": "public",
        "page_count": 0, "file_hash": file_hash(doc.path),
        "extraction_method": "knowledge_corpus",
        "metadata": {"domain": doc.domain, "sections": len(doc.sections),
                     "claim_types": sorted({s.claim_type for s in doc.sections}),
                     "frontmatter": doc.meta},
    })

    embedded = 0
    if embed:
        ok, detail = knowledge_base.embedding_available()
        if ok:
            model = knowledge_base.embed_model()
            for chunk in chunks:
                vec = knowledge_base.embed(chunk["chunk_text"])
                if vec:
                    chunk["embedding"] = vec
                    chunk["embedding_model"] = model
                    embedded += 1
        else:
            logger.warning(f"corpus stored without embeddings: {detail}")

    add_industrial_chunks(document_id, chunks)
    log_ingestion(run_id, str(doc.path), "ok", document_id, len(chunks),
                  f"corpus/{doc.domain}; {embedded}/{len(chunks)} embedded")
    return {"file": str(doc.path), "status": "ok", "document_id": document_id,
            "domain": doc.domain, "chunks": len(chunks), "embedded": embedded,
            "claim_types": sorted({c["claim_type"] for c in chunks})}


def ingest_corpus(root: Optional[Path] = None, embed: bool = True,
                  run_id: Optional[str] = None, progress=None) -> Dict[str, Any]:
    """Ingest the whole industrial history corpus."""
    started = time.perf_counter()
    run_id = run_id or f"corpus-{uuid.uuid4().hex[:8]}"
    results: List[dict] = []
    for path in iter_corpus_files(root):
        doc = parse_corpus_file(path)
        if doc is None:
            results.append({"file": str(path), "status": "skipped", "chunks": 0,
                            "detail": "unparseable or empty"})
            continue
        result = ingest_corpus_document(doc, embed=embed, run_id=run_id)
        results.append(result)
        if progress:
            progress(result)
    return {
        "run_id": run_id,
        "files": len(results),
        "ok": sum(1 for r in results if r["status"] == "ok"),
        "skipped": sum(1 for r in results if r["status"] != "ok"),
        "chunks": sum(r.get("chunks", 0) for r in results),
        "embedded": sum(r.get("embedded", 0) for r in results),
        "duration_s": round(time.perf_counter() - started, 2),
        "results": results,
    }


def corpus_stats(root: Optional[Path] = None) -> Dict[str, Any]:
    """Static view of the corpus on disk (no database access)."""
    root = Path(root or CORPUS_ROOT)
    domains: Dict[str, dict] = {}
    total_sections = 0
    claim_counts: Dict[str, int] = {}
    for path in iter_corpus_files(root):
        doc = parse_corpus_file(path)
        if not doc:
            continue
        entry = domains.setdefault(doc.domain, {"files": 0, "sections": 0, "words": 0})
        entry["files"] += 1
        entry["sections"] += len(doc.sections)
        entry["words"] += sum(len(s.text.split()) for s in doc.sections)
        total_sections += len(doc.sections)
        for s in doc.sections:
            claim_counts[s.claim_type] = claim_counts.get(s.claim_type, 0) + 1
    return {"root": str(root), "exists": root.is_dir(),
            "expected_domains": len(DOMAINS), "domains_on_disk": len(domains),
            "domains": domains, "sections": total_sections, "claim_types": claim_counts}


def ensure_domain_directories(root: Optional[Path] = None) -> List[str]:
    root = Path(root or CORPUS_ROOT)
    created = []
    for domain in DOMAINS:
        d = root / domain
        if not d.exists():
            d.mkdir(parents=True, exist_ok=True)
            created.append(domain)
    return created
