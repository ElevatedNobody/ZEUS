"""
A model the trusted registry rejects is not RUN - not only not ROUTED.

The router's last-resort fallback hands back its first preference even when
routing rejected it; the client now refuses such a model at the point of
execution. A registry that does not verify at all is reported elsewhere
(routing, SECURITY_CHECK) and does not stop every model.
"""

import pytest

from core import ollama_client as client_module
from security.integrity import model_integrity


@pytest.fixture
def registry(monkeypatch):
    state = {"registry": {"signature_valid": True, "models": {"good:8b": "d1", "bad:8b": "d2", "embed:latest": "d3"}},
             "live": {"good:8b": "d1", "bad:8b": "TAMPERED", "extra:4b": "d9", "embed:latest": "d3"}, "required": True}
    monkeypatch.setattr(model_integrity, "_registry", state["registry"])
    # Methods are patched on the class: an instance patch leaves a bound method
    # behind on undo that shadows later patches of the class.
    monkeypatch.setattr(type(model_integrity), "load", lambda self: state["registry"])
    monkeypatch.setattr(type(model_integrity), "live_digests", lambda self, force=False: dict(state["live"]))
    monkeypatch.setattr(type(model_integrity), "required", staticmethod(lambda: state["required"]))

    def no_network(*args, **kwargs):
        raise AssertionError("a refused model must not reach the runtime")
    monkeypatch.setattr(client_module, "_post", no_network)
    return state


def test_a_tampered_model_is_refused_before_anything_is_sent(registry):
    reply = client_module.ollama_client.chat("bad:8b", [{"role": "user", "content": "hi"}], retries=0)
    assert client_module.is_failure_text(reply) and "digest" in reply


def test_an_unpinned_model_is_refused_when_trusted_models_are_required(registry):
    reply = client_module.ollama_client.chat("extra:4b", [{"role": "user", "content": "hi"}], retries=0)
    assert client_module.is_failure_text(reply) and "not in the trusted model registry" in reply


def test_a_trusted_model_is_not_refused(registry):
    assert model_integrity.execution_refusal("good:8b") is None
    assert model_integrity.execution_refusal("embed") is None          # name and name:latest are one model


def test_a_registry_that_does_not_verify_does_not_stop_every_model(registry):
    registry["registry"]["signature_valid"] = False
    assert model_integrity.execution_refusal("bad:8b") is None


def test_a_refused_embedding_returns_nothing_with_the_reason(registry):
    registry["live"]["embed:latest"] = "changed"
    assert client_module.ollama_client.embed("embed", "text") is None
    assert "digest" in client_module.ollama_client.last_embed_error


def test_a_failure_string_is_never_shown_as_a_summary(kf, monkeypatch):
    from core.model_router import ModelRouter
    from tests.conftest import kf_ask
    monkeypatch.setattr(ModelRouter, "route", lambda self, task_type="default": "bad:8b")
    monkeypatch.setattr(client_module.OllamaClient, "chat",
                        lambda *a, **k: "[ZEUS model failure: bad:8b was refused: its digest differs]")
    obj = kf_ask("What was the net profit trend over the last five years?", use_model=True)
    assert obj.narrative_source == "deterministic" and "model failure" not in obj.answer
    assert "model unavailable" in obj.retrieval["narrative"]
