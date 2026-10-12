"""
Regression tests for the industrial workbench (SIH26117).

Runs entirely offline with an isolated database and a deterministic fake
embedding, so the retrieval, ranking and citation logic is exercised for real
without needing Ollama running.
"""

import random
import shutil
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memory import database  # noqa: E402

# Isolated database - never touch the real one.
database.DB_PATH = Path("/tmp/medha_test_industrial.db")
if database.DB_PATH.exists():
    database.DB_PATH.unlink()
database.init_db()

from industrial import doc_pipeline, ocr_engines, provenance, reranker, schemas  # noqa: E402
from industrial.extraction import (CLAIM_HISTORICAL_FACT, CLAIM_USER_DOCUMENT,  # noqa: E402
                                   CLASS_KNOWLEDGE_CORPUS, chunk_document,
                                   extract_and_chunk, normalize_document, parse_date)
from industrial.knowledge_base import knowledge_base  # noqa: E402
from industrial.taxonomy import get_taxonomy  # noqa: E402

WORK = Path("/tmp/medha_test_industrial_docs")

INSPECTION_TEXT = """EQUIPMENT INSPECTION REPORT
Document No: INSP-2024-118   Revision: 2   Date: 14-03-2024
Confidential
Equipment ID: P-101A
Unit: Crude Distillation Unit
Department: Mechanical Inspection
Location: CDU pump house, grade level
Inspector: R Menon
Inspection date: 14-03-2024
Operating condition: running, 2900 rpm, discharge 12.5 barg
Observations: External corrosion noted under insulation near the suction nozzle.
Vibration measured at 6.8 mm/s RMS on the drive end bearing.
Severity: high
Recommendation: Plan seal replacement and CUI removal at the next opportunity.
Required action: Raise work order for seal replacement within 30 days.
As per: SOP-MECH-014
"""


def _fake_embed(model, text, timeout=30.0, purpose=None):
    """Deterministic pseudo-embedding that genuinely discriminates topics, so
    ranking assertions are meaningful rather than trivially satisfied."""
    random.seed(hash(text) % 10000)
    vec = [random.random() * 0.1 for _ in range(24)]
    lowered = (text or "").lower()
    if any(w in lowered for w in ("corrosion", "insulation", "cui", "rust")):
        vec[0] += 5.0
    if any(w in lowered for w in ("vibration", "bearing", "pump", "rotating")):
        vec[1] += 5.0
    if any(w in lowered for w in ("seal", "replacement", "maintenance", "repair")):
        vec[2] += 5.0
    if any(w in lowered for w in ("refining", "refinery", "crude", "distillation")):
        vec[3] += 5.0
    return vec


def _make_pdf(path: Path, text: str) -> bool:
    try:
        import pymupdf as mupdf
    except ImportError:
        try:
            import fitz as mupdf
        except ImportError:
            return False
    doc = mupdf.open()
    page = doc.new_page()
    y = 70
    for line in text.splitlines():
        page.insert_text((55, y), line, fontsize=11)
        y += 17
    doc.save(str(path))
    doc.close()
    return True


def setup_module(module):
    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True, exist_ok=True)
    _make_pdf(WORK / "INSP-2024-118.pdf", INSPECTION_TEXT)
    database.init_db()
    database.clear_industrial_knowledge_base()


# ------------------------------------------------------------------ taxonomy

def test_taxonomy_expands_abbreviations():
    tax = get_taxonomy()
    assert tax.expand_abbreviation("P&ID") == "Piping and Instrumentation Diagram"
    assert tax.expand_abbreviation("DCS") == "Distributed Control System"
    assert tax.expand_abbreviation("FCC") == "Fluid Catalytic Cracking"
    assert tax.expand_abbreviation("HAZOP") == "Hazard and Operability Study"
    assert tax.expand_abbreviation("HSE") == "Health, Safety and Environment"


def test_taxonomy_resolves_synonyms_to_canonical_terms():
    tax = get_taxonomy()
    assert tax.canonical("cat cracker") == "fluid catalytic cracking"
    assert tax.canonical("CDU") == "crude distillation unit"
    assert tax.canonical("shell and tube exchanger") == "heat exchanger"


def test_taxonomy_detects_equipment_tags():
    tags = {t["tag"] for t in get_taxonomy().detect_equipment_tags(
        "Vibration high on P-101A; check E-2301 and PSV-1204.")}
    assert "P-101A" in tags
    assert "E-2301" in tags
    assert "PSV-1204" in tags


def test_query_expansion_adds_related_terms():
    expansion = get_taxonomy().expand_query("FCC riser coking")
    assert "fluid catalytic cracking" in expansion["terms"]
    assert any("Fluid Catalytic Cracking" in e for e in expansion["expansions"])


# ------------------------------------------------------- extraction pipeline

def test_pdf_extraction_preserves_page_and_section():
    norm, chunks, raw = extract_and_chunk(WORK / "INSP-2024-118.pdf")
    assert raw.error is None
    assert chunks, "no chunks produced"
    for c in chunks:
        assert c["page_number"] == 1
        assert c["source_location"].startswith("INSP-2024-118.pdf#")
        assert c["extraction_method"]
        assert c["claim_type"] == CLAIM_USER_DOCUMENT


def test_document_is_classified_as_inspection_report():
    norm, chunks, raw = extract_and_chunk(WORK / "INSP-2024-118.pdf")
    assert norm.document_type == "inspection_report"


def test_metadata_comes_from_the_document_not_the_file_properties():
    norm, chunks, raw = extract_and_chunk(WORK / "INSP-2024-118.pdf")
    assert norm.revision == "2"
    assert norm.doc_date == "2024-03-14"
    assert norm.equipment == "P-101A"
    assert norm.confidentiality == "confidential"
    assert norm.author == "R Menon"


def test_schema_fields_carry_their_own_evidence():
    norm, chunks, raw = extract_and_chunk(WORK / "INSP-2024-118.pdf")
    fields = norm.metadata["schema_fields"]
    assert fields["equipment_id"]["value"] == "P-101A"
    assert fields["severity"]["value"] == "high"
    assert "seal replacement" in fields["recommendation"]["value"]
    for name, value in fields.items():
        assert "method" in value, f"{name} has no extraction method"


def test_validation_reports_rather_than_silently_fixing():
    results = schemas.validate_fields("inspection_report", {})
    assert any(r["field"] == "equipment_id" and r["status"] == "fail" for r in results)
    # policy rules are surfaced, not silently passed
    assert any(r["status"] == "policy" for r in results)


def test_date_parser_refuses_to_guess():
    assert parse_date("14-03-2024") == "2024-03-14"
    assert parse_date("2024-03-14") == "2024-03-14"
    assert parse_date("not a date") is None
    assert parse_date("") is None


def test_unsupported_file_fails_cleanly():
    bad = WORK / "thing.xyz"
    bad.write_text("nonsense")
    doc = doc_pipeline.extract_document(bad)
    assert doc.error and "unsupported" in doc.error.lower()


def test_missing_file_does_not_raise():
    doc = doc_pipeline.extract_document(WORK / "does_not_exist.pdf")
    assert doc.error == "file does not exist"


# -------------------------------------------------------------------- OCR

def test_ocr_engine_layer_is_available_and_reports_itself():
    report = ocr_engines.engine_report()
    assert isinstance(report["available"], list)
    assert report["order"][0] == "paddleocr"      # strongest first
    assert "tesseract" in report["order"]         # fallback never removed
    assert report["layout_analyser"] in ("PP-Structure", "heuristic (geometric)")


def test_scanned_pdf_goes_through_ocr_with_confidence():
    if not ocr_engines.available_engines():
        return  # no OCR engine on this machine - nothing to assert
    try:
        import pymupdf as mupdf
    except ImportError:
        return
    src = mupdf.open(str(WORK / "INSP-2024-118.pdf"))
    out = mupdf.open()
    for page in src:
        pix = page.get_pixmap(dpi=170)
        new = out.new_page(width=page.rect.width, height=page.rect.height)
        new.insert_image(new.rect, stream=pix.tobytes("png"))
    scanned = WORK / "SCANNED.pdf"
    out.save(str(scanned))
    out.close()
    src.close()

    norm, chunks, raw = extract_and_chunk(scanned)
    assert raw.is_scanned, "scanned page was not detected as scanned"
    assert chunks
    assert raw.mean_ocr_confidence() is not None
    assert any(c.get("ocr_confidence") is not None for c in chunks)


# --------------------------------------------------- knowledge base + ranking

def test_ingest_search_and_evidence_provenance():
    with patch("core.ollama_client.ollama_client.embed", side_effect=_fake_embed), \
         patch("core.ollama_client.ollama_client.is_available", return_value=True), \
         patch("core.ollama_client.ollama_client.list_models", return_value=["bge-m3"]):
        result = knowledge_base.ingest_document(WORK / "INSP-2024-118.pdf")
        assert result["status"] == "ok", result
        assert result["embedded"] > 0

        evidences, info = knowledge_base.search("corrosion under insulation", top_k=3)
        assert evidences, info
        top = evidences[0]
        assert top.evidence_id == "E1"
        assert top.filename == "INSP-2024-118.pdf"
        assert top.page_number == 1
        assert top.claim_type == CLAIM_USER_DOCUMENT
        assert top.extraction_method
        assert info["reranker"]["backend"] in ("hybrid_local", "cross_encoder_flagembedding",
                                               "cross_encoder_sentence_transformers")


def test_corpus_claim_types_stay_separate_from_user_documents():
    from industrial.corpus import corpus_chunks, parse_corpus_file, CORPUS_ROOT
    files = list(CORPUS_ROOT.rglob("petroleum_refining/*.md"))
    if not files:
        return
    doc = parse_corpus_file(files[0])
    assert doc is not None
    chunks = corpus_chunks(doc)
    assert chunks
    claim_types = {c["claim_type"] for c in chunks}
    assert CLAIM_HISTORICAL_FACT in claim_types
    assert CLAIM_USER_DOCUMENT not in claim_types
    for c in chunks:
        assert c["section"], "corpus chunk lost its section heading"


def test_reranker_puts_the_relevant_passage_first():
    rows = [
        {"chunk_text": "The cooling tower basin was cleaned during the shutdown.",
         "element_type": "text", "retrieval_score": 0.56, "document_class": "user_document"},
        {"chunk_text": "Pump P-101A vibration 6.8 mm/s on the drive end bearing, above the "
                       "4.5 mm/s alert level, with mechanical seal weepage.",
         "element_type": "text", "retrieval_score": 0.55, "document_class": "user_document"},
    ]
    ordered, info = reranker.rerank("high vibration on pump P-101A bearing", rows)
    assert "P-101A" in ordered[0]["chunk_text"], info


def test_user_documents_outrank_corpus_at_equal_relevance():
    text = "Corrosion under insulation was found near the suction nozzle."
    user = {"chunk_text": text, "element_type": "text", "retrieval_score": 0.6,
            "document_class": "user_document"}
    corpus = {"chunk_text": text, "element_type": "text", "retrieval_score": 0.6,
              "document_class": "knowledge_corpus"}
    ordered, _ = reranker.rerank("corrosion under insulation", [corpus, user])
    assert ordered[0]["document_class"] == "user_document"


# ------------------------------------------------------ citation enforcement

def _evidence(n=2):
    return [provenance.Evidence(evidence_id=f"E{i}", document_id="d", filename="report.pdf",
                                chunk_text="text", page_number=i) for i in range(1, n + 1)]


def test_citation_validator_flags_invented_evidence_ids():
    report = provenance.CitationValidator(_evidence(2)).validate(
        "Thickness was 8.2 mm [E1]. The seal was replaced last year [E7].")
    assert "E1" in report.valid_citations
    assert "E7" in report.invalid_citations
    assert report.grounded is False
    assert "[citation removed" in report.cleaned_answer or "E7" not in report.cleaned_answer


def test_citation_validator_flags_invented_filenames():
    report = provenance.CitationValidator(_evidence(1)).validate(
        "As stated in maintenance_log_2019.pdf the pump was overhauled [E1].")
    assert "maintenance_log_2019.pdf" in report.invented_filenames
    assert report.grounded is False


def test_citation_validator_accepts_a_properly_grounded_answer():
    report = provenance.CitationValidator(_evidence(2)).validate(
        "Wall thickness was 8.2 mm at CML-01 [E1]. Vibration was 6.8 mm/s [E2].")
    assert report.grounded is True
    assert report.invalid_citations == []
    assert report.uncited_claim_sentences == []


def test_uncited_numeric_claims_are_reported():
    report = provenance.CitationValidator(_evidence(1)).validate(
        "The measured thickness was 7.1 mm.")
    assert report.uncited_claim_sentences


def test_grounding_contract_forbids_inventing_sources():
    contract = provenance.GROUNDING_CONTRACT.lower()
    assert "never cite an evidence id that is not in the list" in contract
    assert "knowledge corpus" in contract


# -------------------------------------------------------------- agent layer

def test_industrial_agents_are_implemented_not_pretending():
    """ZEUS implements the ten agents. With nothing to work on, each must say
    so - never report success and never crash."""
    from industrial.agent_interfaces import INDUSTRIAL_AGENTS, FIRST_AGENT
    assert FIRST_AGENT == "inspection"
    assert set(INDUSTRIAL_AGENTS) == {"inspection", "industrial_document", "engineering",
                                      "industrial_coding", "calculation", "report_approval",
                                      "research", "planning", "safety", "operations"}
    for agent in INDUSTRIAL_AGENTS.values():
        assert agent.implemented is True
        assert agent.workflow, f"{agent.agent_id} has no declared workflow"
        result = agent.run(None)
        assert result.status == "blocked", (agent.agent_id, result.status)


def test_inspection_agent_workflow_matches_the_required_sequence():
    from industrial.agent_interfaces import InspectionAgent
    workflow = InspectionAgent().workflow
    for step in ("ocr_or_vision_pages", "extract_schema_fields",
                 "retrieve_industrial_knowledge", "retrieve_sop_and_manuals",
                 "rank_evidence", "produce_findings", "produce_recommendations",
                 "draft_approval_note", "verify_citations_and_schema", "generate_docx"):
        assert step in workflow


def test_finding_without_evidence_is_marked_as_inference():
    from industrial.agent_interfaces import Finding
    assert Finding(statement="Seal is leaking").is_inference is True
    assert Finding(statement="Seal is leaking", evidence_ids=["E1"]).is_inference is False


# ------------------------------------------------- existing behaviour intact

def test_existing_medha_tools_are_still_registered():
    from core.tool_registry import registry
    from plugins.plugin_loader import discover_plugins
    discover_plugins()   # 'calculate' comes from the plugin layer, as it always did
    names = {t.name for t in registry.list_tools()}
    for original in ("index_folder", "ask_knowledge_base", "list_knowledge_sources",
                     "write_document", "create_presentation", "create_spreadsheet",
                     "read_screen", "analyze_image", "run_diagnostics", "open_application",
                     "calculate", "take_screenshot"):
        assert original in names, f"existing tool '{original}' disappeared"


def test_industrial_tools_are_added_not_substituted():
    from core.tool_registry import registry
    names = {t.name for t in registry.list_tools()}
    for added in ("industrial_status", "ingest_industrial_document", "ask_industrial_kb",
                  "search_industrial_evidence", "industrial_term", "list_industrial_agents"):
        assert added in names


def test_original_rag_table_is_untouched():
    conn = database.get_connection()
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(document_chunks)")}
    finally:
        conn.close()
    assert cols == {"id", "source_path", "chunk_index", "chunk_text", "embedding", "indexed_at"}


def test_no_network_framework_is_imported():
    forbidden = {"fastapi", "flask", "uvicorn", "django", "starlette", "tornado",
                 "sanic", "bottle", "gunicorn"}
    assert not (forbidden & set(sys.modules))


def test_model_registry_declares_capabilities_for_the_installed_models():
    from industrial.model_registry import get_registry
    table = {row["model"]: row for row in get_registry().table()}
    assert table["bge-m3"]["embedding"] is True
    assert table["bge-reranker-v2-m3"]["reranking"] is True
    assert table["qwen2.5vl:3b-q4_K_M"]["vision"] is True
    assert table["qwen2.5-coder:7b-instruct-q4_K_M"]["coding"] is True
    assert table["deepseek-r1:7b-qwen-distill-q4_K_M"]["reasoning"] is True
    for row in table.values():
        assert row["size"] and row["quantization"] and row["location"]


def test_startup_check_survives_a_missing_document_library():
    """The readiness check must REPORT a missing library, never crash on it.

    The environment audit stores None for a library that is not installed, so
    `report[...].get("PyMuPDF", "")` returned None and `"NONE" not in None`
    raised TypeError - the traceback then aborted the launcher the check exists
    to protect. Run the real script with PyMuPDF made unimportable.
    """
    import subprocess
    root = str(Path(__file__).resolve().parent.parent)
    program = (
        "import importlib.abc, sys\n"
        "class Blocker(importlib.abc.MetaPathFinder):\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name.split('.')[0].lower() in ('fitz', 'pymupdf'):\n"
        "            raise ModuleNotFoundError(name)\n"
        "        return None\n"
        "sys.meta_path.insert(0, Blocker())\n"
        f"sys.path.insert(0, {root!r})\n"
        f"sys.path.insert(0, {root + '/scripts'!r})\n"
        "sys.argv = ['startup_check.py']\n"
        "import startup_check\n"
        "startup_check.main()\n"
    )
    done = subprocess.run([sys.executable, "-c", program], capture_output=True,
                          text=True, timeout=180)
    combined = done.stdout + done.stderr
    assert "Traceback" not in combined, combined[-2000:]
    assert "TypeError" not in combined, combined[-2000:]
    assert "PyMuPDF missing" in done.stdout, done.stdout[-2000:]
