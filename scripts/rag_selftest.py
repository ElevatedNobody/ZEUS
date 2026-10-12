"""
End-to-end RAG self-test, against the real local models on THIS machine.

Run it after any change to embedding, retrieval or reranking, and after a
restart to prove the index survived:

    python scripts/rag_selftest.py
    python scripts/rag_selftest.py --reuse      # after restarting, no re-index

It walks the whole pipeline and reports each stage separately, because "RAG
is broken" is never actionable and "the query embedded but nothing scored
above the threshold" is:

     1 document ingestion          8 top-K candidate generation
     2 text extraction             9 reranking
     3 chunking                   10 top-N selection
     4 embedding                  11 evidence extraction
     5 vector storage             12 citation / provenance
     6 query embedding            13 answer generation
     7 vector retrieval           14 final answer delivery

Every stage prints what it actually observed - the model that answered, the
vector width, how many chunks were stored, which passage won. A stage that
cannot run says why and names the command that fixes it.

Nothing here is mocked and nothing is downloaded: it uses whatever is pulled
on this machine right now.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PASS, FAIL, WARN, SKIP = "PASS", "FAIL", "WARN", "SKIP"
_SYMBOL = {PASS: "  ok  ", FAIL: " FAIL ", WARN: " warn ", SKIP: " skip "}

SAMPLE = """Standard Operating Procedure SOP-ME-014 Revision 3
Shell and tube heat exchanger cleaning and return to service.

Section 5.2 Return to service.
Before the exchanger is returned to service a hydrotest shall be performed at
1.3 times the design pressure and held for thirty minutes with no measurable
pressure drop. A visual inspection of the shell side welds shall follow the
hydrotest. The mechanical completion certificate shall be signed by a
competent person and the cleaning record attached to it.

Section 5.3 Acceptance.
Any weld indication longer than 3 mm shall be reported to the inspection
engineer before the certificate is signed.

Section 6.1 Records.
The cleaning contractor shall submit the cleaning record within two working
days of completion.
"""

DISTRACTOR = """Maintenance log - centrifugal pump P-204.
The mechanical seal was replaced during the March outage. Vibration on the
outboard bearing measured 4.8 mm/s RMS before the repair and 1.9 mm/s after.
Lubricant was changed at the same time and the coupling alignment was checked.
"""

QUESTION = "what must be done before the exchanger is returned to service?"

_results = []


def stage(number: int, name: str, status: str, detail: str):
    _results.append((number, name, status, detail))
    print(f"  [{_SYMBOL[status]}] {number:>2}. {name:<28} {detail}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reuse", action="store_true",
                        help="skip ingestion and query the existing index - "
                             "use this after a restart to prove it persisted")
    parser.add_argument("--folder", default="",
                        help="index this folder instead of the built-in sample")
    args = parser.parse_args()

    from core.capabilities import Health, embedding_provider, rerank_provider
    from core.ollama_client import ollama_client
    from core.tool_registry import registry
    from memory.database import get_all_chunks, init_db

    print("\n" + "=" * 74)
    print("  ZEUS - RAG END-TO-END SELF TEST" + ("  (reusing the existing index)"
                                                   if args.reuse else ""))
    print("=" * 74)

    init_db()

    # ---------------------------------------------------------- prerequisites
    if not ollama_client.is_available():
        print("\n  The local model runtime is not reachable.")
        print("  Start it and try again:   ollama serve\n")
        return 2

    embedding = embedding_provider.check()
    print(f"\n  embedding : {embedding.line()}")
    if embedding.health is not Health.READY:
        if embedding.health is Health.MODEL_MISSING:
            print("\n  Fix:  ollama pull bge-m3\n")
        return 2
    reranking = rerank_provider.check()
    print(f"  reranking : {reranking.line()}\n")

    folder = Path(args.folder) if args.folder else Path(
        tempfile.mkdtemp(prefix="medha_rag_selftest_"))
    created = not args.folder

    try:
        # ------------------------------------------------- 1-5 the write path
        if args.reuse:
            stored = get_all_chunks()
            if not stored:
                stage(1, "document ingestion", FAIL,
                      "nothing is indexed - run without --reuse first")
                return 1
            for number, name in ((1, "document ingestion"), (2, "text extraction"),
                                 (3, "chunking"), (4, "embedding")):
                stage(number, name, SKIP, "reusing the existing index")
            stage(5, "vector storage", PASS,
                  f"{len(stored)} chunk(s) already stored, "
                  f"{len(stored[0]['embedding'])}-dimension vectors")
        else:
            if created:
                (folder / "SOP-ME-014.txt").write_text(SAMPLE, encoding="utf-8")
                (folder / "P-204-maintenance-log.txt").write_text(DISTRACTOR,
                                                                  encoding="utf-8")
            started = time.time()
            indexed = registry.execute("index_folder", path=str(folder))
            elapsed = time.time() - started
            if not indexed.success:
                stage(1, "document ingestion", FAIL, indexed.error or "failed")
                return 1
            output = indexed.output
            if output.get("error"):
                stage(1, "document ingestion", FAIL, output["error"])
                return 1

            files = output.get("files_indexed", 0)
            chunks = output.get("chunks_indexed", 0)
            stage(1, "document ingestion", PASS if files else FAIL,
                  f"{files} file(s) read from {folder}")
            stage(2, "text extraction", PASS if chunks else FAIL,
                  "text recovered from every readable file"
                  + (f"; skipped {len(output['files_skipped'])}"
                     if output.get("files_skipped") else ""))
            stage(3, "chunking", PASS if chunks else FAIL,
                  f"{chunks} chunk(s) produced")
            stage(4, "embedding", PASS if chunks else FAIL,
                  f"{embedding.model} embedded {chunks} chunk(s) in {elapsed:.1f}s")

            stored = get_all_chunks()
            width = len(stored[0]["embedding"]) if stored else 0
            stage(5, "vector storage", PASS if stored else FAIL,
                  f"{len(stored)} vector(s) of width {width} written to the local database")

        # -------------------------------------------------- 6-14 the read path
        started = time.time()
        query_vector = embedding_provider.embed(QUESTION)
        stage(6, "query embedding", PASS if query_vector else FAIL,
              f"{embedding.model} returned a {len(query_vector or [])}-dimension vector "
              f"in {(time.time() - started) * 1000:.0f}ms")
        if not query_vector:
            return 1

        started = time.time()
        answered = registry.execute("ask_knowledge_base", query=QUESTION)
        elapsed = time.time() - started
        if not answered.success:
            stage(7, "vector retrieval", FAIL, answered.error or "failed")
            return 1
        payload = answered.output
        if payload.get("error"):
            stage(7, "vector retrieval", FAIL, payload["error"])
            return 1

        retrieval = payload.get("retrieval", {})
        candidates = retrieval.get("candidates", 0)
        returned = retrieval.get("returned", 0)
        stage(7, "vector retrieval", PASS if candidates else FAIL,
              f"{candidates} chunk(s) scored above the relevance threshold")
        stage(8, "top-K candidates", PASS if candidates else FAIL,
              f"{candidates} candidate(s) handed to the reranker")

        backend = (retrieval.get("reranker") or {}).get("backend", "")
        stage(9, "reranking", PASS if backend else FAIL,
              f"{backend or 'none'}"
              + ("" if candidates > returned else
                 " (nothing to choose between - only one candidate)"))
        stage(10, "top-N selection", PASS if returned else FAIL,
              f"{returned} passage(s) kept for the answer")

        evidence = payload.get("evidence") or []
        stage(11, "evidence extraction", PASS if evidence else FAIL,
              f"{len(evidence)} evidence item(s): "
              + ", ".join(f"{item['id']}={item['document']}" for item in evidence[:4]))

        documents = (payload.get("sources") or {}).get("documents") or []
        expected = "SOP-ME-014" if created else ""
        cited_right = (not expected) or any(expected in name for name in documents)
        stage(12, "citation / provenance", PASS if documents and cited_right else FAIL,
              f"cited {', '.join(documents) or 'nothing'}"
              + ("" if cited_right else f" - expected {expected}"))

        answer = (payload.get("answer") or "").strip()
        stage(13, "answer generation", PASS if answer else FAIL,
              f"{payload.get('model', 'unknown model')} produced "
              f"{len(answer)} characters in {elapsed:.1f}s")

        report = payload.get("citation_report") or {}
        invented = report.get("invalid_citations") or []
        stage(14, "final answer delivery",
              FAIL if invented else (PASS if answer else FAIL),
              "grounded, every citation checked against the evidence"
              if not invented else f"INVENTED citations were stripped: {invented}")

        print("\n" + "-" * 74)
        print("  ANSWER")
        print("-" * 74)
        for line in (answer or "(none)").splitlines():
            print(f"  {line}")
        print("-" * 74)

    finally:
        if created and not args.reuse and folder.exists():
            shutil.rmtree(folder, ignore_errors=True)

    failures = [name for _, name, status, _ in _results if status == FAIL]
    print()
    if failures:
        print(f"  RESULT: {len(failures)} stage(s) FAILED - {', '.join(failures)}")
        print("=" * 74 + "\n")
        return 1
    print("  RESULT: the whole pipeline works on this machine.")
    print("  Now restart ZEUS and run:  python scripts/rag_selftest.py --reuse")
    print("  That proves the vector store survived the restart.")
    print("=" * 74 + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
