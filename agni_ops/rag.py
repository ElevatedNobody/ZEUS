"""
python -m agni_ops rag <action> - the RAG BAT files (RAG_INGEST, RAG_REINDEX,
RAG_EVALUATE) and `test rag`.

    ingest <file> [--origin company|sample|demo|general_knowledge] [--confidentiality ...]
           [--department ...] [--restricted] [--location ...] [--year N] [--force]
    ingest-dir <folder> [same options]        years from folder names (2019/, FY2019-20/) when present
    ingest-year <folder> <year> [options]     every file belongs to that year
    ingest-archive <file.zip> [options]
    update <file> [options]                   re-ingest a changed file (same as ingest --force)
    remove <document_id>
    reindex | reembed [--model-change] | rebuild-graph | validate
    provenance <document_id> | coverage [--first Y --last Y] | status | evaluate
    models                                    every RAG model and store: present, loads, dimensions match
    trace "<question>"                        one question through the chat route, every stage printed
                                              (development: shows document text and the model request)

The console acts as the machine's operator (it can read the database file
directly anyway), so it reports counts, states and provenance - not document
text. Exit codes: 0 = done / PASS, 1 = a FAIL, 2 = bad arguments.
"""

from __future__ import annotations

import argparse
import json
from typing import List, Optional

from agni_ops.status import FAIL, NOT_TESTED, PASS, WARN, Result

ACTIONS = ("ingest", "ingest-dir", "ingest-year", "ingest-archive", "update", "remove", "reindex", "reembed",
           "rebuild-graph", "validate", "provenance", "coverage", "status", "evaluate", "trace", "models")


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("action", choices=ACTIONS)
    parser.add_argument("target", nargs="?", default="")
    parser.add_argument("year", nargs="?", default="")
    parser.add_argument("--origin", default="company",
                        choices=("company", "sample", "demo", "general_knowledge"))
    parser.add_argument("--confidentiality", default="")
    parser.add_argument("--department", default="")
    parser.add_argument("--restricted", action="store_true", help="only the department (and admins) may see it")
    parser.add_argument("--location", default="", help="site name, e.g. 'Panipat Refinery'")
    parser.add_argument("--dataset", default="", help="name of the corpus the files belong to - shown as their provenance")
    parser.add_argument("--dataset-note", default="", help="what the corpus says about itself (e.g. its README disclaimer)")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--model-change", action="store_true")
    parser.add_argument("--first", type=int, default=0)
    parser.add_argument("--last", type=int, default=0)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-embed", action="store_true", help="skip vectors now; add them with RAG_REINDEX.bat")
    parser.add_argument("--fixture", action="store_true",
                        help="trace: run on the labelled SYNTHETIC test corpus in a temporary database "
                             "(ingest, chunk, embed, store, then the question) - your knowledge base is untouched")
    parser.add_argument("--spoken", action="store_true",
                        help="trace: start from the question SPOKEN aloud - Windows voice -> Whisper -> the chat "
                             "engine -> retrieval -> the local model -> answer + sources")


def _console_context():
    from knowledge_fabric.access import AccessContext
    return AccessContext("console", "admin", True, system=True)


def _overrides(args) -> dict:
    out = {}
    if args.confidentiality:
        out["confidentiality"] = args.confidentiality
    if args.department:
        out["department"] = args.department
    if args.restricted:
        out["department_restricted"] = True
    if args.location:
        out["location"] = args.location
    if getattr(args, "dataset", ""):
        out["dataset"] = args.dataset
    if getattr(args, "dataset_note", ""):
        out["dataset_note"] = args.dataset_note
    return out


def _ingest_summary(result: dict, what: str) -> List[Result]:
    if not result.get("results") and result.get("error"):
        return [Result(what, FAIL, result["error"], group="rag")]
    out = []
    for r in result.get("results", []):
        state = {"ingested": PASS, "updated": PASS, "skipped": WARN, "failed": FAIL}.get(r["status"], WARN)
        out.append(Result(r.get("title") or r["source_path"], state, f"{r['status']}: {r.get('detail') or ''}".strip(": "),
                          where=r["source_path"], details=r.get("counts") or {}, group="rag"))
    counts = {k: result.get(k, 0) for k in ("ingested", "updated", "skipped", "failed")}
    out.append(Result(what, FAIL if counts["failed"] else PASS,
                      f"{result.get('files', 0)} file(s): " + ", ".join(f"{v} {k}" for k, v in counts.items()),
                      group="rag"))
    return out


def evaluation_results(progress=None) -> List[Result]:
    from knowledge_fabric import evaluation
    report = evaluation.run(progress=progress)
    if report["status"] == "NOT_TESTED":
        return [Result("RAG evaluation", NOT_TESTED, report.get("reason", ""), group="rag")]
    results = [Result(f"rag {c['category']}: {c['id']}", PASS if c["status"] == "PASS" else FAIL, c["expected"],
                      evidence="" if c["status"] == "PASS" else json.dumps(c["detail"], default=str)[:600],
                      duration_s=c.get("seconds", 0.0), group="rag") for c in report["cases"]]
    results.append(Result("RAG evaluation", PASS if report["status"] == "PASS" else FAIL,
                          f"{report['passed']}/{report['total']} checks on the synthetic test fixture", group="rag"))
    return results


def corpus_results() -> List[Result]:
    from knowledge_fabric import FIRST_YEAR, LAST_YEAR, coverage, ingest
    out = []
    check = ingest.validate_corpus()
    totals = check["totals"]
    for finding in check["findings"]:
        serious = finding["check"] in ("unindexed_nodes", "orphan_facts", "mixed_vector_dimensions")
        out.append(Result(f"corpus: {finding['check']}", FAIL if serious else WARN,
                          f"{finding['count']} - {finding['detail']}", group="rag"))
    out.append(Result("knowledge base integrity", PASS if check["ok"] else FAIL,
                      f"{totals['kf_documents']} documents, {totals['kf_nodes']} passages, {totals['kf_facts']} table "
                      f"facts, {totals['kf_vectors']} vectors", group="rag"))
    rep = coverage.report(_console_context(), FIRST_YEAR, LAST_YEAR)
    summary = rep["summary"]
    state = PASS if summary["missing_years"] == 0 and summary["sample_only_years"] == 0 else WARN
    out.append(Result(f"company records {FIRST_YEAR}-{LAST_YEAR}", state,
                      f"{summary['available_years']} year(s) with company records, {summary['sample_only_years']} "
                      f"sample-only, {summary['general_only_years']} general-only, {summary['missing_years']} missing "
                      f"- gaps are REPORTED, never filled", group="rag",
                      details={"missing_periods": rep["missing_periods"]}))
    if not totals["kf_vectors"]:
        out.append(Result("vector search", WARN, "no embeddings stored - full-text, entity, graph and table search "
                          "work; run RAG_REINDEX.bat once the embedding model is installed",
                          fallback="lexical + entity + structured retrieval", group="rag"))
    return out


def model_checks() -> List[Result]:
    """Every model and store the RAG pipeline uses - checked, not assumed."""
    import importlib.util as _spec
    from knowledge_fabric import store
    out: List[Result] = []
    # 1 embedding model (Ollama) and its agreement with the stored vectors
    try:
        from core.capabilities import embedding_provider
        model = embedding_provider.resolve(force=True)
        vector = embedding_provider.embed("pump discharge pressure") if model else None
    except Exception as error:
        model, vector = None, None
        out.append(Result("RAG: embedding model", FAIL, f"embedding provider error: {error}", group="rag"))
    with store.connection() as conn:
        dims = {r[0]: r[1] for r in conn.execute("SELECT dim, COUNT(*) FROM kf_vectors GROUP BY dim")}
        # the same selection reembed uses: headings and register rows are not embedded
        embeddable = conn.execute("SELECT COUNT(*) FROM kf_nodes WHERE kind NOT IN ('section', 'document', 'record') "
                                  "AND text IS NOT NULL AND length(text) > 20").fetchone()[0]
        fts = conn.execute("SELECT COUNT(*) FROM kf_fts").fetchone()[0]
        nodes = conn.execute("SELECT COUNT(*) FROM kf_nodes").fetchone()[0]
    if not model:
        out.append(Result("RAG: embedding model", FAIL, "no embedding model answers (bge-m3 via Ollama) - is Ollama "
                          "running? STEP 2 : START starts it; install with STEP 1 : INSTALL",
                          fallback="full-text, entity, graph and table search", group="rag"))
    elif not vector:
        out.append(Result("RAG: embedding model", FAIL, f"{model} is installed but returned no vector "
                          f"({getattr(embedding_provider, 'last_error', '') or 'see the Ollama log'})", group="rag"))
    else:
        mismatch = {d: n for d, n in dims.items() if d != len(vector)}
        status = PASS if not mismatch else FAIL
        out.append(Result("RAG: embedding model", status,
                          f"{model} embeds locally: {len(vector)} dimensions; stored vectors "
                          + (", ".join(f"{n} x {d}-dim" for d, n in dims.items()) or "none")
                          + ("" if not mismatch else " - some were made by another model: run RAG_REINDEX.bat"),
                          where="Ollama (model files managed by Ollama)", group="rag"))
    # 2 vector store + full-text index
    stored = sum(dims.values())
    out.append(Result("RAG: vector store", PASS if stored else WARN,
                      f"SQLite knowledge_fabric.db: {stored} vectors for {embeddable} embeddable passages "
                      f"({(100 * stored / embeddable) if embeddable else 0:.0f}% covered)", group="rag",
                      where=str(store.db_path())))
    out.append(Result("RAG: full-text index", PASS if fts else FAIL, f"FTS5: {fts} rows for {nodes} passages",
                      group="rag"))
    # 3 reranker
    libs = {name: bool(_spec.find_spec(name)) for name in ("torch", "sentence_transformers", "FlagEmbedding",
                                                           "onnxruntime")}
    try:
        from core import model_roles
        role = model_roles.select("RERANKER", probe=True).to_dict()
        test = (role.get("evidence") or {}).get("self_test") or {}
        status = PASS if role["state"] in ("VERIFIED", "INSTALLED") else (WARN if test.get("passed") else FAIL)
        out.append(Result("RAG: reranker", status,
                          f"{role['state']} {role['model']}: self-test {'passed' if test.get('passed') else 'FAILED'}; "
                          f"BGE-Reranker-v2-M3 not installed (needs torch + sentence-transformers or FlagEmbedding: "
                          f"{', '.join(k for k, v in libs.items() if v) or 'none'} present)"
                          if role["state"] == "HYBRID" else f"{role['state']} {role['model']}: {role['reason'][:160]}",
                          fallback="hybrid lexical + dense + metadata + temporal + authority + entity ranking"
                          if role["state"] == "HYBRID" else "", group="rag"))
    except Exception as error:
        out.append(Result("RAG: reranker", FAIL, str(error)[:200], group="rag"))
    # 4 the model that writes answers from the evidence
    try:
        from core import model_roles
        role = model_roles.select("GENERAL_REASONING").to_dict()
        out.append(Result("RAG: answer model", PASS if role["state"] in ("VERIFIED", "INSTALLED") else FAIL,
                          f"{role['state']} {role['model']}: {role['reason'][:160]}", group="rag"))
    except Exception as error:
        out.append(Result("RAG: answer model", FAIL, str(error)[:200], group="rag"))
    return out


def trace(question: str, log, fixture: bool = False, spoken: bool = False) -> List[Result]:
    """One question through the SAME route as chat, every stage printed. With
    fixture=True the synthetic test corpus is ingested (and embedded) into a
    temporary database first, so ingestion, chunking and embedding are shown too.
    spoken=True starts from the question SPOKEN aloud (see _spoken_trace)."""
    run = _spoken_trace if spoken else _trace
    if not fixture:
        return run(question, log)
    import shutil
    import tempfile
    from pathlib import Path
    from knowledge_fabric import ORIGIN_FIXTURE, ingest, store
    from knowledge_fabric.evaluation import CONFIDENTIAL_FIXTURES, FIXTURE_DIR
    workdir = Path(tempfile.mkdtemp(prefix="medha_rag_trace_"))
    results: List[Result] = []
    try:
        with store.using_database(workdir / "knowledge_fabric_trace.db"):
            log.say("\nINGEST (synthetic test corpus 'Test Fixture Refinery', temporary database):")
            for path in sorted(FIXTURE_DIR.glob("*.md")):
                overrides = {"confidentiality": "confidential"} if path.name in CONFIDENTIAL_FIXTURES else {}
                r = ingest.ingest_document(path, origin=ORIGIN_FIXTURE, overrides=overrides, embed=None)
                log.say(f"  {r.status:8} {path.name}: {r.counts.get('nodes', 0)} chunks, "
                        f"{r.counts.get('facts', 0)} table facts, {r.counts.get('embedded', r.counts.get('vectors', 0))} embeddings")
                if r.status not in ("ingested", "updated"):
                    results.append(Result("RAG trace: ingest", FAIL, f"{path.name}: {r.detail}", group="rag"))
            with store.connection() as conn:
                docs = conn.execute("SELECT COUNT(*) FROM kf_documents").fetchone()[0]
                chunks = conn.execute("SELECT COUNT(*) FROM kf_nodes WHERE kind IN ('paragraph', 'row_range', 'table')").fetchone()[0]
                vectors = conn.execute("SELECT COUNT(*), MAX(dim) FROM kf_vectors").fetchone()
            log.say(f"  stored: {docs} documents, {chunks} text chunks, {vectors[0]} vectors ({vectors[1] or 0}-dim)")
            results.append(Result("RAG trace: ingest + chunk + embed", PASS if docs and chunks and vectors[0] else WARN,
                                  f"{docs} documents, {chunks} chunks, {vectors[0]} vectors of {vectors[1] or 0} dims"
                                  + ("" if vectors[0] else " - no embedding model answered: vector search is off"),
                                  group="rag"))
            results += run(question, log)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return results


def _show_exchange(inj: dict, exchange: dict, narrative: str, say) -> Optional[str]:
    """CONTEXT SENT / MODEL REQUEST / MODEL RESPONSE from one trace. Returns the response."""
    say("\nCONTEXT SENT TO MODEL:")
    say(f"  sections: {', '.join(inj.get('sections') or []) or '-'}")
    say(f"  evidence in the model request: {inj.get('evidence_in_prompt', 0)} of {inj.get('evidence_retrieved', 0)}; "
        f"facts {inj.get('facts_in_prompt', 0)}; calculations {inj.get('calculations_in_prompt', 0)}; "
        f"memory {'yes' if inj.get('memory_in_prompt') else 'no'}; {inj.get('prompt_chars', 0)} characters"
        + (f"  ERROR: {inj['error']}" if inj.get("error") else ""))
    request = exchange.get("model_request") or {}
    say("\nMODEL REQUEST:")
    if request:
        user = next((m["content"] for m in request.get("messages", []) if m["role"] == "user"), "")
        start = user.find("## ENTERPRISE EVIDENCE")
        say(f"  model: {request.get('model')}; messages: " + ", ".join(m["role"] for m in request.get("messages", [])))
        say("  user message, evidence section (excerpt):")
        for line in user[start:start + 900].splitlines()[:14]:
            say(f"    {line}")
    else:
        say(f"  (no model request: {inj.get('reason') or inj.get('model_error') or 'see the trace file'})")
    say("\nMODEL RESPONSE:")
    response = exchange.get("model_response")
    say("  " + ((response or "").strip()[:1200].replace("\n", "\n  ") if response else
                f"(none: {inj.get('model_error') or narrative or 'model not called'})"))
    return response


def _context_results(inj: dict, response: Optional[str], narrative: str) -> List[Result]:
    if not inj.get("model_called"):
        return [Result("RAG trace: context injection", WARN, inj.get("reason") or "the model was not called",
                       group="rag")]
    return [Result("RAG trace: context injection", PASS if inj.get("ok") else FAIL,
                   inj.get("error") or f"all {inj.get('evidence_in_prompt')} retrieved passages are in the "
                                       f"model request", group="rag"),
            Result("RAG trace: model answer", PASS if response and not inj.get("model_error") else FAIL,
                   inj.get("model_error") or (narrative or "")[:200], group="rag")]


def _spoken_trace(question: str, log) -> List[Result]:
    """VOICE -> WHISPER -> QUERY -> ROUTER -> RAG -> CONTEXT -> LOCAL LLM -> ANSWER + SOURCES, live.

    The question is spoken by the Windows voice into a WAV and heard by the app's
    own recogniser (SpeechToText - the one the microphone button uses), which is
    released before the language model loads: one heavy model at a time. The
    transcript then goes through the SAME DecisionEngine.process(..., spoken=True)
    as a question asked aloud - under the read-only diagnostic identity, and
    without writing to the conversation memory."""
    import json
    import tempfile
    import time
    from pathlib import Path
    from agni_ops.voice import make_known_speech, similarity
    say = log.say
    say(f"\nVOICE INPUT (spoken by the Windows voice):\n  {question!r}")
    wav = make_known_speech(Path(tempfile.mkdtemp(prefix="medha_spoken_rag_")), question)
    if wav is None:
        return [Result("voice -> RAG: speech sample", NOT_TESTED, "the Windows voice could not speak the question",
                       group="rag")]
    say(f"  {wav}")
    from voice.speech_to_text import SpeechToText
    stt = SpeechToText(preload=True)
    try:
        heard = stt.transcribe_file(str(wav)) if stt.is_available else ""
        backend = (stt.status().get("worker") or {}).get("active") or "in process"
        error = stt.last_error
    finally:
        stt.close()                                  # released before the language model loads
    score = similarity(question, heard)
    say(f"\nWHISPER ({backend}):\n  heard {heard!r}  ({int(score * 100)}% of the words)")
    results = [Result("voice -> RAG: Whisper transcription", PASS if heard and score >= 0.7 else FAIL,
                      f"{backend}: '{heard}' ({int(score * 100)}% of the words)" if heard
                      else f"nothing transcribed: {error or 'no error reported'}", group="rag")]
    if not heard:
        return results
    say(f"\nUSER QUERY:\n  {heard}")
    from core.decision_engine import DecisionEngine
    from core.paths import log_dir
    from knowledge_fabric import service
    from security import policy
    started = time.time()
    with policy.diagnostic_scope(), service.force_trace():
        reply = DecisionEngine().process(heard, {"history": [], "spoken": True})
    pipeline = reply.get("pipeline") or {}
    tools = pipeline.get("tools") or []
    say(f"\nROUTER:\n  {pipeline.get('task_kind')} -> tools {', '.join(tools) or '(none: the model answered alone)'}"
        f"; agent {reply.get('agent') or '-'}")
    results.append(Result("voice -> RAG: route", PASS if "company_intelligence" in tools else FAIL,
                          f"tools: {', '.join(tools) or 'none'}", group="rag"))
    traces = sorted((p for p in (log_dir() / "rag_trace").glob("*.json") if p.stat().st_mtime >= started - 1),
                    key=lambda p: p.stat().st_mtime)
    exchange = json.loads(traces[-1].read_text(encoding="utf-8")) if traces else {}
    retrieved = exchange.get("retrieved") or []
    say(f"\nRAG RETRIEVAL ({len(retrieved)}):")
    for e in retrieved[:6]:
        say(f"  [{e.get('id')}] {e.get('citation') or e.get('title')}")
    results.append(Result("voice -> RAG: retrieval", PASS if retrieved else FAIL,
                          f"{len(retrieved)} passage(s)" if retrieved else "no retrieval trace for this request",
                          group="rag"))
    inj = exchange.get("context_injection") or {}
    narrative = exchange.get("narrative") or ""
    response = _show_exchange(inj, exchange, narrative, say)
    results += _context_results(inj, response, narrative) if exchange else []
    answer = reply.get("response") or ""
    say("\nANSWER (what ZEUS shows and speaks):\n  " + answer.strip()[:1500].replace("\n", "\n  "))
    sources = exchange.get("sources") or []
    say("\nSOURCES:")
    for s in sources[:8]:
        say(f"  {s if isinstance(s, str) else json.dumps(s, default=str)[:200]}")
    # What the ANSWER cites - not the evidence list the presenter appends under it,
    # which names every retrieved passage whether the answer used it or not.
    body = answer.split("\n---\n")[0]
    cited = [i for i in (exchange.get("validation") or {}).get("cited") or []
             if f"[{i}]" in body and any(e.get("id") == i for e in retrieved)]
    named = sorted({e.get("title") or e.get("citation") for e in retrieved if e.get("id") in cited})
    say(f"\nCITED BY THE ANSWER: {', '.join(cited) or 'none'}" + (f"  ->  {'; '.join(named)}" if named else ""))
    results.append(Result("voice -> RAG: answer with sources", PASS if cited else FAIL,
                          f"the answer cites {', '.join(cited)} ({'; '.join(named)[:160]})" if cited
                          else "the answer shown cites no retrieved passage", group="rag"))
    if traces:
        say(f"\nFULL TRACE (development only, contains document text): {traces[-1]}")
    return results


def _trace(question: str, log) -> List[Result]:
    """One question through the SAME route as chat, every stage printed."""
    from core.orchestration import classifier, task_graph
    from knowledge_fabric import service
    say = log.say
    say(f"\nQUERY:\n  {question}")
    cls = classifier.classify(question)
    plan = task_graph.build_plan(question, cls, None)
    tool = plan[0].tool if plan else "(none - the model would answer alone)"
    asked = plan[0].args.get("question", question) if plan else question
    say(f"\nROUTE:\n  classifier: {cls.kind}  ->  plan: {tool}" + (f"  (question: {asked!r})" if asked != question else ""))
    if not plan or tool != "company_intelligence":
        return [Result("RAG trace: route", FAIL if not plan else WARN,
                       f"this request is not routed to retrieval (plan: {tool})", group="rag")]
    with service.force_trace():
        obj = service.analyze(asked, ctx=_console_context(), use_model=True, use_memory=True, make_artifacts=False)
    r = obj.retrieval
    emb = r.get("embedding") or {}
    say("\nEMBEDDING + SEARCH:")
    say(f"  embedding model {emb.get('model') or '-'}: query {emb.get('query_dims') or '-'} dims; "
        f"{emb.get('vectors_same_dim', 0)} of {emb.get('stored_vectors', 0)} stored vectors compared"
        + (f"  NOTE: {r.get('vector_note')}" if r.get("vector_note") else ""))
    say("  legs: " + ", ".join(f"{k} {v}" for k, v in (r.get("legs") or {}).items()))
    say(f"  rerank: {(r.get('rerank') or {}).get('backend')}  ({(r.get('rerank') or {}).get('scored')} scored)")
    say(f"\nRETRIEVED ({len(obj.evidence)}):")
    for e in obj.evidence[:8]:
        say(f"  [{e['evidence_id']}] {e['citation']}  ({e['origin']}, {e['authority_label']}, score {float(e.get('score') or 0):.3f})")
        say(f"       {(e.get('text') or '')[:220].replace(chr(10), ' ')}")
    inj = r.get("context_injection") or {}
    exchange = {}
    if r.get("trace_file"):
        from pathlib import Path
        exchange = json.loads(Path(r["trace_file"]).read_text(encoding="utf-8"))
    response = _show_exchange(inj, exchange, r.get("narrative") or "", say)
    say(f"\nANSWER SHOWN: {obj.narrative_source} - {r.get('narrative') or ''}")
    say(f"VERIFICATION: {obj.verification_state}")
    cited = set((obj.validation or {}).get("cited") or [])
    by_id = {e["evidence_id"]: e for e in obj.evidence}
    say("\nSOURCES CITED IN THE ANSWER:")
    for ident in sorted(i for i in cited if i in by_id):
        e = by_id[ident]
        say(f"  [{ident}] {e['citation']}  ({e.get('filename') or e.get('title')}, node {e.get('node_id')})")
    if not any(i in by_id for i in cited):
        say("  (no passage cited)")
    others = len([e for e in obj.evidence if e["evidence_id"] not in cited])
    if others:
        say(f"  ...and {others} more passage(s) retrieved and given to the model but not cited")
    if r.get("trace_file"):
        say(f"\nFULL TRACE (development only, contains document text): {r['trace_file']}")
    results = [Result("RAG trace: route", PASS, f"routed to retrieval: {tool}", group="rag"),
               Result("RAG trace: retrieval", PASS if obj.evidence else FAIL,
                      f"{len(obj.evidence)} passage(s)" if obj.evidence else "retrieval returned zero chunks", group="rag")]
    return results + _context_results(inj, response, r.get("narrative") or "")


def run(args, log) -> int:
    from knowledge_fabric import FIRST_YEAR, LAST_YEAR, coverage, ingest
    action = args.action
    needs_target = {"ingest", "ingest-dir", "ingest-year", "ingest-archive", "update", "remove", "provenance"}
    if action in needs_target and not args.target:
        log.say(f"  '{action}' needs a path or document id")
        return 2
    results: List[Result] = []
    if action in ("ingest", "update"):
        result = ingest.ingest_document(args.target, origin=args.origin, force=args.force or action == "update",
                                        overrides=_overrides(args) | ({"year": int(args.year)} if args.year else {}),
                                        embed=False if args.no_embed else None)
        results = _ingest_summary({"results": [result.to_dict()], "files": 1,
                                   **{result.status: 1}}, f"rag {action}")
    elif action == "ingest-dir":
        results = _ingest_summary(ingest.ingest_directory(args.target, origin=args.origin, force=args.force,
                                                          overrides=_overrides(args),
                                                          embed=False if args.no_embed else None,
                                                          progress=lambda i, n, r: log.say(f"  [{i}/{n}] {r.status:8} {r.source_path}")),
                                  "rag ingest-dir")
    elif action == "ingest-year":
        if not args.year.isdigit():
            log.say("  usage: rag ingest-year <folder> <year>")
            return 2
        results = _ingest_summary(ingest.ingest_year(args.target, int(args.year), origin=args.origin, force=args.force,
                                                     overrides=_overrides(args),
                                                     embed=False if args.no_embed else None), "rag ingest-year")
    elif action == "ingest-archive":
        results = _ingest_summary(ingest.ingest_archive(args.target, origin=args.origin, force=args.force,
                                                        overrides=_overrides(args),
                                                        embed=False if args.no_embed else None), "rag ingest-archive")
    elif action == "remove":
        outcome = ingest.remove_document(args.target)
        results = [Result("rag remove", PASS if outcome.get("removed") else FAIL,
                          (f"removed from the index: {outcome.get('title')} (the file itself is untouched)"
                           if outcome.get("removed") else outcome.get("reason", "")), group="rag")]
    elif action == "reindex":
        outcome = ingest.reindex()
        results = [Result("full-text index", PASS if outcome.get("ok") else FAIL,
                          f"{outcome.get('indexed_nodes', 0)} passages indexed", group="rag")]
    elif action == "reembed":
        outcome = ingest.reembed(model_change=args.model_change)
        state = PASS if outcome.get("ok") and (outcome.get("embedded") or not outcome.get("candidates")) else WARN
        results = [Result("vector embeddings", state, json.dumps({k: v for k, v in outcome.items() if k != "ok"}),
                          fallback="" if state == PASS else "lexical + entity + structured retrieval", group="rag")]
    elif action == "rebuild-graph":
        outcome = ingest.rebuild_graph()
        results = [Result("knowledge graph", PASS if outcome.get("ok") else FAIL,
                          f"{outcome.get('nodes', 0)} passages, {outcome.get('mentions', 0)} entity mentions, "
                          f"{outcome.get('relations', 0)} relations, {outcome.get('corrections_reapplied', 0)} human "
                          f"corrections re-applied", group="rag")]
    elif action == "validate":
        results = corpus_results()
    elif action == "provenance":
        from knowledge_fabric import store
        with store.connection() as conn:
            row = conn.execute("SELECT document_id, source_path, filename, title, document_type, year, doc_date, "
                               "confidentiality, origin, authority, source_hash, ingested_at, updated_at "
                               "FROM kf_documents WHERE document_id = ?", (args.target,)).fetchone()
        if not row:
            results = [Result("rag provenance", FAIL, "no such document", group="rag")]
        else:
            log.say(json.dumps(dict(row), indent=2, default=str))
            results = [Result("rag provenance", PASS, row["title"] or row["filename"], group="rag")]
    elif action == "coverage":
        rep = coverage.report(_console_context(), args.first or FIRST_YEAR, args.last or LAST_YEAR)
        for row in rep["years"]:
            log.say(f"  {row['year']}  {row['status']:<10} " + ", ".join(
                f"{k}={v}" for k, v in row.items() if k not in ("year", "status") and v and not isinstance(v, (list, dict))))
        log.say(f"  missing document types: {', '.join(rep['missing_document_types']) or 'none'}")
        log.say(f"  missing departments:    {', '.join(rep['missing_departments']) or 'none'}")
        s = rep["summary"]
        results = [Result("company records coverage", PASS if not s["missing_years"] else WARN,
                          f"{s['available_years']} available, {s['sample_only_years']} sample-only, "
                          f"{s['general_only_years']} general-only, {s['missing_years']} missing", group="rag")]
    elif action == "status":
        results = corpus_results()
    elif action == "evaluate":
        results = evaluation_results(progress=lambda m: log.say(f"  {m}"))
    elif action == "models":
        results = model_checks()
    elif action == "trace":
        if not args.target.strip():
            log.say('  usage: rag trace "<question>"')
            return 2
        results = trace(" ".join(filter(None, [args.target, args.year])).strip(), log, fixture=args.fixture,
                        spoken=args.spoken)
    for r in results:
        log.result(r)
    return 1 if any(r.status == FAIL for r in results) else 0
