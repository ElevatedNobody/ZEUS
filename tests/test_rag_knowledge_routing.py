"""
"Read the refinery SOP from the knowledge base", "Use the RAG information to answer
this" ... must RETRIEVE before the model answers. They used to reach read_file
("read ..." taken as a file name) or the plain model with nothing retrieved.

Routing is checked with the real classifier and planner; the full chat path
(DecisionEngine -> orchestrator -> company_intelligence -> knowledge fabric ->
model request) runs on the labelled SYNTHETIC fixture corpus with the model client
replaced by one that records exactly what it was sent.
"""

import pytest

from core.orchestration import task_graph
from core.orchestration.classifier import classify


def _plan(text, direct=None, history=None):
    return task_graph.build_plan(text, classify(text), direct, history=history)


@pytest.mark.parametrize("text, expected_question", [
    ("Read the refinery SOP from the knowledge base.", "Read the refinery SOP"),
    ("Search the knowledge base for the maintenance procedure.", "the maintenance procedure"),
    ("According to the uploaded SOP, what is the procedure?", "According to the uploaded SOP, what is the procedure"),
    ("What does the HSE SOP say about permits?", "What does the HSE SOP say about permits"),
    ("Use the RAG information: what is the start-up sequence of the hydrogen unit?",
     "what is the start-up sequence of the hydrogen unit"),
])
def test_knowledge_requests_retrieve_first(text, expected_question):
    plan = _plan(text)
    assert [s.tool for s in plan] == ["company_intelligence"], plan
    assert plan[0].args["question"] == expected_question


def test_read_the_sop_is_not_a_file_name():
    """The route table matches 'read <anything>' as read_file - a knowledge request wins."""
    plan = _plan("Read the refinery SOP from the knowledge base.", direct=("read_file", {"path": "the refinery SOP"}))
    assert plan[0].tool == "company_intelligence"


@pytest.mark.parametrize("text", ["Use the RAG information to answer this.", "Read this RAG.",
                                  "Use the relevant enterprise knowledge."])
def test_use_the_rag_answers_the_previous_question_from_the_records(text):
    history = [{"role": "user", "content": "What is the start-up procedure of the hydrogen unit?"},
               {"role": "assistant", "content": "Generally, a hydrogen unit is started by ..."}]
    plan = _plan(text, history=history)
    assert plan[0].tool == "company_intelligence"
    assert plan[0].args["question"] == "What is the start-up procedure of the hydrogen unit?"


def test_with_nothing_asked_yet_it_says_what_the_knowledge_base_holds():
    assert [s.tool for s in _plan("Read this RAG.")] == ["knowledge_overview"]


@pytest.mark.parametrize("text, tool", [
    ("read C:/Reports/Unit5_review.docx", "read_file"),                       # a real file stays a file
    ("search my documents for maintenance procedure", "ask_knowledge_base"),  # the existing grounded route
    ("rag status", "knowledge_stats"),
    ("index my knowledge base", "index_my_documents"),
])
def test_existing_routes_keep_working(text, tool):
    from core.decision_engine import DecisionEngine
    direct = DecisionEngine._match_direct_command(DecisionEngine.__new__(DecisionEngine), text)
    assert direct and direct[0] == tool
    assert _plan(text, direct=direct)[0].tool == tool


def test_writing_an_sop_is_a_document_not_a_lookup():
    assert not task_graph.knowledge_request("Write an SOP for pump start-up", classify("Write an SOP for pump start-up"))


def test_general_questions_are_not_captured():
    for text in ("what is EBITDA?", "tell me a joke", "convert 10 bar to psi"):
        assert not task_graph.knowledge_request(text, classify(text)), text


# ------------------------------------------- the full chat path, model recorded --

SOP_ANSWER = """From SOP-HGU-004 Hydrogen Generation Unit start-up procedure - Revision 3 [E1]:
1. Confirm the permit to work is closed and the unit is handed back by maintenance. [E1]
2. Purge the reformer with nitrogen until oxygen is below the limit in the operating manual. [E1]"""


def test_the_retrieved_sop_enters_the_model_request_through_the_chat_path(kf, monkeypatch):
    from core.decision_engine import DecisionEngine
    from core.model_router import ModelRouter
    from core.ollama_client import OllamaClient
    sent = []

    def chat(self, model, messages, **kwargs):
        sent.append({"model": model, "messages": messages, "purpose": kwargs.get("purpose")})
        return SOP_ANSWER if kwargs.get("purpose") == "ZEUS company intelligence" else "ok"
    monkeypatch.setattr(ModelRouter, "route", lambda self, task_type="default": "test-model")
    monkeypatch.setattr(OllamaClient, "chat", chat)
    result = DecisionEngine().process("Read the refinery SOP from the knowledge base.", {"history": []})
    rag_calls = [c for c in sent if c["purpose"] == "ZEUS company intelligence"]
    assert rag_calls, "the knowledge request never reached the model with retrieved context"
    user = rag_calls[-1]["messages"][-1]["content"]
    assert "## ENTERPRISE EVIDENCE" in user
    assert "Purge the reformer with nitrogen" in user                     # the SOP text itself is in the request
    assert "untrusted_document" in user                                   # framed as data, not instructions
    response = result["response"]
    assert "Purge the reformer with nitrogen" in response and "[E1]" in response
    assert "SOP-HGU-004" in response                                      # the source is named


def test_a_follow_up_retrieves_for_the_previous_question(kf, monkeypatch):
    from core.decision_engine import DecisionEngine
    from core.model_router import ModelRouter
    from core.ollama_client import OllamaClient
    questions = []

    def chat(self, model, messages, **kwargs):
        if kwargs.get("purpose") == "ZEUS company intelligence":
            questions.append(messages[-1]["content"])
            return SOP_ANSWER
        return "ok"
    monkeypatch.setattr(ModelRouter, "route", lambda self, task_type="default": "test-model")
    monkeypatch.setattr(OllamaClient, "chat", chat)
    history = [{"role": "user", "content": "What is the start-up procedure of the hydrogen generation unit?"},
               {"role": "assistant", "content": "A typical unit starts with ..."}]
    DecisionEngine().process("Use the RAG information to answer this.", {"history": history})
    assert questions and "## USER REQUEST\nWhat is the start-up procedure of the hydrogen generation unit?" in questions[-1]
