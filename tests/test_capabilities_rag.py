"""
Capability-aware model access, and RAG end to end.

The bug these exist for: `ollama pull bge-m3` installs `bge-m3:latest`, and
the model matcher accepted `-` and `_` suffixes but not `:`. So an embedding
model that was present and working was reported as
"No models available for task 'embedding'", and RAG looked broken on a machine
that had everything installed.

These tests run against a local HTTP server that speaks Ollama's protocol
(tests/fake_ollama.py). That matters: the failure was an INTERFACE failure -
wrong name form, wrong endpoint - and mocking the client would have hidden it
exactly the way the original code did.

The full pipeline is exercised: ingest, chunk, embed, store, query-embed,
retrieve, rerank, cite, answer - and then again after a simulated restart.
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from memory import database  # noqa: E402

from core import capabilities as cap  # noqa: E402
from core.ollama_client import ollama_client  # noqa: E402
from tests.fake_ollama import FakeOllama  # noqa: E402

DB_PATH = Path(tempfile.gettempdir()) / "medha_test_capabilities.db"
DOCS = Path(tempfile.mkdtemp(prefix="medha_rag_docs_"))

SOP = """Standard Operating Procedure SOP-ME-014
Shell and tube exchanger cleaning.

Section 5.2 Return to service.
Before the exchanger returns to service a hydrotest shall be performed at
1.3 times design pressure and held for thirty minutes. A visual inspection of
the shell side welds shall follow the hydrotest. The mechanical completion
certificate shall be signed by a competent person and the cleaning record
shall be attached to it.

Section 6.1 Records.
The cleaning contractor shall submit the cleaning record within two days.
"""

LOG = """Maintenance log for pump P-204.
The mechanical seal was replaced during the March outage. Vibration on the
outboard bearing was 4.8 mm/s RMS before the repair and 1.9 mm/s after.
Lubricant was changed at the same time.
"""


def _reset_registries():
    cap.embedding_provider._model = None
    cap.embedding_provider._dimension = None
    ollama_client._available = None
    ollama_client._available_checked_at = 0.0
    from core.model_router import model_router
    model_router._cache.clear()


@pytest.fixture
def fresh_db():
    database.DB_PATH = DB_PATH
    if DB_PATH.exists():
        DB_PATH.unlink()
    database.init_db()
    yield DB_PATH


@pytest.fixture
def runtime(fresh_db):
    """A local server speaking Ollama's protocol, wired into the client."""
    original_host = ollama_client.host
    with FakeOllama() as server:
        ollama_client.host = server.host
        _reset_registries()
        yield server
    ollama_client.host = original_host
    _reset_registries()


@pytest.fixture
def corpus():
    (DOCS / "SOP-ME-014.txt").write_text(SOP, encoding="utf-8")
    (DOCS / "P-204-log.txt").write_text(LOG, encoding="utf-8")
    return DOCS


# ------------------------------------------------- the root-cause bug --

def test_an_installed_model_with_a_latest_tag_is_found():
    """`ollama pull bge-m3` gives `bge-m3:latest`. The old matcher missed it,
    which is the whole reason embedding reported as unavailable."""
    assert ollama_client._model_matches("bge-m3:latest", "bge-m3")
    assert ollama_client._model_matches("bge-m3:567m", "bge-m3")
    assert ollama_client._model_matches("qwen2.5:3b-instruct-q4_K_M", "qwen2.5:3b")


def test_the_matcher_still_refuses_a_different_size():
    """Loose matching would be worse than the bug: a 7B request must never be
    satisfied by the 3B model that happens to be installed."""
    assert not ollama_client._model_matches("qwen2.5:3b", "qwen2.5:7b")
    assert not ollama_client._model_matches("qwen2.5:latest", "qwen2.5:7b")


def test_embedding_resolves_and_returns_a_real_vector(runtime):
    report = cap.embedding_provider.check()
    assert report.health is cap.Health.READY, report.detail
    assert report.model.startswith("bge-m3")
    assert report.interface == "ollama_embeddings", \
        "embeddings must not go through the chat interface"
    assert report.evidence["dimension"] > 0
    assert cap.embedding_provider.dimension() == report.evidence["dimension"]


def test_embedding_dimension_is_discovered_not_assumed(runtime):
    cap.embedding_provider.check()
    vector = cap.embedding_provider.embed("relief valve set pressure")
    assert vector is not None
    assert len(vector) == cap.embedding_provider.dimension()


def test_similar_text_embeds_closer_than_unrelated_text(runtime):
    """If this fails, retrieval ordering is meaningless whatever the plumbing."""
    cap.embedding_provider.check()

    def cosine(a, b):
        return sum(x * y for x, y in zip(a, b))

    query = cap.embedding_provider.embed("hydrotest before return to service")
    near = cap.embedding_provider.embed("a hydrotest shall be performed before service")
    far = cap.embedding_provider.embed("the lubricant was changed at the same time")
    assert cosine(query, near) > cosine(query, far)


# ------------------------------------------------------- honest health --

def test_a_missing_model_is_reported_as_missing_not_as_broken(fresh_db):
    original = ollama_client.host
    with FakeOllama(models=["qwen2.5:3b-instruct-q4_K_M"]) as server:
        ollama_client.host = server.host
        _reset_registries()
        report = cap.embedding_provider.check()
        assert report.health is cap.Health.MODEL_MISSING
        assert "ollama pull" in report.detail
    ollama_client.host = original
    _reset_registries()


def test_a_present_model_with_a_dead_endpoint_says_adapter_required(fresh_db):
    """The distinction the brief asks for: MODEL DETECTED / ADAPTER REQUIRED
    is a different problem from NO MODEL AVAILABLE, and says so."""
    original = ollama_client.host
    with FakeOllama(embeddings_enabled=False) as server:
        ollama_client.host = server.host
        _reset_registries()
        report = cap.embedding_provider.check()
        assert report.health is cap.Health.ADAPTER_REQUIRED
        assert "installed" in report.detail
        assert "ADAPTER REQUIRED" in report.line()
    ollama_client.host = original
    _reset_registries()


def test_a_down_runtime_is_reported_as_down(fresh_db):
    original = ollama_client.host
    ollama_client.host = "http://127.0.0.1:1"        # nothing listens here
    _reset_registries()
    report = cap.embedding_provider.check()
    assert report.health is cap.Health.RUNTIME_DOWN
    assert not report.ok
    ollama_client.host = original
    _reset_registries()


def test_nothing_reports_ready_without_a_real_call(fresh_db):
    original = ollama_client.host
    ollama_client.host = "http://127.0.0.1:1"
    _reset_registries()
    for name, report in cap.check_all().items():
        assert report.health is not cap.Health.READY or report.evidence, \
            f"{name} claimed READY with no evidence"
    ollama_client.host = original
    _reset_registries()


# ---------------------------------------------------------- reranking --

def test_reranking_works_without_any_ollama_involvement(fresh_db):
    """Reranking is scoring, not generation. It must not need the runtime."""
    original = ollama_client.host
    ollama_client.host = "http://127.0.0.1:1"
    _reset_registries()
    report = cap.rerank_provider.check()
    assert report.ok, report.detail
    assert report.evidence["probe_ranked_correctly"] is True
    ollama_client.host = original
    _reset_registries()


def test_reranking_is_honest_about_using_the_fallback():
    report = cap.rerank_provider.check()
    if not report.evidence.get("cross_encoder"):
        assert report.health is cap.Health.DEGRADED
        assert "hybrid" in report.detail.lower()


def test_reranker_puts_the_relevant_passage_first():
    rows = [
        {"chunk_text": LOG, "document_title": "P-204 log"},
        {"chunk_text": SOP, "document_title": "SOP-ME-014"},
    ]
    ordered, info = cap.rerank_provider.rerank(
        "what is required before the exchanger returns to service", rows, top_n=2)
    assert ordered
    assert "hydrotest" in ordered[0]["chunk_text"].lower()
    assert info.get("backend")


# ---------------------------------------------------- RAG end to end --

def _index_and_ask(corpus_dir: Path, question: str):
    from core.tool_registry import registry
    indexed = registry.execute("index_folder", path=str(corpus_dir))
    answered = registry.execute("ask_knowledge_base", query=question)
    return indexed, answered


def test_rag_pipeline_end_to_end(runtime, corpus):
    """Ingest, chunk, embed, store, query-embed, retrieve, answer, cite."""
    FakeOllama.set_chat_reply(
        "A hydrotest at 1.3 times design pressure held for thirty minutes, "
        "then a visual inspection of the shell side welds.")

    indexed, answered = _index_and_ask(
        corpus, "what is required before the exchanger returns to service")

    assert indexed.success, indexed.error
    assert indexed.output.get("chunks_indexed", 0) > 0, indexed.output
    assert indexed.output.get("files_indexed", 0) >= 2

    assert answered.success, answered.error
    payload = answered.output
    assert "error" not in payload, payload
    assert payload.get("answer"), "no answer produced"

    # 9-10. the reranker actually ran, and it was given more than it kept
    rerank_info = payload["retrieval"]["reranker"]
    assert rerank_info.get("backend"), f"no reranking recorded: {rerank_info}"
    assert payload["retrieval"]["candidates"] >= payload["retrieval"]["returned"]

    # 11-12. evidence and provenance
    assert payload.get("evidence"), "no evidence table"
    assert all(item["id"].startswith("E") for item in payload["evidence"])
    documents = payload["sources"]["documents"]
    assert documents, "an answer with no citation is not grounded"
    assert any("SOP-ME-014" in name for name in documents), \
        f"the governing document was not cited: {documents}"


def test_vectors_survive_a_restart(runtime, corpus):
    """The store is on disk; a new process must retrieve without re-indexing."""
    FakeOllama.set_chat_reply("Answer from the indexed corpus.")
    _index_and_ask(corpus, "warm the index")

    stored = database.get_all_chunks()
    assert stored, "nothing was written to the vector store"
    dimension = len(stored[0]["embedding"])

    # Simulate a restart: forget every cached handle, keep only the database.
    _reset_registries()
    database.init_db()

    from core.tool_registry import registry
    answered = registry.execute(
        "ask_knowledge_base",
        query="what is required before the exchanger returns to service")
    assert answered.success
    assert "error" not in answered.output, answered.output
    assert answered.output["sources"]["documents"], \
        "retrieval stopped working after restart"

    reloaded = database.get_all_chunks()
    assert len(reloaded) == len(stored), "the store changed across the restart"
    assert len(reloaded[0]["embedding"]) == dimension


def test_rag_says_so_when_the_shelf_is_empty(runtime):
    """Insufficient evidence is a correct answer, not a failure to hide."""
    from core.tool_registry import registry
    database.clear_all_chunks()
    answered = registry.execute("ask_knowledge_base", query="anything at all")
    assert answered.success
    assert "error" in answered.output or "Nothing" in str(answered.output)


def test_indexing_survives_an_empty_and_a_malformed_document(runtime, corpus):
    (corpus / "empty.txt").write_text("", encoding="utf-8")
    (corpus / "binary.txt").write_bytes(b"\x00\x01\x02\x03 not text at all \xff")
    from core.tool_registry import registry
    outcome = registry.execute("index_folder", path=str(corpus))
    assert outcome.success, outcome.error
    assert outcome.output.get("chunks_indexed", 0) > 0, \
        "one bad file stopped the whole index"


def test_indexing_reports_clearly_when_the_runtime_is_down(fresh_db, corpus):
    original = ollama_client.host
    ollama_client.host = "http://127.0.0.1:1"
    _reset_registries()
    from core.tool_registry import registry
    outcome = registry.execute("index_folder", path=str(corpus))
    text = str(outcome.output or outcome.error).lower()
    assert "ollama" in text or "embed" in text, \
        f"a down runtime produced an unhelpful message: {text[:160]}"
    ollama_client.host = original
    _reset_registries()


# --------------------------------------------------------- reporting --

def test_capability_summary_lists_every_capability(runtime):
    data = cap.summary()
    for name in ("embedding", "reranking", "chat", "reasoning", "vision"):
        assert name in data["capabilities"]
    assert "embedding" in data["ready"], data["capabilities"]["embedding"]


def test_no_capability_is_routed_through_the_chat_model_router():
    """The architectural rule: specialised capabilities have their own
    interface. If embedding ever goes back through the chat router, the
    original bug comes back with it."""
    source = (Path(__file__).resolve().parent.parent / "industrial"
              / "knowledge_base.py").read_text(encoding="utf-8")
    assert 'model_router.route("embedding")' not in source
    tools = (Path(__file__).resolve().parent.parent / "core"
             / "builtin_tools.py").read_text(encoding="utf-8")
    assert 'model_router.route("embedding")' not in tools


# ------------------------------------------------- the Qwen3 reranker --

def test_a_qwen3_reranker_is_recognised_as_a_causal_lm(tmp_path):
    """Qwen3-Reranker has no classification head. Loaded as a CrossEncoder it
    gets a randomly initialised one and returns confident noise, which is
    worse than not reranking at all. It must be detected by architecture."""
    import json
    from industrial import reranker

    directory = tmp_path / "qwen3-reranker"
    directory.mkdir()
    (directory / "config.json").write_text(json.dumps(
        {"model_type": "qwen3", "architectures": ["Qwen3ForCausalLM"]}))
    assert reranker._looks_like_qwen3_reranker(directory)

    bge = tmp_path / "bge"
    bge.mkdir()
    (bge / "config.json").write_text(json.dumps(
        {"model_type": "xlm-roberta",
         "architectures": ["XLMRobertaForSequenceClassification"]}))
    assert not reranker._looks_like_qwen3_reranker(bge)


def test_qwen3_weights_without_torch_fall_back_rather_than_mislead(tmp_path):
    """The weights being present is not the same as being able to score with
    them. Without torch the honest result is the hybrid scorer, named as
    such - not a CrossEncoder load that would silently produce noise."""
    import json
    from industrial import reranker

    directory = tmp_path / "qwen3-reranker"
    directory.mkdir()
    (directory / "config.json").write_text(json.dumps(
        {"model_type": "qwen3", "architectures": ["Qwen3ForCausalLM"]}))

    original_dirs = list(reranker.LOCAL_RERANKER_DIRS)
    reranker.LOCAL_RERANKER_DIRS[:] = [directory]
    reranker.reset_backend()
    try:
        backend = reranker.get_backend()
        assert backend.name == "hybrid_local"
        assert "qwen3" in backend.detail.lower()
        assert not backend.is_cross_encoder
    finally:
        reranker.LOCAL_RERANKER_DIRS[:] = original_dirs
        reranker.reset_backend()


def test_pair_scoring_backends_are_reported_as_pair_scorers():
    """The distinction a report has to be able to make: did something read the
    query and the passage together, or did it only reorder cosine scores?"""
    from industrial import reranker
    for name, expected in (("cross_encoder_flagembedding", True),
                           ("cross_encoder_sentence_transformers", True),
                           ("qwen3_local_adapter", True),
                           ("hybrid_local", False)):
        backend = reranker.RerankBackend(name, "test")
        assert backend.is_cross_encoder is expected, name
