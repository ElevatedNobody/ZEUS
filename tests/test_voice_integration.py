"""
Voice integration tests: local Whisper STT + local Chatterbox TTS.

The real weights are multi-hundred-megabyte downloads, so the model
runtimes are replaced with strict stubs that stand in for
`faster_whisper.WhisperModel`, `chatterbox.tts.ChatterboxTTS`,
`sounddevice` and `torch`. Everything ZEUS actually owns runs for
real: offline enforcement, model-path resolution, missing-model
errors, lazy/preload lifecycle, the STT -> handle_text -> memory ->
TTS chain, and persistence across restarts.

The stubs assert the offline contract rather than merely standing in:
`ChatterboxTTS.from_pretrained` raises if anything ever calls it, and
the Whisper stub fails the test if `local_files_only` is not set.
"""

import os
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

WORK = Path("/tmp/medha_voice_test")
MODELS = WORK / "models"
DB_PATH = WORK / "medha_voice.db"

# --------------------------------------------------------------------- stubs

_PLAYED = []          # audio handed to sounddevice.play()
_LOADED = {}          # what the model stubs were constructed with
_TRANSCRIPTS = ["remember that I prefer concise industrial reports"]


class _FakeSegment:
    def __init__(self, text):
        self.text = text


class _FakeWhisperModel:
    def __init__(self, model_path, device="cpu", compute_type="int8",
                 local_files_only=False, **kwargs):
        # The offline contract: ZEUS must pass a real local directory and
        # must set local_files_only. A bare size string like "base" would
        # send faster-whisper to the Hugging Face Hub.
        assert Path(model_path).is_dir(), f"whisper path is not a local dir: {model_path}"
        assert local_files_only is True, "faster-whisper was not pinned to local files"
        _LOADED["whisper"] = {"path": str(model_path), "device": device,
                              "compute_type": compute_type}

    def transcribe(self, audio, beam_size=1, **kwargs):
        text = _TRANSCRIPTS[0] if _TRANSCRIPTS else ""
        return [_FakeSegment(text)], types.SimpleNamespace(language="en")


class _FakeTensor:
    def __init__(self, values):
        self._values = values

    def detach(self):
        return self

    def cpu(self):
        return self

    def numpy(self):
        import numpy as np
        return np.asarray(self._values, dtype="float32")


class _FakeChatterboxTTS:
    sr = 24000

    @classmethod
    def from_local(cls, model_dir, device):
        assert Path(model_dir).is_dir(), f"chatterbox path is not a local dir: {model_dir}"
        _LOADED["chatterbox"] = {"path": str(model_dir), "device": device}
        return cls()

    @classmethod
    def from_pretrained(cls, *args, **kwargs):
        raise AssertionError(
            "from_pretrained() downloads from Hugging Face - it must never be "
            "called at runtime")

    def generate(self, text, **kwargs):
        _LOADED["last_generate"] = {"text": text, "kwargs": kwargs}
        # 0.25s of quiet audio, shaped (1, samples) like the real model.
        return _FakeTensor([[0.0] * (self.sr // 4)])


def _stub_module(name: str) -> types.ModuleType:
    """A stub that also carries a __spec__, so importlib.util.find_spec() -
    which voice/offline.py uses to report what is installed - sees it the way
    it would see a real package."""
    import importlib.machinery
    module = types.ModuleType(name)
    module.__spec__ = importlib.machinery.ModuleSpec(name, None)
    return module


# Audio device behaviour the stub emulates. Tests flip these to reproduce
# the real-world failures: a microphone that refuses 16 kHz, no device at
# all, and a capture that returns silence.
_AUDIO = {"devices": [{"name": "Stub Microphone", "max_input_channels": 1,
                       "max_output_channels": 0, "default_samplerate": 48000.0},
                      {"name": "Stub Speakers", "max_input_channels": 0,
                       "max_output_channels": 2, "default_samplerate": 48000.0}],
          "supported_rates": [48000, 44100],   # deliberately NOT 16000
          "recorded_amplitude": 0.2,
          "last_rec": {}}


def _install_stubs():
    sounddevice = _stub_module("sounddevice")

    def _rec(frames, samplerate=16000, channels=1, dtype="float32", device=None, **kwargs):
        import numpy as np
        if samplerate not in _AUDIO["supported_rates"]:
            raise RuntimeError("Error opening InputStream: Invalid sample rate")
        _AUDIO["last_rec"] = {"frames": frames, "samplerate": samplerate,
                              "device": device}
        # A quiet sine so the RMS gate sees real signal (or silence when the
        # test sets recorded_amplitude to 0).
        t = np.arange(frames, dtype="float32") / float(samplerate)
        signal = (_AUDIO["recorded_amplitude"]
                  * np.sin(2 * np.pi * 220.0 * t)).astype("float32")
        return signal.reshape(-1, channels)

    def _query_devices(index=None, kind=None):
        if index is None:
            return list(_AUDIO["devices"])
        return _AUDIO["devices"][index]

    def _check_input_settings(device=None, channels=1, samplerate=16000, dtype=None):
        if not any(d["max_input_channels"] for d in _AUDIO["devices"]):
            raise RuntimeError("no input device")
        if samplerate not in _AUDIO["supported_rates"]:
            raise ValueError("Invalid sample rate")
        return True

    sounddevice.rec = _rec
    sounddevice.wait = lambda *a, **k: None
    sounddevice.play = lambda audio, samplerate=None, **k: _PLAYED.append(
        (len(audio), samplerate))
    sounddevice.stop = lambda *a, **k: None
    sounddevice.query_devices = _query_devices
    sounddevice.check_input_settings = _check_input_settings
    sounddevice.default = types.SimpleNamespace(device=(0, 1))
    sys.modules["sounddevice"] = sounddevice

    faster_whisper = _stub_module("faster_whisper")
    faster_whisper.WhisperModel = _FakeWhisperModel
    sys.modules["faster_whisper"] = faster_whisper

    chatterbox = _stub_module("chatterbox")
    chatterbox_tts = _stub_module("chatterbox.tts")
    chatterbox_tts.ChatterboxTTS = _FakeChatterboxTTS
    chatterbox.tts = chatterbox_tts
    sys.modules["chatterbox"] = chatterbox
    sys.modules["chatterbox.tts"] = chatterbox_tts

    if "torch" not in sys.modules:
        try:
            import torch  # noqa: F401
        except ImportError:
            torch = _stub_module("torch")
            torch.cuda = types.SimpleNamespace(is_available=lambda: False,
                                               empty_cache=lambda: None)
            torch.backends = types.SimpleNamespace(mps=None)
            torch.__version__ = "2.6.0"
            # torch.load exists so the weights_only retry patches something
            # real, exactly as it would against a genuine torch install.
            torch.load = lambda *a, **k: {}
            sys.modules["torch"] = torch


def _make_model_dirs():
    from voice.offline import CHATTERBOX_REQUIRED_FILES, WHISPER_REQUIRED_FILES
    whisper = MODELS / "whisper"
    chatterbox = MODELS / "chatterbox"
    whisper.mkdir(parents=True, exist_ok=True)
    chatterbox.mkdir(parents=True, exist_ok=True)
    for name in WHISPER_REQUIRED_FILES:
        (whisper / name).write_bytes(b"stub")
    (whisper / "vocabulary.txt").write_bytes(b"stub")
    for name in CHATTERBOX_REQUIRED_FILES:
        (chatterbox / name).write_bytes(b"stub")
    return whisper, chatterbox


_REAL_MEMORY_GUARD = {}


def setup_module(module):
    import shutil
    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True, exist_ok=True)
    os.environ["MEDHA_MODELS_DIR"] = str(MODELS)
    _install_stubs()
    _make_model_dirs()
    # The engines here are stubs that need no memory, but voice.memory_guard
    # measures the REAL machine: with under ~5 GB free it refused the stubbed
    # Chatterbox (the tests then saw the system voice and failed, depending on
    # what else was open), and it asked the operator's real Ollama to unload
    # its model in the middle of a test run. Pinned for this module only.
    from voice import memory_guard
    _REAL_MEMORY_GUARD.update(available_gb=memory_guard.available_gb,
                              free_the_chat_model=memory_guard.free_the_chat_model)
    memory_guard.available_gb = lambda: 64.0
    memory_guard.free_the_chat_model = lambda: (False, "test: nothing to unload")


def teardown_module(module):
    from voice import memory_guard
    for name, original in _REAL_MEMORY_GUARD.items():
        setattr(memory_guard, name, original)

    from memory import database
    database.DB_PATH = DB_PATH
    database.init_db()


def setup_function(function):
    """Re-pin the database for every test: other modules point
    database.DB_PATH at their own file when pytest imports them."""
    from memory import database
    database.DB_PATH = DB_PATH
    database.set_active_user(None)


# ------------------------------------------------------------ offline mode

def test_offline_flags_are_set_before_any_model_library_loads():
    from voice.offline import enforce_offline, is_offline_enforced
    enforce_offline()
    assert is_offline_enforced()
    for flag in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_DATASETS_OFFLINE"):
        assert os.environ.get(flag) == "1", f"{flag} not set"
    assert os.environ.get("HF_HUB_DISABLE_TELEMETRY") == "1"
    assert os.environ.get("GRADIO_ANALYTICS_ENABLED") == "False"


def test_setup_only_helper_can_reopen_the_network():
    """allow_network() exists so scripts/setup_models.py can download once.
    It must clear exactly the download flags and nothing else."""
    from voice.offline import allow_network, enforce_offline
    enforce_offline()
    allow_network()
    assert os.environ.get("HF_HUB_OFFLINE") is None
    # telemetry stays off even during setup
    assert os.environ.get("HF_HUB_DISABLE_TELEMETRY") == "1"
    enforce_offline()
    assert os.environ.get("HF_HUB_OFFLINE") == "1"


def test_the_banner_names_the_provider_that_actually_loaded():
    """The old banner printed 'STT: LOCAL WHISPER (READY)' from a directory
    listing, naming two engines whether or not they were the ones in use. The
    banner now states the reached state and the provider behind it."""
    from voice.providers import VoiceManager
    manager = VoiceManager()
    manager.start()
    lines = manager.banner()

    assert lines[0].startswith("VOICE: ")
    assert manager.state.value.upper() in lines[0]
    assert "EXTERNAL VOICE SERVICES: DISABLED" in lines[-1]

    speaking = next(line for line in lines if "SPEECH OUT" in line)
    if manager.output is None:
        assert "none" in speaking
    else:
        assert manager.output.name in speaking
    manager.close()


def test_the_old_unverified_banner_is_gone():
    from voice import offline
    assert not hasattr(offline, "voice_banner"), \
        "the file-listing banner is back; it can claim READY without a load"


# ------------------------------------------------------------- model paths

def test_model_paths_resolve_absolutely_regardless_of_cwd():
    from voice.offline import resolve_chatterbox_dir, resolve_models_dir, resolve_whisper_dir
    cwd = os.getcwd()
    try:
        os.chdir("/tmp")
        assert resolve_models_dir().is_absolute()
        assert resolve_whisper_dir() == MODELS / "whisper"
        assert resolve_chatterbox_dir() == MODELS / "chatterbox"
    finally:
        os.chdir(cwd)


def test_model_paths_are_never_hardcoded_user_directories():
    source = (Path(__file__).resolve().parent.parent / "voice" / "offline.py").read_text()
    assert "C:\\\\Users" not in source
    assert "/home/" not in source


def test_missing_model_is_an_actionable_error_not_a_download():
    from voice.offline import MissingModelError, WHISPER_REQUIRED_FILES, require_model_dir
    empty = WORK / "empty_models"
    empty.mkdir(exist_ok=True)
    try:
        require_model_dir(empty, WHISPER_REQUIRED_FILES, "Whisper STT")
    except MissingModelError as e:
        assert "setup_models.py" in str(e)
        assert "model.bin" in str(e)
    else:
        raise AssertionError("a missing model must raise MissingModelError")


def test_check_voice_models_reports_completeness():
    from voice.offline import check_voice_models
    report = check_voice_models()
    assert report["whisper"]["present"] is True
    assert report["chatterbox"]["present"] is True
    assert report["whisper"]["missing"] == []


# --------------------------------------------------------------------- STT

def test_whisper_loads_from_the_local_directory():
    from voice.speech_to_text import SpeechToText
    stt = SpeechToText(preload=True)
    assert stt.is_available, stt.last_error
    assert _LOADED["whisper"]["path"] == str(MODELS / "whisper")
    assert _LOADED["whisper"]["compute_type"] == "int8"
    stt.close()


def test_listen_once_returns_the_transcript():
    from voice.speech_to_text import SpeechToText
    stt = SpeechToText(preload=True)
    assert stt.listen_once(duration=1.0) == _TRANSCRIPTS[0]
    stt.close()


def _stages_during(fn):
    from voice.speech_to_text import _stage_listeners, subscribe_stage
    seen = []

    def record(stage, detail=""):
        seen.append(stage)
    subscribe_stage(record)
    try:
        return fn(), seen
    finally:
        _stage_listeners.remove(record)


def test_listening_publishes_its_real_stages_in_order():
    """The MIC indicator shows what the recogniser is doing: it is not LISTENING
    while Whisper is still loading - speech said then would be lost."""
    from voice.speech_to_text import SpeechToText
    _LOADED.pop("whisper", None)
    stt = SpeechToText(preload=False)
    text, seen = _stages_during(lambda: stt.listen_once(duration=1.0))
    assert text == _TRANSCRIPTS[0]
    assert seen == ["PREPARING RECOGNISER", "LISTENING", "SPEECH DETECTED", "TRANSCRIBING", "TRANSCRIBED"]
    text, seen = _stages_during(lambda: stt.listen_once(duration=1.0))      # loaded now: no PREPARING
    assert seen[0] == "LISTENING"
    stt.close()


def test_silence_is_no_speech_not_an_error():
    from voice.speech_to_text import SpeechToText
    stt = SpeechToText(preload=True)
    _AUDIO["recorded_amplitude"] = 0.0
    try:
        text, seen = _stages_during(lambda: stt.listen_once(duration=1.0))
    finally:
        _AUDIO["recorded_amplitude"] = 0.2
    assert text == "" and stt.last_error == "silence" and seen[-1] == "NO SPEECH"
    stt.close()


def test_a_memory_refusal_is_retried_not_a_dead_microphone(monkeypatch):
    """A chat model resident in memory made Whisper's load fail once - that used to
    switch voice input off for the whole session."""
    from voice.speech_to_text import SpeechToText
    stt = SpeechToText(preload=False)

    def refuse(self):
        raise RuntimeError("insufficient runtime memory: 1.2 GB commit available, 5.5 GB needed")
    with monkeypatch.context() as m:
        m.setattr(SpeechToText, "_load_model", refuse)
        text, seen = _stages_during(lambda: stt.listen_once(duration=1.0))
    assert text == "" and stt.is_available and "insufficient runtime memory" in stt.last_error
    assert seen == ["PREPARING RECOGNISER", "STT ERROR"]
    assert stt.listen_once(duration=1.0) == _TRANSCRIPTS[0]              # the next press works
    stt.close()


def test_the_assistant_says_why_nothing_was_heard_and_routes_speech_like_typing():
    import types as _types
    import main
    assistant = main.MedhaAssistant.__new__(main.MedhaAssistant)
    assistant._listening = False
    replies = {}
    for reason, expected in (("silence", "microphone stayed silent"),
                             ("insufficient runtime memory: 1.2 GB commit available", "could not start"),
                             ("Error opening InputStream: -9999", "Voice input failed")):
        assistant.voice = _types.SimpleNamespace(can_listen=True, detail="", listen_once=lambda: "",
                                                 last_listen_error=reason)
        replies[reason] = main.MedhaAssistant.listen_and_process(assistant)
        assert expected in replies[reason]["response"], replies[reason]
    assert replies["silence"]["stt"]["stage"] == "NO SPEECH"
    assert replies["Error opening InputStream: -9999"]["stt"]["stage"] == "STT ERROR"
    seen = []
    assistant.voice = _types.SimpleNamespace(can_listen=True, detail="", listen_once=lambda: "status of pump P-101",
                                             last_listen_error="")
    assistant.handle_text = lambda text, spoken=False: seen.append((text, spoken)) or {"response": "ok"}
    result = main.MedhaAssistant.listen_and_process(assistant)
    assert seen == [("status of pump P-101", True)] and result["transcript"] == "status of pump P-101"


def test_stt_lazy_mode_defers_the_load_but_still_verifies_the_model():
    from voice.speech_to_text import SpeechToText
    _LOADED.pop("whisper", None)
    stt = SpeechToText(preload=False)
    assert stt.is_available, stt.last_error
    assert "whisper" not in _LOADED, "lazy mode should not load the model yet"
    assert stt.listen_once(duration=1.0)
    assert "whisper" in _LOADED, "lazy mode should load on first use"
    stt.close()


def test_stt_reports_missing_model_instead_of_crashing():
    from voice.speech_to_text import SpeechToText
    os.environ["MEDHA_STT_MODEL_DIR"] = str(WORK / "nowhere")
    try:
        stt = SpeechToText()
        assert stt.is_available is False
        assert "setup_models.py" in (stt.last_error or "")
        assert stt.listen_once(duration=1.0) == ""     # degrades, does not raise
    finally:
        os.environ.pop("MEDHA_STT_MODEL_DIR", None)


# --------------------------------------------------------------------- TTS

def test_chatterbox_loads_from_local_files_only():
    from voice.text_to_speech import TextToSpeech
    tts = TextToSpeech(preload=True)
    assert tts.is_available, tts.last_error
    assert _LOADED["chatterbox"]["path"] == str(MODELS / "chatterbox")
    tts.close()


def test_speak_generates_and_plays_audio_locally():
    from voice.text_to_speech import TextToSpeech
    _PLAYED.clear()
    tts = TextToSpeech(preload=True)
    tts.speak("Inspection report processed.")
    assert _PLAYED, "nothing was sent to the audio device"
    samples, rate = _PLAYED[-1]
    assert samples > 0 and rate == 24000
    assert _LOADED["last_generate"]["text"] == "Inspection report processed."
    tts.close()


def test_tts_ignores_empty_and_non_string_input():
    from voice.text_to_speech import TextToSpeech
    _PLAYED.clear()
    tts = TextToSpeech(preload=True)
    tts.speak("")
    tts.speak(None)
    tts.speak("   ")
    assert _PLAYED == []
    tts.close()


def test_tts_model_is_loaded_at_most_once():
    from voice.text_to_speech import TextToSpeech
    tts = TextToSpeech(preload=True)
    first = tts._engine
    tts.speak("one")
    tts.speak("two")
    assert tts._engine is first, "the model was reloaded between sentences"
    tts.close()


def test_tts_synthesize_and_save_to_file():
    from voice.text_to_speech import TextToSpeech
    tts = TextToSpeech(preload=True)
    result = tts.synthesize("Findings ready.")
    assert result is not None
    audio, rate = result
    assert len(audio) > 0 and rate == 24000
    out = WORK / "spoken.wav"
    assert tts.save_to_file("Findings ready.", str(out)) is True
    assert out.is_file() and out.stat().st_size > 44   # bigger than a bare header
    tts.close()


def test_tts_reports_missing_model_instead_of_crashing():
    from voice.text_to_speech import TextToSpeech
    os.environ["MEDHA_TTS_MODEL_DIR"] = str(WORK / "nowhere")
    try:
        tts = TextToSpeech()
        assert tts.is_available is False
        assert "setup_models.py" in (tts.last_error or "")
        tts.speak("this should be silently skipped")   # must not raise
    finally:
        os.environ.pop("MEDHA_TTS_MODEL_DIR", None)


def test_config_ini_voice_section_is_honoured():
    from voice.offline import voice_config, voice_enabled, preload_models
    config = voice_config(refresh=True)
    # whatever config.ini says is what is used ("auto" = the device and
    # precision STEP 1 : INSTALL proved on this machine)
    import configparser
    ini = configparser.ConfigParser()
    ini.read(Path(__file__).resolve().parent.parent / "config.ini", encoding="utf-8")
    assert config["stt_compute_type"] == ini.get("voice", "stt_compute_type").strip()
    assert voice_enabled() is True
    assert isinstance(preload_models(), bool)


# ------------------------------------------- voice -> ZEUS -> memory ----

def _assistant():
    from main import MedhaAssistant
    return MedhaAssistant(use_ui=False, use_voice=True)


def test_voice_input_goes_through_the_normal_medha_pipeline():
    """The core integration requirement: speech becomes ordinary input."""
    _TRANSCRIPTS[0] = "what is the time"
    _PLAYED.clear()
    medha = _assistant()
    assert medha.use_voice is True
    from voice.providers import VoiceState
    assert medha.voice.state is VoiceState.AVAILABLE, medha.voice.detail
    assert medha.voice.can_listen and medha.voice.can_speak

    result = medha.listen_and_process()
    assert result["transcript"] == "what is the time"
    assert result["response"], "the normal command pipeline produced no response"
    # The text answer comes back immediately; speech is queued behind it, so
    # wait for the queue before asserting that it was spoken.
    medha.voice.wait_until_spoken()
    assert _PLAYED, "the response was not spoken"
    medha.close_voice()


def test_spoken_and_typed_input_share_one_memory():
    """Phase 19 demo, executed for real: speak a preference, then read it
    back after a restart - through the SAME memory system as typed input."""
    from memory import database

    _TRANSCRIPTS[0] = "remember that I prefer concise industrial reports"
    medha = _assistant()
    spoken = medha.listen_and_process()
    assert spoken["transcript"].startswith("remember that")
    medha.close_voice()

    facts = database.get_all_facts()
    assert any("concise industrial reports" in v for v in facts.values()), facts

    # Restart: a brand new assistant instance must still see the preference,
    # and must see it via the ordinary typed path too.
    restarted = _assistant()
    typed = restarted.handle_text("what do you remember")
    assert "concise industrial reports" in typed["response"].lower()
    restarted.close_voice()


def test_spoken_memory_instruction_is_confirmed_without_a_model_call():
    """A spoken preference must be stored AND confirmed even with Ollama
    down - that is what makes the offline demo reliable."""
    _TRANSCRIPTS[0] = "remember that I prefer concise industrial reports"
    medha = _assistant()
    result = medha.listen_and_process()
    assert "concise industrial reports" in result["response"]
    assert "ollama" not in result["response"].lower(), result["response"]
    medha.close_voice()


def test_forgetting_a_fact_works_from_voice_too():
    from memory import database
    medha = _assistant()
    medha.handle_text("remember that the spare seal is in store B")
    assert any("store B" in v for v in database.get_all_facts().values())
    _TRANSCRIPTS[0] = "forget about the spare seal"
    medha.listen_and_process()
    assert not any("store B" in v for v in database.get_all_facts().values() if v)
    medha.close_voice()


def test_voice_history_is_the_same_conversation_history():
    from memory import database
    _TRANSCRIPTS[0] = "what is the time"
    medha = _assistant()
    medha.handle_text("typed question about the plant")
    medha.listen_and_process()
    medha.close_voice()

    recent = [content for _role, content in database.get_recent_messages(20)]
    assert "typed question about the plant" in recent
    assert "what is the time" in recent, "spoken turns are missing from history"


def test_voice_does_not_create_a_separate_memory_store():
    project = Path(__file__).resolve().parent.parent
    for forbidden in ("voice/voice_memory.py", "voice/voice_history.py",
                      "voice/voice_database.py", "voice/memory.py"):
        assert not (project / forbidden).exists(), f"duplicate memory system: {forbidden}"


def test_voice_reaches_the_industrial_layer_through_the_same_engine():
    """Spoken input must be able to drive the industrial tools - no separate
    voice-only command architecture. Run for real, without Ollama: the
    taxonomy tool is deterministic."""
    from core.tool_registry import registry
    assert registry.get("industrial_term") is not None
    assert registry.get("ask_industrial_kb") is not None

    _TRANSCRIPTS[0] = "industrial term FCC"
    medha = _assistant()
    result = medha.listen_and_process()
    assert result["transcript"] == "industrial term FCC"
    assert "fluid catalytic cracking" in result["response"].lower(), result["response"]
    medha.close_voice()


def test_spoken_industrial_commands_route_the_same_as_typed_ones():
    from core.decision_engine import DecisionEngine
    engine = DecisionEngine()
    for spoken, expected_tool in [
        ("industrial status", "industrial_status"),
        ("analyze the report D:/Reports/INSP-2024-118.pdf", "ingest_industrial_document"),
        ("ask my industrial knowledge base about corrosion under insulation",
         "ask_industrial_kb"),
        ("list industrial agents", "list_industrial_agents"),
    ]:
        matched = engine._match_direct_command(spoken)
        assert matched is not None, f"'{spoken}' did not route anywhere"
        assert matched[0] == expected_tool, f"'{spoken}' -> {matched[0]}"


def test_route_arguments_keep_their_original_case():
    """A spoken or typed path must not be lowercased on its way to a tool."""
    from core.decision_engine import DecisionEngine
    engine = DecisionEngine()
    tool, args = engine._match_direct_command(
        "analyze the report D:/Reports/INSP-2024-118.pdf")
    assert args["path"] == "D:/Reports/INSP-2024-118.pdf"
    tool, args = engine._match_direct_command("index folder D:/College Notes")
    assert args["path"] == "D:/College Notes"


def test_voice_report_exposes_the_runtime_state():
    medha = _assistant()
    report = medha.voice_report()
    assert report["external_voice_services"] == "DISABLED"
    assert report["downloads_at_runtime"] == "DISABLED"
    assert report["runtime"]["stt_available"] is True
    assert report["runtime"]["tts_available"] is True
    assert report["state"] == "available"
    # Every claim names the provider it came from, so "voice ready" can be
    # traced to something that actually loaded.
    assert report["input"]["provider"] == "whisper"
    assert report["output"]["provider"] == "chatterbox"
    assert report["text_mode"] is False
    medha.close_voice()


# ------------------------------------------------------- no cloud anywhere

def test_no_cloud_speech_library_is_imported():
    """Sovereignty check: nothing that sends audio off the machine.

    pyttsx3 is deliberately NOT in this list. It is a thin wrapper around the
    speech engine already built into the operating system - SAPI5 on Windows,
    NSSpeechSynthesizer on macOS, espeak on Linux - so it synthesises locally
    and downloads nothing. It is what lets a machine with no model weights
    still speak. `gtts`, by contrast, is Google's cloud endpoint and stays
    forbidden even though its name looks similar.
    """
    forbidden = {"speech_recognition", "azure", "boto3", "elevenlabs",
                 "gtts", "deepgram", "assemblyai"}
    leaked = forbidden & set(sys.modules)
    assert not leaked, f"cloud voice-service libraries imported: {leaked}"


def test_voice_sources_contain_no_cloud_endpoints():
    voice_dir = Path(__file__).resolve().parent.parent / "voice"
    banned = ("api.openai.com", "speech.googleapis", "cognitiveservices",
              "elevenlabs.io", "api.assemblyai", "deepgram.com")
    for path in voice_dir.glob("*.py"):
        text = path.read_text(encoding="utf-8").lower()
        for needle in banned:
            assert needle not in text, f"{path.name} references {needle}"


def test_runtime_never_calls_from_pretrained():
    """from_pretrained() downloads weights. Runtime code may only use
    from_local() / an explicit local directory.

    Tokenised so that documentation mentioning the name does not trip the
    check - only real code is examined.
    """
    import io
    import tokenize

    voice_dir = Path(__file__).resolve().parent.parent / "voice"
    for path in voice_dir.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        names = [tok.string for tok in tokenize.generate_tokens(io.StringIO(source).readline)
                 if tok.type == tokenize.NAME]
        assert "from_pretrained" not in names, f"{path.name} calls from_pretrained()"

    # ...and the engine that loads Chatterbox must use the local loader.
    engine_names = [tok.string for tok in tokenize.generate_tokens(
        io.StringIO((voice_dir / "chatterbox_engine.py").read_text(encoding="utf-8")).readline)
        if tok.type == tokenize.NAME]
    assert "from_local" in engine_names, "chatterbox_engine.py must load from_local()"


# ==========================================================================
# Voice repair: the failures that actually stopped voice from working.
# ==========================================================================

def test_microphone_that_refuses_16khz_still_works():
    """Root cause #1: many microphones only offer 44.1/48 kHz. Asking
    PortAudio for 16 kHz raises 'Invalid sample rate' and voice input looks
    dead. ZEUS must record at a supported rate and resample."""
    from voice.speech_to_text import SpeechToText
    _AUDIO["supported_rates"] = [48000, 44100]
    stt = SpeechToText(preload=True)
    assert stt.microphone_available is True
    assert stt.capture_samplerate == 48000, "did not fall back to a supported rate"
    text = stt.listen_once(duration=1.0)
    assert text, "capture produced nothing"
    assert _AUDIO["last_rec"]["samplerate"] == 48000
    stt.close()


def test_resampling_produces_16khz_audio_for_whisper():
    from voice.speech_to_text import _resample_to_16k, DEFAULT_SAMPLERATE
    import numpy as np
    one_second_at_48k = np.zeros(48000, dtype="float32")
    resampled = _resample_to_16k(one_second_at_48k, 48000)
    assert len(resampled) == DEFAULT_SAMPLERATE
    unchanged = _resample_to_16k(np.zeros(16000, dtype="float32"), 16000)
    assert len(unchanged) == 16000


def test_silence_is_not_transcribed():
    """Root cause #2: Whisper hallucinates text from silence ('Thank you.'),
    which then gets executed as a command."""
    from voice.speech_to_text import SpeechToText
    _AUDIO["supported_rates"] = [16000, 48000]
    _AUDIO["recorded_amplitude"] = 0.0
    try:
        stt = SpeechToText(preload=True)
        assert stt.listen_once(duration=1.0) == ""
        assert stt.last_error == "silence"
        stt.close()
    finally:
        _AUDIO["recorded_amplitude"] = 0.2


def test_missing_microphone_is_reported_not_crashed():
    from voice.speech_to_text import SpeechToText
    saved = _AUDIO["devices"]
    _AUDIO["devices"] = [{"name": "Speakers only", "max_input_channels": 0,
                          "max_output_channels": 2, "default_samplerate": 48000.0}]
    try:
        stt = SpeechToText(preload=True)
        assert stt.microphone_available is False
        assert stt.listen_once(duration=1.0) == ""      # no exception
        assert "microphone" in (stt.last_error or "").lower()
        stt.close()
    finally:
        _AUDIO["devices"] = saved


def test_input_device_can_be_selected_by_name_or_index():
    from voice.speech_to_text import list_input_devices, default_input_device
    devices = list_input_devices()
    assert devices and devices[0]["name"] == "Stub Microphone"
    assert default_input_device()["index"] == 0


def test_capture_errors_carry_an_actionable_hint():
    from voice.speech_to_text import SpeechToText
    _AUDIO["supported_rates"] = [16000]
    stt = SpeechToText(preload=True)
    _AUDIO["supported_rates"] = []      # device disappears between calls
    assert stt.listen_once(duration=1.0) == ""
    assert "sample rate" in (stt.last_error or "").lower()
    _AUDIO["supported_rates"] = [48000, 44100]
    stt.close()


def test_spoken_reply_does_not_delay_the_written_one():
    """Root cause #3: speak() blocked the pipeline, so the text answer only
    appeared in the UI after playback finished."""
    import time
    from voice.text_to_speech import TextToSpeech

    slow = {"calls": 0}
    original = _FakeChatterboxTTS.generate

    def slow_generate(self, text, **kwargs):
        slow["calls"] += 1
        time.sleep(0.4)
        return original(self, text, **kwargs)

    _FakeChatterboxTTS.generate = slow_generate
    try:
        # warmup=False: this test counts generations, and the startup warm-up
        # is a real extra one.
        tts = TextToSpeech(preload=True, warmup=False)
        started = time.perf_counter()
        tts.speak_async("A long industrial answer that takes time to speak.")
        elapsed = time.perf_counter() - started
        assert elapsed < 0.2, f"speak_async blocked for {elapsed:.2f}s"
        tts.wait_until_spoken()
        assert slow["calls"] == 1
        assert _PLAYED, "the queued speech never played"
        tts.close()
    finally:
        _FakeChatterboxTTS.generate = original


def test_queued_speech_is_spoken_in_order_and_never_overlaps():
    from voice.text_to_speech import TextToSpeech
    spoken = []
    original = _FakeChatterboxTTS.generate

    def recording_generate(self, text, **kwargs):
        spoken.append(text)
        return original(self, text, **kwargs)

    _FakeChatterboxTTS.generate = recording_generate
    try:
        tts = TextToSpeech(preload=True, warmup=False)
        for line in ("first", "second", "third"):
            tts.speak_async(line)
        tts.wait_until_spoken()
        assert spoken == ["first", "second", "third"]
        tts.close()
    finally:
        _FakeChatterboxTTS.generate = original


def test_torch_weights_only_failure_is_recovered():
    """Root cause #4: torch >= 2.6 defaults torch.load(weights_only=True),
    which refuses Chatterbox's pickled conds.pt. The first load fails; ZEUS
    must recognise that exact error and retry for its own local file."""
    from voice.chatterbox_engine import ChatterboxEngine, _is_weights_only_error

    error = Exception("Weights only load failed. Re-running torch.load with "
                      "weights_only set to False will likely succeed")
    assert _is_weights_only_error(error) is True
    assert _is_weights_only_error(ValueError("some other problem")) is False

    attempts = {"n": 0}
    original = _FakeChatterboxTTS.from_local

    @classmethod
    def failing_once(cls, model_dir, device):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise Exception("_pickle.UnpicklingError: Weights only load failed")
        return original.__func__(cls, model_dir, device)

    _FakeChatterboxTTS.from_local = failing_once
    try:
        engine = ChatterboxEngine()
        assert attempts["n"] == 2, "the retry never happened"
        assert engine._model is not None
        engine.close()
    finally:
        _FakeChatterboxTTS.from_local = original


def test_voice_doctor_runs_and_reports_findings():
    """The diagnostic itself must never crash - it is what the user runs
    when nothing else works."""
    import subprocess
    project = Path(__file__).resolve().parent.parent
    result = subprocess.run([sys.executable, "scripts/voice_doctor.py"],
                            cwd=project, capture_output=True, text=True, timeout=180)
    assert "ZEUS VOICE DOCTOR" in result.stdout
    assert "9 Offline" in result.stdout
    assert result.returncode in (0, 1)      # 1 = problems found, still a clean run


# ----------------------------------------------- setup path (voice) ------

def _setup_voice(name: str):
    import importlib.util
    path = Path(__file__).resolve().parent.parent / "scripts" / "setup_voice.py"
    assert path.exists(), "scripts/setup_voice.py is missing"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_setup_voice_script_exists_and_parses():
    """The one-command setup must at least be importable and offer --check."""
    module = _setup_voice("setup_voice_under_test")
    for name in ("environment", "install_listening", "download_whisper",
                 "install_speaking", "verify", "microphone", "report_check",
                 "main"):
        assert hasattr(module, name), f"setup_voice.{name} missing"


def test_setup_voice_installs_chatterbox_outside_medhas_environment():
    """chatterbox-tts pins torch==2.6.0, transformers==5.2.0 and numpy<2.0.0.
    ZEUS needs numpy 2.x for the analytics and document builders, so putting
    both in one environment means one of them loses - and which one is decided
    by whatever pip resolved last, not by anybody's judgement.

    So the installer puts Chatterbox in a SEPARATE environment. Whisper, which
    pins nothing aggressively, stays in ZEUS's own. This test is the guard on
    that split: if torch ever appears in the list installed into the running
    interpreter, the conflict is back.
    """
    module = _setup_voice("setup_voice_split")
    into_medha = " ".join(package for group in module.STT_PACKAGES
                          for package in group["packages"]).lower()
    for banned in ("torch", "chatterbox", "transformers", "diffusers"):
        assert banned not in into_medha, (
            f"{banned} would be installed into ZEUS's own environment")
    assert "faster-whisper" in into_medha, "the listening stack is missing"
    assert "sounddevice" in into_medha, "there is no audio I/O"


def test_setup_voice_does_not_install_a_web_framework():
    """chatterbox-tts depends on gradio, which drags in FastAPI, Starlette and
    uvicorn. A sovereign offline workbench must not end up with a web server
    sitting in its environment - and ZEUS's environment never sees any of it
    now, because Chatterbox is installed somewhere else entirely."""
    module = _setup_voice("setup_voice_deps")
    everything = " ".join(package for group in module.STT_PACKAGES
                          for package in group["packages"]).lower()
    for banned in ("gradio", "fastapi", "uvicorn", "starlette", "flask"):
        assert banned not in everything, f"the installer would pull in {banned}"


def test_setup_voice_delegates_the_heavy_stage():
    """The venv logic, the torch pin and the model URLs live in exactly one
    place each. Two copies means two places to fix a bug, and one of them
    always gets missed."""
    module = _setup_voice("setup_voice_delegation")
    assert module.ENGINE_SCRIPT.name == "setup_voice_engine.py"
    assert module.ENGINE_SCRIPT.is_file(), "the engine installer is missing"

    source = module.ENGINE_SCRIPT.read_text(encoding="utf-8")
    assert "ResembleAI/chatterbox" in source, (
        "the engine installer no longer owns the model repository")


def test_setup_voice_check_mode_installs_nothing():
    """--check is what a nervous operator runs first, and what the .bat file
    runs before asking whether to proceed. It must not pip-install, and it
    must not start a multi-gigabyte download."""
    module = _setup_voice("setup_voice_check")
    source = module.ENGINE_SCRIPT.parent.joinpath("setup_voice.py").read_text(
        encoding="utf-8")
    main = source[source.index("def main("):]
    assert main.index("args.check") < main.index("install_listening("), (
        "--check is handled after the install has already begun")


def test_setup_scripts_are_the_only_ones_allowed_to_use_the_network():
    """A guard on the offline promise: no voice runtime module may call
    allow_network(); only the two setup scripts may."""
    root = Path(__file__).resolve().parent.parent
    for module in (root / "voice").glob("*.py"):
        text = module.read_text(encoding="utf-8")
        if module.name == "offline.py":
            continue          # defines it
        assert "allow_network()" not in text, f"{module.name} calls allow_network()"
    allowed = {"setup_models.py", "setup_voice.py"}
    for script in (root / "scripts").glob("*.py"):
        text = script.read_text(encoding="utf-8")
        if "allow_network()" in text:
            assert script.name in allowed, f"{script.name} must not enable the network"
