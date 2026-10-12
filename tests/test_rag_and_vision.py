import sys
import shutil
import random
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import core.builtin_tools as bt
from core.capabilities import embedding_provider
from core.decision_engine import DecisionEngine
from core.security_manager import security_manager
from memory import database

security_manager.set_confirm_callback(lambda msg: True)
engine = DecisionEngine()

RAG_TEST_DIR = Path("/tmp/medha_test_rag")


@pytest.fixture(autouse=True)
def installed_embedding_model():
    """Every test here runs as if this machine has bge-m3 pulled.

    Patching only `embed` is not enough any more, and that is deliberate:
    embedding now resolves a real model name through the capability layer
    first, so a machine with nothing pulled refuses to index instead of
    silently producing an empty index. These tests therefore have to say
    which model is installed - the same thing `ollama list` would say.
    """
    with patch("core.ollama_client.OllamaClient.list_models",
               return_value=["bge-m3:latest", "qwen2.5:3b-instruct-q4_K_M"]):
        embedding_provider._model = None
        yield
        embedding_provider._model = None


def _fake_embed(model, text, timeout=30.0):
    """Deterministic pseudo-embedding that actually discriminates topics,
    so retrieval-ranking tests are meaningful rather than trivially true."""
    random.seed(hash(text) % 10000)
    vec = [random.random() for _ in range(16)]
    lowered = text.lower()
    if any(w in lowered for w in ("newton", "gravity", "inertia", "physics")):
        vec[0] += 5.0
    if any(w in lowered for w in ("egg", "omelette", "pan", "cook")):
        vec[1] += 5.0
    if any(w in lowered for w in ("revolution", "france", "monarchy", "history")):
        vec[2] += 5.0
    return vec


def _cleanup():
    shutil.rmtree(RAG_TEST_DIR, ignore_errors=True)
    RAG_TEST_DIR.mkdir(parents=True, exist_ok=True)
    database.init_db()
    conn = database.get_connection()
    try:
        conn.execute("DELETE FROM document_chunks")
        conn.commit()
    finally:
        conn.close()


def _write_sample_docs():
    (RAG_TEST_DIR / "physics.txt").write_text(
        "Newton's first law states that an object at rest stays at rest and an "
        "object in motion stays in motion unless acted upon by an external force. "
        "This is the law of inertia. Gravity attracts masses toward each other."
    )
    (RAG_TEST_DIR / "cooking.txt").write_text(
        "To make a good omelette, whisk eggs with salt, heat butter in a pan over "
        "medium heat, pour in the eggs, and fold gently once the edges set."
    )
    (RAG_TEST_DIR / "history.txt").write_text(
        "The French Revolution began in 1789 and ended the monarchy in France, "
        "establishing a republic and transforming French politics and society."
    )


# --- routing ---

def test_index_folder_routes_correctly():
    direct = engine._match_direct_command("index folder /tmp/my_notes")
    assert direct == ("index_folder", {"path": "/tmp/my_notes"})


def test_ask_knowledge_base_routes_correctly():
    direct = engine._match_direct_command("search my documents for gravity")
    assert direct == ("ask_knowledge_base", {"query": "gravity"})


def test_clear_knowledge_base_routes_correctly():
    direct = engine._match_direct_command("clear my knowledge base")
    assert direct == ("clear_knowledge_base", {})


def test_analyze_image_routes_correctly_for_screen_and_file():
    d1 = engine._match_direct_command("analyze this screen")
    assert d1[0] == "analyze_image"
    d2 = engine._match_direct_command("analyze the image /tmp/photo.jpg")
    assert d2 == ("analyze_image", {"image_path": "/tmp/photo.jpg"})


def test_read_screen_route_unaffected_by_new_vision_routes():
    """analyze_image and read_screen must stay distinct - OCR text
    extraction vs actual visual understanding are different tools."""
    assert engine._match_direct_command("what is on my screen")[0] == "read_screen"
    assert engine._match_direct_command("read my screen")[0] == "read_screen"


# --- indexing & chunking ---

def test_index_folder_produces_real_chunks_in_database():
    _cleanup()
    _write_sample_docs()
    with patch("core.ollama_client.OllamaClient.is_available", return_value=True), \
         patch("core.ollama_client.OllamaClient.embed", side_effect=_fake_embed):
        r = bt.index_folder(str(RAG_TEST_DIR))
        assert r["files_indexed"] == 3
        assert r["chunks_indexed"] == 3

    chunks = database.get_all_chunks()
    assert len(chunks) == 3
    assert any("Newton" in c["chunk_text"] for c in chunks)
    _cleanup()


def test_reindexing_replaces_not_duplicates():
    _cleanup()
    _write_sample_docs()
    with patch("core.ollama_client.OllamaClient.is_available", return_value=True), \
         patch("core.ollama_client.OllamaClient.embed", side_effect=_fake_embed):
        bt.index_folder(str(RAG_TEST_DIR))
        bt.index_folder(str(RAG_TEST_DIR))  # index again, same files

    chunks = database.get_all_chunks()
    assert len(chunks) == 3  # not 6 - re-indexing replaced, didn't duplicate
    _cleanup()


def test_index_folder_rejects_nonexistent_path():
    r = bt.index_folder("/tmp/definitely_does_not_exist_xyz_123")
    assert "error" in r


def test_index_folder_reports_ollama_offline_clearly():
    with patch("core.ollama_client.OllamaClient.is_available", return_value=False):
        r = bt.index_folder(str(RAG_TEST_DIR))
        assert "error" in r
        assert "ollama" in r["error"].lower()


# --- retrieval quality: this is the real test of RAG working, not fake ---

def test_retrieval_ranks_by_actual_relevance_not_arbitrarily():
    """The core proof RAG works: a question about history must surface the
    history document specifically, not physics or cooking, when forced to
    pick just one (top_k=1)."""
    _cleanup()
    _write_sample_docs()
    with patch("core.ollama_client.OllamaClient.is_available", return_value=True), \
         patch("core.ollama_client.OllamaClient.embed", side_effect=_fake_embed):
        bt.index_folder(str(RAG_TEST_DIR))

        with patch("core.ollama_client.OllamaClient.chat", return_value="mock grounded answer"):
            r = bt.ask_knowledge_base("tell me about the french revolution", top_k=1)
            assert r["sources"]["documents"] == ["history.txt"]

            r2 = bt.ask_knowledge_base("what is newton's first law", top_k=1)
            assert r2["sources"]["documents"] == ["physics.txt"]

            r3 = bt.ask_knowledge_base("how do I make an omelette", top_k=1)
            assert r3["sources"]["documents"] == ["cooking.txt"]
    _cleanup()


def test_ask_knowledge_base_reranks_more_candidates_than_it_keeps():
    """A reranker handed exactly the passages that will be kept cannot change
    anything. Retrieval must widen first, so several relevant documents
    compete for one slot."""
    _cleanup()
    _write_sample_docs()
    for n in range(3):
        (RAG_TEST_DIR / f"physics_notes_{n}.txt").write_text(
            f"Note {n} on Newton and inertia: gravity acts on every mass, and "
            f"an object in motion continues in motion. Physics revision sheet.")
    with patch("core.ollama_client.OllamaClient.is_available", return_value=True), \
         patch("core.ollama_client.OllamaClient.embed", side_effect=_fake_embed):
        bt.index_folder(str(RAG_TEST_DIR))
        with patch("core.ollama_client.OllamaClient.chat", return_value="mock answer"):
            r = bt.ask_knowledge_base("what is newton's first law", top_k=1)
    retrieval = r["retrieval"]
    assert retrieval["candidates"] > retrieval["returned"], (
        f"the reranker was given {retrieval['candidates']} passage(s) and kept "
        f"{retrieval['returned']} - it had nothing to choose between")
    assert retrieval["reranker"].get("backend"), "no reranking was recorded"
    _cleanup()


def test_ask_knowledge_base_fails_clearly_with_nothing_indexed():
    _cleanup()
    with patch("core.ollama_client.OllamaClient.is_available", return_value=True):
        r = bt.ask_knowledge_base("anything")
        assert "error" in r
        assert "index" in r["error"].lower()


def test_ask_knowledge_base_answer_is_grounded_with_citations():
    _cleanup()
    _write_sample_docs()
    with patch("core.ollama_client.OllamaClient.is_available", return_value=True), \
         patch("core.ollama_client.OllamaClient.embed", side_effect=_fake_embed):
        bt.index_folder(str(RAG_TEST_DIR))

        with patch("core.ollama_client.OllamaClient.chat",
                   return_value="This is the law of inertia [E1].") as mock_chat:
            r = bt.ask_knowledge_base("what is newton's first law")
            assert "answer" in r
            assert r["sources"]["documents"] == ["physics.txt"]

            # The prompt must carry the retrieved passages, not just the
            # question - otherwise the model is answering from memory and the
            # citations are decoration.
            sent_prompt = mock_chat.call_args[0][1][1]["content"]
            assert "EVIDENCE" in sent_prompt
            assert "[E1]" in sent_prompt
            assert "law of inertia" in sent_prompt or "Newton" in sent_prompt

            # And the citation the model produced was checked against what it
            # was actually given.
            assert r["grounded"] is True
            assert "E1" in r["citation_report"]["valid_citations"]
    _cleanup()


def test_an_invented_citation_is_caught_not_passed_through():
    """The failure that matters most in a plant: a source that does not exist.
    The model is given one passage and cites four."""
    _cleanup()
    _write_sample_docs()
    with patch("core.ollama_client.OllamaClient.is_available", return_value=True), \
         patch("core.ollama_client.OllamaClient.embed", side_effect=_fake_embed):
        bt.index_folder(str(RAG_TEST_DIR))
        with patch("core.ollama_client.OllamaClient.chat",
                   return_value="Inertia [E1]. Also confirmed by [E7] and [E9]."):
            r = bt.ask_knowledge_base("what is newton's first law", top_k=1)
    report = r["citation_report"]
    assert set(report["invalid_citations"]) >= {"E7", "E9"}
    assert "E7" not in r["answer"], "an invented citation was left in the answer"
    _cleanup()


# --- forget / clear ---

def test_forget_document_removes_only_that_source():
    _cleanup()
    _write_sample_docs()
    with patch("core.ollama_client.OllamaClient.is_available", return_value=True), \
         patch("core.ollama_client.OllamaClient.embed", side_effect=_fake_embed):
        bt.index_folder(str(RAG_TEST_DIR))

    r = bt.forget_document(str(RAG_TEST_DIR / "cooking.txt"))
    assert r["removed_chunks"] == 1
    remaining = database.get_all_chunks()
    assert len(remaining) == 2
    assert not any("omelette" in c["chunk_text"] for c in remaining)
    _cleanup()


def test_forget_nonexistent_document_reports_clearly():
    r = bt.forget_document("/tmp/never_indexed.txt")
    assert "error" in r


def test_clear_knowledge_base_removes_everything():
    _cleanup()
    _write_sample_docs()
    with patch("core.ollama_client.OllamaClient.is_available", return_value=True), \
         patch("core.ollama_client.OllamaClient.embed", side_effect=_fake_embed):
        bt.index_folder(str(RAG_TEST_DIR))

    r = bt.clear_knowledge_base()
    assert r["cleared_chunks"] == 3
    assert database.get_all_chunks() == []
    _cleanup()


def test_list_knowledge_sources_reflects_real_state():
    _cleanup()
    assert bt.list_knowledge_sources()["sources"] == "Nothing indexed yet."

    _write_sample_docs()
    with patch("core.ollama_client.OllamaClient.is_available", return_value=True), \
         patch("core.ollama_client.OllamaClient.embed", side_effect=_fake_embed):
        bt.index_folder(str(RAG_TEST_DIR))

    sources = bt.list_knowledge_sources()["sources"]
    assert len(sources) == 3
    _cleanup()


# --- vision / multimodal ---

def test_analyze_image_uses_dedicated_vision_model_not_text_model():
    """Regression test for the real bug found while building this: vision
    routing previously pointed at qwen2.5:7b, a text-only model."""
    from PIL import Image
    img_path = RAG_TEST_DIR / "test.png"
    Image.new("RGB", (10, 10), color="red").save(img_path)

    with patch("core.ollama_client.OllamaClient.is_available", return_value=True), \
         patch("core.ollama_client.OllamaClient.chat_vision", return_value="A red square.") as mock_vision:
        r = bt.analyze_image(image_path=str(img_path))
        assert r["description"] == "A red square."
        model_used = mock_vision.call_args[0][0]
        assert model_used != "qwen2.5:7b", "Vision routed to a text-only model - the bug is back"
        # ZEUS: the vision slot prefers Qwen3-VL and falls back through the
        # earlier vision models - it must be ONE OF THEM, never a text model.
        from core.model_router import DEFAULT_PREFERENCES
        assert any(model_used.startswith(p.split(":")[0]) for p in DEFAULT_PREFERENCES["vision"])
    _cleanup()


def test_analyze_image_reports_missing_vision_model_clearly():
    from PIL import Image
    img_path = RAG_TEST_DIR / "test2.png"
    Image.new("RGB", (10, 10)).save(img_path)

    with patch("core.ollama_client.OllamaClient.is_available", return_value=True), \
         patch("core.ollama_client.OllamaClient.chat_vision",
               return_value="[Vision model 'qwen2.5vl:3b-q4_K_M' not found. Try: ollama pull qwen2.5vl:3b-q4_K_M]"):
        r = bt.analyze_image(image_path=str(img_path))
        assert "error" in r
        assert "qwen2.5vl" in r["error"]
    _cleanup()


def test_analyze_image_rejects_nonexistent_file():
    with patch("core.ollama_client.OllamaClient.is_available", return_value=True):
        r = bt.analyze_image(image_path="/tmp/does_not_exist.png")
        assert "error" in r


def test_vision_gets_its_own_model_not_the_text_model():
    from core.model_router import model_router
    model_router._cache.clear()
    from core.model_router import DEFAULT_PREFERENCES
    chosen = model_router.route("vision")
    assert any(chosen.startswith(p.split(":")[0]) for p in DEFAULT_PREFERENCES["vision"])
    model_router._cache.clear()


def test_embedding_cannot_be_routed_to_a_chat_model():
    """The architectural rule, enforced rather than documented: asking the
    chat router for an embedding model is a mistake at the call site, not a
    thing that quietly returns a name the chat endpoint cannot use."""
    from core.model_router import model_router
    for capability in ("embedding", "reranker"):
        with pytest.raises(ValueError, match="not a chat capability"):
            model_router.route(capability)


def test_embedding_resolves_to_the_installed_tag_not_the_preference():
    """`ollama pull bge-m3` installs `bge-m3:latest`. Resolution must return
    the name that is actually installed, because that is the name the
    embeddings endpoint will accept."""
    embedding_provider._model = None
    assert embedding_provider.resolve() == "bge-m3:latest"


if __name__ == "__main__":
    import inspect
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and inspect.isfunction(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                fails += 1
                print(f"FAIL {name}: {e}")
            except Exception as e:
                fails += 1
                print(f"ERROR {name}: {e}")
    _cleanup()
    sys.exit(1 if fails else 0)
