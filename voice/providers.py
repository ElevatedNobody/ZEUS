"""
Voice providers: what ZEUS can actually hear and say on THIS machine.

The problem this replaces
------------------------
Voice used to mean exactly two engines - faster-whisper for input and
Chatterbox for output - and both need large model weights plus a ~2 GB torch
install. A machine without them had no voice at all, the startup banner still
printed a state that had never been verified, and any library left to its own
devices would try to fetch weights from the network on first use.

Three rules follow from that, and this module exists to enforce them:

  1. NOTHING is downloaded, ever, at runtime. A provider whose weights are
     absent reports MODEL MISSING and stops. `voice.offline.enforce_offline()`
     is called before any ML library is imported, as a second line of defence.

  2. NO status is claimed without a check. `probe()` looks at the filesystem
     and the installed modules; `activate()` performs the real load. A provider
     only reports AVAILABLE once a real call has succeeded, and the reason for
     anything less is carried in the report.

  3. NO voice is a valid state. If nothing can speak and nothing can listen,
     ZEUS runs in TEXT MODE. That is a supported configuration, not an error,
     and it must never stop the application from starting.

Providers are tried in cost order - cheapest first - so a laptop with nothing
installed still gets spoken output from the voice already built into its
operating system, and only opts into a heavy neural engine if its weights are
already sitting on disk.

    output (TTS)                  needs                       download
    ----------------------------------------------------------------------
    system      OS speech engine  pyttsx3 (a small wrapper)    none
    piper       piper + a .onnx   local binary + local voice   none at runtime
    chatterbox  neural, expressive torch + local checkpoints   none at runtime

    input (STT)                   needs                       download
    ----------------------------------------------------------------------
    whisper     faster-whisper    a complete local model dir   none at runtime
    vosk        vosk              a local model dir            none at runtime

There is no zero-install option for input: every speech recogniser needs
weights. So on a fresh machine output works and input does not, which is
DEGRADED - and reported as exactly that rather than as READY or as a failure.
"""

from __future__ import annotations

import platform
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.logger import get_logger

from .offline import (
    CHATTERBOX_REQUIRED_FILES,
    WHISPER_REQUIRED_FILES,
    config_bool,
    config_float,
    config_str,
    enforce_offline,
    missing_files,
    resolve_chatterbox_dir,
    resolve_models_dir,
    resolve_whisper_dir,
    voice_enabled,
)

logger = get_logger("voice.providers")


class VoiceState(str, Enum):
    """The five states the voice subsystem can be in."""

    INITIALIZING = "initializing"   # a probe or load is in flight
    AVAILABLE = "available"         # input and output both verified working
    DEGRADED = "degraded"           # one direction works, the other does not
    DISABLED = "disabled"           # switched off, or nothing installed: TEXT MODE
    ERROR = "error"                 # something explicitly configured failed to load


class ProviderState(str, Enum):
    """The state of one provider, which is narrower than the subsystem's."""

    READY = "ready"                 # loaded, and a real call succeeded
    INSTALLABLE = "installable"     # supported here, but its library is absent
    MODEL_MISSING = "model_missing"  # library present, weights are not
    UNSUPPORTED = "unsupported"     # cannot work on this platform at all
    FAILED = "failed"               # tried to load and could not
    NOT_PROBED = "not_probed"


@dataclass
class ProviderReport:
    name: str
    direction: str                  # "input" or "output"
    state: ProviderState = ProviderState.NOT_PROBED
    detail: str = ""
    requires_download: bool = False
    evidence: Dict[str, Any] = field(default_factory=dict)

    @property
    def ready(self) -> bool:
        return self.state is ProviderState.READY

    def as_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "direction": self.direction,
                "state": self.state.value, "detail": self.detail,
                "requires_download": self.requires_download,
                "evidence": dict(self.evidence)}


def _installed(module: str) -> bool:
    """Is a module importable? Checked by spec, so nothing is imported and
    no library gets the chance to reach for the network."""
    import importlib.util
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        return False


# ============================================================== output ====

class SpeechState:
    """What the voice is doing RIGHT NOW, as opposed to whether it works.

    ProviderState answers "can this speak at all"; this answers "is it
    speaking at this instant". The UI needs both: a provider can be READY and
    idle, READY and mid-sentence, or READY and paused, and those look
    different to the person watching.

    GENERATING is separate from SPEAKING on purpose. On a CPU the gap between
    asking for speech and hearing it is measured in seconds, and a status that
    sat on IDLE through all of it would look like nothing had happened.
    """

    IDLE = "IDLE"
    GENERATING = "GENERATING"
    SPEAKING = "SPEAKING"
    PAUSED = "PAUSED"
    STOPPED = "STOPPED"

    ALL = (IDLE, GENERATING, SPEAKING, PAUSED, STOPPED)


class OutputProvider:
    """Base class for anything that can turn text into sound."""

    name = "abstract"
    direction = "output"
    requires_download = False

    def probe(self) -> ProviderReport:
        raise NotImplementedError

    def activate(self) -> ProviderReport:
        """Load for real. Only called when this provider has been chosen."""
        raise NotImplementedError

    def speak(self, text: str) -> bool:
        raise NotImplementedError

    def stop(self):
        pass

    def close(self):
        pass

    # -- live status -------------------------------------------------------
    # Providers that cannot report progress inherit these, so a caller can ask
    # any provider the same question and get an honest answer rather than
    # having to know which class it holds.

    _speech_state: str = SpeechState.IDLE
    _state_listener = None

    def speech_state(self) -> str:
        return getattr(self, "_speech_state", SpeechState.IDLE)

    def on_state_change(self, callback) -> None:
        """Register one callback, called with (provider_name, state).

        Used by the UI. It must not raise and must not block - it is invoked
        on the speaking thread, and an exception there would abort speech
        mid-sentence.
        """
        self._state_listener = callback

    def _set_state(self, state: str) -> None:
        if getattr(self, "_speech_state", None) == state:
            return
        self._speech_state = state
        listener = getattr(self, "_state_listener", None)
        if listener is None:
            return
        try:
            listener(self.name, state)
        except Exception as error:
            logger.debug(f"voice state listener raised: {error}")

    def pause(self) -> bool:
        """Pause mid-utterance. False means this provider cannot."""
        return False

    def resume(self) -> bool:
        return False

    def is_speaking(self) -> bool:
        return self.speech_state() in (SpeechState.GENERATING, SpeechState.SPEAKING)


class SystemVoiceProvider(OutputProvider):
    """The speech engine already built into the operating system.

    SAPI5 on Windows, NSSpeechSynthesizer on macOS, espeak on Linux - reached
    through pyttsx3, which is a thin wrapper of a few hundred kilobytes and
    downloads nothing. This is what makes spoken output work on a machine that
    has never run a setup script, and it is why voice being unavailable is now
    rare rather than the default.
    """

    name = "system"
    requires_download = False

    def __init__(self, rate: int = 175, volume: float = 1.0):
        self._engine = None
        self._rate = rate
        self._volume = max(0.0, min(float(volume), 1.0))
        self._lock = threading.Lock()

    @staticmethod
    def _playback_supported() -> tuple:
        """Can this machine actually turn synthesised speech into sound?

        Synthesis and playback are separate steps, and on Linux they are
        separate programs: pyttsx3's espeak driver writes a wav and plays it
        with `aplay` through os.system, which returns a status nobody checks.
        So on a box without ALSA, speak() completed "successfully" in silence.
        Reporting a working voice on that machine is precisely the kind of
        unverified claim this subsystem is not allowed to make.

        Windows and macOS synthesise and play inside the OS speech API, so
        the driver initialising IS the check there.
        """
        system = platform.system()
        if system in ("Windows", "Darwin"):
            return True, ""
        import shutil
        for player in ("aplay", "paplay", "pw-play"):
            if shutil.which(player):
                return True, player
        return False, ("espeak can synthesise here but there is no audio player "
                       "(aplay/paplay/pw-play) to play it through")

    def probe(self) -> ProviderReport:
        report = ProviderReport(self.name, self.direction)
        if not _installed("pyttsx3"):
            report.state = ProviderState.INSTALLABLE
            report.detail = ("the operating system's own voice is available "
                             "through pyttsx3 - install it with: pip install pyttsx3")
            return report
        playable, why = self._playback_supported()
        if not playable:
            report.state = ProviderState.UNSUPPORTED
            report.detail = why
            return report
        # Installed and playable, but "the driver answers" is only knowable by
        # asking it, which activate() does. Probing stops here, not guesses.
        report.state = ProviderState.NOT_PROBED
        report.detail = "pyttsx3 is installed; the OS voice has not been opened yet"
        return report

    def activate(self) -> ProviderReport:
        report = ProviderReport(self.name, self.direction)
        started = time.time()
        playable, why = self._playback_supported()
        if not playable:
            report.state = ProviderState.UNSUPPORTED
            report.detail = why
            return report
        try:
            import pyttsx3
        except Exception as error:
            report.state = ProviderState.INSTALLABLE
            report.detail = f"pyttsx3 could not be imported: {error}"
            return report

        try:
            with self._lock:
                engine = pyttsx3.init()
                engine.setProperty("rate", self._rate)
                engine.setProperty("volume", self._volume)
                # A real query of the driver: if the platform has no usable
                # speech engine, this is where it fails, not silently later.
                voices = engine.getProperty("voices") or []
                self._engine = engine
            if not voices:
                report.state = ProviderState.UNSUPPORTED
                report.detail = ("the system speech engine started but has no "
                                 "installed voices")
                return report
            report.state = ProviderState.READY
            report.detail = (f"the {platform.system() or 'system'} speech engine "
                             f"responded with {len(voices)} installed "
                             f"voice{'s' if len(voices) != 1 else ''}")
            report.evidence = {"voices": len(voices), "playback": why or "os",
                               "load_ms": int((time.time() - started) * 1000)}
        except Exception as error:
            report.state = ProviderState.FAILED
            report.detail = f"{type(error).__name__}: {error}"
        return report

    def speak(self, text: str) -> bool:
        if not self._engine or not text:
            return False
        try:
            with self._lock:
                self._engine.say(text)
                self._engine.runAndWait()
            return True
        except Exception as error:
            logger.warning(f"system voice failed to speak: {error}")
            return False

    def stop(self):
        if self._engine:
            try:
                self._engine.stop()
            except Exception:
                pass

    def close(self):
        self.stop()
        self._engine = None


class PiperVoiceProvider(OutputProvider):
    """Piper: small neural TTS, better than the system voice, ~60 MB a voice.

    Used only when the binary and a voice file are ALREADY on disk. Nothing
    here fetches either one.
    """

    name = "piper"
    requires_download = False

    def __init__(self):
        self._voice_path: Optional[Path] = None

    def _find_voice(self) -> Optional[Path]:
        configured = config_str("piper_voice", "")
        if configured:
            candidate = Path(configured)
            return candidate if candidate.is_file() else None
        piper_dir = resolve_models_dir() / "piper"
        if not piper_dir.is_dir():
            return None
        voices = sorted(piper_dir.glob("*.onnx"))
        return voices[0] if voices else None

    def probe(self) -> ProviderReport:
        report = ProviderReport(self.name, self.direction)
        if not _installed("piper"):
            report.state = ProviderState.INSTALLABLE
            report.detail = "piper-tts is not installed"
            return report
        voice = self._find_voice()
        if not voice:
            report.state = ProviderState.MODEL_MISSING
            report.detail = (f"no piper voice (*.onnx) found in "
                             f"{resolve_models_dir() / 'piper'}")
            return report
        self._voice_path = voice
        report.state = ProviderState.NOT_PROBED
        report.detail = f"piper voice present: {voice.name}"
        return report

    def activate(self) -> ProviderReport:
        report = ProviderReport(self.name, self.direction)
        probe = self.probe()
        if probe.state in (ProviderState.INSTALLABLE, ProviderState.MODEL_MISSING):
            return probe
        try:
            enforce_offline()
            from piper import PiperVoice           # noqa: F401  (import is the check)
            self._voice = PiperVoice.load(str(self._voice_path))
            report.state = ProviderState.READY
            report.detail = f"piper loaded {self._voice_path.name}"
            report.evidence = {"voice_file": str(self._voice_path)}
        except Exception as error:
            report.state = ProviderState.FAILED
            report.detail = f"{type(error).__name__}: {error}"
        return report

    def speak(self, text: str) -> bool:
        voice = getattr(self, "_voice", None)
        if not voice or not text:
            return False
        try:
            import io
            import wave

            import numpy as np
            import sounddevice as sd

            buffer = io.BytesIO()
            with wave.open(buffer, "wb") as handle:
                voice.synthesize(text, handle)
            buffer.seek(0)
            with wave.open(buffer, "rb") as handle:
                rate = handle.getframerate()
                frames = handle.readframes(handle.getnframes())
            audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
            sd.play(audio, rate)
            sd.wait()
            return True
        except Exception as error:
            logger.warning(f"piper failed to speak: {error}")
            return False


class ChatterboxVoiceProvider(OutputProvider):
    """In-process Chatterbox, for an environment that already has it.

    Superseded as the default by ChatterboxBridgeProvider, which runs the same
    model in an isolated environment. Kept because an operator who already had
    chatterbox-tts working in the main environment should not have it taken
    away; reachable with `tts_provider = chatterbox-inprocess`.
    """
    """The existing expressive neural voice. Kept, but demoted.

    It is the best-sounding option and it stays supported - it is simply no
    longer the thing that decides whether ZEUS has a voice at all. It is
    selected only when torch and its checkpoints are already present, and it
    never downloads them.
    """

    name = "chatterbox"
    requires_download = False

    def __init__(self):
        self._tts = None

    def probe(self) -> ProviderReport:
        report = ProviderReport(self.name, self.direction)
        if not (_installed("torch") and _installed("chatterbox")):
            report.state = ProviderState.INSTALLABLE
            report.detail = ("chatterbox-tts and torch are not installed "
                             "(about 2 GB - optional)")
            return report
        directory = resolve_chatterbox_dir()
        absent = missing_files(directory, CHATTERBOX_REQUIRED_FILES)
        if absent:
            report.state = ProviderState.MODEL_MISSING
            report.detail = (f"checkpoints missing from {directory}: "
                             f"{', '.join(absent)} - run scripts/setup_models.py "
                             f"once, on a machine with a network")
            report.evidence = {"missing": absent, "directory": str(directory)}
            return report
        report.state = ProviderState.NOT_PROBED
        report.detail = f"checkpoints present in {directory}"
        return report

    def activate(self) -> ProviderReport:
        report = ProviderReport(self.name, self.direction)
        probe = self.probe()
        if probe.state in (ProviderState.INSTALLABLE, ProviderState.MODEL_MISSING):
            return probe

        # Chatterbox holds about 3.6 GB. On a 16 GB laptop that fits next to a
        # 7B chat model and the UI - but only just, and a machine that starts
        # paging does not run slowly, it stops responding. So check first and
        # say so, rather than finding out by taking the machine down.
        from . import memory_guard
        verdict = memory_guard.check("chatterbox")
        if not verdict.ok:
            freed, detail = memory_guard.free_the_chat_model()
            if freed:
                verdict = memory_guard.check("chatterbox")
            if not verdict.ok:
                report.state = ProviderState.UNSUPPORTED
                report.detail = verdict.detail
                report.evidence = {"available_gb": round(verdict.available_gb, 1),
                                   "needed_gb": verdict.needed_gb,
                                   "tried_unloading_chat_model": freed}
                return report

        started = time.time()
        try:
            enforce_offline()                    # before torch/HF are imported
            from .text_to_speech import TextToSpeech
            engine = TextToSpeech()
            if not engine.is_available:
                report.state = ProviderState.FAILED
                report.detail = engine.last_error or "chatterbox did not become available"
                return report
            self._tts = engine
            report.state = ProviderState.READY
            report.detail = "chatterbox loaded from local checkpoints"
            report.evidence = {"load_ms": int((time.time() - started) * 1000)}
        except Exception as error:
            report.state = ProviderState.FAILED
            report.detail = f"{type(error).__name__}: {error}"
        return report

    def speak(self, text: str) -> bool:
        if not self._tts:
            return False
        try:
            self._tts.speak(text)
            return True
        except Exception as error:
            logger.warning(f"chatterbox failed to speak: {error}")
            return False

    def stop(self):
        if self._tts:
            try:
                self._tts.stop()
            except Exception:
                pass

    def close(self):
        if self._tts:
            try:
                self._tts.close()
            except Exception:
                pass
        self._tts = None


class ChatterboxBridgeProvider(OutputProvider):
    """Chatterbox, running in its own Python environment.

    The dependency problem, stated plainly: chatterbox-tts pins torch 2.6.0,
    transformers 5.2.0, diffusers 0.29.0 and gradio 6.8.0 as hard equalities.
    Installing those into ZEUS's own environment would drag half its
    dependency tree along with them, so this provider talks to a SEPARATE
    interpreter over a pipe and neither side constrains the other.

    Chatterbox is the primary voice because it is the one that works. Chatterbox
    was tried first and removed: it declares `torchcodec`, which loads FFmpeg
    shared libraries at import time, and Windows ships no FFmpeg. Chatterbox's
    torch 2.6 pin sits below the release where torchaudio started delegating
    to torchcodec, so that whole failure mode is absent here.

    Nothing here downloads anything. If the environment or the weights are
    absent this reports MODEL MISSING with the one command that fixes it, and
    ZEUS carries on in text mode.
    """

    name = "chatterbox"
    requires_download = False

    def __init__(self):
        self._python: Optional[Path] = None
        self._profile: dict = {}
        self._environment: dict = {}
        self._server = None                 # voice.chatterbox_server.ChatterboxServer
        self._server_lock = threading.Lock()
        self._watcher: Optional[threading.Thread] = None
        self._closed = threading.Event()
        self._lock = threading.Lock()
        self._playing = False
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._last_timing: dict = {}
        self.last_failure: dict = {}

    # -- where things live -------------------------------------------------

    @staticmethod
    def home() -> Path:
        """The dedicated Python environment, and nothing else.

        The WEIGHTS are not here. They live in resolve_chatterbox_dir() -
        models/chatterbox - which is where setup_models.py puts them and where
        the in-process provider reads them from. Two directories holding the
        same 3.2 GB is a bug that only shows up as a mysteriously full disk,
        so there is exactly one definition of that path and it is not here.
        """
        return resolve_models_dir() / "voice" / "chatterbox"

    @staticmethod
    def model_dir() -> Path:
        """Where the Chatterbox checkpoint is. Shared with every other reader."""
        return resolve_chatterbox_dir()

    @classmethod
    def env_python(cls) -> Optional[Path]:
        """The interpreter of the dedicated environment, if it exists."""
        base = cls.home() / "env"
        for candidate in (base / "Scripts" / "python.exe", base / "bin" / "python"):
            if candidate.is_file():
                return candidate
        return None

    @classmethod
    def profile_path(cls) -> Path:
        name = config_str("voice_profile", "medha") or "medha"
        return resolve_models_dir() / "voice" / "profiles" / name / "profile.json"

    def _load_profile(self) -> dict:
        path = self.profile_path()
        if not path.is_file():
            return {}
        try:
            import json
            profile = json.loads(path.read_text(encoding="utf-8"))
        except Exception as error:
            logger.warning(f"voice profile unreadable: {error}")
            return {}
        reference = profile.get("reference")
        if reference and not Path(reference).is_absolute():
            profile["reference"] = str(path.parent / reference)

        # config.ini wins over profile.json where they overlap. The profile
        # describes the VOICE - its recording, its language, its default
        # delivery. config.ini is where the operator tunes THIS installation.
        # Without this precedence the documented keys would be inert, which is
        # the kind of thing people only discover after an hour of changing a
        # value that does nothing.
        speed = config_float("voice_speed", None)
        if speed is not None:
            profile["speed"] = speed
        volume = config_float("tts_volume", None)
        if volume is not None:
            profile["volume"] = volume
        device = config_str("voice_device", "").strip().lower()
        if device:
            profile["device"] = device
        profile["polish"] = config_bool("voice_polish", True)
        cap = config_float("max_spoken_chars", None)
        if cap is not None:
            profile["max_spoken_chars"] = int(cap)

        # Chatterbox's own three dials. Documented in config.ini and left empty
        # there by default, which means "use the profile's value" - so an empty
        # key must not overwrite the profile with None.
        for config_key, profile_key in (("tts_exaggeration", "exaggeration"),
                                        ("tts_cfg_weight", "cfg_weight"),
                                        ("tts_temperature", "temperature")):
            value = config_float(config_key, None)
            if value is not None:
                profile[profile_key] = value
        return profile

    # -- probing -----------------------------------------------------------

    def probe(self) -> ProviderReport:
        """Filesystem only. Never launches the environment, never loads a
        model - a probe that costs seconds would slow every startup."""
        report = ProviderReport(self.name, self.direction)
        home = self.home()

        python = self.env_python()
        if python is None:
            report.state = ProviderState.INSTALLABLE
            report.detail = ("the Chatterbox environment is not installed - run "
                             "SETUP_VOICE.bat to install it")
            report.evidence = {"expected_at": str(home / "env")}
            return report

        model_dir = self.model_dir()
        absent = missing_files(model_dir, CHATTERBOX_REQUIRED_FILES)
        if absent:
            report.state = ProviderState.MODEL_MISSING
            report.detail = (f"the Chatterbox environment is installed but the model "
                             f"in {model_dir} is incomplete - missing "
                             f"{', '.join(absent)}. Run SETUP_VOICE.bat")
            report.evidence = {"model_dir": str(model_dir), "missing": absent}
            return report

        # A reference clip is OPTIONAL. Chatterbox ships a default speaker in
        # conds.pt, so it can speak with no recording at all - unlike a pure
        # cloning engine, where an absent clip means silence. Treating a
        # missing reference as MODEL_MISSING here would report a working
        # install as broken, which is the opposite of the point of this probe.
        profile = self._load_profile()
        reference = profile.get("reference", "")
        cloned = bool(reference and Path(reference).is_file())

        report.state = ProviderState.NOT_PROBED
        report.detail = (f"Chatterbox is installed with the "
                         f"'{profile.get('name', 'medha')}' voice "
                         f"({'reference clip' if cloned else 'built-in speaker'}); "
                         f"not yet loaded")
        report.evidence = {"python": str(python), "model_dir": str(model_dir),
                           "reference": reference if cloned else ""}
        return report

    def activate(self) -> ProviderReport:
        """Ask the environment what it actually is, and synthesise nothing yet.

        The probe below is a real subprocess call into the Chatterbox environment, so
        a broken install, a missing torch or an unimportable chatterbox surfaces
        HERE with the reason, rather than as silence when the user first
        speaks.
        """
        report = ProviderReport(self.name, self.direction)
        probed = self.probe()
        if probed.state in (ProviderState.INSTALLABLE, ProviderState.MODEL_MISSING):
            return probed

        python = self.env_python()
        started = time.time()
        try:
            import json
            import subprocess
            result = subprocess.run(
                [str(python), str(Path(__file__).resolve().parent / "chatterbox_bridge.py"),
                 "--probe"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=120,
                cwd=str(Path(__file__).resolve().parent.parent))
            answer = json.loads(result.stdout.strip() or "{}")
        except subprocess.TimeoutExpired:
            report.state = ProviderState.FAILED
            report.detail = ("the Chatterbox environment did not respond within two "
                             "minutes - it may be importing torch for the first "
                             "time; try again, then run SETUP_VOICE.bat")
            return report
        except Exception as error:
            report.state = ProviderState.FAILED
            report.detail = f"could not start the Chatterbox environment: {error}"
            return report

        environment = (answer or {}).get("environment") or {}
        if not answer.get("ok") or not environment.get("chatterbox"):
            report.state = ProviderState.FAILED
            report.detail = (f"the Chatterbox environment exists but chatterbox will not "
                             f"import: {environment.get('chatterbox_error', 'unknown')}")
            report.evidence = environment
            return report

        self._python = python
        self._profile = self._load_profile()
        self._environment = environment

        device = environment.get("device", "cpu")
        gpu = environment.get("gpu_name")
        # The status says GPU only when torch itself reports a usable CUDA
        # device. Claiming acceleration that is not there would make every
        # later timing figure a lie.
        acceleration = (f"GPU ACCELERATED ({gpu}, {environment.get('vram_gb')} GB)"
                        if device == "cuda" and gpu else
                        "Apple GPU" if device == "mps" else "CPU MODE")
        report.state = ProviderState.READY
        report.detail = (f"Chatterbox ready, {acceleration}, torch "
                         f"{environment.get('torch')}, voice "
                         f"'{self._profile.get('name', 'medha')}'")
        report.evidence = {**environment,
                           "load_ms": int((time.time() - started) * 1000),
                           "acceleration": acceleration,
                           "keep_loaded": self._keep_loaded()}
        self._maybe_warm_up()
        return report

    # -- keeping the model loaded -----------------------------------------

    def _device(self) -> str:
        """The device the bridge will actually use: an explicit
        [voice] voice_device wins, otherwise what torch reported in activate()."""
        requested = str((self._profile or {}).get("device", "auto") or "auto").strip().lower()
        if requested not in ("", "auto"):
            return requested
        return str((self._environment or {}).get("device", "cpu") or "cpu")

    def _keep_loaded(self) -> bool:
        """[voice] tts_keep_loaded = auto | true | false.

        auto keeps one bridge loaded on a GPU, where loading is most of the
        cost of a sentence and the resident cost is small. On the CPU the
        resident bridge was MEASURED at 7.1 GB committed (9.1 GB while
        speaking; 2026-09-25) - more than this 15.4 GB laptop can hold next to
        a chat model - so there it still starts per sentence unless asked."""
        setting = (config_str("tts_keep_loaded", "auto") or "auto").strip().lower()
        if setting in ("1", "true", "yes", "on"):
            return True
        if setting in ("0", "false", "no", "off"):
            return False
        return self._device() == "cuda"

    @staticmethod
    def _idle_seconds() -> float:
        """[voice] tts_idle_seconds: release the loaded voice after this long
        without speaking (default 300 s, like Ollama's keep_alive)."""
        value = config_float("tts_idle_seconds", None)
        return 300.0 if value is None else max(30.0, float(value))

    @staticmethod
    def _server_log() -> Optional[Path]:
        try:
            from core.paths import log_dir
            return log_dir() / "chatterbox_bridge.log"
        except Exception:
            return None

    def _ensure_server(self):
        """The loaded bridge, started (after making room) if it is not."""
        from voice.chatterbox_server import ChatterboxServer
        with self._server_lock:
            server = self._server
            if server is not None and server.alive:
                return server
            if server is not None:
                server.stop()
            self._make_room_for_synthesis(self._load_need_gb())
            server = ChatterboxServer(self._python, self._server_log())
            server.start()
            self._server = server
            try:
                from core.model_lifecycle import register_releaser
                register_releaser("chatterbox-voice", self.release_if_idle)
            except Exception:
                pass
            self._start_watcher()
            logger.info(f"Chatterbox voice started (pid {server.pid}, {self._device()}); "
                        f"it stays loaded while you talk")
            return server

    def _stop_server(self, why: str) -> None:
        with self._server_lock:
            server, self._server = self._server, None
        if server is None:
            return
        server.stop()
        try:
            from core.model_lifecycle import unregister_releaser
            unregister_releaser("chatterbox-voice", self.release_if_idle)
        except Exception:
            pass
        logger.info(f"Chatterbox voice released ({why})")

    def release_if_idle(self) -> bool:
        """Called by core/model_lifecycle.py when a language model needs the
        memory: frees the loaded voice unless it is speaking right now."""
        server = self._server
        if server is None or not server.alive or server.busy or self._playing:
            return False
        self._stop_server("a language model needed the memory")
        return True

    def _start_watcher(self) -> None:
        """One thread per provider that releases the voice after
        tts_idle_seconds of silence, so a loaded model does not sit on memory
        all afternoon because somebody spoke once after lunch."""
        if self._watcher is not None and self._watcher.is_alive():
            return

        def watch():
            while not self._closed.wait(10.0):
                server = self._server
                if server is None or not server.alive or server.busy or self._playing:
                    continue
                idle = time.time() - server.last_used
                if idle > self._idle_seconds():
                    self._stop_server(f"idle for {int(idle)} s")

        self._watcher = threading.Thread(target=watch, name="chatterbox-idle", daemon=True)
        self._watcher.start()

    def _maybe_warm_up(self) -> None:
        """With [voice] preload = true, load the voice in the background right
        after it is chosen, so the first reply does not wait for the load."""
        try:
            from core.model_lifecycle import active_in_this_process
            if not active_in_this_process() or not self._keep_loaded():
                return
            if not config_bool("preload", True):
                return
        except Exception:
            return
        threading.Thread(target=self.warm_up, name="chatterbox-warm-up", daemon=True).start()

    def warm_up(self, speak_a_word: bool = True) -> dict:
        """Load the model in the bridge now (and compile the GPU kernels by
        saying one word into nothing). Returns the bridge's answer."""
        if self._python is None:
            return {"ok": False, "error": "not_active"}
        started = time.time()
        if self._server is None or not self._server.alive:
            # Loading into a machine without room is how the 2026-09-24
            # "paging file too small" crashes happened. At start-up, wait for
            # the first sentence instead (which makes room first) and say why.
            try:
                from core.sysmem import has_headroom
                self._make_room_for_synthesis(self._load_need_gb())
                ok, reason = has_headroom(self._load_need_gb(), margin_gb=0.5)
            except Exception:
                ok, reason = True, ""
            if not ok:
                logger.warning(f"Chatterbox voice not preloaded - {reason}. It will load when it "
                               f"first speaks; a larger Windows page file removes this wait "
                               f"(AGNI_REPAIR_CHANGELOG.md section 6)")
                return {"ok": False, "error": "insufficient_memory", "detail": reason}
        profile = self._profile or {}
        load_request = {"op": "load", "model_dir": str(self.model_dir()),
                        "device": profile.get("device", "auto"), "warm": bool(speak_a_word),
                        "reference": profile.get("reference", "")}
        if profile.get("exaggeration") is not None:
            load_request["exaggeration"] = float(profile["exaggeration"])
        try:
            server = self._ensure_server()
            answer = server.request(load_request, timeout=600)
        except Exception as error:
            logger.warning(f"Chatterbox warm-up failed: {error}")
            return {"ok": False, "error": "warm_up_failed", "detail": str(error)}
        if answer.get("ok"):
            logger.info(f"Chatterbox voice ready on {answer.get('device')} in "
                        f"{time.time() - started:.1f} s (load {answer.get('load_seconds')} s, "
                        f"first word {answer.get('warm_seconds')} s)")
        else:
            logger.warning(f"Chatterbox warm-up: {answer.get('error')} - {answer.get('detail')}")
        return answer

    # -- speaking ----------------------------------------------------------

    # Commit memory one synthesis needs. The weights are ~3 GB on disk, but
    # the bridge process was MEASURED at 8.4 GB private (committed) memory
    # while loading them on the CPU (2026-09-25, RTX 5060 laptop) - which is
    # why every synthesis failed with "paging file too small" on 2026-09-24
    # while a language model was resident.
    SYNTHESIS_NEED_GB = 8.5
    # The same on the GPU - MEASURED 2026-09-25 (torch 2.8.0+cu128, RTX 5060):
    # 1.8 GB torch + CUDA, +0.95 GB chatterbox's imports, +3.75 GB for the
    # model (on Windows, CUDA device memory is charged to the commit limit as
    # well), +1.5 GB at the first sentence = 8.0 GB committed, of which only
    # ~2.6 GB is ever resident. Commit, not RAM: a larger page file covers it.
    GPU_LOAD_NEED_GB = 8.5

    def _load_need_gb(self) -> float:
        return self.GPU_LOAD_NEED_GB if self._device() == "cuda" else self.SYNTHESIS_NEED_GB

    def _make_room_for_synthesis(self, need_gb: Optional[float] = None) -> None:
        """If the machine cannot hold a synthesis right now, release idle
        language models first. On 2026-09-24 every Chatterbox call failed with
        "The paging file is too small" (os error 1455) and then crashed, while
        the chat model that had already answered sat resident in Ollama."""
        try:
            from core.model_lifecycle import active_in_this_process
            if not active_in_this_process():
                return
            from core.sysmem import has_headroom
            need = self.SYNTHESIS_NEED_GB if need_gb is None else need_gb
            ok, _reason = has_headroom(need, margin_gb=0.5)
            if not ok:
                from core.model_lifecycle import unload_all
                dropped = unload_all()
                if dropped:
                    logger.info(f"unloaded {', '.join(dropped)} to make room for speech synthesis")
        except Exception as error:
            logger.debug(f"synthesis memory check skipped: {error}")

    @staticmethod
    def _gpu_out_of_memory(detail: str) -> bool:
        detail = (detail or "").lower()
        return "out of memory" in detail and ("cuda" in detail or "gpu" in detail)

    def _synthesise_on_server(self, request: dict, timeout: float) -> Optional[Path]:
        """One sentence through the loaded bridge. A GPU out-of-memory error
        is retried once after unloading the language models (they share the
        8 GB of VRAM); a crash is reported with the bridge's own evidence."""
        from voice.chatterbox_server import BridgeDied
        for attempt in (1, 2):
            try:
                answer = self._ensure_server().request(request, timeout=timeout)
            except TimeoutError as error:
                logger.warning(f"Chatterbox: {error}")
                self._stop_server("it stopped answering")
                return None
            except BridgeDied as error:
                from voice.stt_manager import classify_failure
                kind, why = classify_failure(error.exit_code, error.stderr or "")
                logger.error(f"Chatterbox worker crashed ({kind}): {why}; stderr: {error.stderr}")
                self.last_failure = {"classification": kind, "reason": why,
                                     "exit_code": error.exit_code, "stderr_tail": error.stderr}
                self._stop_server("it crashed")
                return None
            except Exception as error:
                logger.warning(f"Chatterbox bridge failed: {error}")
                return None
            if answer.get("ok"):
                self._last_timing = {"audio": answer.get("seconds"),
                                     "compute": answer.get("synthesis_seconds"),
                                     "device": answer.get("device")}
                return Path(answer["path"])
            detail = f"{answer.get('error')} - {answer.get('detail')}"
            if attempt == 1 and self._gpu_out_of_memory(detail):
                try:
                    from core.model_lifecycle import unload_all
                    dropped = unload_all()
                    logger.info(f"GPU memory full while speaking - unloaded "
                                f"{', '.join(dropped) or 'nothing'} and retrying")
                except Exception:
                    pass
                continue
            logger.warning(f"Chatterbox: {detail}")
            return None
        return None

    def synthesise(self, text: str) -> Optional[Path]:
        """Text to a WAV file, or None with the reason logged."""
        import json
        import subprocess
        import tempfile

        if self._python is None:
            return None
        profile = self._profile or {}
        output = Path(tempfile.mkdtemp(prefix="medha_tts_")) / "speech.wav"
        # Exactly the keys voice/chatterbox_bridge.py reads. It used to carry
        # F5-TTS's vocab_file, nfe and reference_text as well; the bridge
        # ignored them, which meant config.ini could be changed all day with
        # no audible effect and nothing to say why.
        request = {
            "text": text,
            "output": str(output),
            "model_dir": str(self.model_dir()),
            "reference": profile.get("reference", ""),
            "device": profile.get("device", "auto"),
        }
        for key in ("exaggeration", "cfg_weight", "temperature"):
            value = profile.get(key)
            if value is not None:
                request[key] = float(value)
        if self._keep_loaded():
            return self._synthesise_on_server(request, float(profile.get("timeout", 300)))

        self._make_room_for_synthesis()
        try:
            result = subprocess.run(
                [str(self._python),
                 str(Path(__file__).resolve().parent / "chatterbox_bridge.py")],
                input=json.dumps(request), capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                timeout=float(profile.get("timeout", 300)),
                cwd=str(Path(__file__).resolve().parent.parent))
            answer = json.loads(result.stdout.strip() or "{}")
        except subprocess.TimeoutExpired:
            logger.warning("Chatterbox took too long and was stopped")
            return None
        except Exception as error:
            logger.warning(f"Chatterbox bridge failed: {error}")
            return None

        if not answer and result.returncode != 0:
            # The bridge died without replying - a native crash (the c10.dll
            # access violation of 2026-09-24) or an abort. Say which, with
            # the evidence, instead of "Chatterbox: None - None".
            from voice.stt_manager import classify_failure
            kind, why = classify_failure(result.returncode, result.stderr or "")
            tail = " | ".join((result.stderr or "").strip().splitlines()[-4:])[:400]
            logger.error(f"Chatterbox worker crashed ({kind}): {why}; stderr: {tail}")
            self.last_failure = {"classification": kind, "reason": why,
                                 "exit_code": result.returncode, "stderr_tail": tail}
            return None
        if not answer.get("ok"):
            logger.warning(f"Chatterbox: {answer.get('error')} - {answer.get('detail')}")
            return None
        self._last_timing = {"audio": answer.get("seconds"),
                             "compute": answer.get("synthesis_seconds")}
        return Path(answer["path"])

    def speak(self, text: str) -> bool:
        """Speak the whole reply, generating ahead of the playback.

        This is the difference between usable and unusable on a CPU. Chatterbox
        needs several seconds of compute per second of speech, so synthesising
        the whole answer before making a sound means a minute of silence. Here
        a producer thread synthesises sentence N+1 while sentence N is
        playing, and playback starts as soon as the FIRST sentence is ready.
        The wait is one sentence, once.

        The queue is bounded at two. Unbounded, the producer would race ahead
        and hold the entire answer as decoded float32 audio - tens of
        megabytes on a long RAG reply, on a machine that is already carrying a
        chat model. Two is enough to keep the speaker fed.
        """
        import queue

        from voice.speech_prep import prepare

        if not text or not text.strip():
            return False
        if not self._streaming_enabled():
            return self._speak_sequentially(text)

        self._stop.clear()
        self._pause.clear()
        prepared = prepare(text,
                           max_chars=int(self._profile.get("max_spoken_chars", 0)))
        if not prepared or not prepared.chunks:
            return False

        pending: "queue.Queue" = queue.Queue(maxsize=2)
        SENTINEL = object()

        def produce():
            try:
                for chunk in prepared.chunks:
                    if self._stop.is_set():
                        break
                    audio = self.synthesise(chunk.text)
                    if self._stop.is_set():
                        # Nothing will play this; do not leave it on disk.
                        self._discard(audio)
                        break
                    if audio is not None:
                        # A bounded put would block forever if the consumer
                        # died, so it is polled against the stop flag.
                        while not self._stop.is_set():
                            try:
                                pending.put((audio, chunk.pause_after), timeout=0.25)
                                break
                            except queue.Full:
                                continue
            except Exception as error:
                logger.warning(f"Chatterbox generation stopped: {error}")
            finally:
                try:
                    pending.put(SENTINEL, timeout=5)
                except Exception:
                    pass

        producer = threading.Thread(target=produce, name="chatterbox-generate",
                                    daemon=True)
        self._set_state(SpeechState.GENERATING)
        producer.start()

        spoke = False
        interrupted = False
        try:
            while True:
                try:
                    item = pending.get(timeout=0.5)
                except queue.Empty:
                    if self._stop.is_set():
                        interrupted = True
                        break
                    if not producer.is_alive() and pending.empty():
                        break          # producer died without a sentinel
                    # Still generating. Say so - the alternative is a status
                    # that looks idle while the CPU is flat out.
                    if not self._pause.is_set():
                        self._set_state(SpeechState.GENERATING)
                    continue
                if item is SENTINEL or self._stop.is_set():
                    if item is not SENTINEL:
                        interrupted = True
                        self._discard(item[0])
                    break
                audio, pause_after = item
                if self._play(audio, pause_after):
                    spoke = True
        finally:
            # `self._stop` is set here to release a producer blocked on put(),
            # so it can no longer distinguish "the user pressed stop" from
            # "we finished". `interrupted` is captured BEFORE that, which is
            # why the status after a real stop stays STOPPED instead of
            # quietly reverting to IDLE while the person is still looking at
            # the button they just pressed.
            interrupted = interrupted or self._stop.is_set()
            self._stop.set()
            producer.join(timeout=2)
            self._drain(pending, SENTINEL)
            self._set_state(SpeechState.IDLE if spoke and not interrupted
                            else SpeechState.STOPPED)
        return spoke

    def _speak_sequentially(self, text: str) -> bool:
        """Synthesise then play, one chunk at a time, no producer thread.

        Kept because `voice_streaming = false` has to mean something: when
        somebody is diagnosing an audio fault, a second thread is one more
        thing to rule out.
        """
        from voice.speech_prep import prepare

        self._stop.clear()
        prepared = prepare(text,
                           max_chars=int(self._profile.get("max_spoken_chars", 0)))
        if not prepared:
            return False
        spoke = False
        try:
            for chunk in prepared.chunks:
                if self._stop.is_set():
                    break
                self._set_state(SpeechState.GENERATING)
                audio = self.synthesise(chunk.text)
                if audio is None:
                    continue
                if self._stop.is_set():
                    self._discard(audio)
                    break
                if self._play(audio, chunk.pause_after):
                    spoke = True
        finally:
            self._set_state(SpeechState.STOPPED if not spoke else SpeechState.IDLE)
        return spoke

    @staticmethod
    def _streaming_enabled() -> bool:
        try:
            from voice.offline import config_bool
            return config_bool("voice_streaming", True)
        except Exception:
            return True

    @staticmethod
    def _discard(audio: Optional[Path]) -> None:
        """Delete audio that will never be played. Without this, an interrupted
        long answer leaves a trail of temp directories."""
        if audio is None:
            return
        try:
            audio.unlink(missing_ok=True)
            audio.parent.rmdir()
        except Exception:
            pass

    def _drain(self, pending, sentinel) -> None:
        import queue
        while True:
            try:
                item = pending.get_nowait()
            except queue.Empty:
                return
            if item is not sentinel:
                self._discard(item[0])

    def _polish(self, audio, np):
        """Light, reversible cleanup of one synthesised chunk.

        The brief for this is "do not over-process the voice", so this does
        three cheap things and stops:

        1. A short fade at each end. Chunks are generated independently, so
           chunk N can end mid-waveform and chunk N+1 begin mid-waveform; the
           discontinuity is audible as a click between sentences. Three
           milliseconds is below the threshold of hearing as a fade and well
           above the click.

        2. Peak normalisation only when the chunk is quiet or clipping. Levels
           drift between chunks because each is generated separately, and a
           sentence noticeably louder than the one before it is the artifact
           people notice first. Chunks already sitting in a sensible range are
           left completely alone.

        3. A hard clip guard, so volume above 1.0 cannot produce the crackle
           of samples wrapping around.

        No compression, no EQ, no de-noising, no loudness matching against a
        target curve. Those make speech sound processed, which is exactly what
        this voice is meant not to sound like.
        """
        if not bool(self._profile.get("polish", True)):
            return audio
        try:
            if audio.ndim > 1:
                audio = audio.mean(axis=1)
            peak = float(np.max(np.abs(audio))) if audio.size else 0.0
            # 0.35 is quiet enough to be noticeably weaker than its
            # neighbours; 0.99 is on the edge of clipping. Between the two,
            # leave it be.
            if 0.0 < peak < 0.35 or peak > 0.99:
                audio = audio * (0.85 / peak)

            fade = min(int(0.003 * 24000), audio.size // 2)
            if fade > 0:
                ramp = np.linspace(0.0, 1.0, fade, dtype=audio.dtype)
                audio[:fade] *= ramp
                audio[-fade:] *= ramp[::-1]
            return np.clip(audio, -1.0, 1.0)
        except Exception as error:
            # Cosmetic. Never worth losing the sentence over.
            logger.debug(f"audio polish skipped: {error}")
            return audio

    def _play(self, path: Path, pause_after: float = 0.0) -> bool:
        """Play one WAV and wait for it, then rest for the planned pause."""
        try:
            import numpy as np
            import sounddevice as sd
            import soundfile as sf
        except Exception as error:
            logger.warning(f"cannot play audio: {error}")
            return False
        try:
            audio, rate = sf.read(str(path), dtype="float32")
            audio = self._polish(audio, np)
            volume = float(self._profile.get("volume", 1.0))
            if volume != 1.0:
                audio = np.clip(audio * volume, -1.0, 1.0)
            sd.wait()                       # never talk over the previous chunk

            # Pause takes effect at a sentence boundary. Cutting the waveform
            # mid-word and resuming from the same sample sounds broken, and
            # sentence granularity is what a listener expects from a pause
            # button anyway.
            while self._pause.is_set() and not self._stop.is_set():
                self._set_state(SpeechState.PAUSED)
                time.sleep(0.1)
            if self._stop.is_set():
                return False

            self._playing = True
            self._set_state(SpeechState.SPEAKING)
            sd.play(audio, samplerate=rate)
            sd.wait()
            self._playing = False
            if pause_after and not self._stop.is_set():
                time.sleep(pause_after)
            return True
        except Exception as error:
            self._playing = False
            logger.warning(f"playback failed: {error}")
            return False
        finally:
            try:
                path.unlink(missing_ok=True)
                path.parent.rmdir()
            except Exception:
                pass

    def stop(self):
        """Interrupt immediately: stop the audio AND the generation loop.

        Both matter. Stopping only the audio leaves the producer thread
        burning CPU synthesising sentences nobody will hear, which on this
        hardware is the expensive half.
        """
        self._stop.set()
        self._pause.clear()
        try:
            import sounddevice as sd
            sd.stop()
        except Exception:
            pass
        self._playing = False
        self._set_state(SpeechState.STOPPED)

    def pause(self) -> bool:
        if not self.is_speaking():
            return False
        self._pause.set()
        return True

    def resume(self) -> bool:
        if not self._pause.is_set():
            return False
        self._pause.clear()
        self._set_state(SpeechState.SPEAKING)
        return True

    def close(self):
        self.stop()
        self._closed.set()
        self._stop_server("voice closed")
        self._python = None


# =============================================================== input ====

class InputProvider:
    """Base class for anything that can turn sound into text."""

    name = "abstract"
    direction = "input"
    requires_download = False
    last_error = ""          # why the last listen produced no text ("silence", a device error ...)

    def probe(self) -> ProviderReport:
        raise NotImplementedError

    def activate(self) -> ProviderReport:
        raise NotImplementedError

    def transcribe_array(self, audio, samplerate: int = 16000) -> str:
        raise NotImplementedError

    def listen_once(self, duration: float = 5.0) -> str:
        """Record from the default microphone and transcribe it.

        The default implementation captures with sounddevice and hands the
        waveform to `transcribe_array`, so a provider only has to know how to
        turn samples into text.
        """
        try:
            import numpy as np
            import sounddevice as sd
        except Exception as error:
            logger.warning(f"cannot capture audio: {error}")
            return ""
        try:
            samplerate = 16000
            frames = int(duration * samplerate)
            audio = sd.rec(frames, samplerate=samplerate, channels=1, dtype="float32")
            sd.wait()
            audio = np.squeeze(audio)
            # Transcribing silence makes recognisers hallucinate ("Thank you.",
            # subtitle credits) which then gets executed as a command.
            if float(np.sqrt(np.mean(audio ** 2))) < 0.004:
                return ""
            return self.transcribe_array(audio, samplerate)
        except Exception as error:
            logger.warning(f"capture failed: {error}")
            return ""

    def close(self):
        pass


class WhisperInputProvider(InputProvider):
    """faster-whisper, loaded from an explicit local directory.

    `WhisperModel` is given a path, never a Hugging Face repo id, so there is
    no code path on which it can download. If the directory is incomplete this
    reports MODEL MISSING and ZEUS listens to nothing - which is a fine way
    to run.
    """

    name = "whisper"
    requires_download = False

    def __init__(self):
        self._stt = None

    def probe(self) -> ProviderReport:
        report = ProviderReport(self.name, self.direction)
        if not _installed("faster_whisper"):
            report.state = ProviderState.INSTALLABLE
            report.detail = "faster-whisper is not installed (optional)"
            return report
        directory = resolve_whisper_dir()
        absent = missing_files(directory, WHISPER_REQUIRED_FILES)
        if absent:
            report.state = ProviderState.MODEL_MISSING
            report.detail = (f"no local Whisper model in {directory} "
                             f"(missing: {', '.join(absent)})")
            report.evidence = {"missing": absent, "directory": str(directory)}
            return report
        if not _installed("sounddevice"):
            report.state = ProviderState.MODEL_MISSING
            report.detail = "sounddevice is not installed, so there is no way to capture audio"
            return report
        report.state = ProviderState.NOT_PROBED
        report.detail = f"local Whisper model present in {directory}"
        return report

    def activate(self) -> ProviderReport:
        report = ProviderReport(self.name, self.direction)
        probe = self.probe()
        if probe.state in (ProviderState.INSTALLABLE, ProviderState.MODEL_MISSING):
            return probe
        started = time.time()
        try:
            enforce_offline()
            from .speech_to_text import SpeechToText
            engine = SpeechToText()
            if not engine.is_available:
                report.state = ProviderState.FAILED
                report.detail = engine.last_error or "whisper did not become available"
                return report
            self._stt = engine
            report.state = ProviderState.READY
            report.detail = "faster-whisper loaded from the local model directory"
            report.evidence = {"load_ms": int((time.time() - started) * 1000)}
        except Exception as error:
            report.state = ProviderState.FAILED
            report.detail = f"{type(error).__name__}: {error}"
        return report

    def transcribe_array(self, audio, samplerate: int = 16000) -> str:
        if not self._stt:
            return ""
        return self._stt.transcribe_array(audio, samplerate)

    def listen_once(self, duration: Optional[float] = None) -> str:
        # SpeechToText already owns capture, including the resampling that
        # microphones refusing a 16 kHz stream need. Use it rather than a
        # second, worse copy of that logic. duration=None lets it end the
        # recording when the speaker stops ([voice] listen_until_silence).
        if not self._stt:
            self.last_error = "speech recognition is not loaded"
            return ""
        text = self._stt.listen_once(duration)
        self.last_error = "" if text else (getattr(self._stt, "last_error", "") or "")
        return text

    def close(self):
        if self._stt:
            try:
                self._stt.close()
            except Exception:
                pass
        self._stt = None


class VoskInputProvider(InputProvider):
    """Vosk: a much smaller recogniser (~50 MB), used if it is already here."""

    name = "vosk"
    requires_download = False

    def _model_dir(self) -> Optional[Path]:
        configured = config_str("vosk_model", "")
        if configured:
            candidate = Path(configured)
            return candidate if candidate.is_dir() else None
        base = resolve_models_dir() / "vosk"
        if base.is_dir() and (base / "am").is_dir():
            return base
        if base.is_dir():
            for child in sorted(base.iterdir()):
                if child.is_dir() and (child / "am").is_dir():
                    return child
        return None

    def probe(self) -> ProviderReport:
        report = ProviderReport(self.name, self.direction)
        if not _installed("vosk"):
            report.state = ProviderState.INSTALLABLE
            report.detail = "vosk is not installed (optional)"
            return report
        directory = self._model_dir()
        if not directory:
            report.state = ProviderState.MODEL_MISSING
            report.detail = f"no vosk model directory under {resolve_models_dir() / 'vosk'}"
            return report
        self._directory = directory
        report.state = ProviderState.NOT_PROBED
        report.detail = f"vosk model present: {directory.name}"
        return report

    def activate(self) -> ProviderReport:
        report = ProviderReport(self.name, self.direction)
        probe = self.probe()
        if probe.state in (ProviderState.INSTALLABLE, ProviderState.MODEL_MISSING):
            return probe
        try:
            from vosk import KaldiRecognizer, Model
            self._model = Model(str(self._directory))
            self._recognizer_class = KaldiRecognizer
            report.state = ProviderState.READY
            report.detail = f"vosk loaded {self._directory.name}"
        except Exception as error:
            report.state = ProviderState.FAILED
            report.detail = f"{type(error).__name__}: {error}"
        return report

    def transcribe_array(self, audio, samplerate: int = 16000) -> str:
        model = getattr(self, "_model", None)
        if model is None:
            return ""
        try:
            import json

            import numpy as np
            recognizer = self._recognizer_class(model, samplerate)
            pcm = (np.asarray(audio) * 32767).astype(np.int16).tobytes()
            recognizer.AcceptWaveform(pcm)
            return (json.loads(recognizer.FinalResult()) or {}).get("text", "")
        except Exception as error:
            logger.warning(f"vosk transcription failed: {error}")
            return ""


# ============================================================= manager ====

# Cheapest first: a provider that needs nothing installed is preferred over one
# that needs 2 GB of torch, even though torch sounds better. Quality is the
# tie-breaker between providers that are equally present, not a reason to have
# no voice at all.
# Chatterbox first: it is the voice this system is built around, and it runs in
# its own environment so its presence costs the rest of ZEUS nothing. The
# rest of the ladder is the fallback chain, cheapest last, so a machine with
# nothing installed still speaks with the voice built into the OS.
# The order ZEUS tries speech-out providers in, best first.
#
# "chatterbox" is the bridge: Chatterbox in its own environment, installed by
# SETUP_VOICE.bat. "chatterbox-inprocess" is the same model loaded directly in
# ZEUS's environment, which works on a machine where chatterbox-tts happens
# to be importable there already. The bridge is preferred because it cannot be
# broken by ZEUS's own dependencies - but leaving the in-process one out of
# this list entirely would mean a machine with a perfectly working Chatterbox
# reports that it cannot speak, which is exactly the wrong answer.
#
# piper and the system voice are the fallbacks. The system voice is last and
# always present on Windows, so there is a floor below which ZEUS still
# talks rather than going silent.
OUTPUT_ORDER = ("chatterbox", "chatterbox-inprocess", "piper", "system")
INPUT_ORDER = ("whisper", "vosk")


class VoiceManager:
    """Chooses providers, holds the honest state, and never blocks startup.

    Construction does nothing expensive. `probe()` is filesystem-only and safe
    to call during startup; `start()` performs the real loads and is what moves
    the subsystem out of INITIALIZING.
    """

    def __init__(self):
        self.state = VoiceState.DISABLED
        self.detail = "not started"
        self.last_listen_error = ""          # why the last listen gave no text
        self.output: Optional[OutputProvider] = None
        self.input: Optional[InputProvider] = None
        self._reports: Dict[str, ProviderReport] = {}
        self._lock = threading.Lock()
        self._started = False
        self._fallback_notes: List[str] = []

    # -- construction of the candidate list ------------------------------

    def _output_candidates(self) -> List[OutputProvider]:
        # ChatterboxBridgeProvider, not ChatterboxVoiceProvider: the same
        # model, loaded in an isolated environment so its hard version pins
        # (torch 2.6, transformers 5.2, diffusers 0.29) cannot reach ZEUS's
        # own dependencies. The in-process class below is kept for an operator
        # who already has chatterbox-tts installed in the main environment.
        built = {"chatterbox": ChatterboxBridgeProvider,
                 "chatterbox-inprocess": ChatterboxVoiceProvider,
                 "system": SystemVoiceProvider,
                 "piper": PiperVoiceProvider}
        preferred = config_str("tts_provider", "").strip().lower()
        order = list(OUTPUT_ORDER)
        if preferred in built:
            # An explicit choice is honoured first, and if it fails that is an
            # ERROR rather than a quiet fallback: the operator asked for it.
            order = [preferred] + [name for name in order if name != preferred]
        return [built[name]() for name in order]

    def _input_candidates(self) -> List[InputProvider]:
        built = {"whisper": WhisperInputProvider, "vosk": VoskInputProvider}
        preferred = config_str("stt_provider", "").strip().lower()
        order = list(INPUT_ORDER)
        if preferred in built:
            order = [preferred] + [name for name in order if name != preferred]
        return [built[name]() for name in order]

    # -- cheap inspection -------------------------------------------------

    def probe(self) -> Dict[str, ProviderReport]:
        """What COULD work here. Filesystem and import checks only: no model
        is loaded, no audio device is opened, nothing is downloaded."""
        reports: Dict[str, ProviderReport] = {}
        for provider in self._output_candidates() + self._input_candidates():
            try:
                reports[provider.name] = provider.probe()
            except Exception as error:                  # a probe must never raise
                reports[provider.name] = ProviderReport(
                    provider.name, provider.direction, ProviderState.FAILED,
                    f"{type(error).__name__}: {error}")
        self._reports = reports
        return reports

    # -- the real thing ---------------------------------------------------

    def start(self, want_input: bool = True, want_output: bool = True) -> VoiceState:
        """Load the best available provider in each direction.

        Returns the resulting state. Never raises: every failure becomes a
        state and a reason, because a machine with no microphone must still
        get a working application.
        """
        with self._lock:
            if not voice_enabled():
                self.state = VoiceState.DISABLED
                self.detail = "voice is switched off in config.ini [voice] enabled"
                self._started = True
                return self.state

            self.state = VoiceState.INITIALIZING
            self.detail = "selecting local voice providers"
            enforce_offline()

            explicit_output = config_str("tts_provider", "").strip().lower()
            explicit_input = config_str("stt_provider", "").strip().lower()
            failures: List[str] = []
            self._fallback_notes = []

            if want_output:
                chosen = self._select(self._output_candidates(), explicit_output,
                                      "voice", failures)
                if isinstance(chosen, VoiceState):     # an explicit choice broke
                    return chosen
                self.output = chosen

            if want_input:
                chosen = self._select(self._input_candidates(), explicit_input,
                                      "recogniser", failures)
                if isinstance(chosen, VoiceState):
                    return chosen
                self.input = chosen

            self._started = True
            self.state, self.detail = self._settle(want_input, want_output, failures)
            logger.info(f"voice: {self.state.value} - {self.detail}")
            return self.state

    def _select(self, candidates, explicit: str, label: str, failures: List[str]):
        """Activate candidates in order and return the first that works.

        Returns a VoiceState instead when the provider the operator NAMED in
        config could not start: an explicit choice that silently falls back to
        something else is how a machine ends up sounding wrong with no
        explanation anywhere.
        """
        for provider in candidates:
            report = provider.activate()
            self._reports[provider.name] = report
            if report.ready:
                return provider
            # A provider that BROKE is worth reporting as an error. One that
            # is merely absent or unsupported here is not a fault, and must
            # not turn "this machine has no voice" into "something is wrong".
            if report.state is ProviderState.FAILED:
                failures.append(f"{provider.name}: {report.detail}")
            if provider.name == explicit and not report.ready:
                if config_bool("voice_fallback", True):
                    # [voice] voice_fallback = true (the default): keep going
                    # with the next provider, and SAY that the one the operator
                    # named did not start. Returning here used to abandon the
                    # input side too, so a missing Chatterbox also switched
                    # off the microphone.
                    self._fallback_notes.append(
                        f"the configured {label} '{provider.name}' could not start "
                        f"({report.detail})")
                    continue
                self.state = VoiceState.ERROR
                self.detail = (f"the configured {label} '{provider.name}' could "
                               f"not start - {report.detail}")
                self._started = True
                return self.state
        return None

    def _settle(self, want_input: bool, want_output: bool,
                failures: List[str]):
        """Turn what loaded into one of the five states, honestly."""
        has_out = self.output is not None
        has_in = self.input is not None
        notes = list(getattr(self, "_fallback_notes", []))

        if notes and (has_out or has_in):
            working = []
            if has_in:
                working.append(f"listening with {self.input.name}")
            if has_out:
                working.append(f"speaking with {self.output.name}")
            return VoiceState.DEGRADED, "; ".join(notes + working)

        if has_out and has_in:
            return VoiceState.AVAILABLE, (
                f"listening with {self.input.name}, speaking with {self.output.name}")
        if has_out and not want_input:
            return VoiceState.AVAILABLE, f"speaking with {self.output.name}"
        if has_in and not want_output:
            return VoiceState.AVAILABLE, f"listening with {self.input.name}"
        if has_out:
            return VoiceState.DEGRADED, (
                f"speaking with {self.output.name}; no speech recogniser is "
                f"installed, so input stays in text mode")
        if has_in:
            return VoiceState.DEGRADED, (
                f"listening with {self.input.name}; nothing on this machine can "
                f"produce speech, so replies stay in text")
        if failures:
            # Something was present and broke - that is worth saying loudly.
            return VoiceState.ERROR, "; ".join(failures[:2])
        return VoiceState.DISABLED, (
            "no local voice provider is installed - ZEUS is running in text "
            "mode, which is a supported configuration")

    # -- use ---------------------------------------------------------------

    @property
    def can_speak(self) -> bool:
        return self.output is not None

    @property
    def can_listen(self) -> bool:
        return self.input is not None

    def speak(self, text: str) -> bool:
        """Say something, or return False. Never raises into a reply path."""
        if not self.output or not text:
            return False
        try:
            return self.output.speak(text)
        except Exception as error:
            logger.warning(f"speaking failed: {error}")
            return False

    def speak_async(self, text: str) -> bool:
        """Speak without making the caller wait for playback.

        The written reply has to appear the moment it is ready; speech takes
        seconds. One worker thread at a time, so replies are never spoken over
        each other.
        """
        if not self.output or not text:
            return False
        worker = getattr(self, "_speech_thread", None)
        if worker is not None and worker.is_alive():
            # What to do about the utterance already in progress is a
            # judgement call, so it is configuration rather than a hard rule.
            #
            #   interruptible = true  -> cut it off and speak the new reply.
            #     Right for conversation: you asked something new, you want an
            #     answer to THAT, not the tail of the last one.
            #
            #   interruptible = false -> let it finish, then speak.
            #     Right when replies carry safety-critical content, where a
            #     half-delivered instruction is worse than a delayed one.
            #
            # Either way exactly one utterance is audible at a time.
            if config_bool("voice_interruptible", True):
                self.stop()
                worker.join(timeout=2.0)
            else:
                worker.join(timeout=120.0)
                if worker.is_alive():
                    # It has outlasted any plausible reply. Speaking over it
                    # would be worse than cutting it off.
                    logger.warning("previous speech did not finish; stopping it")
                    self.stop()
                    worker.join(timeout=2.0)
        thread = threading.Thread(target=self.speak, args=(text,),
                                  name="medha-speech", daemon=True)
        self._speech_thread = thread
        thread.start()
        return True

    def wait_until_spoken(self, timeout: float = 30.0) -> bool:
        """Block until queued speech has finished. For tests and for shutdown,
        so a reply is not cut off mid-sentence by the process exiting."""
        worker = getattr(self, "_speech_thread", None)
        if worker is not None:
            worker.join(timeout=timeout)
        engine = getattr(self.output, "_tts", None)
        if engine is not None and hasattr(engine, "wait_until_spoken"):
            engine.wait_until_spoken()
        return True

    def transcribe_array(self, audio, samplerate: int = 16000) -> str:
        if not self.input:
            return ""
        try:
            return self.input.transcribe_array(audio, samplerate)
        except Exception as error:
            logger.warning(f"transcription failed: {error}")
            return ""

    def listen_once(self, duration: Optional[float] = None) -> str:
        """Record one request. No duration: until the speaker stops (a fixed
        5 s used to be passed here for every request, whatever was said)."""
        self.last_listen_error = ""
        if not self.input:
            self.last_listen_error = self.detail or "no speech recogniser is active"
            return ""
        try:
            if duration is None and not isinstance(self.input, WhisperInputProvider):
                duration = 5.0          # only Whisper's capture can end on silence
            text = self.input.listen_once(duration)
            if not text:
                self.last_listen_error = getattr(self.input, "last_error", "") or ""
            return text
        except Exception as error:
            logger.warning(f"listening failed: {error}")
            self.last_listen_error = str(error)
            try:
                from voice.speech_to_text import publish_stage
                publish_stage("STT ERROR", str(error))
            except Exception:
                pass
            return ""

    def stop(self):
        if self.output:
            self.output.stop()

    def close(self):
        for provider in (self.output, self.input):
            if provider:
                try:
                    provider.close()
                except Exception:
                    pass
        self.output = None
        self.input = None
        self.state = VoiceState.DISABLED
        self.detail = "shut down"

    # -- reporting ---------------------------------------------------------

    def report(self) -> Dict[str, Any]:
        """The whole truth, for the UI and the readiness check. Every claim
        here comes from a probe or a load that actually happened."""
        if not self._reports:
            self.probe()
        return {
            "state": self.state.value,
            "detail": self.detail,
            "started": self._started,
            "text_mode": self.state in (VoiceState.DISABLED, VoiceState.ERROR)
                         or not self.can_speak,
            "output": {"provider": self.output.name if self.output else None,
                       "available": self.can_speak,
                       # What the provider reported after it LOADED - the
                       # device it is really using, the torch build, the GPU
                       # name if there is one. The interface draws "CPU MODE"
                       # or "GPU MODE" from this rather than from config, so
                       # the badge cannot claim an accelerator that is absent.
                       "evidence": (self._reports[self.output.name].evidence
                                    if self.output and self.output.name in self._reports
                                    else {}),
                       "speech_state": (self.output.speech_state()
                                        if self.output else SpeechState.IDLE)},
            "input": {"provider": self.input.name if self.input else None,
                      "available": self.can_listen},
            "providers": {name: report.as_dict()
                          for name, report in sorted(self._reports.items())},
            "external_voice_services": "DISABLED",
            "downloads_at_runtime": "DISABLED",
        }

    def output_provider(self) -> Optional[OutputProvider]:
        """The provider that is actually speaking, or None in text mode.

        Exposed so the interface can subscribe to live state and drive stop
        and pause without reaching into the manager's attributes.
        """
        return self.output

    def stop_speaking(self) -> bool:
        if self.output is None:
            return False
        self.output.stop()
        return True

    def pause_speaking(self) -> bool:
        return bool(self.output and self.output.pause())

    def resume_speaking(self) -> bool:
        return bool(self.output and self.output.resume())

    def speech_state(self) -> str:
        return self.output.speech_state() if self.output else SpeechState.IDLE

    def banner(self) -> List[str]:
        """Startup lines. Every one is a state that was actually checked."""
        report = self.report()
        lines = [f"VOICE: {self.state.value.upper()} — {self.detail}"]
        if self.output:
            lines.append(f"  SPEECH OUT: {self.output.name}")
        else:
            lines.append("  SPEECH OUT: none (text replies)")
        if self.input:
            lines.append(f"  SPEECH IN : {self.input.name}")
        else:
            lines.append("  SPEECH IN : none (typed input)")
        lines.append("  EXTERNAL VOICE SERVICES: DISABLED")
        return lines


voice_manager = VoiceManager()
