"""
The broad functional suites, reused rather than re-written:

  offline_selftest()   scripts/offline_test.py - PDF, OCR, vision pipeline,
                       voice providers, embeddings, reranker, RAG knowledge
                       base, DOCX/XLSX/PPTX generation, the industrial plant
                       layer, the orchestration pipeline (agents + tools),
                       and the external-dependency (no phone-home) check
  regression()         the pytest suite in tests/ (excluding the UI e2e run)

Their results are mapped into the ZEUS states; SKIP becomes NOT_TESTED, never
PASS.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List

from agni_ops.status import FAIL, NOT_TESTED, PASS, UNAVAILABLE, WARN, Result

ROOT = Path(__file__).resolve().parent.parent

# offline_test check -> report group
GROUPS = {"Ollama": "models", "Local Models": "models", "PDF Engine": "documents", "OCR": "ocr",
          "Vision Pipeline": "vision", "Voice providers": "tts", "Embedding Model": "models",
          "Reranker": "rag", "Knowledge Base": "rag", "Document Generation": "artifacts",
          "Industrial Plant Layer": "tools", "Orchestration Pipeline": "agents",
          "Local Inference": "models", "External Dependency Check": "security"}
_STATE = {"PASS": PASS, "FAIL": FAIL, "WARN": WARN, "SKIP": NOT_TESTED}


def offline_selftest(deep: bool = False, only_groups: List[str] = None) -> List[Result]:
    out = Path(tempfile.mkdtemp(prefix="agni_selftest_")) / "offline_test.json"
    command = [sys.executable, str(ROOT / "scripts" / "offline_test.py"), "--quiet", "--json", str(out)]
    if deep:
        command.append("--deep")
    try:
        run = subprocess.run(command, capture_output=True, text=True, timeout=1800, cwd=str(ROOT),
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        data = json.loads(out.read_text(encoding="utf-8"))
    except Exception as error:
        return [Result("offline self-test", FAIL, reason=f"did not complete: {error}",
                       where="scripts/offline_test.py", group="agents")]
    results = []
    for check in data.get("results", []):
        group = GROUPS.get(check["check"], "tools")
        if only_groups and group not in only_groups:
            continue
        results.append(Result(check["check"], _STATE.get(check["status"], FAIL), group=group,
                              reason=check.get("detail", ""), where="scripts/offline_test.py",
                              evidence=json.dumps(check.get("evidence", {}), default=str)[:1500],
                              duration_s=check.get("seconds", 0)))
    blocked = data.get("blocked_external_attempts") or []
    if blocked:
        results.append(Result("no phone-home during self-test", FAIL, group="security",
                              reason=f"external connection attempts: {', '.join(blocked)[:300]}"))
    return results


def regression(timeout: float = 3600) -> List[Result]:
    """The pytest suite. Needs pytest (requirements-dev.txt)."""
    import importlib.util
    if importlib.util.find_spec("pytest") is None:
        return [Result("regression suite (pytest)", UNAVAILABLE, group="regression",
                       reason="pytest is not installed - pip install -r requirements-dev.txt")]
    report = Path(tempfile.mkdtemp(prefix="agni_pytest_")) / "junit.xml"
    command = [sys.executable, "-m", "pytest", "tests", "--ignore=tests/e2e", "-q", "-p",
               "no:cacheprovider", "-rfE", f"--junitxml={report}"]
    try:
        run = subprocess.run(command, capture_output=True, text=True, timeout=timeout, cwd=str(ROOT),
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return [Result("regression suite (pytest)", FAIL, group="regression",
                       reason=f"did not finish within {int(timeout / 60)} minutes")]
    text = run.stdout or ""
    summary = next((line for line in reversed(text.splitlines())
                    if re.search(r"\d+ (passed|failed)", line)), "no summary")
    failed = [line.split(" ", 1)[1].split(" - ")[0] for line in text.splitlines()
              if line.startswith(("FAILED ", "ERROR "))]
    known = _known_environment_failures()
    new = [f for f in failed if f not in known]
    results = [Result("regression suite (pytest)", PASS if not failed else (WARN if not new else FAIL),
                      group="regression", reason=summary.strip("= ").strip()
                      + (f"; {len(failed) - len(new)} known environment-dependent" if failed and not new else ""),
                      evidence="\n".join(failed)[:3000], where="tests/",
                      details={"failed": failed, "new_failures": new})]
    for name in new:
        results.append(Result(f"test {name}", FAIL, group="regression", where=name,
                              reason="failed (not a known environment-dependent failure)"))
    return results


def _known_environment_failures() -> Dict[str, str]:
    """Tests documented as failing for reasons of THIS machine's environment,
    each with the reason (tests/KNOWN_ENVIRONMENT_FAILURES.json)."""
    try:
        return json.loads((ROOT / "tests" / "KNOWN_ENVIRONMENT_FAILURES.json").read_text(encoding="utf-8"))
    except Exception:
        return {}
