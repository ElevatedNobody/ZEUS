"""
Whisper: the low-memory fallback, the one-time verified model download, and the
WHISPER STATUS diagnostic.

The recogniser used to fail outright when memory was short: every backend of
the large model was refused and no smaller model existed, because the setup
script could not download one (the ZEUS firewall blocks the venv python.exe).
These tests pin down the pieces of the fix; the live checks (real model, real
microphone, real speech) are `python -m agni_ops stt-status` / STT_CHECK.bat.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

from voice import stt_manager
from voice.stt_manager import STTManager, build_chain, need_gb

ROOT = Path(__file__).resolve().parent.parent


def _setup_models():
    spec = importlib.util.spec_from_file_location("setup_models_under_test", ROOT / "scripts" / "setup_models.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _model(folder: Path, size: int) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / "model.bin", "wb") as handle:
        handle.truncate(size)                        # sparse - the size is what counts
    return folder


LARGE = int(1.51 * 1024 ** 3)
SMALL = int(0.45 * 1024 ** 3)


# ------------------------------------------------------- memory budgets --

def test_the_budget_of_a_small_model_is_its_measured_peak_not_a_file_size_fraction():
    # whisper-small on the CPU peaked at 3.22 GiB; the old rule budgeted 1.8
    assert need_gb("cpu", "int8", 0.45) == pytest.approx(3.55)
    assert need_gb("cuda", "int8_float16", 0.45) == pytest.approx(2.5)
    # the large model keeps its measured figures
    assert need_gb("cuda", "float16", 1.51) == pytest.approx(4.6)
    assert need_gb("cpu", "int8", 1.51) == pytest.approx(5.3)
    # a model smaller than whisper-small is not budgeted below the fixed cost
    assert need_gb("cpu", "int8", 0.07) == need_gb("cpu", "int8", 0.45)
    # a larger one (large-v3, 2.9 GiB) is extrapolated upwards
    assert need_gb("cpu", "int8", 2.9) > need_gb("cpu", "int8", 1.51)


def test_under_memory_pressure_the_small_model_goes_to_the_gpu_first_then_the_cpu(tmp_path, monkeypatch):
    monkeypatch.setattr(stt_manager, "_smaller_model_dirs",
                        lambda root, primary: [_model(tmp_path / "whisper-small", SMALL)])
    chain = build_chain(_model(tmp_path / "whisper", LARGE), has_gpu=True, model_label="large-v3-turbo")
    assert [(Path(b.model_dir).name, b.device, b.compute_type, b.role) for b in chain] == [
        ("whisper", "cuda", "float16", "PRIMARY"),
        ("whisper", "cuda", "int8_float16", "FALLBACK"),
        ("whisper", "cpu", "int8", "FALLBACK"),
        ("whisper-small", "cuda", "int8_float16", "FALLBACK"),
        ("whisper-small", "cpu", "int8", "LAST_RESORT"),
    ]
    assert chain[3].label == "whisper-small" and chain[3].need_gb < chain[0].need_gb


def test_no_gpu_or_a_cpu_setting_keeps_the_small_model_on_the_cpu(tmp_path, monkeypatch):
    monkeypatch.setattr(stt_manager, "_smaller_model_dirs",
                        lambda root, primary: [_model(tmp_path / "whisper-small", SMALL)])
    large = _model(tmp_path / "whisper", LARGE)
    for chain in (build_chain(large, has_gpu=False), build_chain(large, device_pref="cpu", has_gpu=True)):
        assert [(b.device, Path(b.model_dir).name) for b in chain] == [("cpu", "whisper"), ("cpu", "whisper-small")]


@pytest.mark.parametrize("setting, expected", [
    ("auto", ["whisper-base", "whisper-small"]),      # every complete folder, smallest first
    ("small", ["whisper-small"]),
    ("none", []),
])
def test_the_fallback_model_is_chosen_in_config(tmp_path, monkeypatch, setting, expected):
    import voice.offline as offline
    primary = _model(tmp_path / "whisper", 4096)
    _model(tmp_path / "whisper-small", 2048)
    _model(tmp_path / "whisper-base", 1024)
    (tmp_path / "whisper-medium").mkdir()                     # no model.bin: not a model
    monkeypatch.setattr(offline, "config_str", lambda key, default="": setting if key == "stt_fallback_model" else default)
    assert [d.name for d in stt_manager._smaller_model_dirs(tmp_path, primary)] == expected


def test_when_nothing_fits_every_reason_is_given_and_nothing_is_blacklisted(tmp_path, monkeypatch):
    monkeypatch.setattr(stt_manager, "_blacklist", {})
    monkeypatch.setattr(stt_manager, "_smaller_model_dirs",
                        lambda root, primary: [_model(tmp_path / "whisper-small", SMALL)])
    chain = build_chain(_model(tmp_path / "whisper", LARGE), has_gpu=True)
    manager = STTManager(chain)
    monkeypatch.setattr(STTManager, "_room_for",
                        staticmethod(lambda backend: (False, f"needs {backend.need_gb} GB, 1.9 GB free")))
    try:
        with pytest.raises(RuntimeError) as caught:
            manager.ensure_loaded()
        message = str(caught.value)
        assert message.startswith("no Whisper backend could be loaded")
        assert message.count("GB free") == len(chain) and "whisper-small cpu/int8" in message
        assert all(a["classification"] == "insufficient_memory" for a in manager.attempts)
        assert stt_manager._blacklist == {}                  # a shortage is a moment, not a broken backend
    finally:
        manager.close()


# ------------------------------------------- one-time, verified download --

class FakeCurl:
    """Stands in for curl.exe: writes `payload` to the -o path, records calls."""

    def __init__(self, payload: bytes, code: int = 0):
        self.payload, self.code, self.calls = payload, code, []

    def __call__(self, args, timeout=0):
        self.calls.append(args)
        if self.code == 0:
            Path(args[args.index("-o") + 1]).write_bytes(self.payload)
        return subprocess.CompletedProcess(args, self.code, "", "" if self.code == 0 else "curl: (7) blocked")


def _entry(payload: bytes, lfs: bool = True):
    return {"path": "model.bin", "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest() if lfs else None}


def test_a_download_is_hash_checked_and_then_reused(tmp_path, monkeypatch):
    setup = _setup_models()
    payload = b"weights" * 1000
    curl = FakeCurl(payload)
    monkeypatch.setattr(setup, "_run_curl", curl)
    assert setup.fetch_file("Systran/faster-whisper-small", _entry(payload), tmp_path) == "downloaded"
    assert (tmp_path / "model.bin").read_bytes() == payload and not (tmp_path / "model.bin.part").exists()
    assert "-C" in curl.calls[0]                                  # an interrupted download resumes
    assert setup.fetch_file("Systran/faster-whisper-small", _entry(payload), tmp_path) == "present"
    assert len(curl.calls) == 1                                   # the second run downloaded nothing


def test_a_corrupt_download_is_deleted_and_reported(tmp_path, monkeypatch):
    setup = _setup_models()
    monkeypatch.setattr(setup, "_run_curl", FakeCurl(b"tampered-bytes"))
    entry = _entry(b"tampered-bytes")
    entry["sha256"] = hashlib.sha256(b"the real weights").hexdigest()
    with pytest.raises(setup.DownloadError, match="does not match"):
        setup.fetch_file("Systran/faster-whisper-small", entry, tmp_path)
    assert not (tmp_path / "model.bin").exists() and not (tmp_path / "model.bin.part").exists()


def test_a_short_or_blocked_download_says_so(tmp_path, monkeypatch):
    setup = _setup_models()
    monkeypatch.setattr(setup, "_run_curl", FakeCurl(b"half"))
    entry = _entry(b"the whole file")
    with pytest.raises(setup.DownloadError, match="kept for resume"):
        setup.fetch_file("r", entry, tmp_path)
    assert (tmp_path / "model.bin.part").exists()                 # kept: the next run resumes it
    monkeypatch.setattr(setup, "_run_curl", FakeCurl(b"", code=7))
    with pytest.raises(setup.DownloadError, match="curl exit 7"):
        setup.fetch_file("r", _entry(b"x" * 10), tmp_path / "other")


def test_the_manifest_catches_a_missing_or_altered_file(tmp_path):
    setup = _setup_models()
    (tmp_path / "model.bin").write_bytes(b"weights")
    (tmp_path / "config.json").write_text("{}")
    (tmp_path / setup.MANIFEST).write_text(json.dumps({"files": [
        {"name": "model.bin", "size": 7, "sha256": hashlib.sha256(b"weights").hexdigest()},
        {"name": "config.json", "size": 2, "sha256": None}]}))
    assert setup.verify_manifest(tmp_path) == ""
    assert setup._manifest_summary(tmp_path) == "2 file(s) match their size; SHA-256 verified for model.bin"
    (tmp_path / "model.bin").write_bytes(b"WEIGHTS")                # same size, different bytes
    assert "SHA-256" in setup.verify_manifest(tmp_path)
    (tmp_path / "model.bin").write_bytes(b"weights")
    (tmp_path / "config.json").unlink()
    assert "config.json is missing" in setup.verify_manifest(tmp_path)


def test_the_setup_script_offers_the_fallback_and_honours_the_model_folder(monkeypatch, tmp_path):
    setup = _setup_models()
    monkeypatch.setenv("MEDHA_MODELS_DIR", str(tmp_path))
    assert setup.fallback_dir("small") == tmp_path.resolve() / "whisper-small"
    assert setup.WHISPER_REPOS["small"] == "Systran/faster-whisper-small"


# --------------------------------------------------------- WHISPER STATUS --

def _no_microphone(monkeypatch):
    import sounddevice as sd
    import voice.speech_to_text as stt_module
    monkeypatch.setattr(stt_module, "list_input_devices", lambda: [])
    monkeypatch.setattr(stt_module, "default_input_device", lambda: None)
    monkeypatch.setattr(sd, "rec", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no device to open")))


def test_a_missing_dependency_is_named_with_the_fix(monkeypatch):
    from agni_ops import voice
    monkeypatch.setattr(voice, "_worker_stage", lambda *a, **k: (
        False, [], "ModuleNotFoundError: No module named 'ctranslate2'", 1, "runtime_missing"))
    lines = voice.whisper_status(load=False)
    assert lines[0] == {"check": "Runtime available", "ok": False,
                        "detail": "dependency missing: ctranslate2 - install requirements-voice.txt (SETUP_VOICE.bat)"}


def test_a_missing_model_is_named_with_the_install_command(monkeypatch, tmp_path):
    from agni_ops import voice
    monkeypatch.setattr(voice, "_worker_stage", lambda *a, **k: (True, [{"faster_whisper": "1.2.1",
                                                                          "ctranslate2": "4.8.2", "cuda_devices": 0}],
                                                                  "", None, ""))
    monkeypatch.setenv("MEDHA_STT_MODEL_DIR", str(tmp_path / "whisper"))
    lines = voice.whisper_status(load=False)
    assert [line["ok"] for line in lines] == [True, False] and len(lines) == 2   # load=False: nothing loaded
    assert "no CUDA device - CPU only" in lines[0]["detail"]
    assert "model missing" in lines[1]["detail"] and "setup_models.py" in lines[1]["detail"]


def test_a_failed_microphone_is_a_failed_line_not_a_crash(monkeypatch, tmp_path):
    from agni_ops import voice
    monkeypatch.setattr(voice, "_worker_stage", lambda *a, **k: (
        False, [], "ModuleNotFoundError: No module named 'faster_whisper'", 1, "runtime_missing"))
    _no_microphone(monkeypatch)
    lines = voice.whisper_status(load=True)
    by_check = {line["check"]: line for line in lines}
    assert list(by_check) == list(voice.STATUS_CHECKS)
    assert by_check["Microphone available"]["ok"] is False
    assert "no input device" in by_check["Microphone available"]["detail"]
    # a stage that could not run is NOT reported as passed or failed
    assert by_check["Model loaded"]["ok"] is None and by_check["STT pipeline ready"]["ok"] is None


def test_the_status_block_marks_pass_fail_and_not_run():
    from agni_ops.voice import render_status
    text = render_status([{"check": "Runtime available", "ok": True, "detail": "a"},
                          {"check": "Model loaded", "ok": False, "detail": "b"},
                          {"check": "STT pipeline ready", "ok": None, "detail": "c"}])
    assert text.splitlines() == ["WHISPER STATUS", "✓ Runtime available     a", "✗ Model loaded          b",
                                 "– STT pipeline ready    c"]


def test_a_console_without_unicode_still_reads_pass_and_fail(capsys, monkeypatch, tmp_path):
    from agni_ops.runlog import RunLog
    log = RunLog.__new__(RunLog)
    log.quiet, log.write_file = False, lambda line: None
    real_print = print

    def cp1252_print(line, **kwargs):
        line.encode("cp1252")                         # raises for ✓ / ✗ like a legacy console
        real_print(line, **kwargs)
    monkeypatch.setattr("builtins.print", cp1252_print)
    log.say("✓ Runtime available ok")
    log.say("✗ Model loaded failed")
    assert capsys.readouterr().out.splitlines() == ["OK Runtime available ok", "X  Model loaded failed"]
