"""
Document -> knowledge fabric.

    file ─► industrial.doc_pipeline (PDF/DOCX/XLSX/PPTX/CSV/TXT/MD/HTML/images, OCR, tables)
         ─► industrial.extraction.harvest_metadata (title, date, author, revision, unit, dept,
                                                    confidentiality, equipment tags)
         ─► document type, status, authority, classification, period covered, origin
         ─► HIERARCHY: document > section > subsection > paragraph | table > row range | figure
                       (every node keeps page, section path, heading, date/year, hash)
         ─► tables -> cells -> numeric facts (knowledge_fabric.tables)
         ─► entities + evidence-stated equivalences (knowledge_fabric.entities)
         ─► relations with their sentence (knowledge_fabric.graph)
         ─► FTS5 index, optional vectors (BGE-M3 through Ollama, loopback)
Audio (wav/mp3/m4a/...) goes through the local Whisper transcriber first and is
stored as a transcript with timestamps.

Nothing is invented: an unknown date stays unknown (and the coverage report
says so), a date taken from the filename or the text is labelled with where it
came from, sample / demo data keeps its label.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import tempfile
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from core.logger import get_logger
from knowledge_fabric import registers, ORIGIN_COMPANY, ORIGINS, authority, graph, store, tables, temporal
from knowledge_fabric.access import normalise_classification
from knowledge_fabric.entities import Extractor, record_equivalence, upsert_mentions

logger = get_logger("knowledge_fabric.ingest")

PARAGRAPH_CHARS = 1200
ROW_RANGE = 25
AUDIO_TYPES = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".aac", ".wma", ".mp4"}
HTML_TYPES = {".html", ".htm"}


@dataclass
class IngestResult:
    source_path: str
    status: str                       # ingested | updated | skipped | failed
    document_id: Optional[str] = None
    title: Optional[str] = None
    detail: str = ""
    counts: Dict[str, int] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


def supported(path: Path) -> bool:
    from industrial.doc_pipeline import SUPPORTED_EXTENSIONS
    suffix = path.suffix.lower()
    return suffix in SUPPORTED_EXTENSIONS or suffix in AUDIO_TYPES or suffix in HTML_TYPES


def _embedding_enabled() -> bool:
    try:
        import configparser
        from core.paths import config_path
        parser = configparser.ConfigParser()
        parser.read(config_path(), encoding="utf-8")
        return parser.get("knowledge", "embed_on_ingest", fallback="auto").strip().lower() not in ("off", "false", "no", "0")
    except Exception:
        return True


def _vault_root() -> Optional[Path]:
    try:
        from core.vault import vault
        root = getattr(vault, "root", None) or getattr(vault, "directory", None)
        return Path(root).resolve() if root else None
    except Exception:
        return None


# ---------------------------------------------------------------- extraction --

def _extract(path: Path, ocr: bool = True):
    from industrial.doc_pipeline import ExtractedDocument, ExtractionOptions, DocElement, extract_document, file_hash
    suffix = path.suffix.lower()
    if suffix in AUDIO_TYPES:
        from industrial.meetings import transcribe
        result = transcribe(str(path))
        doc = ExtractedDocument(source_path=str(path), filename=path.name, file_type="audio")
        doc.file_hash = file_hash(path)
        if result.get("error"):
            doc.error = result["error"]
            return doc
        doc.extraction_methods.append("whisper_local")
        for order, segment in enumerate(result.get("segments", [])):
            doc.elements.append(DocElement(element_type="text", text=segment["text"], order=order,
                                           extraction_method="whisper_local",
                                           source_location=f"t={segment['start']}-{segment['end']}s"))
        return doc
    if suffix in HTML_TYPES:
        text = path.read_text(encoding="utf-8", errors="ignore")
        text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
        text = re.sub(r"(?i)<h([1-6])[^>]*>", lambda m: "\n\n" + "#" * int(m.group(1)) + " ", text)
        text = re.sub(r"(?i)</(p|div|li|tr|h[1-6])>", "\n", text)
        text = re.sub(r"(?i)</t[dh]>", " | ", text)
        text = re.sub(r"<[^>]+>", " ", text)
        import html as _html
        text = _html.unescape(text)
        tmp = Path(tempfile.mkdtemp(prefix="kf_html_")) / (path.stem + ".md")
        tmp.write_text(text, encoding="utf-8")
        doc = extract_document(tmp, ExtractionOptions(ocr=False, vision=False))
        doc.source_path, doc.filename, doc.file_type = str(path), path.name, "html"
        doc.file_hash = file_hash(path)
        shutil.rmtree(tmp.parent, ignore_errors=True)
        return doc
    return extract_document(path, ExtractionOptions(ocr=ocr, vision=False, images=False))


def _date_parts(doc_date: Optional[str]) -> Dict[str, Optional[int]]:
    if not doc_date:
        return {"year": None, "quarter": None, "month": None}
    m = re.match(r"(\d{4})(?:-(\d{2}))?", doc_date)
    if not m:
        return {"year": None, "quarter": None, "month": None}
    year = int(m.group(1))
    month = int(m.group(2)) if m.group(2) else None
    return {"year": year, "month": month, "quarter": ((month - 1) // 3 + 1) if month else None}


def _period_from_name(name: str) -> Dict[str, Any]:
    """'Annual_Report_2017-18.pdf' -> FY 2017-18; 'Maintenance 2019.xlsx' -> 2019."""
    stem = Path(name).stem.replace("_", " ")
    m = re.search(r"(?:fy\s*)?((?:19|20)\d{2})\s*[-–]\s*(\d{2}|(?:19|20)\d{2})\b", stem, re.I)
    if m:
        a = int(m.group(1))
        b = int(m.group(2)) if len(m.group(2)) == 4 else (a // 100 * 100 + int(m.group(2)))
        if b == a + 1:
            return {"start": a, "end": b, "fiscal": f"FY{a}-{str(b)[-2:]}"}
    years = temporal.years_in_text(stem)
    if len(years) == 1:
        return {"start": years[0], "end": years[0], "fiscal": None}
    if len(years) == 2 and years[1] > years[0]:
        # 'FINANCIAL_RECORD_1990_2026' - a document spanning years, not one of them
        return {"start": years[0], "end": years[1], "fiscal": None, "range": True}
    return {}


_YEARS_FIELD = re.compile(r"\*\*Years?\s*:\*\*\s*((?:19|20)\d{2})\s*[-–to ]+\s*((?:19|20)\d{2})", re.I)
_DEPT_FIELD = re.compile(r"\*\*Department\s*:\*\*\s*([A-Za-z&/ ]{2,40}?)\s*(?:\||\n|$)", re.I)
_SITE_FIELD = re.compile(r"\*\*Site\s*:\*\*\s*([^|\n(]{2,40}?)\s*(?:\(([A-Z]{2,5})\))?\s*(?:\||\n|$)", re.I)
_HEADING_YEAR = re.compile(r"^\s*(?:FY\s*)?((?:19|20)\d{2})\s*$", re.I)
_YEAR_SPAN = re.compile(r"\b(?:19|20)\d{2}\s*(?:-|–|—|to)\s*(?:19|20)\d{2}\b")
_ACRONYMS = {"HSE", "IT", "QC", "HR", "EHS", "R&D"}


def canonical_department(name: Optional[str]) -> Optional[str]:
    """'FINANCE' -> 'Finance', 'HUMAN RESOURCES' -> 'Human Resources', 'HSE' -> 'HSE' -
    the gazetteer's own spelling when it knows the department."""
    name = re.sub(r"\s+", " ", (name or "")).strip(" .")
    if not name:
        return None
    try:
        from knowledge_fabric.entities import gazetteer
        for entry in gazetteer().get("DEPARTMENT", []):
            canonical = entry["name"] if isinstance(entry, dict) else entry
            aliases = entry.get("aliases", []) if isinstance(entry, dict) else []
            if name.lower() in [canonical.lower()] + [a.lower() for a in aliases]:
                return canonical
    except Exception:
        pass
    return name if name.upper() in _ACRONYMS else " ".join(w if w.upper() in _ACRONYMS else w.capitalize()
                                                          for w in name.lower().split())


def _site_in(title: str) -> Optional[str]:
    """'Test Fixture Refinery - Maintenance Report 2020' -> 'Test Fixture Refinery'."""
    from knowledge_fabric.entities import _NOT_DESCRIPTORS, _SITE
    m = _SITE.search(title or "")
    if not m:
        return None
    words = [w for w in m.group(1).split() if w.lower() not in _NOT_DESCRIPTORS]
    return " ".join(words + [m.group(2)]) if words else None


def _heading_level(element) -> int:
    extra = element.extra or {}
    if "level" in extra:
        try:
            return max(1, int(extra["level"]))
        except (TypeError, ValueError):
            pass
    style = str(extra.get("style") or "")
    m = re.search(r"(\d)", style)
    if m:
        return max(1, int(m.group(1)))
    if style.lower() == "title":
        return 1
    return 2 if element.element_type == "heading" else 1


def _split_paragraphs(text: str, limit: int = PARAGRAPH_CHARS) -> List[str]:
    text = (text or "").strip()
    if len(text) <= limit:
        return [text] if text else []
    parts, current = [], ""
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        if len(current) + len(sentence) + 1 > limit and current:
            parts.append(current.strip())
            current = sentence
        else:
            current = f"{current} {sentence}" if current else sentence
    if current.strip():
        parts.append(current.strip())
    out = []
    for part in parts:                      # a single enormous "sentence"
        while len(part) > limit * 1.5:
            out.append(part[:limit])
            part = part[limit:]
        out.append(part)
    return out


def _hash(text: str) -> str:
    return hashlib.sha1((text or "").encode("utf-8", "ignore")).hexdigest()[:16]


_PIPE_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def _split_mixed(text: str) -> List[tuple]:
    """Text that carries markdown / pipe tables -> [('text', ...), ('table', grid), ...].
    The text extractor keeps such tables inside paragraphs; left there they would be
    flattened, and their numbers would never become facts."""
    blocks: List[tuple] = []
    current: List[str] = []
    table: List[str] = []

    def flush_text():
        if current and "\n".join(current).strip():
            blocks.append(("text", "\n".join(current).strip()))
        current.clear()

    def flush_table():
        rows = []
        for line in table:
            if _PIPE_SEPARATOR.match(line):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            rows.append(cells)
        width = max((len(r) for r in rows), default=0)
        if len(rows) >= 2 and width >= 2:
            blocks.append(("table", [r + [""] * (width - len(r)) for r in rows]))
        elif table:
            current.extend(table)
        table.clear()

    for line in (text or "").splitlines():
        if line.strip().startswith("|") and line.count("|") >= 3:
            if current:
                flush_text()
            table.append(line)
        else:
            if table:
                flush_table()
            current.append(line)
    if table:
        flush_table()
    flush_text()
    return blocks


# ------------------------------------------------------------------- ingest ----

def ingest_document(path: str | Path, origin: str = ORIGIN_COMPANY, force: bool = False,
                    overrides: Optional[Dict[str, Any]] = None, run_id: Optional[str] = None,
                    embed: Optional[bool] = None, ocr: bool = True,
                    allow_vault: bool = False) -> IngestResult:
    path = Path(path).expanduser().resolve()
    overrides = dict(overrides or {})
    run_id = run_id or uuid.uuid4().hex[:12]
    if origin not in ORIGINS:
        return IngestResult(str(path), "failed", detail=f"unknown origin '{origin}'")
    if not path.is_file():
        return _log(IngestResult(str(path), "failed", detail="file not found"), run_id)
    if not supported(path):
        return _log(IngestResult(str(path), "skipped", detail=f"unsupported type '{path.suffix}'"), run_id)
    vault_root = _vault_root()
    vault_linked = bool(vault_root and str(path).lower().startswith(str(vault_root).lower()))
    if vault_linked and not allow_vault:
        return _log(IngestResult(str(path), "skipped",
                                 detail="inside the Vault - vault files are not indexed unless explicitly "
                                        "ingested as restricted, vault-linked documents"), run_id)

    from industrial.doc_pipeline import file_hash, make_document_id
    digest = file_hash(path)
    document_id = make_document_id(path)
    with store.connection() as conn:
        existing = conn.execute("SELECT document_id, source_hash FROM kf_documents WHERE document_id = ? "
                                "OR (source_hash = ? AND source_path = ?)", (document_id, digest, str(path))).fetchone()
    if existing and existing["source_hash"] == digest and not force:
        return _log(IngestResult(str(path), "skipped", existing["document_id"], detail="unchanged (same SHA hash)"), run_id)
    if not existing:                                 # --force re-reads a file; it never stores a second copy
        twin = None
        with store.connection() as conn:
            row = conn.execute("SELECT document_id, filename, metadata FROM kf_documents WHERE source_hash = ? "
                               "AND source_path != ?", (digest, str(path))).fetchone()
            if row:
                # Byte-identical content is ONE document: a second copy would double every
                # figure and every citation. The copy's path is kept on the original.
                twin = dict(row)
                meta = store.loads(twin["metadata"], {}) or {}
                meta["also_at"] = sorted(set(meta.get("also_at", []) + [str(path)]))
                conn.execute("UPDATE kf_documents SET metadata = ? WHERE document_id = ?",
                             (store.dumps(meta), twin["document_id"]))
        if twin:                                     # logged after the update is committed
            return _log(IngestResult(str(path), "skipped", twin["document_id"],
                                     detail=f"duplicate of {twin['filename']} (same content, SHA-256)"), run_id)

    started = time.time()
    try:
        doc = _extract(path, ocr=ocr)
    except Exception as error:
        logger.exception(f"extraction failed for {path}")
        return _log(IngestResult(str(path), "failed", document_id, detail=f"extraction failed: {error}"), run_id)
    if doc.error and not doc.elements:
        return _log(IngestResult(str(path), "failed", document_id, detail=doc.error), run_id)

    from industrial.extraction import harvest_metadata
    meta = harvest_metadata(doc) if doc.elements else {}
    text_head = "\n".join(e.text for e in doc.elements[:60] if e.text)
    title = overrides.get("title") or meta.get("title") or path.stem
    doc_type = overrides.get("document_type") or authority.classify_type(text_head, path.name, doc.file_type)
    if doc_type == "other" and doc.file_type == "audio":
        doc_type = "transcript"
    if doc_type in ("other", "image") and doc.is_scanned:
        doc_type = "scanned_document"
    if origin == "general_knowledge":
        doc_type = overrides.get("document_type") or "general_knowledge"
    status = overrides.get("status") or authority.detect_status(text_head, path.name)

    # ---- time: the document's own statement first, then filename, then content --
    date_source = None
    doc_date = overrides.get("doc_date") or meta.get("doc_date")
    if doc_date:
        date_source = "override" if overrides.get("doc_date") else "document text / properties"
    period = _period_from_name(path.name)
    parts = _date_parts(doc_date)
    year = overrides.get("year") or parts["year"]
    if not period:
        span = _YEARS_FIELD.search(text_head[:3000])
        if span and int(span.group(2)) > int(span.group(1)):
            period = {"start": int(span.group(1)), "end": int(span.group(2)), "fiscal": None, "range": True}
    if year is None and period and period.get("range"):
        date_source = f"spans {period['start']}-{period['end']} (sections carry their own years)"
    elif year is None and period:
        year, date_source = period["end"] if period.get("fiscal") else period["start"], "filename"
    if year is None and not (period and period.get("range")):
        dominant = temporal.dominant_year(text_head[:4000])
        if dominant:
            year, date_source = dominant, "most frequent year in the text"
    time_start = overrides.get("time_start") or (period.get("start") if period else year)
    time_end = overrides.get("time_end") or (period.get("end") if period else year)
    fiscal = period.get("fiscal") if period else None

    department = overrides.get("department") or meta.get("department")
    location = overrides.get("location") or meta.get("plant_unit")
    # Units are told apart by SITE only when the site is stated - an explicit
    # --location, or a site named in the title ("X Refinery"). A guessed
    # location must not split one Unit 5 into per-document copies.
    entity_site = overrides.get("location") or _site_in(title)
    classification = normalise_classification(overrides.get("confidentiality") or meta.get("confidentiality"))
    if vault_linked:
        classification = "restricted"
    rank = authority.rank(doc_type, status, origin, doc.mean_ocr_confidence())

    counts = {"nodes": 0, "tables": 0, "facts": 0, "mentions": 0, "relations": 0, "vectors": 0}
    extractor = Extractor()
    node_texts: Dict[str, str] = {}
    fts_rows = []

    with store.connection() as conn:
        _delete_document_rows(conn, document_id)
        conn.execute(
            "INSERT OR REPLACE INTO kf_documents(document_id, source_path, filename, title, document_type, doc_date, "
            "year, quarter, month, fiscal_year, time_start, time_end, department, business_unit, location, "
            "confidentiality, department_restricted, vault_linked, author, revision, version, status, authority, "
            "origin, source_hash, page_count, language, supersedes, metadata, ingested_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (document_id, str(path), path.name, title, doc_type, doc_date, year, parts["quarter"], parts["month"],
             fiscal, time_start, time_end, department, overrides.get("business_unit"), location, classification,
             1 if overrides.get("department_restricted") else 0, 1 if vault_linked else 0,
             meta.get("author"), meta.get("revision"), overrides.get("version"), status, rank, origin, digest,
             doc.page_count, overrides.get("language"), overrides.get("supersedes"),
             store.dumps({"date_source": date_source, "equipment_tags": meta.get("equipment_tags", []),
                          "extraction_methods": doc.extraction_methods, "scanned_pages": doc.scanned_pages,
                          "mean_ocr_confidence": doc.mean_ocr_confidence(), "warnings": doc.warnings,
                          "file_type": doc.file_type, "run_id": run_id, "entity_site": entity_site,
                          "dataset": overrides.get("dataset"), "dataset_note": overrides.get("dataset_note"),
                          "authority": authority.describe(rank)}),
             store.now(), store.now()))

        root_id = f"{document_id}:0"
        conn.execute("INSERT INTO kf_nodes(node_id, document_id, parent_id, kind, ordinal, depth, text, text_hash, year) "
                     "VALUES (?, ?, NULL, 'document', 0, 0, ?, ?, ?)", (root_id, document_id, title, _hash(title), year))
        fts_rows.append((title, "", title, root_id, document_id))

        stack: List[tuple] = []          # (level, node_id, heading)
        ordinal = 0
        table_ordinal = 0
        found_registers: List[Dict[str, Any]] = []
        section_tags: Dict[str, Dict[str, Any]] = {}
        tag_node: Optional[str] = None
        tag_level = 0
        doc_declared = authority.declared_type(Path(path.name).stem) or \
            (doc_type if doc_type in authority.DOCUMENT_TYPES and doc_type not in
             ("records_pack", "presentation", "spreadsheet", "register", "other", "image") else None)
        record_nodes: set = set()

        def parent() -> tuple:
            return (stack[-1][1], len(stack)) if stack else (root_id, 0)

        def section_path() -> str:
            return " > ".join(h for _l, _n, h in stack)

        def add_node(kind: str, text: str, element, parent_id: str, depth: int, heading: Optional[str]) -> str:
            nonlocal ordinal
            ordinal += 1
            node_id = f"{document_id}:{ordinal}"
            # "Years: 1990-2026" states a span, not the year the passage is about.
            node_year = temporal.dominant_year(_YEAR_SPAN.sub(" ", text or "")) if kind in ("paragraph", "row_range") else None
            # A year after the document's own is a plan or a deadline ("deferred to
            # 2023"), not the period the paragraph reports on.
            if node_year and year and node_year > year:
                node_year = None
            if not node_year and kind in ("paragraph", "row_range"):
                # no year of its own: the nearest year heading above it ("## FY1995")
                node_year = next((int(m.group(1)) for _l, _n, hd in reversed(stack)
                                  if (m := _HEADING_YEAR.match(hd or ""))), None)
            node_year = node_year if node_year and 1950 <= node_year <= 2049 else year
            if kind == "record":                 # a register row carries its own dates
                node_year = None
            elif kind == "event":                # an event row IS dated: '2020-04-01 | COVID ...'
                event_year = re.match(r"\s*\w[\w ]*:\s*((?:19|20)\d{2})-\d{2}-\d{2}", text or "")
                node_year = int(event_year.group(1)) if event_year else None
            conn.execute(
                "INSERT INTO kf_nodes(node_id, document_id, parent_id, kind, ordinal, depth, page, section_path, heading, "
                "text, text_hash, date, year, element_type, extraction_method, ocr_confidence, is_inference, source_location) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (node_id, document_id, parent_id, kind, ordinal, depth, getattr(element, "page_number", None),
                 section_path(), heading, text, _hash(text), doc_date, node_year,
                 getattr(element, "element_type", kind), getattr(element, "extraction_method", None),
                 getattr(element, "ocr_confidence", None), 1 if getattr(element, "is_inference", False) else 0,
                 getattr(element, "source_location", None)))
            counts["nodes"] += 1
            if kind == "record":
                record_nodes.add(node_id)
            if text and kind != "section":
                node_texts[node_id] = text
                fts_rows.append((text, heading or "", title, node_id, document_id))
            return node_id


        def add_table(grid: List[List[str]], element, pid: str, depth: int, table_title: str) -> None:
            """A table node, its cells, row-range nodes for long tables, and its facts."""
            nonlocal table_ordinal
            table_ordinal += 1
            parsed = tables.parse_table(grid, document_id, table_ordinal, doc_year=year, title=table_title)
            profile = registers.profile(parsed.headers, parsed.rows)
            if profile and not profile["has_period"] and parsed.facts:
                # Rows are records: 'LS-1001 | rent 85,000' is not a value for the year the
                # workbook mentions most. Only a real Period / Month / FY column dates them.
                parsed.notes.append(f"register - rows are records; {len(parsed.facts)} undated values "
                                    "are kept as records, not as dated facts")
                parsed.facts = []
            table_text = tables.table_markdown(parsed.headers, parsed.rows)
            table_node = add_node("table", table_text, element, pid, depth + 1, table_title or None)
            conn.execute("INSERT OR REPLACE INTO kf_tables(table_id, document_id, node_id, page, title, headers, units, "
                         "n_rows, n_cols, orientation, notes) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (parsed.table_id, document_id, table_node, getattr(element, "page_number", None), table_title,
                          store.dumps(parsed.headers), store.dumps(parsed.units), len(parsed.rows),
                          len(parsed.headers), parsed.orientation, store.dumps(parsed.notes)))
            counts["tables"] += 1
            for r_index, row in enumerate(parsed.rows[:2000]):
                label = row[0] if row else ""
                for c_index, cell in enumerate(row):
                    if not cell:
                        continue
                    conn.execute("INSERT OR REPLACE INTO kf_cells(table_id, row_index, col_index, header, row_label, "
                                 "value_text, value_num, unit) VALUES (?,?,?,?,?,?,?,?)",
                                 (parsed.table_id, r_index, c_index,
                                  parsed.headers[c_index] if c_index < len(parsed.headers) else "",
                                  label, cell[:500], tables.parse_number(cell),
                                  parsed.units[c_index] if c_index < len(parsed.units) else None))
            if profile:
                found_registers.append({"name": table_title or title, "sensitive": profile["sensitive"],
                                        "records": len(parsed.rows)})
                counts["records"] = counts.get("records", 0) + registers.store_register(
                    conn, parsed.table_id, document_id, table_node, table_title or title, parsed.headers,
                    parsed.rows, profile,
                    lambda text, heading, kind="record": add_node(kind, text, element, table_node, depth + 2, heading))
            # A daily ledger's rows live on as monthly / per-site facts (each citing its
            # row range); hundreds of 25-row passages of it would only crowd search.
            if len(parsed.rows) > ROW_RANGE and parsed.orientation != "daily":
                for start in range(0, len(parsed.rows), ROW_RANGE):
                    chunk = parsed.rows[start:start + ROW_RANGE]
                    add_node("row_range", tables.table_markdown(parsed.headers, chunk, limit=ROW_RANGE),
                             element, table_node, depth + 2,
                             f"{table_title} rows {start + 1}-{start + len(chunk)}".strip())
            for fact in parsed.facts:
                entity_id = None
                if fact.entity_label:
                    found = extractor.mentions(fact.entity_label, entity_site)
                    if found:
                        upsert_mentions(conn, table_node, found[:1])
                        entity_id = found[0].entity_id
                conn.execute("INSERT INTO kf_facts(metric, metric_norm, entity_id, entity_label, period, year, quarter, "
                             "month, value, unit, document_id, node_id, table_id, cell_ref, source_text, confidence) "
                             "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             (fact.metric, fact.metric_norm, entity_id, fact.entity_label, fact.period, fact.year,
                              fact.quarter, fact.month, fact.value, fact.unit, document_id, table_node,
                              parsed.table_id, fact.cell_ref, fact.source_text, fact.confidence))
                counts["facts"] += 1

        for element in doc.elements:
            etype = element.element_type
            text = (element.text or "").strip()
            if not text or etype in ("header", "footer"):
                continue
            if etype in ("heading", "slide"):
                level = _heading_level(element) if etype == "heading" else 1
                while stack and stack[-1][0] >= level:
                    stack.pop()
                pid, depth = parent()
                kind = "section" if depth == 0 else "subsection"
                node_id = add_node(kind, text[:300], element, pid, depth + 1, text[:200])
                if kind == "section":            # subsections are indexed by add_node itself
                    fts_rows.append((text[:300], text[:200], title, node_id, document_id))
                stack.append((level, node_id, text[:120]))
                # A section that states what it is: '## FINANCIAL_RECORD' in a records
                # pack, or '## FY1990' in a document of one declared type.
                heading_type = authority.declared_type(text) if authority._TYPE_HEADING.match(text) else None
                heading_year = _HEADING_YEAR.match(text)
                if heading_type or (heading_year and doc_declared):
                    section_tags[node_id] = {"document_type": heading_type or doc_declared,
                                             "year": int(heading_year.group(1)) if heading_year else year,
                                             "department": None, "site": None}
                    tag_node, tag_level = node_id, level
                elif tag_node and level <= tag_level:
                    tag_node = None
                continue
            pid, depth = parent()
            heading = stack[-1][2] if stack else None
            if tag_node:
                dept, site = _DEPT_FIELD.search(text), _SITE_FIELD.search(text)
                if dept and not section_tags[tag_node]["department"]:
                    section_tags[tag_node]["department"] = canonical_department(dept.group(1))
                if site and not section_tags[tag_node]["site"]:
                    section_tags[tag_node]["site"] = site.group(1).strip()
            if etype in ("table", "sheet"):
                add_table(tables.grid_from_text(text), element, pid, depth,
                          (element.extra or {}).get("sheet") or heading or "")
                continue
            kind = "figure" if etype in ("figure", "image") else "paragraph"
            for block_kind, content in (_split_mixed(text) if kind == "paragraph" else [("text", text)]):
                if block_kind == "table":
                    add_table(content, element, pid, depth, heading or "")
                    continue
                for part in _split_paragraphs(content):
                    add_node(kind, part, element, pid, depth + 1, heading)

        conn.executemany("INSERT OR REPLACE INTO kf_section_tags(node_id, document_id, year, document_type, department, "
                         "site) VALUES (?,?,?,?,?,?)",
                         [(node, document_id, tag["year"], tag["document_type"], tag["department"], tag["site"])
                          for node, tag in section_tags.items()])
        counts["sections_typed"] = len(section_tags)

        # ---- what the content showed: registers, personal data, department -----
        settled = _settle_document(conn, document_id, doc_type, date_source, department, classification,
                                   found_registers, title, path.name, text_head, overrides, origin, status,
                                   doc.mean_ocr_confidence())
        doc_type, classification, department = settled["document_type"], settled["confidentiality"], settled["department"]
        rank = settled["authority"]
        if settled.get("year_cleared"):
            year, date_source = None, settled["date_source"]

        # ---- entities, equivalences, relations --------------------------------
        for node_id, text in node_texts.items():
            if len(text) > 20000:
                text = text[:20000]
            mentions = extractor.mentions(text, entity_site)
            if mentions:
                counts["mentions"] += len(upsert_mentions(conn, node_id, mentions))
            for mention, other in extractor.equivalences(text, entity_site):
                record_equivalence(conn, mention, other, node_id)
            if not text.startswith("|") and " | " not in text[:200]:
                relations = graph.extract_relations(text, extractor, entity_site)
                counts["relations"] += graph.store_relations(conn, relations, node_id, document_id,
                                                             valid_from=doc_date or (str(year) if year else None))
        conn.executemany("INSERT INTO kf_fts(text, heading, title, node_id, document_id) VALUES (?,?,?,?,?)", fts_rows)

    if embed if embed is not None else _embedding_enabled():
        # Register rows are found by the register engine and by their codes, not by
        # meaning - embedding hundreds of near-identical rows would cost for nothing.
        counts["vectors"] = embed_nodes([(k, v) for k, v in node_texts.items() if k not in record_nodes])
    status_word = "updated" if existing else "ingested"
    result = IngestResult(str(path), status_word, document_id, title,
                          detail=f"{doc_type}, {authority.describe(rank)}, {classification}, "
                                 f"year {year if year else 'unknown'}"
                                 + (f" ({date_source})" if date_source else "")
                                 + f", {time.time() - started:.1f}s",
                          counts=counts, warnings=list(doc.warnings))
    return _log(result, run_id)


def embed_nodes(items: List[tuple], limit: int = 5000, budget_s: float = 900.0) -> int:
    """Vectors for nodes, through the same embedding provider the industrial
    knowledge base uses. Missing embeddings are not an error: retrieval still has
    full-text, entity, graph and table search, and says the vector leg is off."""
    try:
        import numpy as np
        from core.capabilities import embedding_provider
        model = embedding_provider.resolve() or ""
    except Exception as error:
        logger.info(f"embeddings skipped: {error}")
        return 0
    if not model:
        return 0
    stored, started = 0, time.time()
    with store.connection() as conn:
        for node_id, text in items[:limit]:
            if time.time() - started > budget_s:
                logger.info("embedding budget reached - run `rag reembed` for the rest")
                break
            vector = embedding_provider.embed(text[:2000])
            if not vector:
                continue
            array = np.asarray(vector, dtype="float32")
            conn.execute("INSERT OR REPLACE INTO kf_vectors(node_id, model, dim, vector) VALUES (?, ?, ?, ?)",
                         (node_id, model, int(array.size), array.tobytes()))
            stored += 1
    return stored


def _delete_document_rows(conn, document_id: str) -> None:
    node_ids = [r["node_id"] for r in conn.execute("SELECT node_id FROM kf_nodes WHERE document_id = ?", (document_id,))]
    if node_ids:
        for start in range(0, len(node_ids), 500):
            chunk = node_ids[start:start + 500]
            conn.execute(f"DELETE FROM kf_vectors WHERE node_id IN ({store.placeholders(chunk)})", chunk)
            conn.execute(f"DELETE FROM kf_mentions WHERE node_id IN ({store.placeholders(chunk)})", chunk)
    table_ids = [r["table_id"] for r in conn.execute("SELECT table_id FROM kf_tables WHERE document_id = ?", (document_id,))]
    for table_id in table_ids:
        conn.execute("DELETE FROM kf_cells WHERE table_id = ?", (table_id,))
        conn.execute("DELETE FROM kf_records WHERE register_id = ?", (table_id,))
    for table in ("kf_tables", "kf_registers", "kf_section_tags", "kf_facts", "kf_relations", "kf_nodes"):
        conn.execute(f"DELETE FROM {table} WHERE document_id = ?", (document_id,))
    conn.execute("DELETE FROM kf_fts WHERE document_id = ?", (document_id,))


def remove_document(document_id: str) -> Dict[str, Any]:
    with store.connection() as conn:
        row = conn.execute("SELECT title, source_path FROM kf_documents WHERE document_id = ?", (document_id,)).fetchone()
        if not row:
            return {"removed": False, "reason": "no such document"}
        _delete_document_rows(conn, document_id)
        conn.execute("DELETE FROM kf_documents WHERE document_id = ?", (document_id,))
        _drop_orphan_entities(conn)
    return {"removed": True, "document_id": document_id, "title": row["title"], "source_path": row["source_path"]}


def _drop_orphan_entities(conn) -> None:
    conn.execute("DELETE FROM kf_entities WHERE entity_id NOT IN (SELECT entity_id FROM kf_mentions) "
                 "AND entity_id NOT IN (SELECT src FROM kf_relations) AND entity_id NOT IN (SELECT dst FROM kf_relations) "
                 "AND entity_id NOT IN (SELECT entity_id FROM kf_facts WHERE entity_id IS NOT NULL) "
                 "AND entity_id NOT IN (SELECT entity_id FROM kf_aliases WHERE source = 'human')")
    conn.execute("DELETE FROM kf_aliases WHERE entity_id NOT IN (SELECT entity_id FROM kf_entities)")


_DEPARTMENT_RULES = (
    ("Human Resources", r"\bemployees?\b|\bhandbook\b|\bposh\b|\bpayroll\b|\bleave\s+policy\b|\bhr\b|"
                        r"\bhuman\s+resources?\b|\bappointment\s+letter\b|\bgrievance\b"),
    ("Finance", r"\bgst\b|\btds\b|\bincome\s+tax\b|\btax(?:es|ation)?\b|\binvoices?\b|\bfinance\b|\bitr\b|"
                r"\bchallans?\b|\bbudget\b"),
    ("Legal", r"\blease\b|\blessor\b|\blessee\b|\btitle\s+(?:deed|chain)\b|\bsale\s+deed\b|\bagreement\b|"
              r"\bgoverning\s+law\b|\bindemnit\w*\b|\bjurisdiction\b"),
    ("Procurement", r"\bvendors?\b|\bsuppliers?\b|\bpurchase\b|\bprocurement\b|\bmsa\b|\bsow\b|\bpurchase\s+orders?\b"),
    ("HSE", r"\behs\b|\bsafety\b|\bppe\b|\bloto\b|\bnear[\s-]?miss\b|\bhse\b|\bwork\s+permit\b"),
    ("Administration", r"\bpropert(?:y|ies)\b|\bfacilit(?:y|ies)\b|\bestates?\b|\bfire\s+noc\b|\bfactory\s+licen[cs]e\b"),
)
_DEPARTMENT_PATTERNS = [(name, re.compile(pattern, re.I)) for name, pattern in _DEPARTMENT_RULES]
# Personal identifiers that make a register a personnel record - restricted to HR.
_PERSONNEL = re.compile(r"(?:^|[\s_])(?:pan|aadhaa?r|uan|ctc|salary|basic|hra|pf|esi|passport|dob|birth)(?:$|[\s_])", re.I)


def infer_department(title: str, filename: str, text: str) -> Optional[str]:
    """The department a document belongs to, from its title and opening text.
    Only a clear lead counts - otherwise the department stays unknown."""
    name = f"{title} {Path(filename or '').stem.replace('_', ' ')}"
    scores = []
    for department, pattern in _DEPARTMENT_PATTERNS:
        score = 4 * len(pattern.findall(name)) + min(6, len(pattern.findall((text or "")[:6000])))
        scores.append((score, department))
    scores.sort(reverse=True)
    top, second = scores[0], scores[1]
    if top[0] >= 4 and top[0] >= 1.5 * max(1, second[0]):
        return top[1]
    return None


def _settle_document(conn, document_id: str, doc_type: str, date_source: Optional[str], department: Optional[str],
                     classification: str, found_registers: List[Dict[str, Any]], title: str, filename: str,
                     text_head: str, overrides: Dict[str, Any], origin: str, status: str,
                     ocr_confidence: Optional[float]) -> Dict[str, Any]:
    """What the parsed content shows about the document as a whole: a workbook
    of registers is a register (and its records carry their own dates); a
    register of personal data is confidential and restricted to HR; a document
    without a stated department gets one only when its title and text agree."""
    out: Dict[str, Any] = {"document_type": doc_type, "confidentiality": classification, "department": department}
    meta_notes: List[str] = []
    restricted = None
    workbook = Path(filename or "").suffix.lower() in (".xlsx", ".xlsm", ".xls", ".csv", ".ods")
    if found_registers and (workbook or doc_type in ("spreadsheet", "other")) and not overrides.get("document_type"):
        out["document_type"] = "register"            # its rows may mention 'lessor' - it is still a register
    if found_registers and date_source == "most frequent year in the text" and not overrides.get("year"):
        out["year_cleared"], out["date_source"] = True, "register - each record carries its own dates"
        conn.execute("UPDATE kf_documents SET year = NULL, quarter = NULL, month = NULL, fiscal_year = NULL, "
                     "time_start = NULL, time_end = NULL WHERE document_id = ?", (document_id,))
        conn.execute("UPDATE kf_nodes SET year = NULL WHERE document_id = ? AND kind IN ('document', 'table', 'row_range')",
                     (document_id,))
    elif date_source == "most frequent year in the text" and not overrides.get("year"):
        # A workbook whose tables run 1990-2026 month by month is not "a 1990 document"
        # because 1990 happens to be written most often: it spans its table periods.
        span = conn.execute("SELECT MIN(year), MAX(year) FROM kf_facts WHERE document_id = ? AND year IS NOT NULL",
                            (document_id,)).fetchone()
        if span and span[0] and span[1] and span[1] - span[0] > 5:
            out["year_cleared"], out["date_source"] = True, f"spans {span[0]}-{span[1]} (its table periods)"
            conn.execute("UPDATE kf_documents SET year = NULL, quarter = NULL, month = NULL, fiscal_year = NULL, "
                         "time_start = ?, time_end = ? WHERE document_id = ?", (span[0], span[1], document_id))
            conn.execute("UPDATE kf_nodes SET year = NULL WHERE document_id = ? AND kind IN ('document', 'table')",
                         (document_id,))
            for node in conn.execute("SELECT node_id, text FROM kf_nodes WHERE document_id = ? "
                                     "AND kind IN ('paragraph', 'row_range')", (document_id,)).fetchall():
                own = temporal.dominant_year(_YEAR_SPAN.sub(" ", node["text"] or ""))
                conn.execute("UPDATE kf_nodes SET year = ? WHERE node_id = ?",
                             (own if own and 1950 <= own <= 2049 else None, node["node_id"]))
    if not department:
        out["department"] = infer_department(title, filename, text_head)
        if out["department"]:
            meta_notes.append(f"department inferred from the title and text: {out['department']}")
    personal = sorted({c for reg in found_registers for c in reg["sensitive"] if _PERSONNEL.search(c)})
    if len(personal) >= 2 and not overrides.get("confidentiality"):
        if classification in ("public", "internal"):
            out["confidentiality"] = "confidential"
        out["department"] = "Human Resources"
        restricted = 1
        meta_notes.append("personal data columns (" + ", ".join(personal[:8]) + ") - confidential, "
                          "visible only to Human Resources members and administrators")
    out["authority"] = authority.rank(out["document_type"], status, origin, ocr_confidence)
    row = conn.execute("SELECT metadata FROM kf_documents WHERE document_id = ?", (document_id,)).fetchone()
    meta = store.loads(row["metadata"] if row else None, {}) or {}
    if meta_notes:
        meta["ingest_notes"] = meta.get("ingest_notes", []) + meta_notes
    if found_registers:
        meta["registers"] = found_registers
    if out.get("date_source"):
        meta["date_source"] = out["date_source"]
    meta["authority"] = authority.describe(out["authority"])
    conn.execute("UPDATE kf_documents SET document_type = ?, confidentiality = ?, department = ?, authority = ?, "
                 "department_restricted = CASE WHEN ? IS NULL THEN department_restricted ELSE ? END, metadata = ? "
                 "WHERE document_id = ?",
                 (out["document_type"], out["confidentiality"], out["department"], out["authority"],
                  restricted, restricted, store.dumps(meta), document_id))
    return out


def _log(result: IngestResult, run_id: str) -> IngestResult:
    try:
        with store.connection() as conn:
            conn.execute("INSERT INTO kf_ingestion_log(run_id, source_path, status, document_id, detail, at) "
                         "VALUES (?, ?, ?, ?, ?, ?)",
                         (run_id, result.source_path, result.status, result.document_id, result.detail[:500], store.now()))
    except Exception:
        pass
    level = logger.warning if result.status == "failed" else logger.info
    level(f"ingest {result.status}: {Path(result.source_path).name} - {result.detail}")
    return result


# ------------------------------------------------------------- bulk inputs ----

def ingest_directory(directory: str | Path, origin: str = ORIGIN_COMPANY, recursive: bool = True,
                     year: Optional[int] = None, force: bool = False,
                     progress: Optional[Callable[[int, int, IngestResult], None]] = None,
                     overrides: Optional[Dict[str, Any]] = None, embed: Optional[bool] = None) -> Dict[str, Any]:
    root = Path(directory).expanduser().resolve()
    if not root.is_dir():
        return {"ok": False, "error": f"'{root}' is not a folder"}
    files = sorted(p for p in (root.rglob("*") if recursive else root.glob("*"))
                   if p.is_file() and supported(p) and not p.name.startswith("~$"))
    run_id = uuid.uuid4().hex[:12]
    results: List[Dict[str, Any]] = []
    for index, path in enumerate(files, 1):
        per_file = dict(overrides or {})
        folder_year = year or _folder_year(path, root)
        if folder_year and "year" not in per_file:
            per_file["year_hint"] = folder_year
        result = ingest_document(path, origin=origin, force=force, overrides=per_file, run_id=run_id, embed=embed)
        if result.status in ("ingested", "updated") and folder_year:
            _apply_year_hint(result.document_id, folder_year)
        results.append(result.to_dict())
        if progress:
            progress(index, len(files), result)
    summary = {s: sum(1 for r in results if r["status"] == s) for s in ("ingested", "updated", "skipped", "failed")}
    return {"ok": summary["failed"] == 0, "run_id": run_id, "folder": str(root), "files": len(files),
            **summary, "results": results}


def _folder_year(path: Path, root: Path) -> Optional[int]:
    for part in path.relative_to(root).parts[:-1]:
        m = re.fullmatch(r"(?:fy\s*)?((?:19|20)\d{2})(?:\s*[-–]\s*\d{2,4})?", part.strip(), re.I)
        if m:
            return int(m.group(1))
    return None


def _apply_year_hint(document_id: Optional[str], year: int) -> None:
    """A document with no date of its own, filed under a year folder, takes the
    folder's year - labelled as such."""
    if not document_id:
        return
    with store.connection() as conn:
        row = conn.execute("SELECT year, metadata FROM kf_documents WHERE document_id = ?", (document_id,)).fetchone()
        if row and row["year"] is None:
            meta = store.loads(row["metadata"], {})
            meta["date_source"] = "archive folder name"
            conn.execute("UPDATE kf_documents SET year = ?, time_start = ?, time_end = ?, metadata = ? WHERE document_id = ?",
                         (year, year, year, store.dumps(meta), document_id))
            conn.execute("UPDATE kf_nodes SET year = ? WHERE document_id = ? AND year IS NULL", (year, document_id))


def ingest_year(directory: str | Path, year: int, **kwargs) -> Dict[str, Any]:
    return ingest_directory(directory, year=int(year), **kwargs)


def ingest_archive(archive: str | Path, origin: str = ORIGIN_COMPANY, **kwargs) -> Dict[str, Any]:
    """A .zip of documents: unpacked into the data directory (never next to the
    archive), each member path checked so nothing escapes the target folder."""
    from core.paths import data_dir
    archive = Path(archive).expanduser().resolve()
    if not archive.is_file() or archive.suffix.lower() != ".zip":
        return {"ok": False, "error": "give a .zip archive"}
    target = data_dir() / "knowledge_fabric_archives" / f"{archive.stem}_{time.strftime('%Y%m%d_%H%M%S')}"
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            destination = (target / member.filename).resolve()
            if not str(destination).startswith(str(target.resolve())):
                return {"ok": False, "error": f"unsafe path in archive: {member.filename}"}
        bundle.extractall(target)
    result = ingest_directory(target, origin=origin, **kwargs)
    result["archive"] = str(archive)
    return result


# ------------------------------------------------------------- maintenance ----

def reindex() -> Dict[str, Any]:
    """Rebuild the full-text index from the stored nodes."""
    with store.connection() as conn:
        conn.execute("DELETE FROM kf_fts")
        rows = conn.execute("SELECT n.node_id, n.document_id, n.text, n.heading, n.kind, d.title FROM kf_nodes n "
                            "JOIN kf_documents d ON d.document_id = n.document_id WHERE n.text IS NOT NULL AND n.text != ''").fetchall()
        conn.executemany("INSERT INTO kf_fts(text, heading, title, node_id, document_id) VALUES (?,?,?,?,?)",
                         [(r["text"], r["heading"] or "", r["title"] or "", r["node_id"], r["document_id"]) for r in rows])
    return {"ok": True, "indexed_nodes": len(rows)}


def reembed(limit: int = 5000, model_change: bool = False) -> Dict[str, Any]:
    with store.connection() as conn:
        if model_change:
            conn.execute("DELETE FROM kf_vectors")
        rows = conn.execute("SELECT n.node_id, n.text FROM kf_nodes n LEFT JOIN kf_vectors v ON v.node_id = n.node_id "
                            "WHERE v.node_id IS NULL AND n.kind NOT IN ('section', 'document', 'record') "
                            "AND n.text IS NOT NULL AND length(n.text) > 20 LIMIT ?", (limit,)).fetchall()
    stored = embed_nodes([(r["node_id"], r["text"]) for r in rows], limit=limit)
    return {"ok": True, "candidates": len(rows), "embedded": stored,
            "note": "" if stored or not rows else "no embedding model reachable - vector search stays off"}


def rebuild_graph() -> Dict[str, Any]:
    """Re-extract entities and relations from every node, then re-apply the human
    corrections log so a person's merges and aliases survive the rebuild."""
    extractor = Extractor()
    counts = {"nodes": 0, "mentions": 0, "relations": 0, "corrections_reapplied": 0}
    with store.connection() as conn:
        human = conn.execute("SELECT action, payload, username FROM kf_corrections ORDER BY correction_id").fetchall()
        conn.execute("DELETE FROM kf_mentions")
        conn.execute("DELETE FROM kf_relations")
        conn.execute("DELETE FROM kf_aliases WHERE source != 'human'")
        conn.execute("DELETE FROM kf_entities WHERE entity_id NOT IN (SELECT entity_id FROM kf_aliases)")
        docs = {r["document_id"]: r for r in conn.execute("SELECT document_id, metadata, doc_date, year FROM kf_documents")}
        for node in conn.execute("SELECT node_id, document_id, text FROM kf_nodes WHERE kind NOT IN ('document', 'section') "
                                 "AND text IS NOT NULL").fetchall():
            doc = docs.get(node["document_id"])
            location = store.loads(doc["metadata"], {}).get("entity_site") if doc else None
            mentions = extractor.mentions(node["text"][:20000], location)
            counts["mentions"] += len(upsert_mentions(conn, node["node_id"], mentions))
            for mention, other in extractor.equivalences(node["text"], location):
                record_equivalence(conn, mention, other, node["node_id"])
            if " | " not in node["text"][:200]:
                relations = graph.extract_relations(node["text"][:20000], extractor, location)
                counts["relations"] += graph.store_relations(
                    conn, relations, node["node_id"], node["document_id"],
                    valid_from=(doc["doc_date"] or (str(doc["year"]) if doc and doc["year"] else None)) if doc else None)
            counts["nodes"] += 1
    from knowledge_fabric.entities import correct
    for row in human:
        try:
            correct(row["action"], store.loads(row["payload"], {}), row["username"] or "")
            counts["corrections_reapplied"] += 1
        except Exception as error:
            logger.warning(f"correction not re-applied ({row['action']}): {error}")
    return {"ok": True, **counts}


def validate_corpus() -> Dict[str, Any]:
    """Integrity of the store - problems are listed, not fixed silently."""
    findings: List[Dict[str, Any]] = []
    with store.connection() as conn:
        def add(kind, detail, count):
            if count:
                findings.append({"check": kind, "detail": detail, "count": count})
        add("documents_without_content", "documents with no nodes",
            conn.execute("SELECT COUNT(*) FROM kf_documents d WHERE NOT EXISTS (SELECT 1 FROM kf_nodes n WHERE n.document_id = d.document_id AND n.kind != 'document')").fetchone()[0])
        add("unindexed_nodes", "nodes with text missing from the full-text index",
            conn.execute("SELECT COUNT(*) FROM kf_nodes n WHERE n.text IS NOT NULL AND n.text != '' AND n.kind != 'section' "
                         "AND n.node_id NOT IN (SELECT node_id FROM kf_fts)").fetchone()[0])
        add("orphan_facts", "facts whose table no longer exists",
            conn.execute("SELECT COUNT(*) FROM kf_facts WHERE table_id IS NOT NULL AND table_id NOT IN (SELECT table_id FROM kf_tables)").fetchone()[0])
        add("duplicate_content", "documents with identical content (same hash, different paths)",
            conn.execute("SELECT COUNT(*) FROM (SELECT source_hash FROM kf_documents GROUP BY source_hash HAVING COUNT(*) > 1)").fetchone()[0])
        add("undated_documents", "documents with no year (they count as MISSING in coverage)",
            conn.execute("SELECT COUNT(*) FROM kf_documents WHERE year IS NULL").fetchone()[0])
        add("ambiguous_entities", "entities with conflicting descriptors - need a human decision",
            conn.execute("SELECT COUNT(*) FROM kf_entities WHERE status = 'ambiguous'").fetchone()[0])
        add("low_ocr_documents", "scanned documents with mean OCR confidence < 0.6 (authority lowered)",
            conn.execute("SELECT COUNT(*) FROM kf_documents WHERE json_extract(metadata, '$.mean_ocr_confidence') < 0.6").fetchone()[0])
        dims = conn.execute("SELECT COUNT(DISTINCT dim) FROM kf_vectors").fetchone()[0]
        add("mixed_vector_dimensions", "vectors from different embedding models - run `rag reembed --model-change`",
            dims - 1 if dims > 1 else 0)
        missing_files = sum(1 for r in conn.execute("SELECT source_path FROM kf_documents")
                            if r["source_path"] and not Path(r["source_path"]).exists())
        add("source_file_missing", "source files no longer on disk (content is still indexed)", missing_files)
        totals = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("kf_documents", "kf_nodes", "kf_tables", "kf_facts", "kf_entities", "kf_relations", "kf_vectors")}
    return {"ok": not any(f["check"] in ("unindexed_nodes", "orphan_facts", "mixed_vector_dimensions") for f in findings),
            "findings": findings, "totals": totals}
