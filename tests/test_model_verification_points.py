"""The eight checks of a model are read from its recorded test - a check that was
not reached is NOT RUN, never PASS."""

from agni_ops import models


def _names(points):
    return {p["point"]: p["status"] for p in points}


def test_a_passing_chat_model_passes_all_eight():
    record = {"model": "qwen3:8b", "type": "CHAT", "present": True, "installed_as": "qwen3:8b", "checksum": "trusted",
              "load": True, "load_s": 8.1, "resident_gb": 5.2, "inference": True, "inference_s": 0.4, "function": True,
              "function_detail": "answered 'READY'", "capabilities": ["completion", "thinking"],
              "thinking_control": {"think_false_leaks": False}, "vram_gb": 5.2, "commit_delta_gb": 6.2,
              "unloaded": True, "tested_at": 1.0}
    points = models.verification_points(record)
    assert [p["point"] for p in points] == list(models.POINTS) and len(points) == 8
    assert set(_names(points).values()) == {"PASS"}


def test_a_load_crash_stops_the_later_checks():
    record = {"model": "big:14b", "type": "CHAT", "present": True, "checksum": "trusted", "load": False, "load_s": 30.0,
              "error": "llama runner terminated 0xc0000409", "cause": "insufficient runtime memory - the runner crashed"}
    status = _names(models.verification_points(record))
    assert status["LOADED + RESIDENT"] == "FAIL"
    assert status["ANSWERED"] == status["OUTPUT VALIDATED"] == status["BEHAVIOUR CALIBRATED"] == "NOT RUN"
    assert status["UNLOADED + METRICS"] == "NOT RUN"


def test_a_missing_model_fails_the_first_check_and_runs_no_other():
    status = _names(models.verification_points({"model": "qwen3:14b", "present": False, "cause": "not installed"}))
    assert status["INSTALLED"] == "FAIL" and list(status.values())[1:] == ["NOT RUN"] * 7


def test_an_unconfirmed_unload_is_not_a_pass():
    record = {"model": "m", "type": "EMBEDDING", "present": True, "checksum": "trusted", "load": True, "inference": True,
              "function": True, "tested_at": 1.0}
    assert _names(models.verification_points(record))["UNLOADED + METRICS"] == "NOT RUN"
