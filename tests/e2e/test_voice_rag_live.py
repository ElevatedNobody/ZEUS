"""
Live end-to-end, nothing faked:

    VOICE -> WHISPER -> QUERY -> ROUTER -> RAG RETRIEVAL -> CONTEXT -> LOCAL LLM -> ANSWER + SOURCES

Opt-in - it loads Whisper and then the local language model (one at a time):

    set AGNI_RUN_LIVE=1
    python -m pytest tests/e2e/test_voice_rag_live.py -v

The question is spoken by the Windows voice, heard by the app's own recogniser
(in its worker process, as in the app), and answered through the same
DecisionEngine path as the microphone button - on the labelled SYNTHETIC
fixture corpus in a temporary database. The same run as
`RAG_TRACE.bat "<question>" --fixture --spoken`.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("AGNI_RUN_LIVE") != "1",
                                reason="loads Whisper and the local model - set AGNI_RUN_LIVE=1")


class _Log:
    def __init__(self):
        self.lines = []

    def say(self, line: str = ""):
        self.lines.append(line)


def test_a_spoken_knowledge_question_is_answered_from_the_retrieved_sop(monkeypatch):
    monkeypatch.setenv("AGNI_STT_ISOLATION", "process")          # the app's default, not the suite's
    from agni_ops import rag
    from agni_ops.status import PASS
    log = _Log()
    results = rag.trace("Search the knowledge base for the hydrogen unit start-up procedure", log,
                        fixture=True, spoken=True)
    text = "\n".join(log.lines)
    failed = [f"{r.component}: {r.status} {r.reason}" for r in results if r.status != PASS]
    assert not failed, "\n".join(failed) + "\n" + text[-4000:]
    stages = {r.component for r in results}
    assert {"voice -> RAG: Whisper transcription", "voice -> RAG: route", "voice -> RAG: retrieval",
            "RAG trace: context injection", "RAG trace: model answer",
            "voice -> RAG: answer with sources"} <= stages
    # the SOP text was in the model request and in the answer, and the answer names it as its source
    request = text.split("MODEL REQUEST:")[1].split("MODEL RESPONSE:")[0]
    assert "Purge the reformer with nitrogen" in request
    assert "Purge the reformer with nitrogen" in text.split("ANSWER (what ZEUS shows and speaks):")[1]
    assert "SOP-HGU-004" in text.split("CITED BY THE ANSWER:")[1].splitlines()[0]
