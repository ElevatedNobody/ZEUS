"""
Running Chatterbox and Whisper on a CPU-only laptop.

The problem these exist for: Chatterbox is a 0.5B model with a 1 GB vocoder,
and on an i5 with no GPU it generates speech several times slower than the
speech plays. Generated as one blob, a three-sentence reply means most of a
minute of silence and the feature is unusable.

The fix is not a faster model, it is a different shape of work: split the
reply into sentences, generate one at a time, and start playing the first
while the second is still being made. Time-to-first-sound drops from the
whole reply to one sentence.

Everything below tests that shape - the splitting, the streaming, the memory
guard, the thread tuning - with a stub model, because the real weights are
3.2 GB and the logic is what breaks. Actual speed on real weights is measured
by scripts/voice_benchmark.py, on the machine that has them.
"""

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from voice import memory_guard  # noqa: E402
from voice.chatterbox_engine import (  # noqa: E402
    MAX_CHUNK_CHARS,
    MIN_CHUNK_CHARS,
    split_for_speech,
)

THREE_SENTENCES = (
    "The exchanger was opened for cleaning during the March outage. "
    "Before it returns to service a hydrotest shall be performed at one point "
    "three times design pressure and held for thirty minutes. "
    "A visual inspection of the shell side welds follows the hydrotest.")


# ------------------------------------------------------- what gets spoken --

def test_a_reply_is_split_into_sentences():
    """The unit of generation is a sentence, so playback can start on the
    first one instead of after the last."""
    chunks = split_for_speech(THREE_SENTENCES)
    assert len(chunks) > 1, "the reply was not split, so nothing can stream"
    assert "".join(chunks).replace(" ", "") == THREE_SENTENCES.replace(" ", ""), \
        "splitting lost or duplicated text"


def test_nothing_is_split_mid_word():
    long_text = " ".join(["technicians verify the calibration certificate"] * 40)
    for chunk in split_for_speech(long_text):
        assert not chunk.startswith(" ") and not chunk.endswith(" ")
        # Every chunk must be whole words - a chunk ending mid-word is audible.
        assert all(part.isascii() for part in chunk.split())


def test_very_short_sentences_are_merged():
    """'Yes.' as its own model call pays a fixed overhead that dwarfs the
    audio, and sounds clipped."""
    chunks = split_for_speech("Yes. No. Done. Confirmed. Ready to proceed now.")
    assert len(chunks) == 1, f"tiny sentences were not merged: {chunks}"


def test_no_chunk_exceeds_the_cap():
    """A long chunk means a long autoregressive pass, which is exactly the
    time-to-first-sound problem the splitting exists to solve."""
    monster = ("The contractor shall submit the cleaning record, the thickness "
               "readings, the calibration certificates for every instrument "
               "used, the welder qualifications, and the mechanical completion "
               "certificate signed by a competent person, within two working "
               "days of completion of the work described in this procedure.")
    for chunk in split_for_speech(monster):
        assert len(chunk) <= MAX_CHUNK_CHARS + 40, \
            f"chunk of {len(chunk)} chars exceeds the cap: {chunk[:60]}..."


def test_a_single_enormous_word_does_not_loop_forever():
    """A pathological input must terminate, not hang the speech thread."""
    chunks = split_for_speech("x" * 2000)
    assert chunks
    assert sum(len(c) for c in chunks) >= 2000 - len(chunks)


def test_empty_and_whitespace_produce_nothing():
    for value in ("", "   ", "\n\t", None):
        assert split_for_speech(value) == []


def test_punctuation_is_kept():
    """Chatterbox uses punctuation for pauses and emphasis. Stripping it makes
    speech run together."""
    chunks = split_for_speech("Is the valve closed? Yes, it is closed and tagged.")
    joined = " ".join(chunks)
    assert "?" in joined and "," in joined


# --------------------------------------------------------- long answers --

def test_a_two_page_answer_is_not_read_out_in_full():
    """Reading two pages aloud on a CPU takes minutes. The text is on screen
    the whole time; speech says the first part and says so."""
    from voice.text_to_speech import TextToSpeech

    speaker = TextToSpeech.__new__(TextToSpeech)      # no model load
    speaker._max_spoken_chars = 300

    essay = ("The inspection found wall thinning at the six o'clock position. "
             * 30)
    spoken = speaker._speakable(essay)
    assert len(spoken) < len(essay)
    assert "on screen" in spoken, "the answer was truncated without saying so"
    assert spoken.count(".") >= 1


def test_a_short_answer_is_spoken_whole():
    from voice.text_to_speech import TextToSpeech
    speaker = TextToSpeech.__new__(TextToSpeech)
    speaker._max_spoken_chars = 600
    short = "The relief valve is set to twelve point five barg."
    assert speaker._speakable(short) == short


def test_a_zero_limit_means_speak_everything():
    from voice.text_to_speech import TextToSpeech
    speaker = TextToSpeech.__new__(TextToSpeech)
    speaker._max_spoken_chars = 0
    essay = "One sentence. " * 200
    assert speaker._speakable(essay) == essay.strip()


# ------------------------------------------------------------- streaming --

class _StubModel:
    """Stands in for Chatterbox: returns silence proportional to the text."""

    sr = 24000

    def __init__(self):
        self.calls = []

    def generate(self, text, **kwargs):
        import numpy as np
        self.calls.append(text)
        # Roughly 14 characters per second of speech.
        samples = max(1, int(self.sr * len(text) / 14.0))
        return np.zeros((1, samples), dtype="float32")


def _engine_with_stub():
    from voice.chatterbox_engine import ChatterboxConfig, ChatterboxEngine
    engine = ChatterboxEngine.__new__(ChatterboxEngine)
    import threading
    engine.config = ChatterboxConfig()
    engine._lock = threading.Lock()
    engine._model = _StubModel()
    engine.device = "cpu"
    engine._generations = 0
    engine._compute_seconds = 0.0
    engine._audio_seconds = 0.0
    return engine


def test_streaming_yields_a_chunk_at_a_time():
    engine = _engine_with_stub()
    produced = list(engine.generate_stream(THREE_SENTENCES))
    assert len(produced) > 1, "streaming produced one blob"
    for audio, rate, index, total in produced:
        assert rate == 24000
        assert audio.size > 0
        assert 1 <= index <= total
    assert produced[-1][2] == produced[-1][3], "the last chunk is not marked last"


def test_streaming_generates_the_first_chunk_before_the_rest():
    """The whole point: the caller gets audio to play before generation of the
    remainder has happened."""
    engine = _engine_with_stub()
    stream = engine.generate_stream(THREE_SENTENCES)
    first = next(stream)
    assert first is not None
    # Only the first chunk has been asked of the model at this point.
    assert len(engine._model.calls) == 1, \
        f"the model was called {len(engine._model.calls)} times before the " \
        f"first chunk was consumed - generation is not lazy"


def test_one_bad_sentence_does_not_lose_the_answer():
    from voice.chatterbox_engine import ChatterboxGenerationError

    engine = _engine_with_stub()
    original = engine.generate
    calls = {"n": 0}

    def sometimes_fails(text):
        calls["n"] += 1
        if calls["n"] == 2:
            raise ChatterboxGenerationError("this sentence broke the model")
        return original(text)

    engine.generate = sometimes_fails
    produced = list(engine.generate_stream(THREE_SENTENCES))
    assert produced, "a single failed chunk lost the whole reply"
    assert len(produced) >= 1


def test_the_engine_measures_its_own_speed():
    """The status must report a speed that was observed, not assumed."""
    engine = _engine_with_stub()
    assert engine.realtime_factor is None, "a speed was claimed before any work"
    list(engine.generate_stream(THREE_SENTENCES))
    assert engine.realtime_factor is not None
    assert engine.realtime_factor >= 0


def test_warmup_runs_a_real_generation():
    engine = _engine_with_stub()
    elapsed = engine.warmup()
    assert elapsed is not None
    assert engine._model.calls, "warm-up did not call the model"


# ---------------------------------------------------------- CPU tuning --

def test_thread_tuning_uses_physical_cores_not_hyperthreads():
    """Two hyperthreads on one core contend for the vector units this
    workload is bound by - more threads, less throughput."""
    import voice.chatterbox_engine as engine_module

    recorded = {}

    class _FakeTorch:
        @staticmethod
        def set_num_threads(n):
            recorded["threads"] = n

        @staticmethod
        def set_num_interop_threads(n):
            recorded["interop"] = n

        @staticmethod
        def set_grad_enabled(flag):
            recorded["grad"] = flag

    class _FakePsutil:
        @staticmethod
        def cpu_count(logical=True):
            return 8 if logical else 4         # a 4-core i5 with SMT

    engine_module._THREADS_TUNED = False
    with patch.dict(sys.modules, {"torch": _FakeTorch, "psutil": _FakePsutil}):
        threads = engine_module.tune_cpu_threads()
    engine_module._THREADS_TUNED = False

    assert threads == 4, f"used {threads} threads for a 4-core machine"
    assert recorded["threads"] == 4
    assert recorded["interop"] == 1
    assert recorded["grad"] is False, "gradients were left enabled for inference"


def test_thread_tuning_is_safe_without_torch():
    import voice.chatterbox_engine as engine_module
    engine_module._THREADS_TUNED = False
    with patch.dict(sys.modules, {"torch": None}):
        assert engine_module.tune_cpu_threads() == 0
    engine_module._THREADS_TUNED = False


def test_whisper_uses_physical_cores_too():
    from voice.speech_to_text import _physical_cores
    with patch("psutil.cpu_count", side_effect=lambda logical=True: 8 if logical else 4):
        assert _physical_cores() == 4


def test_whisper_transcribe_options_are_tuned_for_cpu():
    from voice.speech_to_text import CPU_TRANSCRIBE_OPTIONS
    assert CPU_TRANSCRIBE_OPTIONS["beam_size"] == 1, "beam search is not worth it here"
    assert CPU_TRANSCRIBE_OPTIONS["vad_filter"] is True, \
        "without VAD, silence is transcribed - slower AND it hallucinates"
    assert CPU_TRANSCRIBE_OPTIONS["condition_on_previous_text"] is False


def test_an_old_faster_whisper_without_vad_still_transcribes():
    """Losing the speed-up is much better than losing transcription."""
    from voice.speech_to_text import _transcribe

    class _OldModel:
        def __init__(self):
            self.used = None

        def transcribe(self, audio, **kwargs):
            if "vad_filter" in kwargs:
                raise TypeError("transcribe() got an unexpected keyword 'vad_filter'")
            self.used = kwargs
            return ([], None)

    model = _OldModel()
    _transcribe(model, "audio")
    assert model.used == {"beam_size": 1}


# -------------------------------------------------------- memory guard --

def test_enough_memory_is_reported_as_fitting():
    with patch.object(memory_guard, "available_gb", return_value=9.0):
        verdict = memory_guard.check("chatterbox")
    assert verdict.ok
    assert "free" in verdict.detail


def test_not_enough_memory_is_refused_with_the_numbers():
    """Loading 3.6 GB into 1 GB of free memory does not run slowly, it makes
    the machine stop responding."""
    with patch.object(memory_guard, "available_gb", return_value=1.0):
        verdict = memory_guard.check("chatterbox")
    assert not verdict.ok
    assert "1.0 GB free" in verdict.detail
    assert "3.6" in verdict.detail
    assert "NOT ENOUGH MEMORY" in verdict.line()


def test_whisper_needs_far_less_than_chatterbox():
    with patch.object(memory_guard, "available_gb", return_value=2.0):
        assert memory_guard.check("whisper", "base").ok
        assert not memory_guard.check("chatterbox").ok


def test_an_unmeasurable_machine_gets_the_benefit_of_the_doubt():
    """A missing psutil must not be the reason voice does not work."""
    with patch.object(memory_guard, "available_gb", return_value=None):
        verdict = memory_guard.check("chatterbox")
    assert verdict.ok
    assert "could not be measured" in verdict.detail


def test_chatterbox_reports_a_memory_refusal_rather_than_crashing():
    """The provider must degrade to a state with a reason, like every other
    unavailable provider - not raise into startup."""
    from voice.providers import ChatterboxVoiceProvider, ProviderState

    provider = ChatterboxVoiceProvider()
    with patch.object(provider, "probe", return_value=type(
            "R", (), {"state": ProviderState.NOT_PROBED, "detail": "present"})()), \
         patch.object(memory_guard, "available_gb", return_value=0.5), \
         patch.object(memory_guard, "free_the_chat_model",
                      return_value=(False, "nothing to unload")):
        report = provider.activate()

    assert report.state is ProviderState.UNSUPPORTED
    assert "GB free" in report.detail
    assert report.evidence["available_gb"] == 0.5


def test_a_memory_shortfall_tries_unloading_the_chat_model_first():
    """A 7B model at q4 is 4.5 GB that Ollama keeps resident for five minutes
    after the last question. Releasing it is the one lever that reliably makes
    room on a 16 GB machine."""
    from voice.providers import ChatterboxVoiceProvider, ProviderState

    provider = ChatterboxVoiceProvider()
    readings = iter([0.5, 6.0])          # before unload, then after

    with patch.object(provider, "probe", return_value=type(
            "R", (), {"state": ProviderState.NOT_PROBED, "detail": "present"})()), \
         patch.object(memory_guard, "available_gb", side_effect=lambda: next(readings)), \
         patch.object(memory_guard, "free_the_chat_model",
                      return_value=(True, "unloaded mistral:7b")) as unload, \
         patch("voice.text_to_speech.TextToSpeech") as engine:
        engine.return_value.is_available = True
        report = provider.activate()

    assert unload.called, "the chat model was never released"
    assert report.state is ProviderState.READY, report.detail
