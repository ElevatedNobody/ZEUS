"""
Phase H - industrial knowledge ingestion tool.

    python scripts/ingest_industrial_knowledge.py

Discovers every supported document under knowledge_sources/, classifies it,
parses it, OCRs scanned pages, analyses layout, extracts tables, images and
metadata, chunks it with full provenance, embeds it with BGE-M3 and stores it
in the local industrial knowledge base. It also ingests the built-in
industrial knowledge corpus under knowledge/industrial_history/.

Everything runs locally in this process. No server is started, no network call
is made other than loopback to the Ollama daemon for embeddings.

Options
-------
  --source PATH        folder to ingest (default: knowledge_sources/)
  --file PATH          ingest a single file (repeatable)
  --corpus-only        only ingest the built-in knowledge corpus
  --skip-corpus        skip the built-in knowledge corpus
  --no-embed           parse and store without embedding (embed later with
                       --embed-pending when Ollama is running)
  --embed-pending      embed chunks that were stored earlier without vectors
  --vision             use the local vision model on pages OCR cannot resolve
  --max-files N        stop after N documents
  --clear              clear the industrial knowledge base first
  --report PATH        also write the ingestion report as JSON
  --quiet              only print the summary
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from industrial import INGESTION_LOG_PATH, KNOWLEDGE_SOURCES_DIR, ensure_directories  # noqa: E402
from industrial.doc_pipeline import (SUPPORTED_EXTENSIONS, UNSTRUCTURED_EXTENSIONS,  # noqa: E402
                                     ExtractionOptions)

SKIP_DIR_NAMES = {".git", "__pycache__", ".venv", "venv", "node_modules", "_to_delete"}


class RunLog:
    """Appends to logs/knowledge_ingestion.log and echoes to the console."""

    def __init__(self, run_id: str, quiet: bool = False):
        self.run_id = run_id
        self.quiet = quiet
        INGESTION_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(INGESTION_LOG_PATH, "a", encoding="utf-8")
        self.write(f"===== ingestion run {run_id} started =====")

    def write(self, message: str, console: bool = True):
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{self.run_id}] {message}"
        self.fh.write(line + "\n")
        self.fh.flush()
        if console and not self.quiet:
            print(message)

    def close(self):
        self.write(f"===== ingestion run {self.run_id} finished =====", console=False)
        self.fh.close()


def discover(source: Path, max_files: int) -> list:
    allowed = SUPPORTED_EXTENSIONS | UNSTRUCTURED_EXTENSIONS
    found = []
    if not source.is_dir():
        return found
    for path in sorted(source.rglob("*")):
        if any(part in SKIP_DIR_NAMES for part in path.parts):
            continue
        if not path.is_file() or path.name.startswith("~$"):
            continue
        if path.suffix.lower() not in allowed:
            continue
        if path.name.upper() in ("README.MD", "README.TXT"):
            continue
        found.append(path)
        if max_files and len(found) >= max_files:
            break
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description="Ingest industrial documents locally")
    parser.add_argument("--source", default=str(KNOWLEDGE_SOURCES_DIR))
    parser.add_argument("--file", action="append", default=[])
    parser.add_argument("--corpus-only", action="store_true")
    parser.add_argument("--skip-corpus", action="store_true")
    parser.add_argument("--no-embed", action="store_true")
    parser.add_argument("--embed-pending", action="store_true")
    parser.add_argument("--vision", action="store_true")
    parser.add_argument("--max-files", type=int, default=0)
    parser.add_argument("--clear", action="store_true")
    parser.add_argument("--report", default="")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    ensure_directories()
    from industrial.corpus import ingest_corpus
    from industrial.knowledge_base import knowledge_base
    from memory.database import init_db

    init_db()
    run_id = f"ing-{uuid.uuid4().hex[:8]}"
    log = RunLog(run_id, args.quiet)
    started = time.perf_counter()

    ok, detail = knowledge_base.embedding_available()
    embed = not args.no_embed and ok
    if not args.no_embed and not ok:
        log.write(f"[WARN] embeddings unavailable: {detail}")
        log.write("[WARN] documents will be parsed and stored WITHOUT vectors. "
                  "Re-run with --embed-pending once Ollama is running.")
    elif embed:
        log.write(f"[OK] embedding model: {knowledge_base.embed_model()}")

    if args.clear:
        removed = knowledge_base.clear()
        log.write(f"[CLEAR] removed {removed} existing chunks")

    report = {"run_id": run_id, "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
              "embedding": knowledge_base.embed_model() if embed else None,
              "corpus": None, "documents": [], "failures": []}

    # ---------------------------------------------------------- corpus ----
    if not args.skip_corpus:
        log.write("[CORPUS] ingesting local industrial knowledge corpus")
        corpus_result = ingest_corpus(embed=embed, run_id=run_id)
        report["corpus"] = {k: v for k, v in corpus_result.items() if k != "results"}
        for r in corpus_result["results"]:
            log.write(f"  [{r['status'].upper():7}] {Path(r['file']).name} "
                      f"({r.get('chunks', 0)} chunks, {r.get('embedded', 0)} embedded)",
                      console=not args.quiet)
        log.write(f"[CORPUS] {corpus_result['ok']}/{corpus_result['files']} files, "
                  f"{corpus_result['chunks']} chunks, "
                  f"{corpus_result['embedded']} embedded "
                  f"({corpus_result['duration_s']}s)")

    # ------------------------------------------------------- documents ----
    if not args.corpus_only:
        options = ExtractionOptions(vision=args.vision)
        targets = [Path(f).expanduser() for f in args.file]
        if not targets:
            source = Path(args.source).expanduser()
            targets = discover(source, args.max_files)
            log.write(f"[DISCOVER] {len(targets)} document(s) under {source}")
            if not targets:
                log.write(f"[NOTE] nothing to ingest - put documents in {source}")

        for i, path in enumerate(targets, start=1):
            prefix = f"[{i}/{len(targets)}]"
            try:
                result = knowledge_base.ingest_document(path, options=options, embed=embed,
                                                        run_id=run_id)
            except Exception as e:  # a bad file must never abort the run
                log.write(f"{prefix} [FAILED ] {path.name}: {type(e).__name__}: {e}")
                report["failures"].append({"file": str(path), "error": f"{type(e).__name__}: {e}"})
                continue

            report["documents"].append(result)
            if result["status"] == "ok":
                scanned = result.get("scanned_pages") or []
                conf = result.get("mean_ocr_confidence")
                extra = ""
                if scanned:
                    extra = (f", OCR on {len(scanned)} page(s)"
                             + (f" @ {conf:.2f} confidence" if conf else ""))
                log.write(f"{prefix} [OK     ] {path.name} -> {result['document_type']}, "
                          f"{result.get('pages', 0)} page(s), {result['chunks']} chunks, "
                          f"{result['embedded']} embedded{extra}")
                for w in result.get("warnings", [])[:3]:
                    log.write(f"           warning: {w}")
            else:
                log.write(f"{prefix} [{result['status'].upper():7}] {path.name}: "
                          f"{result.get('detail', '')}")
                report["failures"].append({"file": str(path),
                                           "error": result.get("detail", result["status"])})

    # ------------------------------------------------- pending embedding --
    if args.embed_pending:
        log.write("[EMBED] embedding chunks stored without vectors")
        pending = knowledge_base.reembed_missing()
        report["embed_pending"] = pending
        log.write(f"[EMBED] {pending.get('embedded', 0)} chunk(s) embedded"
                  + (f" - {pending['error']}" if pending.get("error") else ""))

    # ------------------------------------------------------------ report --
    stats = knowledge_base.stats()
    ok_docs = sum(1 for d in report["documents"] if d["status"] == "ok")
    chunks = sum(d.get("chunks", 0) for d in report["documents"])
    embedded = sum(d.get("embedded", 0) for d in report["documents"])
    report["summary"] = {
        "documents_ingested": ok_docs,
        "documents_failed": len(report["failures"]),
        "document_chunks": chunks,
        "document_chunks_embedded": embedded,
        "corpus_chunks": (report["corpus"] or {}).get("chunks", 0),
        "knowledge_base": stats,
        "duration_s": round(time.perf_counter() - started, 2),
    }

    print()
    print("=" * 62)
    print("  INDUSTRIAL KNOWLEDGE INGESTION REPORT")
    print("=" * 62)
    if report["corpus"]:
        c = report["corpus"]
        print(f"  Knowledge corpus   : {c['ok']}/{c['files']} files, {c['chunks']} chunks")
    print(f"  Documents ingested : {ok_docs}")
    print(f"  Documents failed   : {len(report['failures'])}")
    print(f"  Chunks created     : {chunks + (report['corpus'] or {}).get('chunks', 0)}")
    print(f"  Chunks embedded    : {embedded + (report['corpus'] or {}).get('embedded', 0)}")
    print(f"  Knowledge base now : {stats['documents']} documents / {stats['chunks']} chunks "
          f"({stats['embedded_chunks']} embedded)")
    print(f"  Reranker           : {stats['reranker']['backend']}")
    print(f"  Duration           : {report['summary']['duration_s']}s")
    if report["failures"]:
        print("  Failures:")
        for f in report["failures"][:10]:
            print(f"    - {Path(f['file']).name}: {f['error']}")
    print(f"  Log                : {INGESTION_LOG_PATH}")
    print("=" * 62)

    if args.report:
        out = Path(args.report).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        print(f"  JSON report written to {out}")

    log.write(f"[SUMMARY] {ok_docs} documents, {len(report['failures'])} failures, "
              f"{chunks} document chunks, {embedded} embedded", console=False)
    log.close()
    return 0 if not report["failures"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
