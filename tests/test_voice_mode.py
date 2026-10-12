"""
Voice mode against text mode, and the status the interface shows.

The rule being defended: a question ASKED ALOUD is answered for the ear; a
question TYPED is answered for the screen, even when voice is switched on.
Everything ZEUS writes to a file is untouched by either.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from voice import speech_prep
from voice.providers import SpeechState


# ================================================== the style separation ==

class _RecordingClient:
    """Stands in for Ollama and remembers the system prompt it was given."""

    def __init__(self):
        self.system_prompts = []

    def is_available(self):
        return True

    def chat(self, model, messages, **_kwargs):
        self.system_prompts.append(messages[0]["content"])
        return "The pump is running at 2950 rpm."


@pytest.fixture()
def engine(monkeypatch):
    from core import decision_engine as module

    client = _RecordingClient()
    monkeypatch.setattr(module, "ollama_client", client)
    monkeypatch.setattr(module.model_router, "route", lambda *_a, **_k: "test:latest")
    monkeypatch.setattr(module.model_router, "classify_task", lambda *_a, **_k: "chat")
    instance = module.DecisionEngine()
    instance._recorded = client
    return instance


def test_a_typed_question_gets_the_written_style(engine):
    engine._chat_response("what is the pump speed", [], spoken=False)
    prompt = engine._recorded.system_prompts[-1]
    assert "SPOKEN" not in prompt.upper()
    assert "knowledgeable personal AI assistant" in prompt


def test_a_spoken_question_gets_the_speakable_style(engine):
    engine._chat_response("what is the pump speed", [], spoken=True)
    prompt = engine._recorded.system_prompts[-1]
    assert "SPOKEN" in prompt.upper(), (
        "a question asked aloud was answered with the written-format prompt")


def test_voice_style_can_be_switched_off_in_config(engine, monkeypatch):
    """`voice_style = false` must actually disable it, not be decorative."""
    from voice import offline
    monkeypatch.setattr(offline, "config_bool",
                        lambda key, default=False: False if key == "voice_style"
                        else default)
    engine._chat_response("what is the pump speed", [], spoken=True)
    assert "SPOKEN" not in engine._recorded.system_prompts[-1].upper()


def test_a_broken_voice_import_still_answers(engine, monkeypatch):
    """Voice is an accessory. A failure inside it must never cost the user
    their answer."""
    import builtins

    real_import = builtins.__import__

    def exploding_import(name, *args, **kwargs):
        if "speech_prep" in name:
            raise ImportError("simulated")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", exploding_import)
    answer = engine._chat_response("what is the pump speed", [], spoken=True)
    assert "2950" in answer


def test_the_spoken_flag_reaches_the_engine_only_from_the_microphone():
    """main.py must set it for listen_and_process and NOT for typed input."""
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    assert 'context = {"history": history, "spoken": spoken}' in source
    # History is read BEFORE the new message is stored (2026-09-25): reading
    # it after sent every question to the model twice.
    handle = source[source.index("def handle_text"):]
    assert handle.index("history = self.memory.get_context(8)") < handle.index(
        'self.memory.add_message("user", text)')
    assert "self.handle_text(text, spoken=True)" in source, (
        "spoken input is not flagged, so it would be answered in written style")
    # handle_text's own default must stay False, or typing would be treated as
    # speech the moment --voice is passed.
    assert "def handle_text(self, text: str, spoken: bool = False)" in source


def test_the_orchestrator_tolerates_a_handler_without_the_spoken_argument():
    """Older or injected handlers take (text, history). Passing a third
    argument to those would raise instead of answering."""
    from core.orchestration.orchestrator import Orchestrator

    calls = []

    def two_argument_handler(text, history):
        calls.append(text)
        return "answered"

    orchestrator = Orchestrator(chat_handler=two_argument_handler)
    assert orchestrator._answer_directly("hello", spoken=True) == "answered"
    assert calls == ["hello"]


def test_the_orchestrator_passes_spoken_to_a_handler_that_accepts_it():
    from core.orchestration.orchestrator import Orchestrator

    seen = {}

    def three_argument_handler(text, history, spoken=False):
        seen["spoken"] = spoken
        return "answered"

    orchestrator = Orchestrator(chat_handler=three_argument_handler)
    orchestrator._answer_directly("hello", spoken=True)
    assert seen["spoken"] is True


# =============================================== documents stay written ==

def test_speech_preparation_never_touches_what_is_written_to_disk():
    """prepare() returns a SPOKEN rendering alongside the original. The
    original is what documents and the screen use, and it must come back
    byte-for-byte."""
    written = ("## Findings\n\n"
               "- Pump **P-204A** at 4.82 bar\n"
               "- Vibration 6.8 mm/s (limit 4.5 mm/s)\n")
    prepared = speech_prep.prepare(written)
    assert prepared.original_text == written, (
        "the written text was modified by preparing speech from it")
    assert prepared.spoken_text != written, "nothing was adapted for speech"


def test_truncation_marks_itself_and_keeps_the_original_whole():
    long_answer = " ".join(f"Sentence number {n} of the report." for n in range(200))
    prepared = speech_prep.prepare(long_answer, max_chars=300)
    assert prepared.truncated is True
    assert "on screen" in prepared.spoken_text
    assert prepared.original_text == long_answer


# ============================================================== status ==

def test_the_manager_exposes_the_controls_the_interface_needs():
    """The window drives stop and pause through the manager rather than
    reaching into its attributes."""
    from voice.providers import VoiceManager

    manager = VoiceManager()
    for method in ("output_provider", "stop_speaking", "pause_speaking",
                   "resume_speaking", "speech_state"):
        assert hasattr(manager, method), f"VoiceManager has no {method}()"


def test_status_calls_are_safe_with_no_provider_loaded():
    """Text mode is a supported way to run. Every status call must answer
    rather than raise when nothing is loaded."""
    from voice.providers import VoiceManager

    manager = VoiceManager()
    manager.output = None
    assert manager.stop_speaking() is False
    assert manager.pause_speaking() is False
    assert manager.resume_speaking() is False
    assert manager.speech_state() == SpeechState.IDLE


def test_the_report_carries_the_device_evidence_the_badge_needs():
    from voice.providers import VoiceManager

    report = VoiceManager().report()
    assert "evidence" in report["output"]
    assert "speech_state" in report["output"]


def test_every_documented_state_exists():
    """The eight states the interface promises. A missing one would leave a
    blank badge at exactly the moment somebody is watching."""
    for state in ("IDLE", "GENERATING", "SPEAKING", "PAUSED", "STOPPED"):
        assert hasattr(SpeechState, state)
    assert set(SpeechState.ALL) == {"IDLE", "GENERATING", "SPEAKING",
                                    "PAUSED", "STOPPED"}


# The voice badge and the speech-state signal moved with the interface:
# tests/test_no_previous_ui.py pins both against the ZEUS shell.


# ============================================ interrupt, queue, no overlap ==
# "Never play two ZEUS responses over each other" is the hard rule. How the
# one in progress is ended is configuration.

class _SlowSpeaker:
    """An output provider that takes a measurable time to speak."""

    name = "slow"
    direction = "output"

    def __init__(self, duration=0.4):
        self.duration = duration
        self.log = []
        self._stop = False
        self._concurrent = 0
        self.overlapped = False

    def speak(self, text):
        import time
        self._concurrent += 1
        if self._concurrent > 1:
            self.overlapped = True
        self.log.append(("start", text))
        deadline = time.time() + self.duration
        while time.time() < deadline and not self._stop:
            time.sleep(0.01)
        self.log.append(("end", text))
        self._concurrent -= 1
        return True

    def stop(self):
        self._stop = True

    def speech_state(self):
        return SpeechState.IDLE

    def on_state_change(self, callback):
        pass


def _manager_with(speaker, interruptible=True, monkeypatch=None):
    from voice import providers as module

    manager = module.VoiceManager()
    manager.output = speaker
    monkeypatch.setattr(module, "config_bool",
                        lambda key, default=False: interruptible
                        if key == "voice_interruptible" else default)
    return manager


def test_a_new_reply_interrupts_the_old_one_when_configured(monkeypatch):
    speaker = _SlowSpeaker(duration=2.0)
    manager = _manager_with(speaker, interruptible=True, monkeypatch=monkeypatch)

    import time
    manager.speak_async("first reply")
    time.sleep(0.15)
    manager.speak_async("second reply")
    manager.wait_until_spoken(timeout=10)

    assert speaker.overlapped is False, "two replies were spoken at once"
    assert ("start", "second reply") in speaker.log


def test_replies_queue_instead_of_cutting_off_when_not_interruptible(monkeypatch):
    """With interruptible off, the first utterance finishes before the second
    starts - the right behaviour when a reply carries a safety instruction."""
    speaker = _SlowSpeaker(duration=0.3)
    manager = _manager_with(speaker, interruptible=False, monkeypatch=monkeypatch)

    import time
    manager.speak_async("first reply")
    time.sleep(0.05)
    manager.speak_async("second reply")
    manager.wait_until_spoken(timeout=10)

    assert speaker.overlapped is False
    order = [text for event, text in speaker.log if event == "end"]
    assert order[:2] == ["first reply", "second reply"], (
        f"the first reply was cut off despite voice_interruptible=false: {order}")


def test_speaking_nothing_is_refused_before_a_thread_is_started(monkeypatch):
    speaker = _SlowSpeaker()
    manager = _manager_with(speaker, monkeypatch=monkeypatch)
    assert manager.speak_async("") is False
    assert speaker.log == []


def test_stop_reaches_the_provider(monkeypatch):
    speaker = _SlowSpeaker(duration=5.0)
    manager = _manager_with(speaker, monkeypatch=monkeypatch)

    import time
    manager.speak_async("a long reply")
    time.sleep(0.1)
    assert manager.stop_speaking() is True
    assert manager.wait_until_spoken(timeout=5) is True
