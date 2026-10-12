"""
Voice provider selection, and the states it is allowed to report.

The requirements these encode:

  * starting ZEUS must never depend on downloading a voice model
  * no repeated download attempts at runtime, ever
  * no "voice ready" that was not verified
  * no voice is a supported state (TEXT MODE), not a failure to start

The tests below run with nothing installed, with only an OS voice, with only
a recogniser, and with a provider that is present but broken - because those
are the four machines this has to work on.
"""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from voice import providers  # noqa: E402
from voice.providers import (  # noqa: E402
    ProviderState,
    VoiceManager,
    VoiceState,
)


class _StubOutput(providers.OutputProvider):
    name = "stub_out"

    def __init__(self, state=ProviderState.READY, detail="stub"):
        self._state, self._detail = state, detail
        self.spoken = []

    def probe(self):
        return providers.ProviderReport(self.name, "output", self._state, self._detail)

    def activate(self):
        return providers.ProviderReport(self.name, "output", self._state, self._detail)

    def speak(self, text):
        self.spoken.append(text)
        return True


class _StubInput(providers.InputProvider):
    name = "stub_in"

    def __init__(self, state=ProviderState.READY, detail="stub"):
        self._state, self._detail = state, detail

    def probe(self):
        return providers.ProviderReport(self.name, "input", self._state, self._detail)

    def activate(self):
        return providers.ProviderReport(self.name, "input", self._state, self._detail)

    def transcribe_array(self, audio, samplerate=16000):
        return "stub transcript"

    def listen_once(self, duration=5.0):
        return "stub transcript"


def _manager(outputs, inputs):
    manager = VoiceManager()
    manager._output_candidates = lambda: list(outputs)
    manager._input_candidates = lambda: list(inputs)
    return manager


@pytest.fixture(autouse=True)
def voice_switched_on():
    with patch.object(providers, "voice_enabled", return_value=True):
        yield


# ------------------------------------------------------------- the states --

def test_both_directions_working_is_available():
    manager = _manager([_StubOutput()], [_StubInput()])
    assert manager.start() is VoiceState.AVAILABLE
    assert manager.can_speak and manager.can_listen


def test_output_only_is_degraded_and_says_why():
    """The common case on a fresh laptop: the OS can speak, but no speech
    recogniser is installed. That is DEGRADED, not READY and not broken."""
    manager = _manager([_StubOutput()],
                       [_StubInput(ProviderState.MODEL_MISSING, "no model")])
    assert manager.start() is VoiceState.DEGRADED
    assert manager.can_speak and not manager.can_listen
    assert "text mode" in manager.detail.lower()


def test_input_only_is_degraded():
    manager = _manager([_StubOutput(ProviderState.INSTALLABLE, "not installed")],
                       [_StubInput()])
    assert manager.start() is VoiceState.DEGRADED
    assert manager.can_listen and not manager.can_speak


def test_nothing_installed_is_disabled_not_an_error():
    """The requirement stated plainly: no voice is a valid way to run."""
    manager = _manager([_StubOutput(ProviderState.INSTALLABLE, "not installed")],
                       [_StubInput(ProviderState.INSTALLABLE, "not installed")])
    assert manager.start() is VoiceState.DISABLED
    assert "text mode" in manager.detail.lower()
    assert manager.report()["text_mode"] is True


def test_a_provider_that_breaks_is_an_error_with_the_reason():
    manager = _manager([_StubOutput(ProviderState.FAILED, "audio device is in use")],
                       [_StubInput(ProviderState.FAILED, "model file is corrupt")])
    assert manager.start() is VoiceState.ERROR
    assert "audio device is in use" in manager.detail


def test_the_switch_in_config_wins():
    with patch.object(providers, "voice_enabled", return_value=False):
        manager = _manager([_StubOutput()], [_StubInput()])
        assert manager.start() is VoiceState.DISABLED
        assert not manager.can_speak


def test_state_is_initializing_before_start():
    manager = VoiceManager()
    assert manager.state is VoiceState.DISABLED       # not yet started
    assert manager.report()["started"] is False


# ------------------------------------------------------------- no claims --

def test_a_report_never_claims_a_provider_that_did_not_load():
    manager = _manager([_StubOutput(ProviderState.MODEL_MISSING, "weights absent")],
                       [_StubInput(ProviderState.MODEL_MISSING, "weights absent")])
    manager.start()
    report = manager.report()
    assert report["output"]["provider"] is None
    assert report["output"]["available"] is False
    assert report["input"]["available"] is False
    for line in manager.banner():
        assert "READY" not in line.upper()


def test_the_banner_states_what_is_actually_speaking():
    manager = _manager([_StubOutput()], [_StubInput()])
    manager.start()
    banner = "\n".join(manager.banner())
    assert "stub_out" in banner and "stub_in" in banner
    assert "EXTERNAL VOICE SERVICES: DISABLED" in banner


# ------------------------------------------------------------ no downloads --

def test_probing_touches_no_network_and_loads_no_model():
    """A probe is filesystem and import-spec only. If it ever opens a socket,
    a machine with no network pays a timeout on every startup."""
    manager = VoiceManager()
    import socket
    real_socket = socket.socket

    def refuse(*args, **kwargs):
        raise AssertionError("probing voice providers opened a network socket")

    socket.socket = refuse
    try:
        reports = manager.probe()
    finally:
        socket.socket = real_socket
    assert reports, "no providers were probed"
    for report in reports.values():
        assert report.requires_download is False


def test_no_provider_advertises_a_runtime_download():
    manager = VoiceManager()
    for provider in manager._output_candidates() + manager._input_candidates():
        assert provider.requires_download is False, (
            f"{provider.name} would download at runtime")


def test_missing_weights_are_reported_not_fetched():
    """The distinction that matters: 'the model is not here' must be a state,
    never a trigger to go and get it."""
    manager = VoiceManager()
    with patch.object(providers, "missing_files", return_value=["model.bin"]), \
         patch.object(providers, "_installed", return_value=True):
        for provider in (providers.WhisperInputProvider(),
                         providers.ChatterboxVoiceProvider()):
            report = provider.probe()
            assert report.state is ProviderState.MODEL_MISSING
            assert report.requires_download is False


def test_offline_enforcement_happens_before_any_model_loads():
    """faster-whisper and chatterbox both reach for the Hugging Face Hub on
    load unless these flags are set first."""
    calls = []
    manager = _manager([_StubOutput()], [_StubInput()])
    with patch.object(providers, "enforce_offline",
                      side_effect=lambda: calls.append("enforced")):
        manager.start()
    assert calls, "offline enforcement did not run before providers were loaded"


# ------------------------------------------------------------- behaviour --

def test_speaking_and_listening_go_to_the_selected_provider():
    output, input_provider = _StubOutput(), _StubInput()
    manager = _manager([output], [input_provider])
    manager.start()
    assert manager.speak("shell side welds inspected") is True
    assert output.spoken == ["shell side welds inspected"]
    assert manager.listen_once() == "stub transcript"


def test_speaking_with_no_voice_returns_false_rather_than_raising():
    manager = _manager([_StubOutput(ProviderState.INSTALLABLE, "absent")],
                       [_StubInput(ProviderState.INSTALLABLE, "absent")])
    manager.start()
    assert manager.speak("anything") is False
    assert manager.listen_once() == ""


def test_a_provider_that_throws_while_speaking_does_not_propagate():
    class _Exploding(_StubOutput):
        def speak(self, text):
            raise RuntimeError("the audio device disappeared")

    manager = _manager([_Exploding()], [_StubInput()])
    manager.start()
    assert manager.speak("hello") is False        # reported, not raised


def test_the_cheapest_working_output_is_used_when_the_heavy_one_is_absent():
    heavy = _StubOutput(ProviderState.MODEL_MISSING, "checkpoints absent")
    heavy.name = "chatterbox"
    cheap = _StubOutput()
    cheap.name = "system"
    manager = _manager([heavy, cheap], [_StubInput(ProviderState.INSTALLABLE, "absent")])
    assert manager.start() is VoiceState.DEGRADED
    assert manager.output.name == "system"


# ------------------------------------------------------------- integration --

def test_the_application_starts_with_no_voice_at_all():
    """The whole point: an unavailable voice must not be a fatal error."""
    manager = _manager([_StubOutput(ProviderState.INSTALLABLE, "absent")],
                       [_StubInput(ProviderState.INSTALLABLE, "absent")])
    state = manager.start()
    assert state is VoiceState.DISABLED
    # and the object is still fully usable afterwards
    assert manager.report()["state"] == "disabled"
    manager.close()
