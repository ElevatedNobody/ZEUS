"""
Every RAG stage checked on its own, then the hand-over into the model request:

    ingest -> chunk -> embed -> store -> query embedding -> vector search ->
    rerank -> context -> MODEL REQUEST -> answer -> sources

The embedding model is replaced by a deterministic local one (hashed words ->
64 dimensions) so the vector stages run without Ollama; the model client is
replaced by one that records exactly what it was sent. Nothing else is faked.
"""

import hashlib
import json

import numpy as np
import pytest

from knowledge_fabric import ingest, retrieval, service, store

DIM = 64
SOP = """# SOP-TST-007 Reactor feed pump P-701 start-up procedure

SYNTHETIC TEST DOCUMENT - NOT REAL DATA. Standard operating procedure. Status: approved.

## Steps

1. Confirm the suction valve of P-701 is fully open and the casing is vented.
2. Check the mechanical seal flush flow is established before start.
3. Start P-701 against a closed discharge valve and open it within 30 seconds.

## Safety

Never run P-701 dry: the seal fails within minutes.
"""
NOTE = """# Canteen notice

SYNTHETIC TEST DOCUMENT. The canteen serves rice and dal on weekdays; parking permits are renewed in January.
"""


def _vector(text):
    v = np.zeros(DIM, dtype="float32")
    for word in text.lower().split():
        v[int(hashlib.md5(word.strip(".,:;").encode()).hexdigest(), 16) % DIM] += 1.0
    return v.tolist()


@pytest.fixture
def db(tmp_path, monkeypatch):
    from core.capabilities import embedding_provider
    monkeypatch.setattr(type(embedding_provider), "resolve", lambda self, force=False: "test-embed")
    monkeypatch.setattr(type(embedding_provider), "embed", lambda self, text, timeout=60.0: _vector(text))
    (tmp_path / "SOP_TST_007_Reactor_Feed_Pump.md").write_text(SOP, encoding="utf-8")
    (tmp_path / "Canteen_Notice.md").write_text(NOTE, encoding="utf-8")
    with store.using_database(tmp_path / "kf.db"):
        results = [ingest.ingest_document(tmp_path / name, origin="test_fixture", embed=True)
                   for name in ("SOP_TST_007_Reactor_Feed_Pump.md", "Canteen_Notice.md")]
        yield results


def _ctx():
    from tests.conftest import kf_ctx
    return kf_ctx("engineer")


def test_1_to_4_ingest_chunk_embed_store(db):
    assert [r.status for r in db] == ["ingested", "ingested"]
    with store.connection() as conn:
        chunks = [r["text"] for r in conn.execute("SELECT text FROM kf_nodes WHERE kind = 'paragraph'")]
        vectors = conn.execute("SELECT COUNT(*), MIN(dim), MAX(dim), MIN(model) FROM kf_vectors").fetchone()
    assert any("mechanical seal flush" in c for c in chunks)                  # chunked, text kept
    assert vectors[0] >= 3 and vectors[1] == vectors[2] == DIM and vectors[3] == "test-embed"


def test_5_to_7_query_embedding_vector_search_and_rerank(db):
    result = retrieval.retrieve("How do I start the reactor feed pump?", _ctx(), {"sources": ["fulltext", "vector"]},
                                top_k=5, use_legacy=False)
    d = result.diagnostics
    assert d["embedding"]["query_dims"] == DIM and d["embedding"]["vectors_same_dim"] >= 3
    assert d["legs"]["vector"] >= 1 and d["rerank"]["backend"]
    assert "P-701" in result.evidence[0].text and result.evidence[0].title.startswith("SOP-TST-007")


def test_a_dimension_change_is_reported_not_silently_ignored(db, monkeypatch):
    from core.capabilities import embedding_provider
    monkeypatch.setattr(type(embedding_provider), "embed", lambda self, text, timeout=60.0: [0.1] * 32)
    result = retrieval.retrieve("start the reactor feed pump", _ctx(), {"sources": ["vector"]}, use_legacy=False)
    assert "RAG_REINDEX" in result.diagnostics.get("vector_note", "")


def _record_model(monkeypatch, reply):
    from core.model_router import ModelRouter
    from core.ollama_client import OllamaClient
    sent = []
    monkeypatch.setattr(ModelRouter, "route", lambda self, task_type="default": "test-model")
    monkeypatch.setattr(OllamaClient, "chat", lambda self, model, messages, **k: sent.append(messages) or reply)
    return sent


ANSWER = ("From SOP-TST-007 [E1]:\n1. Confirm the suction valve of P-701 is fully open and the casing is vented. [E1]\n"
          "2. Check the mechanical seal flush flow is established before start. [E1]\n"
          "3. Start P-701 against a closed discharge valve and open it within 30 seconds. [E1]")


def test_8_to_10_the_retrieved_chunks_are_in_the_model_request_and_the_answer_is_sourced(db, monkeypatch):
    sent = _record_model(monkeypatch, ANSWER)
    obj = service.analyze("What is the start-up procedure of the reactor feed pump?", ctx=_ctx(), use_memory=False,
                          use_legacy=False, make_artifacts=False)
    assert len(sent) == 1
    system, user = sent[0][0]["content"], sent[0][1]["content"]
    assert "Answer ONLY from the sections below" in system                     # told to use the context, not memory
    assert "## ENTERPRISE EVIDENCE" in user and "Confirm the suction valve of P-701" in user
    assert "numbered steps in the order the evidence gives them" in user          # the procedure-shaped task
    injection = obj.retrieval["context_injection"]
    assert injection["ok"] and injection["model_called"] and injection["evidence_in_prompt"] == injection["evidence_retrieved"]
    assert obj.narrative_source == "model (validated)" and "open it within 30 seconds" in obj.answer
    assert "E1" in obj.validation["cited"] and obj.evidence[0]["title"].startswith("SOP-TST-007")


def test_an_answer_from_general_knowledge_is_rejected(db, monkeypatch):
    """A model that ignores the evidence and invents a number is not shown."""
    _record_model(monkeypatch, "Pumps are usually started at 1450 rpm after 10 minutes of warm-up [E1].")
    obj = service.analyze("What is the start-up procedure of the reactor feed pump?", ctx=_ctx(), use_memory=False,
                          use_legacy=False, make_artifacts=False)
    assert obj.narrative_source == "deterministic" and "1450" not in obj.answer
    assert "discarded" in obj.retrieval["narrative"]


def test_a_listed_but_unused_company_passage_does_not_make_the_answer_verified(tmp_path, monkeypatch):
    """The evidence list under an answer names every retrieved passage. Only what
    the answer itself cites decides its origin - otherwise an answer drawn from a
    sample SOP was shown as VERIFIED company data because a company passage was listed."""
    import re
    from core.capabilities import embedding_provider
    from core.model_router import ModelRouter
    from core.ollama_client import OllamaClient
    monkeypatch.setattr(type(embedding_provider), "resolve", lambda self, force=False: "test-embed")
    monkeypatch.setattr(type(embedding_provider), "embed", lambda self, text, timeout=60.0: _vector(text))
    (tmp_path / "SOP_TST_007_Reactor_Feed_Pump.md").write_text(SOP, encoding="utf-8")
    (tmp_path / "Pump_Log.md").write_text("# P-701 reactor feed pump log\n\nSYNTHETIC TEST DOCUMENT. The reactor feed "
                                          "pump P-701 was started on the day shift after the seal flush check.\n",
                                          encoding="utf-8")

    def chat(self, model, messages, **kwargs):
        sop = re.search(r"\[(E\d+)\] SOP-TST-007", messages[-1]["content"]).group(1)
        return f"Per SOP-TST-007 [{sop}]: open the suction valve, check the seal flush, then start P-701 [{sop}]."
    monkeypatch.setattr(ModelRouter, "route", lambda self, task_type="default": "test-model")
    monkeypatch.setattr(OllamaClient, "chat", chat)
    with store.using_database(tmp_path / "kf.db"):
        ingest.ingest_document(tmp_path / "SOP_TST_007_Reactor_Feed_Pump.md", origin="sample", embed=True)
        ingest.ingest_document(tmp_path / "Pump_Log.md", origin="company", embed=True)
        obj = service.analyze("What is the start-up procedure of the reactor feed pump P-701?", ctx=_ctx(),
                              use_memory=False, use_legacy=False, make_artifacts=False)
    assert obj.narrative_source == "model (validated)"
    assert {e["origin"] for e in obj.evidence} == {"sample", "company"}         # both retrieved and listed
    assert "### Evidence" in obj.answer and "company" not in obj.cited_origins()
    assert obj.cited_origins() == ["sample"] and obj.verification_state == "VALIDATED"


def test_zero_chunks_is_said_and_the_model_is_not_asked(tmp_path, monkeypatch):
    sent = _record_model(monkeypatch, ANSWER)
    with store.using_database(tmp_path / "empty.db"):                       # nothing ingested at all
        obj = service.analyze("What is the start-up procedure of the reactor feed pump?", ctx=_ctx(),
                              use_memory=False, use_legacy=False, make_artifacts=False)
    injection = obj.retrieval["context_injection"]
    assert not obj.evidence and not sent and not injection["model_called"]
    assert "zero chunks" in injection["reason"] and obj.verification_state == "INSUFFICIENT_DATA"


def test_the_development_trace_is_off_by_default_and_complete_when_on(db, monkeypatch, tmp_path):
    monkeypatch.setenv("AGNI_LOG_DIR", str(tmp_path / "logs"))
    _record_model(monkeypatch, ANSWER)
    obj = service.analyze("What is the start-up procedure of the reactor feed pump?", ctx=_ctx(), use_memory=False,
                          use_legacy=False, make_artifacts=False)
    assert "trace_file" not in obj.retrieval and not (tmp_path / "logs" / "rag_trace").exists()
    with service.force_trace():
        obj = service.analyze("What is the start-up procedure of the reactor feed pump?", ctx=_ctx(), use_memory=False,
                              use_legacy=False, make_artifacts=False)
    trace = json.loads(open(obj.retrieval["trace_file"], encoding="utf-8").read())
    user = trace["model_request"]["messages"][1]["content"]
    assert trace["question"].startswith("What is the start-up") and trace["retrieved"][0]["id"] == "E1"
    assert "Confirm the suction valve of P-701" in user and trace["model_response"] == ANSWER
    assert trace["context_injection"]["ok"] and trace["sources"]
