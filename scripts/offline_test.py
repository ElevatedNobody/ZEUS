"""
Phase I - offline verification.

    python scripts/offline_test.py

Verifies that ZEUS's industrial workbench works with Wi-Fi, Ethernet and VPN
disconnected. The test itself makes NO external network call: before anything
runs, it installs a socket guard that allows loopback (the local Ollama daemon)
and raises on any attempt to reach an external address. If any part of ZEUS
tried to phone home during this test, the External Dependency Check fails and
names the destination.

Checks
    Ollama .................. local daemon reachable on loopback
    Local Models ............ required model slots resolve to pulled models
    PDF Engine .............. PyMuPDF opens and reads a generated PDF
    OCR ..................... an OCR engine reads text from a generated image
    Vision Pipeline ......... image handling + local vision model availability
    Voice providers ......... local speech in/out, or TEXT MODE (both valid)
    Embedding Model ......... BGE-M3 returns a real vector for real text
    Reranker ................ evidence ranking backend works and orders results
    Knowledge Base .......... industrial KB stores, retrieves and cites
    Document Generation ..... DOCX, XLSX and PPTX are produced locally
    External Dependency Check no non-loopback network use, no web server stack

Options
    --deep      also run a live chat and vision inference (slower)
    --json PATH write the full result as JSON
    --quiet     print only the summary block
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import socket
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PASS, WARN, FAIL, SKIP = "PASS", "WARN", "FAIL", "SKIP"

# --------------------------------------------------------------------------
# Network guard - installed before any ZEUS import so nothing can slip out.
# --------------------------------------------------------------------------

_ALLOWED_HOSTS = {"127.0.0.1", "::1", "localhost", "0.0.0.0"}
_EXTERNAL_ATTEMPTS: list = []
_original_connect = socket.socket.connect
_original_connect_ex = socket.socket.connect_ex
_original_getaddrinfo = socket.getaddrinfo


def _is_local(address) -> bool:
    if isinstance(address, tuple) and address:
        host = str(address[0])
        return host in _ALLOWED_HOSTS or host.startswith("127.")
    return True  # unix sockets and the like are local by definition


def _guarded_connect(self, address):
    if not _is_local(address):
        _EXTERNAL_ATTEMPTS.append(str(address))
        raise OSError(f"OFFLINE TEST: blocked external connection to {address}")
    return _original_connect(self, address)


def _guarded_connect_ex(self, address):
    if not _is_local(address):
        _EXTERNAL_ATTEMPTS.append(str(address))
        return 1
    return _original_connect_ex(self, address)


def _guarded_getaddrinfo(host, *args, **kwargs):
    if host not in _ALLOWED_HOSTS and not str(host).startswith("127."):
        _EXTERNAL_ATTEMPTS.append(f"dns:{host}")
        raise socket.gaierror(f"OFFLINE TEST: blocked DNS lookup for {host}")
    return _original_getaddrinfo(host, *args, **kwargs)


def install_network_guard():
    socket.socket.connect = _guarded_connect
    socket.socket.connect_ex = _guarded_connect_ex
    socket.getaddrinfo = _guarded_getaddrinfo
    # Belt and braces: tell libraries that respect these to stay offline.
    for var in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_DATASETS_OFFLINE"):
        os.environ.setdefault(var, "1")
    for var in ("DO_NOT_TRACK", "SCARF_NO_ANALYTICS"):
        os.environ.setdefault(var, "true")
    os.environ.setdefault("UNSTRUCTURED_ANALYTICS", "false")


install_network_guard()

# --------------------------------------------------------------------------


class Result:
    def __init__(self, name: str):
        self.name = name
        self.status = SKIP
        self.detail = ""
        self.evidence: dict = {}
        self.seconds = 0.0

    def set(self, status, detail="", **evidence):
        self.status = status
        self.detail = detail
        self.evidence.update(evidence)
        return self

    def to_dict(self):
        return {"check": self.name, "status": self.status, "detail": self.detail,
                "seconds": round(self.seconds, 2), "evidence": self.evidence}


def timed(fn, result: Result, *args, **kwargs):
    start = time.perf_counter()
    try:
        fn(result, *args, **kwargs)
    except Exception as e:
        result.set(FAIL, f"{type(e).__name__}: {e}")
    result.seconds = time.perf_counter() - start
    return result


# ------------------------------------------------------------------ checks

def check_ollama(r: Result):
    from core.ollama_client import ollama_client
    host = ollama_client.host
    if not host.startswith(("http://localhost", "http://127.0.0.1")):
        r.set(FAIL, f"Ollama host is not loopback: {host}", host=host)
        return
    if ollama_client.is_available(force=True):
        models = ollama_client.list_models()
        r.set(PASS, f"local daemon reachable at {host} with {len(models)} model(s)",
              host=host, model_count=len(models))
    else:
        r.set(FAIL, f"daemon not reachable at {host} - start it with 'ollama serve'",
              host=host)


def check_models(r: Result):
    from industrial.model_registry import get_registry
    registry = get_registry()
    health = registry.health_check(write=True)
    if health["ollama_daemon"] != "reachable":
        r.set(FAIL, "cannot verify models - Ollama not reachable")
        return
    slots = health["slots"]
    blockers = [s for s, v in slots.items() if v["status"] == "BLOCKER"]
    degraded = [s for s, v in slots.items() if v["status"] == "degraded"]
    detail = (f"{health['present']}/{len(registry.models)} registered models present; "
              f"slots resolved: "
              + ", ".join(f"{s}={v['resolved'] or 'none'}" for s, v in slots.items()))
    if blockers:
        r.set(FAIL, f"required slot(s) unavailable: {', '.join(blockers)}", slots=slots)
    elif degraded:
        r.set(WARN, detail + f" | degraded: {', '.join(degraded)}", slots=slots)
    else:
        r.set(PASS, detail, slots=slots)


def _make_test_pdf(path: Path) -> bool:
    text = ("EQUIPMENT INSPECTION REPORT\nEquipment ID: P-101A\nRevision: 2\n"
            "Inspection date: 14-03-2024\nMeasured thickness 8.2 mm at CML-01.")
    try:
        import pymupdf as mupdf
    except ImportError:
        try:
            import fitz as mupdf
        except ImportError:
            return False
    doc = mupdf.open()
    page = doc.new_page()
    page.insert_text((60, 90), text, fontsize=13)
    doc.save(str(path))
    doc.close()
    return True


def check_pdf_engine(r: Result, workdir: Path):
    pdf = workdir / "offline_test.pdf"
    if not _make_test_pdf(pdf):
        r.set(FAIL, "no PDF engine available - pip install PyMuPDF")
        return
    from industrial.doc_pipeline import extract_document, pipeline_status
    doc = extract_document(pdf)
    engine = pipeline_status()["pdf_engine"]
    if doc.error:
        r.set(FAIL, f"{engine} could not read the generated PDF: {doc.error}")
        return
    if "P-101A" not in doc.text:
        r.set(FAIL, f"{engine} read the PDF but the expected text was missing")
        return
    r.set(PASS, f"{engine} wrote and read a PDF, {len(doc.elements)} element(s) extracted",
          engine=engine, characters=len(doc.text))


def _make_test_image(path: Path) -> bool:
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return False
    img = Image.new("RGB", (900, 260), "white")
    d = ImageDraw.Draw(img)
    # Default bitmap font is small but reliably OCR-able when scaled up.
    tmp = Image.new("RGB", (450, 130), "white")
    ImageDraw.Draw(tmp).text((10, 20), "INSPECTION REPORT P-101A\nTHICKNESS 8.2 MM",
                             fill="black")
    img.paste(tmp.resize((900, 260), Image.LANCZOS), (0, 0))
    img.save(path)
    return True


def check_ocr(r: Result, workdir: Path):
    from industrial import ocr_engines
    engines = ocr_engines.available_engines()
    if not engines:
        r.set(FAIL, "no OCR engine usable - install Tesseract (and pytesseract) "
                    "or PaddleOCR")
        return
    image_path = workdir / "offline_test_ocr.png"
    if not _make_test_image(image_path):
        r.set(WARN, f"engines available ({', '.join(engines)}) but Pillow is missing "
                    f"so OCR could not be exercised")
        return
    from PIL import Image
    result = ocr_engines.run_ocr(Image.open(image_path))
    text = result.text.upper().replace(" ", "")
    hit = "P-101A".replace("-", "") in text.replace("-", "") or "INSPECTION" in text
    detail = (f"engines available: {', '.join(engines)}; used {result.engine} "
              f"at {result.confidence:.2f} confidence")
    if result.ok and hit:
        r.set(PASS, detail, engines=engines, engine_used=result.engine,
              confidence=round(result.confidence, 3), text=result.text[:120])
    elif result.ok:
        r.set(WARN, detail + " - text was read but did not match the expected string",
              engines=engines, text=result.text[:120])
    else:
        r.set(FAIL, f"engines available ({', '.join(engines)}) but OCR returned nothing: "
                    f"{result.error}", engines=engines)


def check_vision(r: Result, workdir: Path, deep: bool):
    from industrial.doc_pipeline import vision_available
    try:
        from PIL import Image  # noqa: F401
        pillow = True
    except ImportError:
        pillow = False
    if not pillow:
        r.set(FAIL, "Pillow is not installed - image handling unavailable")
        return
    if not vision_available():
        r.set(WARN, "image processing works; no local vision model pulled, so multimodal "
                    "page understanding is disabled (OCR path still works)")
        return
    if not deep:
        r.set(PASS, "image processing works and a local vision model is available "
                    "(run with --deep to exercise it)")
        return
    from PIL import Image
    from industrial.doc_pipeline import describe_with_vision
    image_path = workdir / "offline_test_ocr.png"
    if not image_path.exists():
        _make_test_image(image_path)
    out = describe_with_vision(Image.open(image_path), 1)
    if out:
        r.set(PASS, "local vision model returned a description", sample=out[:160])
    else:
        r.set(WARN, "vision model is present but returned nothing")


def check_voice(r: Result, deep: bool):
    """What this machine can hear and say, verified rather than assumed.

    Voice is OPTIONAL: no provider installed is TEXT MODE, which is a
    supported configuration and reports as a PASS with the reason. A provider
    that is present and BROKEN is the failure worth catching.

    Nothing here downloads. Every provider loads from an explicit local path,
    and the offline flags are set before any ML library is imported.
    """
    from voice.offline import enforce_offline
    from voice.providers import ProviderState, VoiceState, VoiceManager

    enforce_offline()
    manager = VoiceManager()
    probes = manager.probe()
    evidence = {name: report.state.value for name, report in probes.items()}

    for name, report in probes.items():
        if report.requires_download:
            r.set(FAIL, f"provider '{name}' would download at runtime", **evidence)
            return

    if not deep:
        possible = [name for name, report in probes.items()
                    if report.state is ProviderState.NOT_PROBED]
        if possible:
            r.set(PASS, f"local providers available to load: {', '.join(sorted(possible))} "
                        f"(run with --deep to load them)", **evidence)
        else:
            r.set(PASS, "no local voice provider installed - TEXT MODE, "
                        "which is a supported configuration", **evidence)
        return

    state = manager.start()
    detail = manager.detail
    evidence["state"] = state.value
    evidence["speaking_with"] = manager.output.name if manager.output else None
    evidence["listening_with"] = manager.input.name if manager.input else None

    if state is VoiceState.ERROR:
        r.set(FAIL, f"a voice provider is present but failed: {detail}", **evidence)
    elif state in (VoiceState.AVAILABLE, VoiceState.DEGRADED):
        r.set(PASS, detail, **evidence)
    else:
        r.set(PASS, f"TEXT MODE - {detail}", **evidence)
    manager.close()


def check_embedding(r: Result):
    """Embedding through its own interface, checked with a real call.

    The distinction this reports is the one that matters when it fails:
    NO MODEL PULLED, MODEL DETECTED / ADAPTER REQUIRED, and RUNTIME NOT
    REACHABLE are three different problems with three different fixes, and
    were previously all reported as "no models available for task embedding".
    """
    from core.capabilities import Health, embedding_provider

    report = embedding_provider.check()
    evidence = {"model": report.model or "none", "interface": report.interface,
                **(report.evidence or {})}
    if report.health is Health.READY:
        r.set(PASS, report.detail, **evidence)
    elif report.health is Health.RUNTIME_DOWN:
        r.set(FAIL, report.detail, **evidence)
    else:
        r.set(FAIL, report.line(), **evidence)


def check_reranker(r: Result):
    from industrial import reranker
    status = reranker.status()
    rows = [
        {"chunk_text": "The cooling tower basin was cleaned during the shutdown.",
         "element_type": "text", "retrieval_score": 0.55, "document_class": "user_document"},
        {"chunk_text": "Pump P-101A showed vibration of 6.8 mm/s on the drive end bearing, "
                       "above the 4.5 mm/s alert level, with mechanical seal weepage.",
         "element_type": "text", "retrieval_score": 0.54, "document_class": "user_document"},
    ]
    ordered, info = reranker.rerank("high vibration on pump P-101A bearing", rows)
    if not ordered:
        r.set(FAIL, "reranker returned nothing")
        return
    correct = "P-101A" in ordered[0]["chunk_text"]
    detail = f"backend: {info['backend']}"
    if status["cross_encoder_active"]:
        detail += " (BGE cross-encoder)"
    else:
        detail += " (deterministic hybrid - place BGE-Reranker-v2-M3 in models/reranker "
        detail += "for cross-encoder ranking)"
    if correct:
        r.set(PASS, detail + "; ranked the relevant passage first", reranker=status)
    else:
        r.set(WARN, detail + "; did NOT rank the relevant passage first", reranker=status)


def check_knowledge_base(r: Result, workdir: Path):
    from industrial.knowledge_base import knowledge_base
    from memory.database import init_db
    init_db()
    stats = knowledge_base.stats()

    if not stats["chunks"]:
        r.set(WARN, "industrial knowledge base is empty - run "
                    "scripts/ingest_industrial_knowledge.py", **{
                        k: stats[k] for k in ("documents", "chunks", "embedded_chunks")})
        return
    if not stats["embedded_chunks"]:
        r.set(WARN, f"{stats['chunks']} chunks stored but none embedded - re-run ingestion "
                    f"with Ollama running", **{k: stats[k] for k in ("documents", "chunks")})
        return
    if not stats["embedding_ready"]:
        r.set(WARN, f"{stats['chunks']} chunks stored; retrieval unavailable: "
                    f"{stats['embedding_detail']}")
        return

    evidences, info = knowledge_base.search("corrosion under insulation", top_k=3)
    if not evidences:
        r.set(WARN, f"knowledge base holds {stats['chunks']} chunks but the test query "
                    f"returned nothing ({info.get('error') or info.get('note')})")
        return
    top = evidences[0]
    has_provenance = bool(top.filename) and bool(top.claim_type)
    detail = (f"{stats['documents']} documents / {stats['chunks']} chunks "
              f"({stats['embedded_chunks']} embedded); top match: {top.citation()} "
              f"[{top.claim_type}]")
    r.set(PASS if has_provenance else WARN, detail,
          documents=stats["documents"], chunks=stats["chunks"],
          reranker=(info.get("reranker") or {}).get("backend"),
          top_evidence=top.provenance())


def check_plant_layer(r: Result, workdir: Path):
    """The industrial data layer and its data-driven reports, offline.

    This is the path that must keep working when Ollama is down: equipment,
    maintenance, alerts and inventory come from the local database, and the
    DOCX/XLSX/PPTX built from them need no model at all.
    """
    import sqlite3
    from memory import database as _db

    original = _db.DB_PATH
    try:
        _db.DB_PATH = workdir / "plant_offline.db"
        _db.init_db()
        from industrial import plant_data as _pd
        from industrial import plant_docs as _docs
        from industrial import plant_ops as _ops
        from core import office as _office

        _pd._INITIALISED = False
        _pd.init_plant_schema(force=True)
        seeded = _pd.seed_demo_data(force=True)

        summary = _ops.operations_summary()
        alerts = _ops.alert_overview()
        if not summary["equipment_total"] or not alerts["alert_count"]:
            return r.set("FAIL", detail="plant tables did not populate")

        original_dir = _office.documents_dir
        _office.documents_dir = lambda subfolder="": workdir
        _docs.office.documents_dir = _office.documents_dir
        try:
            report = _docs.maintenance_report_docx("P-204", narrative=False, open_after=False)
            sheet = _docs.export_dataset_xlsx("maintenance", open_after=False)
            deck = _docs.operations_deck_pptx(narrative=False, open_after=False)
        finally:
            _office.documents_dir = original_dir
            _docs.office.documents_dir = original_dir

        for label, produced in (("DOCX", report), ("XLSX", sheet), ("PPTX", deck)):
            if not produced.get("verified"):
                return r.set("FAIL",
                             detail=f"{label} report failed verification: "
                                    f"{produced.get('error')}")

        sources = _pd.data_sources()
        return r.set("PASS", detail=(
            f"{summary['equipment_total']} equipment, {alerts['alert_count']} open alerts, "
            f"reports built with no model: DOCX {report['bytes']}B, XLSX {sheet['bytes']}B, "
            f"PPTX {deck['bytes']}B; {sources['demo_rows']} DEMO rows labelled"))
    except Exception as e:
        return r.set("FAIL", detail=f"{type(e).__name__}: {e}")
    finally:
        _db.DB_PATH = original


def check_orchestration(r: Result, workdir: Path):
    """The pipeline itself, with Ollama assumed down.

    What must hold offline: a plant query is classified, planned, routed to NO
    model, executed, validated and audited; a corrupt artefact is rejected by
    its validator rather than reported as a success; and a repair strategy is
    proposed for it. None of that may depend on a language model being up.
    """
    from memory import database as _db

    original = _db.DB_PATH
    try:
        _db.DB_PATH = workdir / "orchestration_offline.db"
        _db.init_db()
        from industrial import plant_data as _pd
        _pd._INITIALISED = False
        _pd.init_plant_schema(force=True)
        _pd.seed_demo_data(force=True)

        from core.orchestration import autofix as _autofix
        from core.orchestration import validators as _validators
        from core.orchestration.classifier import classify as _classify
        from core.orchestration.contracts import (
            Complexity as _Complexity, MedhaError as _MedhaError,
            PlanStep as _PlanStep, TaskKind as _TaskKind)
        from core.orchestration.router import capability_router as _router
        from core.decision_engine import DecisionEngine as _Engine

        # 1. deterministic routing - a database question must not want a model
        choice = _router.route_for(_TaskKind.INDUSTRIAL_DATA, _Complexity.SIMPLE)
        if not choice.deterministic:
            return r.set("FAIL", detail="a plant query was routed to a model")

        # 2. the whole pipeline, end to end, with no model available
        engine = _Engine()
        # No one is signed in during setup: run as the read-only diagnostic
        # identity (viewer role) instead of switching authentication off.
        from security.policy import diagnostic_scope
        with diagnostic_scope():
            outcome = engine.process("show the plant status")
        pipeline = outcome.get("pipeline") or {}
        stages = {entry["stage"] for entry in pipeline.get("stages", [])}
        required = {"classify", "memory", "plan", "route", "execute", "deliver"}
        if not required.issubset(stages):
            return r.set("FAIL",
                         detail=f"pipeline stages missing: {sorted(required - stages)}")
        if pipeline.get("model"):
            return r.set("FAIL",
                         detail=f"a model ({pipeline['model']}) was loaded for a "
                                f"database query")
        if "plant_status" not in pipeline.get("tools", []):
            return r.set("FAIL", detail="the command did not reach its tool")

        # 3. a corrupt artefact must be refused, and a repair proposed
        corrupt = workdir / "corrupt_check.pptx"
        corrupt.write_bytes(b"not a presentation")
        verdict = _validators.validate("pptx", str(corrupt))
        if verdict.ok:
            return r.set("FAIL", detail="a corrupt deck passed validation")
        repair = _autofix.plan_repair(
            verdict.error, _PlanStep(id="s1", tool="executive_presentation", args={}))
        if repair is None:
            return r.set("FAIL", detail="no repair strategy for a corrupt file")

        # 4. classification is deterministic - no model was consulted
        classified = _classify("create an excel workbook and a powerpoint for P-204")
        if classified.kind is not _TaskKind.MULTI_STEP:
            return r.set("FAIL", detail="multi-deliverable request not decomposed")

        return r.set("PASS", detail=(
            f"{len(stages)} stages recorded; plant query ran with no model; "
            f"{len(_validators.registered_kinds())} validators, "
            f"{len(_autofix.known_strategies())} repair strategies; "
            f"corrupt file refused and '{repair.strategy}' proposed"))
    except Exception as e:
        return r.set("FAIL", detail=f"{type(e).__name__}: {e}")
    finally:
        _db.DB_PATH = original


def check_document_generation(r: Result, workdir: Path):
    produced, missing = [], []

    try:
        from docx import Document
        p = workdir / "offline_test.docx"
        d = Document()
        d.add_heading("Inspection Findings", 0)
        d.add_paragraph("Equipment P-101A: seal weepage observed [E1].")
        t = d.add_table(rows=2, cols=2)
        t.rows[0].cells[0].text = "CML"
        t.rows[0].cells[1].text = "Thickness"
        t.rows[1].cells[0].text = "CML-01"
        t.rows[1].cells[1].text = "8.2 mm"
        d.save(p)
        produced.append(f"DOCX ({p.stat().st_size} bytes)")
    except Exception as e:
        missing.append(f"DOCX: {e}")

    try:
        import openpyxl
        p = workdir / "offline_test.xlsx"
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["CML", "Nominal mm", "Measured mm"])
        ws.append(["CML-01", 9.5, 8.2])
        wb.save(p)
        produced.append(f"XLSX ({p.stat().st_size} bytes)")
    except Exception as e:
        missing.append(f"XLSX: {e}")

    try:
        from pptx import Presentation
        p = workdir / "offline_test.pptx"
        prs = Presentation()
        slide = prs.slides.add_slide(prs.slide_layouts[1])
        slide.shapes.title.text = "Inspection Summary"
        slide.placeholders[1].text = "P-101A - recommend seal replacement"
        prs.save(p)
        produced.append(f"PPTX ({p.stat().st_size} bytes)")
    except Exception as e:
        missing.append(f"PPTX: {e}")

    if missing:
        r.set(FAIL if not produced else WARN,
              f"generated {', '.join(produced) or 'nothing'}; failed: {'; '.join(missing)}",
              produced=produced, failed=missing)
    else:
        r.set(PASS, "generated " + ", ".join(produced), produced=produced)


_NETWORK_FRAMEWORKS = ["fastapi", "flask", "uvicorn", "gunicorn", "django", "starlette",
                       "tornado", "bottle", "sanic", "websockets", "socketserver",
                       "http.server", "aiohttp.web", "zmq", "celery", "pymongo",
                       "psycopg2", "redis", "boto3", "google.cloud", "azure."]
_URL_RE = re.compile(r"https?://([A-Za-z0-9._\-]+)")
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}
# URLs that only ever appear in help text, comments or error messages - the
# "install this / read that" hints a setup script prints. They are documentation,
# not a dependency, and they are reported as notes rather than findings.
_DOC_URL_ALLOWLIST = {"ollama.com", "www.ollama.com", "python.org", "www.python.org",
                      "github.com", "www.google.com", "claude.ai", "docs.claude.com",
                      "cdnjs.cloudflare.com", "anthropic.com",
                      "download.pytorch.org", "huggingface.co", "hf.co"}

# Packages that run while ZEUS is running. A URL in EXECUTABLE code here is a
# real finding even if the same host is allowed in a setup script's help text -
# runtime code has no business naming a remote host. Comments and docstrings in
# these files are documentation and are ignored (see _code_urls).
_RUNTIME_PACKAGES = ("core", "voice", "industrial", "memory", "auth", "ui",
                     "automation", "plugins")

# Runtime URLs that are deliberate and are NOT a ZEUS network dependency.
# Each one needs a reason; anything else is a finding.
_RUNTIME_URL_EXCEPTIONS = {
    ("core/builtin_tools.py", "www.google.com"):
        "default homepage handed to the USER'S browser by the open_browser "
        "tool - ZEUS never fetches it itself",
}

_TRIPLE_QUOTES = ('"' * 3, "'" * 3)


def _code_urls(source: str) -> set:
    """URLs that appear in executable code, ignoring comments and the
    triple-quoted strings used for docstrings and help text."""
    import io
    import tokenize

    hosts = set()
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return set(_URL_RE.findall(source))     # unparseable: be strict
    for token in tokens:
        if token.type == tokenize.COMMENT or token.type != tokenize.STRING:
            continue
        stripped = token.string.lstrip("rbfuRBFU")
        if stripped.startswith(_TRIPLE_QUOTES):
            continue                            # docstring / help text
        hosts.update(_URL_RE.findall(token.string))
    return hosts


def check_external_dependencies(r: Result):
    findings, notes = [], []

    # 1. anything actually attempted during this run
    if _EXTERNAL_ATTEMPTS:
        findings.append("external connection attempted during the test: "
                        + ", ".join(sorted(set(_EXTERNAL_ATTEMPTS))[:5]))

    # 2. loaded modules that imply a network service
    loaded = set(sys.modules)
    for framework in _NETWORK_FRAMEWORKS:
        # Exact module name only. Matching on the package root would flag the
        # stdlib 'http' package, which httpx pulls in for ordinary loopback use.
        if framework in loaded:
            findings.append(f"network framework imported at runtime: {framework}")

    # 3. static scan of the project for non-loopback URLs
    #    tests/        skipped: fixtures use deliberately fake and LAN hosts
    #                  (example.invalid, 192.168.x) to exercise the egress guard
    #    third_party/  vendored code: reported (WARN), not counted as ZEUS's
    #                  own dependency - ZEUS uses InsightFace's FaceAnalysis with
    #                  local models only; its GUI/download helpers are not on
    #                  ZEUS's path, and the egress guard and firewall block them
    scanned = 0
    vendored = []
    for py in ROOT.rglob("*.py"):
        if any(part in {".venv", "venv", "__pycache__", "build", "dist", "site-packages"}
               for part in py.parts):
            continue
        relative = py.relative_to(ROOT)
        top = relative.parts[0] if relative.parts else ""
        if top == "tests":
            continue
        scanned += 1
        try:
            source = py.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        key = str(relative).replace("\\", "/")
        in_runtime_package = bool(relative.parts) and relative.parts[0] in _RUNTIME_PACKAGES
        hosts = _code_urls(source) if in_runtime_package else set(_URL_RE.findall(source))
        for host in hosts:
            if host in _LOCAL_HOSTS or host.startswith("127."):
                continue
            reason = _RUNTIME_URL_EXCEPTIONS.get((key, host))
            if reason:
                notes.append(f"{relative}: {host} - {reason}")
                continue
            if host in _DOC_URL_ALLOWLIST and not in_runtime_package:
                notes.append(f"{relative}: documentation URL {host}")
                continue
            if top == "third_party":
                vendored.append(f"{relative}: {host}")
                continue
            findings.append(f"{relative}: external URL {host}")

    # 4. confirm the model runtime is loopback
    try:
        from core.ollama_client import ollama_client
        host = ollama_client.host
        if not host.startswith(("http://localhost", "http://127.0.0.1")):
            findings.append(f"Ollama host is not loopback: {host}")
    except Exception as e:
        notes.append(f"could not read Ollama host: {e}")

    evidence = {"python_files_scanned": scanned,
                "documentation_urls": sorted(set(notes))[:10],
                "vendored_third_party_urls": sorted(set(vendored)),
                "blocked_attempts": sorted(set(_EXTERNAL_ATTEMPTS))}
    if findings:
        r.set(FAIL, "; ".join(sorted(set(findings))[:6]), findings=sorted(set(findings)),
              **evidence)
    elif vendored:
        r.set(WARN, f"no external dependency in ZEUS's own code ({scanned} python files, model "
                    f"runtime on loopback, no connection attempted); vendored third_party code "
                    f"contains {len(set(vendored))} external URL(s) not on ZEUS's path",
              **evidence)
    else:
        r.set(PASS, f"no external network dependency found "
                    f"({scanned} python files scanned, model runtime on loopback, "
                    f"no web/server framework loaded)", **evidence)


def check_deep_inference(r: Result):
    from core.model_router import model_router
    from core.ollama_client import ollama_client
    if not ollama_client.is_available():
        r.set(SKIP, "Ollama not reachable")
        return
    model = model_router.route("fast")
    out = ollama_client.chat(model, [{"role": "user", "content":
                                      "Reply with the single word OK."}],
                             retries=0, timeout=90.0, task_type="offline_test",
                             purpose="Offline verification")
    if out and not out.strip().startswith("["):
        r.set(PASS, f"{model} answered locally", model=model, reply=out.strip()[:60])
    else:
        r.set(FAIL, f"{model} did not answer: {out[:120]}")


# -------------------------------------------------------------------- main

def main() -> int:
    parser = argparse.ArgumentParser(description="Verify ZEUS runs fully offline")
    parser.add_argument("--deep", action="store_true",
                        help="also run live chat and vision inference")
    parser.add_argument("--json", default="")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    from industrial import ensure_directories
    ensure_directories()

    workdir = Path(tempfile.mkdtemp(prefix="medha_offline_"))
    results = []

    checks = [
        ("Ollama", lambda r: check_ollama(r)),
        ("Local Models", lambda r: check_models(r)),
        ("PDF Engine", lambda r: check_pdf_engine(r, workdir)),
        ("OCR", lambda r: check_ocr(r, workdir)),
        ("Vision Pipeline", lambda r: check_vision(r, workdir, args.deep)),
        ("Voice providers", lambda r: check_voice(r, args.deep)),
        ("Embedding Model", lambda r: check_embedding(r)),
        ("Reranker", lambda r: check_reranker(r)),
        ("Knowledge Base", lambda r: check_knowledge_base(r, workdir)),
        ("Document Generation", lambda r: check_document_generation(r, workdir)),
        ("Industrial Plant Layer", lambda r: check_plant_layer(r, workdir)),
        ("Orchestration Pipeline", lambda r: check_orchestration(r, workdir)),
    ]
    if args.deep:
        checks.append(("Local Inference", lambda r: check_deep_inference(r)))
    # The dependency check runs last so it sees everything the others loaded.
    checks.append(("External Dependency Check", lambda r: check_external_dependencies(r)))

    for name, fn in checks:
        result = timed(fn, Result(name))
        results.append(result)

    failures = [r for r in results if r.status == FAIL]
    warnings = [r for r in results if r.status == WARN]
    ready = not failures

    print()
    print("OFFLINE ENVIRONMENT TEST")
    width = max(len(r.name) for r in results)
    for r in results:
        print(f"{r.name + ':':<{width + 2}} {r.status}")
    print(f"OFFLINE READY: {'YES' if ready else 'NO'}")
    print()

    if not args.quiet:
        print("-" * 72)
        for r in results:
            print(f"[{r.status}] {r.name} ({r.seconds:.2f}s)")
            if r.detail:
                print(f"       {r.detail}")
        print("-" * 72)
        if failures:
            print("BLOCKERS:")
            for r in failures:
                print(f"  - {r.name}: {r.detail}")
        if warnings:
            print("DEGRADED (ZEUS still runs):")
            for r in warnings:
                print(f"  - {r.name}: {r.detail}")
        print(f"\nTest workspace: {workdir}")

    payload = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
               "offline_ready": ready,
               "blocked_external_attempts": sorted(set(_EXTERNAL_ATTEMPTS)),
               "results": [r.to_dict() for r in results]}
    if args.json:
        out = Path(args.json).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        print(f"JSON written to {out}")

    return 0 if ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
