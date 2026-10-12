"""
RAG evaluation - does the pipeline find, compute, cite and refuse correctly?

It runs on the labelled SYNTHETIC test-fixture corpus
(tests/fixtures/knowledge_fabric/corpus - "Test Fixture Refinery", which does
not exist) in a TEMPORARY database, so:

  * the real knowledge base is never read or changed (the database switch is
    context-local - the running app keeps using the real one);
  * the answers are known in advance, so every check is a real pass or fail;
  * it measures the METHOD. It says nothing about how complete the company's own
    records are - that is the coverage report's job.

Categories: retrieval, temporal, entity resolution, calculation, citation
validity, source conflicts, access control (no leakage), prompt-injection
handling, abstention (no evidence -> no invented answer), artifacts (one
analysis -> verified files that agree with it).
"""

from __future__ import annotations

import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from knowledge_fabric import ORIGIN_FIXTURE, store

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "knowledge_fabric" / "corpus"
CONFIDENTIAL_FIXTURES = ("TFR_Board_Minutes_2021.md",)


def _ctx(role: str, authenticated: bool = True):
    from knowledge_fabric.access import AccessContext
    return AccessContext(f"eval_{role}", role, authenticated)


def _titles(obj) -> List[str]:
    return [e["title"] for e in obj.evidence]


def _calc(obj, fragment: str) -> Optional[Dict[str, Any]]:
    return next((c for c in obj.calculations if fragment in c["name"] and c.get("ok")), None)


# Each case: (id, category, role, question, check(obj) -> (ok, detail))
def _cases() -> List[tuple]:
    def fact_lookup(obj):
        ok = (obj.plan["scope"].get("window") in ((2019, 2019), [2019, 2019]) and "120" in obj.answer
              and any(e.startswith("UNIT:5") for e in obj.plan.get("entities", [])))
        return ok, "U5 resolved to Unit 5, 2019 figure 120 from the maintenance report"

    def ranking(obj):
        calc = _calc(obj, "maintenance cost by entity")
        top = calc["value"][0] if calc else {}
        return bool(calc) and top.get("entity") == "Unit 5" and abs(top.get("total", 0) - 335) < 1e-6, \
            "Unit 5 ranked first with 80 + 120 + 135 = 335"

    def losses(obj):
        window = tuple(obj.plan["scope"].get("window") or ())
        evidenced = [d for d in obj.drivers if d["kind"] == "evidenced"]
        conflict = any(c.get("type") == "numeric" and c.get("period") == "2021" for c in obj.contradictions)
        restated = next((p for s in obj.series for p in s["points"] if p["year"] == 2021), None)
        return (window == (2018, 2021) and len(evidenced) >= 2 and conflict and restated is not None
                and restated["value"] == -80 and bool(obj.recommendations)), \
            "period 2019-2021 plus 2018 as the baseline; stated causes found; the 2021 restatement (-85 -> -80) " \
            "flagged as a conflict and the later report preferred; recommendations produced"

    def comparison(obj):
        return obj.plan["scope"].get("points") == [2016, 2023] and "4,350" in obj.answer and "4,900" in obj.answer, \
            "both years compared: 4,350 and 4,900"

    def relative(obj):
        scope = obj.plan["scope"]
        return (scope.get("start"), scope.get("end")) == (2019, 2023), \
            "'last five years' resolved against the latest year in the records (2023): 2019-2023"

    def average(obj):
        calc = _calc(obj, "average net profit")
        expected = (310 + 295 + 280 + 240 + 150) / 5
        return bool(calc) and abs(calc["value"] - expected) < 1e-6, f"average net profit 2015-2019 = {expected:g}"

    def cagr(obj):
        calc = _calc(obj, "CAGR of revenue 2015-2019")
        expected = round(((4300 / 4200) ** (1 / 4) - 1) * 100, 3)
        return bool(calc) and abs(calc["value"] - expected) < 1e-9, f"CAGR 2015-2019 = {expected}%"

    def negative_cagr_refused(obj):
        calc = next((c for c in obj.calculations if c["name"].startswith("CAGR of net profit")), None)
        return bool(calc) and not calc.get("ok") and "positive" in " ".join(calc.get("notes", [])), \
            "CAGR over a period ending in a loss is refused, not computed"

    def procedure(obj):
        return bool(obj.evidence) and obj.evidence[0]["document_type"] == "sop", "the SOP is the first evidence"

    def root_cause(obj):
        edges = [e for e in obj.graph_edges if e["relation"] == "FAILED_DUE_TO" and "bearing" in e["dst_name"]]
        return bool(edges) and "bearing wear" in obj.answer, "C-501 FAILED_DUE_TO bearing wear, cited from the graph"

    def abstain(obj):
        return not obj.evidence and not obj.facts and "No document you can access" in obj.answer, \
            "no records for 1995 -> says so, invents nothing"

    def viewer_no_leak(obj):
        leaked = any("Board Minutes" in t for t in _titles(obj)) or "BLUEHERON" in obj.answer or "42 Cr" in obj.answer
        return not leaked, "a viewer never sees the confidential board minutes - not in evidence, answer or citations"

    def engineer_sees(obj):
        return any("Board Minutes" in t for t in _titles(obj)[:2]), "an engineer may see them, ranked first"

    def injection(obj):
        flagged = [e for e in obj.evidence if any(f.startswith("instruction-like") for f in e.get("flags", []))]
        return bool(flagged) and "quoted as data" in obj.answer, \
            "the vendor note's 'ignore all previous instructions' is flagged and quoted as data"

    def unauthenticated(obj):
        return not obj.evidence and obj.validation.get("reason") == "not authenticated", "no session -> nothing searched"

    return [
        ("temporal_entity_lookup", "entity", "engineer", "What was the maintenance cost of U5 in 2019?", fact_lookup),
        ("entity_ranking", "calculation", "engineer", "Which unit had the highest maintenance cost?", ranking),
        ("root_cause_losses", "reasoning", "engineer",
         "Why did we make losses between 2019 and 2021 and what can we do?", losses),
        ("comparison_points", "temporal", "engineer", "Compare revenue in 2016 and 2023", comparison),
        ("relative_period", "temporal", "engineer", "What was the net profit trend over the last five years?", relative),
        ("average", "calculation", "engineer", "Calculate the average net profit from 2015 to 2019", average),
        ("cagr", "calculation", "engineer", "What was the CAGR of revenue from 2015 to 2019?", cagr),
        ("cagr_refusal", "calculation", "engineer", "What was the CAGR of net profit from 2016 to 2021?",
         negative_cagr_refused),
        ("procedure", "retrieval", "engineer", "How do we start up the hydrogen generation unit?", procedure),
        ("graph_cause", "knowledge_graph", "engineer", "Why did C-501 fail?", root_cause),
        ("abstention", "abstention", "engineer", "What was revenue in 1995?", abstain),
        ("access_viewer", "access_control", "viewer", "What did the board approve for Unit 5?", viewer_no_leak),
        ("access_engineer", "access_control", "engineer", "What did the board approve for Unit 5?", engineer_sees),
        ("prompt_injection", "injection", "engineer", "What did the vendor do on compressor C-501 in 2022?", injection),
        ("unauthenticated", "access_control", None, "What was the net profit in 2019?", unauthenticated),
    ]


def run(keep: bool = False, progress: Optional[Callable[[str], None]] = None,
        artifacts: bool = True) -> Dict[str, Any]:
    """Run every case. Returns {status: PASS|FAIL|NOT_TESTED, cases, summary}."""
    say = progress or (lambda _m: None)
    if not FIXTURE_DIR.is_dir() or not list(FIXTURE_DIR.glob("*.md")):
        return {"status": "NOT_TESTED", "reason": f"test-fixture corpus not found at {FIXTURE_DIR}", "cases": []}
    from knowledge_fabric import ingest, renderers, service
    workdir = Path(tempfile.mkdtemp(prefix="medha_rag_eval_"))
    started = time.time()
    results: List[Dict[str, Any]] = []
    try:
        with store.using_database(workdir / "knowledge_fabric_eval.db"), renderers.writing_to(workdir / "artifacts"):
            for path in sorted(FIXTURE_DIR.glob("*.md")):
                overrides = {"confidentiality": "confidential"} if path.name in CONFIDENTIAL_FIXTURES else {}
                result = ingest.ingest_document(path, origin=ORIGIN_FIXTURE, overrides=overrides, embed=False)
                if result.status not in ("ingested", "updated"):
                    return {"status": "FAIL", "reason": f"fixture not ingested: {path.name} - {result.detail}",
                            "cases": []}
            say(f"ingested {len(list(FIXTURE_DIR.glob('*.md')))} fixture documents")
            losses_obj = None
            for case_id, category, role, question, check in _cases():
                ctx = _ctx(role or "viewer", authenticated=role is not None)
                t0 = time.time()
                try:
                    obj = service.analyze(question, ctx=ctx, use_model=False, use_memory=False, make_artifacts=False,
                                           use_legacy=False)   # isolated from the real stores
                    ok, expected = check(obj)
                    cited_ok = obj.validation.get("ok") if role is not None else True
                    detail = {"verified": obj.verified, "evidence": len(obj.evidence),
                              "unsupported_numbers": obj.validation.get("unsupported_numbers", []),
                              "invalid_citations": obj.validation.get("invalid_citations", [])}
                    passed = bool(ok and cited_ok)
                    if case_id == "root_cause_losses":
                        losses_obj = obj
                except Exception as error:
                    passed, expected, detail = False, "ran without error", {"error": f"{type(error).__name__}: {error}"}
                results.append({"id": case_id, "category": category, "role": role or "not signed in",
                                "question": question, "status": "PASS" if passed else "FAIL",
                                "expected": expected, "detail": detail, "seconds": round(time.time() - t0, 2)})
                say(f"{'PASS' if passed else 'FAIL'}  {case_id}")
            if artifacts:
                results += _artifact_cases(losses_obj, say)
    finally:
        if not keep:
            shutil.rmtree(workdir, ignore_errors=True)
    by_category: Dict[str, Dict[str, int]] = {}
    for r in results:
        entry = by_category.setdefault(r["category"], {"passed": 0, "total": 0})
        entry["total"] += 1
        entry["passed"] += r["status"] == "PASS"
    passed = sum(1 for r in results if r["status"] == "PASS")
    return {"status": "PASS" if passed == len(results) and results else "FAIL",
            "passed": passed, "total": len(results), "by_category": by_category, "cases": results,
            "seconds": round(time.time() - started, 1), "corpus": "SYNTHETIC test fixture (Test Fixture Refinery)",
            "workdir": str(workdir) if keep else None,
            "note": "measures the retrieval / reasoning method on known answers - not the completeness of the "
                    "company's own records (see the coverage report)"}


def _artifact_cases(obj, say) -> List[Dict[str, Any]]:
    from knowledge_fabric import renderers
    out = []
    if obj is None:
        return [{"id": "artifacts", "category": "artifacts", "role": "engineer", "question": "(losses analysis)",
                 "status": "FAIL", "expected": "an analysis to render", "detail": {"error": "no analysis"},
                 "seconds": 0}]
    for fmt in ("xlsx", "docx", "pptx", "pdf"):
        t0 = time.time()
        result = renderers.create(obj, fmt)
        passed = bool(result.get("ok") and result.get("verified"))
        out.append({"id": f"artifact_{fmt}", "category": "artifacts", "role": "engineer",
                    "question": "render the losses analysis", "status": "PASS" if passed else "FAIL",
                    "expected": f"{fmt} opens, passes its validator, carries the analysis id and states no number "
                                f"the analysis does not hold",
                    "detail": {"file_check": result.get("file_check"), "consistency": result.get("consistency"),
                               "error": result.get("error")}, "seconds": round(time.time() - t0, 2)})
        say(f"{'PASS' if passed else 'FAIL'}  artifact_{fmt}")
    return out


def report_text(result: Dict[str, Any]) -> str:
    lines = [f"ZEUS RAG evaluation: {result['status']}"
             + (f" - {result['passed']}/{result['total']} checks" if result.get("total") else ""),
             f"corpus: {result.get('corpus', '-')}", ""]
    if result.get("reason"):
        lines.append(f"reason: {result['reason']}")
    for r in result.get("cases", []):
        lines.append(f"  {r['status']:4}  {r['category']:16} {r['id']:26} {r['expected']}")
        if r["status"] != "PASS":
            lines.append(f"        detail: {r['detail']}")
    if result.get("by_category"):
        lines.append("")
        for category, entry in sorted(result["by_category"].items()):
            lines.append(f"  {category:16} {entry['passed']}/{entry['total']}")
    if result.get("note"):
        lines += ["", result["note"]]
    return "\n".join(lines)
