"""
Whisper runs in a worker process; a native crash there cannot end ZEUS.

The 2026-09-24 report showed a native access violation dialog ("The
instruction at 0x00007FF803A7F804 referenced memory at 0x0000000000000000")
and Whisper failing on every device. These tests prove the isolation with a
REAL native crash (a NULL read, exit 0xC0000005) in a real worker process.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from voice.stt_manager import (STTManager, WhisperWorker, WorkerCrash, build_chain,
                               classify_failure)

ROOT = Path(__file__).resolve().parent.parent


def test_a_native_crash_in_the_worker_is_contained_and_classified(monkeypatch):
    monkeypatch.setenv("AGNI_STT_ALLOW_CRASH_TEST", "1")
    worker = WhisperWorker()
    worker.start()
    with pytest.raises(WorkerCrash) as caught:
        worker.request("crash_test", timeout=60)
    crash = caught.value
    assert crash.classification == "access_violation", crash
    assert (crash.exit_code & 0xFFFFFFFF) == 0xC0000005
    # faulthandler left the Python stack of the crash as evidence.
    assert "stt_worker.py" in crash.stderr_tail
    # ...and this process - ZEUS - is still here to assert it.
    assert os.getpid() > 0


def test_the_crash_test_command_does_nothing_unless_explicitly_enabled(monkeypatch):
    monkeypatch.delenv("AGNI_STT_ALLOW_CRASH_TEST", raising=False)
    worker = WhisperWorker()
    worker.start()
    try:
        reply = worker.request("crash_test", timeout=30)
        assert reply["ok"] is False and reply["error_type"] == "protocol"
        assert worker.alive
    finally:
        worker.stop()


@pytest.mark.parametrize("code,text,expected", [
    (0xC0000005, "", "access_violation"),
    (0xC0000409, "", "fail_fast"),
    (None, "RuntimeError: mkl_malloc: failed to allocate memory", "out_of_memory"),
    (None, "OSError: The paging file is too small for this operation to complete", "out_of_memory"),
    (None, "RuntimeError: CUDA failed with error out of memory", "out_of_memory"),
    (None, "Could not load library cudnn_ops64_9.dll", "cuda_runtime"),
    (None, "ModuleNotFoundError: No module named 'ctranslate2'", "runtime_missing"),
])
def test_failures_are_classified(code, text, expected):
    assert classify_failure(code, text)[0] == expected


def test_the_gpu_is_tried_first_and_the_cpu_is_always_in_the_chain(tmp_path):
    model = tmp_path / "whisper"
    model.mkdir()
    (model / "model.bin").write_bytes(b"0" * 1024)
    chain = build_chain(model, has_gpu=True)
    assert [(b.device, b.compute_type) for b in chain][:2] == [("cuda", "float16"), ("cuda", "int8_float16")]
    assert ("cpu", "int8") in [(b.device, b.compute_type) for b in chain]
    assert chain[0].role == "PRIMARY"
    no_gpu = build_chain(model, has_gpu=False)
    assert [(b.device, b.role) for b in no_gpu] == [("cpu", "PRIMARY")]


def test_a_smaller_model_on_disk_becomes_the_last_resort(tmp_path):
    large, small = tmp_path / "whisper", tmp_path / "whisper-small"
    for folder, size in ((large, 4096), (small, 1024)):
        folder.mkdir()
        (folder / "model.bin").write_bytes(b"0" * size)
    chain = build_chain(large, has_gpu=True)
    assert chain[-1].role == "LAST_RESORT"
    assert Path(chain[-1].model_dir).name == "whisper-small"


def test_a_backend_that_crashes_is_not_retried_in_this_session(tmp_path, monkeypatch):
    """Each backend's load is refused by a worker that crashes; the manager
    records every one and gives up with the reasons - it does not loop."""
    import voice.stt_manager as manager_module
    monkeypatch.setattr(manager_module, "_blacklist", {})
    model = tmp_path / "whisper"
    model.mkdir()
    (model / "model.bin").write_bytes(b"0" * 1024)
    chain = build_chain(model, has_gpu=False)
    manager = STTManager(chain)
    monkeypatch.setattr(STTManager, "_room_for", staticmethod(lambda backend: (True, "test")))
    with pytest.raises(RuntimeError) as caught:
        manager.ensure_loaded()          # model.bin is not a real model: load fails
    assert "no Whisper backend could be loaded" in str(caught.value)
    assert manager.attempts and manager.attempts[-1]["ok"] is False
    assert chain[0].key in manager_module._blacklist
    with pytest.raises(RuntimeError) as again:
        manager.ensure_loaded()
    assert "blacklisted this session" in str(again.value)
    manager.close()


def test_a_worker_released_to_free_memory_is_not_a_failed_backend(monkeypatch):
    """release_if_idle() may stop the worker between loading and use; that
    must read as 'stopped' (load again), never as a crash to blacklist."""
    import voice.stt_manager as manager_module
    monkeypatch.setattr(manager_module, "_blacklist", {})
    worker = WhisperWorker()
    worker.start()
    worker.stop()
    with pytest.raises(WorkerCrash) as caught:
        worker.request("ping", timeout=10)
    assert caught.value.classification == "stopped"
    backend = manager_module.Backend("PRIMARY", "x", "cuda", "float16", 1.0)
    manager = STTManager([backend])
    manager._record_failure(backend, caught.value)
    assert backend.key not in manager_module._blacklist
    manager.close()


def test_process_isolation_is_the_default_outside_the_test_suite():
    code = ("import os, sys; sys.path.insert(0, '.'); os.environ.pop('AGNI_STT_ISOLATION', None); "
            "from voice.speech_to_text import stt_isolation; print(stt_isolation())")
    env = {k: v for k, v in os.environ.items() if k != "AGNI_STT_ISOLATION"}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=str(ROOT),
                         env=env, timeout=120).stdout.strip().splitlines()[-1]
    assert out == "process"


def test_process_mode_never_imports_ctranslate2_into_agni():
    """Checking that Whisper is installed must not load its native DLLs here."""
    source = (ROOT / "voice" / "speech_to_text.py").read_text(encoding="utf-8")
    guard = source[source.index('if self.isolation == "process":'):
                   source.index("from faster_whisper import WhisperModel  # noqa: F401")]
    assert "find_spec" in guard
