"""
Test isolation for the whole suite. Loaded by pytest before any test module.

The suite used to run against the operator's real machine: it deleted
~/Documents/ZEUS_Documents (the folder ZEUS writes generated documents to),
opened every generated file in Word / Excel / PowerPoint, and appended test
noise to the real logs. On 2026-09-23 that destroyed real documents.

So before anything from the project is imported, every place ZEUS writes to is
pointed at a throw-away directory, the home directory itself is replaced (some
tests build paths from Path.home() at import time), and nothing may open a file
in a desktop application. If this file is removed, the suite is unsafe to run.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import webbrowser
from pathlib import Path

_SANDBOX = Path(tempfile.mkdtemp(prefix="agni_tests_"))
_HOME = _SANDBOX / "home"
for _sub in ("home/Documents", "data", "logs", "documents"):
    (_SANDBOX / _sub).mkdir(parents=True, exist_ok=True)

os.environ["AGNI_TEST_MODE"] = "1"
os.environ["AGNI_DATA_DIR"] = str(_SANDBOX / "data")
os.environ["AGNI_LOG_DIR"] = str(_SANDBOX / "logs")
os.environ["AGNI_DOCUMENTS_DIR"] = str(_SANDBOX / "documents")
os.environ["USERPROFILE"] = str(_HOME)
os.environ["HOME"] = str(_HOME)
# Never let a test reach a real network service other than the fake Ollama
# servers the tests themselves start on loopback.
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
# The voice tests replace faster_whisper with stubs in sys.modules, which a
# worker PROCESS would not see - so they exercise the in-process path. The
# worker (the production default) has its own tests in test_stt_isolation.py.
os.environ.setdefault("AGNI_STT_ISOLATION", "inprocess")

OPENED: list = []


def _record_open(path, *args, **kwargs):
    OPENED.append(str(path))


def _record_browser(url, *args, **kwargs):
    OPENED.append(str(url))
    return False


if hasattr(os, "startfile"):
    os.startfile = _record_open            # noqa: attribute exists on Windows
else:                                      # pragma: no cover - non-Windows
    os.startfile = _record_open            # type: ignore[attr-defined]
webbrowser.open = _record_browser
webbrowser.open_new = _record_browser
webbrowser.open_new_tab = _record_browser


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_SANDBOX, ignore_errors=True)


def sandbox_dir() -> Path:
    """The per-run scratch root, for tests that want to assert on it."""
    return _SANDBOX


# ---- ZEUS knowledge fabric: the labelled synthetic fixture corpus ----------
# tests/fixtures/knowledge_fabric/corpus ("Test Fixture Refinery" - it does not
# exist) ingested ONCE per run into its own database. Tests enter it with the
# `kf` fixture; the switch is context-local (knowledge_fabric.store).

import pytest  # noqa: E402

KF_CONFIDENTIAL = ("TFR_Board_Minutes_2021.md",)


@pytest.fixture(scope="session")
def kf_corpus():
    from knowledge_fabric import ORIGIN_FIXTURE, ingest, store
    corpus = Path(__file__).resolve().parent / "fixtures" / "knowledge_fabric" / "corpus"
    database = _SANDBOX / "kf_fixture" / "knowledge_fabric.db"
    with store.using_database(database):
        for path in sorted(corpus.glob("*.md")):
            overrides = {"confidentiality": "confidential"} if path.name in KF_CONFIDENTIAL else {}
            result = ingest.ingest_document(path, origin=ORIGIN_FIXTURE, overrides=overrides, embed=False)
            assert result.status in ("ingested", "updated"), f"{path.name}: {result.detail}"
    return database


@pytest.fixture
def kf(kf_corpus):
    """Run the test against the fixture corpus database."""
    from knowledge_fabric import store
    with store.using_database(kf_corpus):
        yield kf_corpus


def kf_ctx(role: str = "engineer", authenticated: bool = True, departments=(), vault_unlocked: bool = False):
    from knowledge_fabric.access import AccessContext
    return AccessContext(f"test_{role}", role, authenticated, set(departments), vault_unlocked)


def kf_ask(question: str, role: str = "engineer", **kwargs):
    from knowledge_fabric import service
    options = {"use_model": False, "use_memory": False, "use_legacy": False, "make_artifacts": False}
    options.update(kwargs)
    return service.analyze(question, ctx=kf_ctx(role), **options)
