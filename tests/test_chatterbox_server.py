"""
The Chatterbox voice must stay loaded while you talk.

Regression for 2026-09-25: every SENTENCE started a new bridge process that
re-imported torch and re-read the weights (43 s one-shot vs 22 s warm for the
same sentence, measured). These tests drive real processes: the real bridge
script in --serve mode, and small stand-ins for a bridge that crashes or hangs.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------ the process --

def test_one_bridge_process_answers_every_request(tmp_path):
    """Several requests, one process: the model would be loaded once."""
    from voice.chatterbox_server import ChatterboxServer
    server = ChatterboxServer(Path(sys.executable), tmp_path / "bridge.log")
    server.start()
    try:
        pid = server.pid
        # This interpreter has no chatterbox, so each request fails - but it
        # fails as an ANSWER on the same process, which is the protocol under test.
        for _ in range(3):
            answer = server.request({"op": "load", "model_dir": str(tmp_path), "warm": False},
                                    timeout=120)
            assert answer["ok"] is False and answer["error"] == "load_failed"
        answer = server.request({"text": "", "output": str(tmp_path / "x.wav")}, timeout=60)
        assert answer == {"ok": False, "error": "empty_text", "detail": "There was nothing to say."}
        assert server.alive and server.pid == pid and server.requests == 4
    finally:
        server.stop()
    assert not server.alive


def _fake_bridge(tmp_path, monkeypatch, body: str):
    script = tmp_path / "fake_bridge.py"
    script.write_text(textwrap.dedent(body), encoding="utf-8")
    import voice.chatterbox_server as module
    monkeypatch.setattr(module, "BRIDGE", script)
    return module


def test_a_bridge_that_dies_is_reported_with_its_exit_code(tmp_path, monkeypatch):
    module = _fake_bridge(tmp_path, monkeypatch, """
        import sys
        sys.stdin.readline()
        sys.stderr.write("torch: something native went wrong\\n")
        sys.stderr.flush()
        sys.exit(3)
    """)
    server = module.ChatterboxServer(Path(sys.executable))
    server.start()
    with pytest.raises(module.BridgeDied) as caught:
        server.request({"text": "hello"}, timeout=30)
    assert caught.value.exit_code == 3
    assert "something native went wrong" in caught.value.stderr
    server.stop()


def test_a_bridge_that_hangs_is_stopped_not_waited_on_forever(tmp_path, monkeypatch):
    module = _fake_bridge(tmp_path, monkeypatch, """
        import sys, time
        sys.stdin.readline()
        time.sleep(60)
    """)
    server = module.ChatterboxServer(Path(sys.executable))
    server.start()
    with pytest.raises(TimeoutError):
        server.request({"text": "hello"}, timeout=2)
    assert not server.alive, "a bridge that stopped answering must not serve the next sentence"


def test_stray_output_does_not_become_the_answer(tmp_path, monkeypatch):
    module = _fake_bridge(tmp_path, monkeypatch, """
        import json, sys
        for line in sys.stdin:
            print("Loading weights... 100%", flush=True)
            print(json.dumps({"ok": True, "path": "x.wav", "device": "cuda"}), flush=True)
    """)
    server = module.ChatterboxServer(Path(sys.executable))
    server.start()
    try:
        assert server.request({"text": "hi"}, timeout=30)["path"] == "x.wav"
        assert server.device == "cuda"
    finally:
        server.stop()


# ----------------------------------------------------------- the provider --

def _provider(monkeypatch, device="cuda", keep="auto"):
    from voice import providers
    monkeypatch.setattr(providers, "config_str",
                        lambda key, default="": keep if key == "tts_keep_loaded" else default)
    provider = providers.ChatterboxBridgeProvider()
    provider._python = Path(sys.executable)
    provider._environment = {"device": device}
    provider._profile = {"device": "auto"}
    return provider


def test_the_voice_stays_loaded_on_a_gpu_and_not_on_the_cpu(monkeypatch):
    assert _provider(monkeypatch, "cuda")._keep_loaded() is True
    assert _provider(monkeypatch, "cpu")._keep_loaded() is False, (
        "on the CPU the loaded bridge holds ~7 GB - too much for a 15.4 GB laptop")
    assert _provider(monkeypatch, "cpu", keep="true")._keep_loaded() is True
    assert _provider(monkeypatch, "cuda", keep="false")._keep_loaded() is False


class _FakeServer:
    def __init__(self, answers):
        self.answers = list(answers)
        self.requests = []
        self.alive = True
        self.busy = False
        self.last_used = 0.0

    def request(self, payload, timeout=0):
        self.requests.append(payload)
        return self.answers.pop(0)

    def stop(self):
        self.alive = False


def test_sentences_go_to_the_loaded_bridge(monkeypatch):
    provider = _provider(monkeypatch)
    fake = _FakeServer([{"ok": True, "path": "a.wav", "device": "cuda", "synthesis_seconds": 1.2},
                        {"ok": True, "path": "b.wav", "device": "cuda", "synthesis_seconds": 1.1}])
    monkeypatch.setattr(provider, "_ensure_server", lambda: fake)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("a new process per sentence"))
    assert provider.synthesise("First sentence.") == Path("a.wav")
    assert provider.synthesise("Second sentence.") == Path("b.wav")
    assert [r["text"] for r in fake.requests] == ["First sentence.", "Second sentence."]


def test_a_full_gpu_frees_the_language_models_and_retries_once(monkeypatch):
    provider = _provider(monkeypatch)
    fake = _FakeServer([{"ok": False, "error": "synthesis_failed",
                         "detail": "OutOfMemoryError: CUDA out of memory. Tried to allocate 64 MiB"},
                        {"ok": True, "path": "c.wav"}])
    monkeypatch.setattr(provider, "_ensure_server", lambda: fake)
    unloaded = []
    import core.model_lifecycle
    monkeypatch.setattr(core.model_lifecycle, "unload_all",
                        lambda *a, **k: unloaded.append(1) or ["qwen3:8b"])
    assert provider.synthesise("Pressure is stable.") == Path("c.wav")
    assert unloaded == [1] and len(fake.requests) == 2


def test_a_loaded_voice_is_released_for_a_language_model_only_when_silent(monkeypatch):
    provider = _provider(monkeypatch)
    fake = _FakeServer([])
    provider._server = fake
    fake.busy = True
    assert provider.release_if_idle() is False
    fake.busy = False
    provider._playing = True
    assert provider.release_if_idle() is False
    provider._playing = False
    assert provider.release_if_idle() is True
    assert provider._server is None and fake.alive is False


def test_a_crashed_bridge_is_classified_and_replaced_next_time(monkeypatch):
    from voice.chatterbox_server import BridgeDied
    provider = _provider(monkeypatch)

    class Crashing(_FakeServer):
        def request(self, payload, timeout=0):
            raise BridgeDied("ended", 0xC0000005, "Windows fatal exception: access violation")

    crashing = Crashing([])
    provider._server = crashing
    monkeypatch.setattr(provider, "_ensure_server", lambda: crashing)
    assert provider.synthesise("Hello there.") is None
    assert provider.last_failure["exit_code"] == 0xC0000005
    assert provider._server is None, "the dead bridge must not be reused"


# ------------------------------------------------------- the installer ----

def _installer():
    import importlib.util
    spec = importlib.util.spec_from_file_location("setup_voice_engine_under_test",
                                                  ROOT / "scripts" / "setup_voice_engine.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RTX_5060 = {"gpu": "NVIDIA GeForce RTX 5060 Laptop GPU", "driver": "616.56"}


def test_an_rtx_50_series_card_ends_with_the_gpu_build_of_torch():
    engine = _installer()
    assert engine.final_torch_version(RTX_5060) == engine.BLACKWELL_TORCH_VERSION == "2.8.0"
    assert engine.final_torch_version(RTX_5060, force="cpu") == engine.TORCH_VERSION
    assert engine.final_torch_version({"gpu": "NVIDIA GeForce RTX 4060", "driver": "576.0"}) == "2.6.0"
    assert engine.final_torch_version({"gpu": None}) == "2.6.0"
    # not 2.9+: torchaudio 2.9 hands load/save to torchcodec (FFmpeg)
    assert engine.BLACKWELL_TORCH_VERSION.startswith("2.8")


def test_a_repair_run_does_not_undo_the_gpu_voice(monkeypatch):
    """Before 2026-09-25 every installer run put the CPU 2.6 build back."""
    engine = _installer()
    monkeypatch.setattr(engine, "installed_version", lambda module: "2.8.0+cu128")
    monkeypatch.setattr(engine, "installed_in_env", lambda module: module == "chatterbox")
    monkeypatch.setattr(engine, "run", lambda *a, **k: pytest.fail(f"it tried to run {a[0]}"))
    assert engine.install_torch(RTX_5060) is True


def test_a_gpu_build_that_does_not_run_here_is_replaced_by_the_cpu_build(monkeypatch, tmp_path):
    engine = _installer()
    monkeypatch.setattr(engine, "HOME", tmp_path)
    monkeypatch.setattr(engine, "env_python", lambda: Path(sys.executable))
    monkeypatch.setattr(engine, "installed_version", lambda module: "2.6.0+cpu")
    commands = []
    monkeypatch.setattr(engine, "run", lambda command, *a, **k: commands.append(command) or True)
    monkeypatch.setattr(engine, "_gpu_works", lambda python: (False, "no kernel image"))
    monkeypatch.setattr(engine, "_confirm_torch_did_not_move", lambda python: True)
    assert engine.install_gpu_torch(RTX_5060) is True
    installs = [c for c in commands if "install" in c and "--no-index" in c]
    assert "torch==2.8.0" in installs[0] and "torch==2.6.0" in installs[-1], commands


def test_other_cards_are_left_on_the_pinned_build(monkeypatch):
    engine = _installer()
    monkeypatch.setattr(engine, "run", lambda *a, **k: pytest.fail("nothing to do here"))
    assert engine.install_gpu_torch({"gpu": "NVIDIA GeForce RTX 4060", "driver": "576.0"}) is True
    assert engine.install_gpu_torch({"gpu": None}) is True


# ---------------------------------------------------------- the bridge ----

def test_the_bridge_writes_audio_the_wave_module_can_measure(tmp_path):
    """torchaudio.save wrote float WAV, so 'seconds' was always None."""
    out = tmp_path / "speech.wav"
    code = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, r"{ROOT / 'voice'}")
        import numpy as np
        import chatterbox_bridge as bridge
        class Tensor:
            def __init__(self, a): self.a = a
            def detach(self): return self
            def cpu(self): return self
            def numpy(self): return self.a
        bridge.save_wav(__import__("pathlib").Path(r"{out}"),
                        Tensor(np.zeros((1, 24000), dtype=np.float32)), 24000)
    """)
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=120)
    assert result.returncode == 0, result.stderr[-800:]
    with wave.open(str(out), "rb") as handle:
        assert handle.getsampwidth() == 2 and handle.getnframes() == 24000
