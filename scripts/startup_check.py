"""
Startup readiness check used by start_medha_industrial.bat.

Prints a compact readiness report and sets an exit code:
    0  ready (or ready with warnings)   -> ZEUS starts
    1  blocked                          -> the BAT file stops and explains

Fast by design: no model inference, no ingestion, no file parsing. It only
inspects what is installed and what the local Ollama daemon already has.

Robust by design too: every probe runs inside _row(), so a machine that is
missing an optional package gets a WARN line naming the gap instead of a
traceback. A readiness check that crashes is worse than useless - it blocks
the launcher it exists to protect.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _text(value, fallback: str = "") -> str:
    """A detected version, or the fallback. The environment audit stores None
    for a library that is not installed, so `.get(name, "")` is not enough -
    the default only applies when the KEY is absent, not when its value is
    None."""
    return str(value) if value else fallback


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strict", action="store_true",
                        help="treat warnings as blockers")
    args = parser.parse_args()

    from industrial import ENVIRONMENT_REPORT_PATH
    from industrial.corpus import corpus_stats
    from industrial.environment import audit
    from industrial.knowledge_base import knowledge_base
    from industrial.model_registry import get_registry
    from industrial import ocr_engines, reranker
    from voice.offline import voice_status

    def probe(fn, default):
        """Gather a section without letting one failure abort the report."""
        try:
            return fn()
        except Exception as e:
            probe_failures.append(f"{type(e).__name__}: {e}")
            return default

    probe_failures: list = []
    report = probe(lambda: audit(write=True), {})
    voice = probe(voice_status, {"enabled": False})
    kb = probe(knowledge_base.stats, {"documents": 0, "chunks": 0, "embedded_chunks": 0})
    corpus = probe(corpus_stats, {"domains_on_disk": 0, "sections": 0})
    models = probe(lambda: get_registry().health_check(write=True), {"slots": {}})
    ocr = probe(ocr_engines.engine_report, {"available": [], "layout_analyser": "unknown"})

    def line(label, status, detail=""):
        print(f"  {label:<26} {status:<8} {detail}")

    def _row(label, fn):
        """One report row. If the probe raises, say so on the row itself."""
        try:
            status, detail = fn()
        except Exception as e:
            status, detail = "WARN", f"could not be checked: {type(e).__name__}: {e}"
            probe_failures.append(f"{label}: {type(e).__name__}: {e}")
        line(label, status, detail)

    print()
    print("=" * 70)
    print("  ZEUS - READINESS REPORT")
    print("  Autonomous AI Command & Intelligence System")
    print("=" * 70)

    _row("Python", lambda: (
        "OK" if report["python"]["meets_minimum"] else "BLOCK",
        f"{report['python']['version']}"
        + (" (venv)" if report["python"]["virtual_environment"]["active"] else "")))

    def _ollama_row():
        ok = report["ollama"]["daemon_reachable"]
        return ("OK" if ok else "BLOCK",
                f"{report['ollama']['host']} - {len(report['ollama']['models'])} model(s)"
                if ok else "not reachable: run 'ollama serve'")
    _row("Ollama runtime", _ollama_row)

    for slot in ("reasoning", "json_structured", "vision"):
        info = models.get("slots", {}).get(slot)
        if not info:
            continue
        # .get() with a default: an unexpected status must not raise here.
        status = {"ok": "OK", "degraded": "WARN",
                  "BLOCKER": "BLOCK"}.get(info.get("status"), "WARN")
        _row(f"Model: {slot}", lambda i=info, s=status: (s, i.get("resolved") or "not pulled"))

    # Embedding and reranking are NOT chat slots and are not resolved through
    # the chat router. Each is probed with a real call, so what is printed
    # here is what actually happened, not what is configured.
    from core.capabilities import Health, check_all
    capabilities = check_all()
    _severity = {Health.READY: "OK", Health.DEGRADED: "WARN",
                 Health.ADAPTER_REQUIRED: "WARN", Health.MODEL_MISSING: "WARN",
                 Health.RUNTIME_DOWN: "BLOCK", Health.UNAVAILABLE: "BLOCK",
                 Health.UNKNOWN: "WARN"}
    for name in ("embedding", "reranking"):
        probe_report = capabilities.get(name)
        if probe_report is None:
            continue
        _row(f"Capability: {name}",
             lambda r=probe_report: (_severity.get(r.health, "WARN"), r.line()))

    _row("OCR engines", lambda: (
        "OK" if ocr["available"] else "BLOCK",
        ", ".join(ocr["available"]) or "none - install Tesseract or PaddleOCR"))
    _row("Layout analysis", lambda: ("OK", ocr.get("layout_analyser", "heuristic")))

    def _pdf_row():
        version = _text(report.get("document_processors", {}).get("PyMuPDF"))
        if not version:
            return "WARN", "PyMuPDF missing - run: pip install PyMuPDF"
        return ("WARN" if "NONE" in version.upper() else "OK", version)
    _row("PDF engine", _pdf_row)

    _row("Knowledge corpus", lambda: (
        "OK" if corpus["domains_on_disk"] else "WARN",
        f"{corpus['domains_on_disk']} domains / {corpus['sections']} sections on disk"))
    _row("Knowledge base", lambda: (
        "OK" if kb["embedded_chunks"] else "WARN",
        f"{kb['documents']} docs / {kb['chunks']} chunks ({kb['embedded_chunks']} embedded)"
        if kb["chunks"] else "empty - run scripts\\ingest_industrial_knowledge.py"))

    # ------------------------------------------------------------ voice --
    # Voice is OPTIONAL. Nothing here is a blocker, and "no voice provider" is
    # reported as TEXT MODE rather than as a fault: it is a supported way to
    # run ZEUS, not an incomplete installation.
    warnings_extra = None
    from voice.providers import ProviderState, voice_manager

    if voice.get("enabled"):
        probes = voice_manager.probe()
        speakers = [r for r in probes.values() if r.direction == "output"]
        listeners = [r for r in probes.values() if r.direction == "input"]

        def _direction_row(reports, what):
            usable = [r for r in reports
                      if r.state in (ProviderState.READY, ProviderState.NOT_PROBED)]
            if usable:
                return "OK", f"{usable[0].name} — {usable[0].detail}"
            # Nothing is installed, which is fine - but "none installed" on its
            # own leaves the reader to work out what to do about it. The
            # provider already knows the command that fixes it, so say it.
            #
            # `reports` arrives in PREFERENCE order, so the first fixable one
            # is the one worth naming. Filtering by state instead would
            # recommend whichever provider happened to be in the more
            # convenient state - suggesting the optional fallback while the
            # preferred provider sits there needing only its weights.
            fixable = [r for r in reports
                       if r.state in (ProviderState.INSTALLABLE,
                                      ProviderState.MODEL_MISSING)]
            if fixable:
                return "OK", (f"none installed — {what} stays in text mode; "
                              f"{fixable[0].detail}")
            return "OK", f"none installed — {what} stays in text mode"

        _row("Voice: speech out", lambda: _direction_row(speakers, "replies"))
        _row("Voice: speech in", lambda: _direction_row(listeners, "input"))
        line("Voice: external services", "OK", "DISABLED (local providers only)")
        line("Voice: runtime downloads", "OK", "DISABLED (models are never fetched)")
        if not any(r.state in (ProviderState.READY, ProviderState.NOT_PROBED)
                   for r in probes.values()):
            line("Voice mode", "OK", "TEXT MODE — a supported configuration")
    else:
        line("Voice subsystem", "OK", "disabled in config.ini [voice]")

    _row("Existing ZEUS tools", lambda: (
        "OK", f"{report['existing_medha']['tools']['count']} registered"))

    # ---------------------------------------------------- orchestration --
    def _pipeline_row():
        from core.orchestration import agents as _agents
        from core.orchestration import autofix as _autofix
        from core.orchestration.validators import registered_kinds
        return ("OK", f"{len(_agents.SPECIALISTS)} specialist agents, "
                      f"{len(registered_kinds())} validators, "
                      f"{len(_autofix.known_strategies())} repair strategies")
    _row("Orchestration pipeline", _pipeline_row)

    def _router_row():
        from core.orchestration.router import available_ram_gb, capability_router
        table = capability_router.describe()
        pulled = sum(1 for row in table["slots"] if row.get("pulled"))
        free = available_ram_gb()
        return ("OK" if pulled and free > 2.0 else "WARN",
                f"{pulled}/{len(table['slots'])} model slots pulled, "
                f"{free:.1f} GB free, "
                f"{len(table['deterministic_kinds'])} task kinds need no model")
    _row("Model router", _router_row)

    line("Network services", "OK", "none (no REST/web server; Ollama on loopback only)")

    readiness = report.get("readiness", {})
    blockers = list(readiness.get("blockers", []))
    warnings = list(readiness.get("warnings", []))
    if not kb.get("chunks"):
        warnings.append("industrial knowledge base is empty - "
                        "run scripts\\ingest_industrial_knowledge.py")
    if warnings_extra:
        warnings.append(warnings_extra)
    for failure in probe_failures:
        warnings.append(f"readiness probe failed: {failure}")

    print("-" * 70)
    if blockers:
        print("  BLOCKERS:")
        for b in blockers:
            print(f"    - {b}")
    if warnings:
        print("  WARNINGS (ZEUS still starts):")
        for w in warnings:
            print(f"    - {w}")
    state = "NOT READY" if blockers else ("READY (with warnings)" if warnings else "READY")
    print(f"  STATUS: INDUSTRIAL ENVIRONMENT {state}")
    print(f"  Full report: {ENVIRONMENT_REPORT_PATH}")
    print("=" * 70)
    print()

    if blockers:
        return 1
    if warnings and args.strict:
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as e:                                   # pragma: no cover
        # The check itself broke. Report it and let ZEUS start anyway - main.py
        # does its own checks, and a faulty diagnostic must never be the reason
        # the workbench will not open.
        print()
        print("=" * 70)
        print("  READINESS CHECK COULD NOT COMPLETE")
        print(f"  {type(e).__name__}: {e}")
        print("  This does not stop ZEUS - starting anyway.")
        print("  For the full picture run: python scripts\\offline_test.py")
        print("=" * 70)
        print()
        raise SystemExit(0)
