"""Model roles: chosen against this machine's hardware, from installed and
trusted models only, never downloaded, and honest about what is unverified."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from core import model_roles

PLAN = {"slots": {"reasoning": "deepseek-r1:8b", "fast": "qwen3:4b", "code": "qwen2.5-coder:7b",
                  "vision": "qwen3-vl:8b", "json_structured": "qwen3:8b", "default": "qwen3:8b",
                  "classify": "qwen3:1.7b", "embedding": "bge-m3"}}


@pytest.fixture
def machine(monkeypatch):
    state = {"installed": ["deepseek-r1:8b", "qwen3:8b", "qwen3:1.7b", "bge-m3:latest"], "free": 9.0}
    monkeypatch.setattr(model_roles, "_hardware", lambda: (SimpleNamespace(tier="gpu_8gb"), PLAN, state["free"]))
    monkeypatch.setattr(model_roles, "_installed", lambda: list(state["installed"]))
    monkeypatch.setattr(model_roles, "_trusted", lambda name: True)      # the stub models are "pinned"
    from core.model_router import ModelRouter
    routes = {"reasoning": "deepseek-r1:8b", "fast": "qwen3:1.7b", "code": "qwen2.5-coder:7b",
              "vision": "qwen3-vl:8b", "json_structured": "qwen3:8b", "default": "qwen3:8b", "classify": "qwen3:1.7b"}
    monkeypatch.setattr(ModelRouter, "route", lambda self, slot="default": routes[slot])   # the class: see test_kf_reasoning
    return state


def test_every_role_is_known():
    assert set(model_roles.ROLES) == {"GENERAL_REASONING", "FAST", "CODING", "VISION", "EMBEDDING", "RERANKER",
                                      "EXTRACTION", "DOCUMENT", "SMALL_LOCAL", "SPEECH_TO_TEXT", "TEXT_TO_SPEECH"}
    with pytest.raises(ValueError):
        model_roles.select("MAGIC")


def test_an_installed_model_is_installed_not_verified(machine):
    choice = model_roles.select("GENERAL_REASONING")
    assert choice.model == "deepseek-r1:8b" and choice.state == model_roles.INSTALLED
    assert choice.hardware_tier == "gpu_8gb" and choice.fits_now is True and choice.usable


def test_a_missing_model_is_reported_with_how_to_install_it(machine):
    choice = model_roles.select("CODING")
    assert choice.state == model_roles.NOT_INSTALLED and not choice.usable
    assert "STEP 1 : INSTALL" in choice.install_hint


def test_a_substitute_is_labelled_as_a_fallback(machine):
    choice = model_roles.select("FAST")                   # plan says qwen3:4b, qwen3:1.7b is serving
    assert choice.state == model_roles.FALLBACK and "qwen3:4b" in choice.fallback


def test_no_runtime_means_nothing_is_claimed(machine):
    machine["installed"] = []
    choice = model_roles.select("DOCUMENT")
    assert choice.state == model_roles.RUNTIME_DOWN and not choice.usable


def test_memory_requirements_are_enforced(machine):
    machine["free"] = 2.0
    choice = model_roles.select("GENERAL_REASONING", {"must_fit_now": True})
    assert choice.fits_now is False and not choice.meets_requirements and not choice.usable
    assert not model_roles.select("GENERAL_REASONING", {"max_gb": 3}).meets_requirements


def test_embedding_role(machine):
    assert model_roles.select("EMBEDDING").model == "bge-m3:latest"
    machine["installed"] = ["qwen3:8b"]
    choice = model_roles.select("EMBEDDING")
    assert choice.state == model_roles.NOT_INSTALLED and "vector search is off" in choice.reason


def test_an_untrusted_model_is_reported_not_offered(machine, monkeypatch):
    monkeypatch.setattr(model_roles, "_trusted", lambda name: False)
    choice = model_roles.select("GENERAL_REASONING")
    assert choice.state == model_roles.UNTRUSTED and not choice.usable and "registry" in choice.reason


def test_nothing_is_ever_downloaded():
    source = Path(model_roles.__file__).read_text(encoding="utf-8")
    assert "pull" not in source.replace("pulled", "")


def test_a_planned_model_measured_unsuitable_is_a_measured_choice_not_a_fallback(machine, monkeypatch):
    """qwen3:4b is planned for quick replies but was MEASURED to leak its reasoning
    when told not to think; the router serves another model on purpose."""
    machine["installed"].append("qwen3:4b")
    import core.model_calibration as calibration
    monkeypatch.setattr(calibration, "think_false_leaks", lambda name: name.startswith("qwen3:4b"))
    choice = model_roles.select("FAST")
    assert choice.state == model_roles.INSTALLED and choice.fallback == ""
    assert "measured choice" in choice.reason and "qwen3:4b" in choice.reason
    assert choice.evidence["planned_model_rejected"]["model"] == "qwen3:4b"


def test_without_a_cross_encoder_the_reranker_is_hybrid_and_its_self_test_runs(machine, monkeypatch):
    import industrial.reranker as reranker
    monkeypatch.setattr(reranker, "status", lambda: {"backend": "hybrid_local", "detail": "", "cross_encoder_active": False,
                                                      "searched_dirs": ["models/reranker"]})
    choice = model_roles.select("RERANKER", probe=True)
    assert choice.state == model_roles.HYBRID and choice.usable and choice.fallback == ""
    assert "lexical" in choice.reason and "authority" in choice.reason
    assert choice.evidence["self_test"]["passed"] is True


def test_speech_roles_are_installed_from_their_files_never_verified_by_a_listing(machine, monkeypatch):
    import voice.offline as offline
    monkeypatch.setattr(offline, "check_voice_models", lambda: {
        "whisper": {"present": True, "missing": [], "directory": "models/whisper"},
        "chatterbox": {"present": False, "missing": ["conds.pt"], "directory": "models/chatterbox"}})
    stt, tts = model_roles.select("SPEECH_TO_TEXT"), model_roles.select("TEXT_TO_SPEECH")
    assert stt.state == model_roles.INSTALLED and "STT_CHECK.bat" in stt.reason and stt.estimated_gb == 4.8
    assert tts.state == model_roles.NOT_INSTALLED and "conds.pt" in tts.reason and "SAPI" in tts.fallback
