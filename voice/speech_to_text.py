"""
ZEUS Speech-to-Text
Uses faster-whisper (offline) with sounddevice for capture. If the mic
or the model can't be loaded, `is_available` is False and the caller
(main.py) disables voice mode without crashing the app; the reason is
kept in `last_error` so failures are inspectable rather than silent.

OFFLINE: the model is loaded from an explicit local directory. When
`WhisperModel` is given an existing directory it uses it as-is and
never contacts the Hugging Face Hub; `local_files_only=True` is set as
a second line of defence. See `voice/offline.py`.

Interface (unchanged for existing callers):
    stt = SpeechToText()
    stt.is_available          -> bool
    stt.listen_once(duration) -> str          (record from the mic)
    stt.transcribe_array(wav) -> str          (UI-owned capture)
    stt.transcribe_file(path) -> str          (a .wav/.mp3 on disk)
"""

import threading
from pathlib import Path
from typing import Optional

from core.logger import get_logger

from .offline import (
    WHISPER_REQUIRED_FILES,
    MissingModelError,
    config_bool,
    config_float,
    config_str,
    enforce_offline,
    preload_models,
    require_model_dir,
    resolve_whisper_dir,
)

logger = get_logger("stt")

DEFAULT_SAMPLERATE = 16000  # Whisper is trained at 16 kHz; do not change.

# main.py sets this for the windowed application: a preload then happens on a
# background thread, so the window is not held back while a large model (the
# GPU turbo model is ~1.6 GB) loads. Anything that needs the model meanwhile
# simply waits for the same load. Left False, preload stays synchronous.
BACKGROUND_PRELOAD = False

# Below this RMS the capture is treated as silence. Transcribing silence
# makes Whisper hallucinate ("Thank you.", "you", subtitle credits), which
# then gets executed as a command - so this gate matters for correctness,
# not just tidiness.
SILENCE_RMS = 0.004


_level_listeners = []


def subscribe_level(callback) -> None:
    """Receive the live microphone loudness (RMS, 0..~0.5) while listening."""
    if callback not in _level_listeners:
        _level_listeners.append(callback)


def publish_level(level: float) -> None:
    for callback in list(_level_listeners):
        try:
            callback(level)
        except Exception:
            pass


# What the recogniser is really doing, for the MIC indicator - published as it
# happens, never guessed by the page.
STT_STAGES = ("MIC READY", "PREPARING RECOGNISER", "LISTENING", "SPEECH DETECTED", "TRANSCRIBING",
              "TRANSCRIBED", "NO SPEECH", "MIC ERROR", "STT ERROR")
_stage_listeners = []


def subscribe_stage(callback) -> None:
    if callback not in _stage_listeners:
        _stage_listeners.append(callback)


def publish_stage(stage: str, detail: str = "") -> None:
    for callback in list(_stage_listeners):
        try:
            callback(stage, detail)
        except Exception:
            pass


# A load refused for memory is TRANSIENT (a chat model was resident): the next
# press tries again. Only a broken install turns voice input off.
_TRANSIENT_LOAD = ("insufficient runtime memory", "memory", "commit", "out of memory", "timed out", "timeout")


class EndOfSpeech:
    """Decides, block by block, when a spoken request is over.

    The microphone used to record a FIXED five seconds for every request:
    "what is the pressure" waited out the full five, and a longer question
    was cut off at five. Now capture ends shortly after the speaker stops.

      * nobody starts speaking within `wait_for_speech` s -> stop (as before);
      * once speech is heard, stop after `trailing_silence` s of quiet;
      * never record longer than `max_seconds`.

    Levels are judged against the room's own noise floor, estimated from the
    first few blocks, so a fan or a noisy plant floor is not taken for a
    voice. If speech is never recognised as such, this degrades to exactly
    the old fixed-length recording - never to something worse.
    """

    CALIBRATION_BLOCKS = 5
    MIN_SPEECH_RMS = 0.015
    MIN_QUIET_RMS = 0.008

    def __init__(self, block_seconds: float, wait_for_speech: float,
                 trailing_silence: float = 0.7, max_seconds: float = 15.0):
        self.block_seconds = block_seconds
        self.wait_for_speech = wait_for_speech
        self.trailing_silence = trailing_silence
        self.max_seconds = max(max_seconds, wait_for_speech)
        self.elapsed = 0.0
        self.heard = False
        self._loud_run = 0
        self._quiet = 0.0
        self._floor: list = []

    def _noise_floor(self) -> float:
        if not self._floor:
            return 0.0
        ordered = sorted(self._floor)
        return ordered[len(ordered) // 2]

    # Block durations are not exact in binary floating point; without this a
    # 4.0 s wait could end one block late at 4.05 s.
    _EPS = 1e-6

    def feed(self, rms: float) -> bool:
        """One block's loudness in; True once recording should stop."""
        self.elapsed += self.block_seconds
        if len(self._floor) < self.CALIBRATION_BLOCKS:
            self._floor.append(rms)
        floor = self._noise_floor()
        speech_level = max(self.MIN_SPEECH_RMS, floor * 3.0)
        quiet_level = max(self.MIN_QUIET_RMS, floor * 2.0)
        if not self.heard:
            # Two consecutive loud blocks (~0.1 s), so a click or a cough on
            # its own does not count as the start of a request.
            self._loud_run = self._loud_run + 1 if rms >= speech_level else 0
            if self._loud_run >= 2:
                self.heard = True
            return self.elapsed + self._EPS >= self.wait_for_speech and not self.heard
        self._quiet = self._quiet + self.block_seconds if rms < quiet_level else 0.0
        return (self._quiet + self._EPS >= self.trailing_silence
                or self.elapsed + self._EPS >= self.max_seconds)


def stt_isolation() -> str:
    """'process' (default): Whisper runs in a supervised worker process
    (voice/stt_worker.py), so a native crash in CTranslate2/CUDA cannot take
    ZEUS down. 'inprocess': the old behaviour, kept for the test suite's
    stubbed models and for diagnosis.

    Resolution: AGNI_STT_ISOLATION env > config.ini [voice] stt_isolation >
    'process'."""
    import os
    value = (os.environ.get("AGNI_STT_ISOLATION", "").strip().lower()
             or config_str("stt_isolation", "process").strip().lower())
    return "inprocess" if value in ("inprocess", "in-process", "in_process") else "process"


class _WorkerModel:
    """Stands in for faster_whisper.WhisperModel when Whisper runs in the
    worker: same transcribe(audio, **options) -> (segments, info) shape, so
    _transcribe() and every caller work unchanged."""

    def __init__(self, manager):
        self.manager = manager

    def transcribe(self, audio, **options):
        from voice.stt_manager import segments_of
        language = options.pop("language", None)
        reply = self.manager.transcribe(audio, language=language, options=options)
        return segments_of(reply)


def _verified_whisper_choice() -> dict:
    """What SETUP_VOICE.bat verified on this machine (models/whisper/agni_verified.json)."""
    try:
        import json
        from voice.offline import resolve_whisper_dir
        record = resolve_whisper_dir() / "agni_verified.json"
        if record.is_file():
            return json.loads(record.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _physical_cores() -> int:
    """Real cores, not hyperthreads. See the note where this is used."""
    try:
        import psutil
        physical = psutil.cpu_count(logical=False)
        if physical:
            return max(1, min(int(physical), 8))
    except Exception:
        pass
    import os
    return max(1, (os.cpu_count() or 2) // 2)


# Transcription options tuned for a CPU-only laptop.
#
#   beam_size=1        greedy. A beam search costs several times more compute
#                      for a marginal gain on short spoken commands.
#   vad_filter=True    drop silence before it reaches the model. A five-second
#                      recording of someone thinking is mostly silence, and
#                      not transcribing it is the single cheapest speed-up
#                      available - it also stops Whisper hallucinating
#                      "Thank you." and subtitle credits into silence, which
#                      then get executed as commands.
#   condition_on_previous_text=False
#                      each utterance is its own command, and carrying the
#                      previous one as context makes the model repeat itself
#                      when the microphone catches a stray noise.
CPU_TRANSCRIBE_OPTIONS = {
    "beam_size": 1,
    "vad_filter": True,
    "vad_parameters": {"min_silence_duration_ms": 400},
    "condition_on_previous_text": False,
}


def _transcribe(model, audio, language: Optional[str] = None):
    """Run a transcription with the CPU options, falling back if the installed
    faster-whisper does not accept one of them."""
    options = dict(CPU_TRANSCRIBE_OPTIONS)
    if language:
        options["language"] = language
    try:
        return model.transcribe(audio, **options)
    except TypeError as error:
        # An older faster-whisper without VAD support. Losing the speed-up is
        # much better than losing transcription.
        logger.warning(f"faster-whisper rejected a transcribe option ({error}); "
                       f"falling back to greedy decoding only")
        return model.transcribe(audio, beam_size=1)


def list_input_devices() -> list:
    """Every input-capable audio device on this machine. Empty list when
    sounddevice/PortAudio is unavailable - never raises."""
    try:
        import sounddevice as sd
        devices = []
        for index, device in enumerate(sd.query_devices()):
            if device.get("max_input_channels", 0) > 0:
                devices.append({
                    "index": index,
                    "name": device.get("name", f"device {index}"),
                    "channels": device.get("max_input_channels"),
                    "default_samplerate": int(device.get("default_samplerate") or 0),
                })
        return devices
    except Exception as e:
        logger.debug(f"Could not enumerate input devices: {e}")
        return []


def default_input_device() -> Optional[dict]:
    try:
        import sounddevice as sd
        index = sd.default.device[0]
        if index is None or index < 0:
            devices = list_input_devices()
            return devices[0] if devices else None
        info = sd.query_devices(index)
        return {"index": index, "name": info.get("name", str(index)),
                "channels": info.get("max_input_channels"),
                "default_samplerate": int(info.get("default_samplerate") or 0)}
    except Exception:
        devices = list_input_devices()
        return devices[0] if devices else None


def _resample_to_16k(audio, source_rate: int):
    """Linear resample to 16 kHz using numpy only.

    Needed because many Windows microphones refuse a 16 kHz stream and only
    offer 44100/48000; asking PortAudio for 16 kHz on those devices raises
    "Invalid sample rate" and voice input appears to be broken.
    """
    import numpy as np
    if source_rate == DEFAULT_SAMPLERATE or source_rate <= 0:
        return audio
    duration = audio.shape[0] / float(source_rate)
    target_length = int(round(duration * DEFAULT_SAMPLERATE))
    if target_length <= 1:
        return audio
    source_positions = np.linspace(0.0, duration, num=audio.shape[0], endpoint=False)
    target_positions = np.linspace(0.0, duration, num=target_length, endpoint=False)
    return np.interp(target_positions, source_positions, audio).astype("float32")


class SpeechToText:
    # Defaults for an instance built without __init__ (tests, tools that
    # attach an already-verified model): isolation is then resolved on first
    # load, exactly as __init__ would have resolved it.
    isolation: Optional[str] = None
    _manager = None

    def __init__(
        self,
        model_size: Optional[str] = None,
        model_dir: Optional[str] = None,
        device: Optional[str] = None,
        compute_type: Optional[str] = None,
        preload: Optional[bool] = None,
    ):
        """
        `model_size` is retained for interface compatibility and is now
        only used for logging - the actual weights come from
        `model_dir` (default: models/whisper), because a bare size
        string like "base" makes faster-whisper resolve it against the
        Hugging Face Hub, which needs the network.

        Any argument left as None falls back to config.ini [voice]
        (stt_device / stt_compute_type / stt_model_size / preload).
        """
        # Must happen before faster_whisper / huggingface_hub are imported.
        enforce_offline()

        self.is_available = False
        self.last_error: Optional[str] = None
        self._model = None
        self._manager = None
        self.isolation = stt_isolation()
        self._load_lock = threading.Lock()
        self._model_size = model_size or config_str("stt_model_size", "base")
        self._device_pref = (device or config_str("stt_device", "auto") or "auto").lower()
        self._device = device or config_str("stt_device", "cpu") or "cpu"
        self._compute_type = compute_type or config_str("stt_compute_type", "int8") or "int8"
        if self._device == "auto" or self._compute_type == "auto" or self._model_size == "auto":
            # SETUP_VOICE.bat records what it VERIFIED with a real transcription
            # (e.g. large-v3-turbo on CUDA for an RTX GPU). Without that record
            # the safe choice is the CPU.
            verified = _verified_whisper_choice()
            if self._device == "auto":
                self._device = verified.get("device", "cpu")
            if self._compute_type == "auto":
                self._compute_type = verified.get("compute_type",
                                                  "float16" if self._device == "cuda" else "int8")
            if self._model_size == "auto":
                self._model_size = verified.get("model", "base")
        self._model_dir_arg = model_dir
        self.model_dir: Optional[Path] = None
        self.default_duration = config_float("listen_seconds", 2.8) or 2.8
        self.device_index = self._configured_device()
        self.capture_samplerate = DEFAULT_SAMPLERATE
        self.input_device: Optional[dict] = None
        self.microphone_available = False

        try:
            import sounddevice  # noqa: F401 - capture dependency, checked eagerly
            if self.isolation == "process":
                # Checked WITHOUT importing: importing faster_whisper loads
                # CTranslate2's native DLLs into this process, which is
                # exactly what the worker exists to avoid.
                import importlib.util
                if importlib.util.find_spec("faster_whisper") is None:
                    raise ImportError("faster-whisper is not installed")
            else:
                from faster_whisper import WhisperModel  # noqa: F401 - checked eagerly

            # Verify the local model BEFORE deciding it is available, whether
            # or not we preload: a missing model must be reported at startup,
            # not on the first spoken command.
            self.model_dir = require_model_dir(
                Path(self._model_dir_arg) if self._model_dir_arg else resolve_whisper_dir(),
                WHISPER_REQUIRED_FILES,
                "Whisper STT",
            )
            self._probe_microphone()

            should_preload = preload_models() if preload is None else preload
            if should_preload and BACKGROUND_PRELOAD:
                logger.info(f"STT: loading faster-whisper '{self._model_size}' in the background")
                threading.Thread(target=self._model_or_none, name="agni-stt-preload",
                                 daemon=True).start()
            elif should_preload:
                self._load_model()
            else:
                logger.info(
                    f"STT ready (lazy: faster-whisper '{self._model_size}' will load from "
                    f"{self.model_dir} on first use)"
                )
            self.is_available = True
        except MissingModelError as e:
            # Distinct from other failures: setup simply hasn't been run.
            self.last_error = str(e)
            logger.error(f"STT unavailable: {e}")
        except Exception as e:
            self.last_error = str(e)
            logger.warning(f"STT unavailable: {e}")

    # ------------------------------------------------------------- audio --

    @staticmethod
    def _configured_device():
        """config.ini [voice] input_device: an index, a name fragment, or
        empty for the system default."""
        raw = config_str("input_device", "")
        if not raw:
            return None
        if raw.lstrip("-").isdigit():
            return int(raw)
        for device in list_input_devices():
            if raw.lower() in device["name"].lower():
                return device["index"]
        logger.warning(f"No input device matches '{raw}' - using the system default")
        return None

    def _probe_microphone(self):
        """Find a usable capture rate BEFORE the first spoken command.

        A microphone that cannot open at 16 kHz is the single most common
        cause of "voice does nothing": PortAudio raises and the failure is
        only seen when the user presses the mic button. Probing here picks
        a rate the device actually supports and records it.
        """
        import sounddevice as sd

        devices = list_input_devices()
        if not devices:
            self.microphone_available = False
            logger.warning("No audio input device found - voice input will not work. "
                           "Check that a microphone is connected and, on Windows, "
                           "that microphone access is allowed in Privacy settings.")
            return

        self.input_device = (next((d for d in devices if d["index"] == self.device_index),
                                  None) if self.device_index is not None
                             else default_input_device() or devices[0])
        index = self.input_device["index"] if self.input_device else None
        candidates = [DEFAULT_SAMPLERATE]
        if self.input_device and self.input_device.get("default_samplerate"):
            candidates.append(self.input_device["default_samplerate"])
        candidates.extend([48000, 44100, 32000, 22050, 8000])

        for rate in dict.fromkeys(candidates):
            try:
                sd.check_input_settings(device=index, channels=1, samplerate=rate,
                                        dtype="float32")
                self.capture_samplerate = rate
                self.microphone_available = True
                note = "" if rate == DEFAULT_SAMPLERATE else " (resampled to 16 kHz)"
                logger.info(f"Microphone: {self.input_device['name']} @ {rate} Hz{note}")
                return
            except Exception:
                continue

        self.microphone_available = False
        logger.warning(f"Microphone '{self.input_device['name'] if self.input_device else '?'}' "
                       f"could not be opened at any tested sample rate")

    # ------------------------------------------------------------ loading --

    def _load_model(self):
        """Load the CTranslate2 model. Called at construction when preload is
        on (possibly on a background thread), otherwise on first transcription.
        One load at a time: a caller that arrives while it runs waits for it."""
        if self._model is not None:
            return self._model
        with self._load_lock:
            if self._model is not None:
                return self._model
            if self.isolation is None:
                self.isolation = stt_isolation()
            if self.isolation == "process":
                return self._load_worker()
            from faster_whisper import WhisperModel

            # One worker thread per PHYSICAL core. CTranslate2 defaults to the
            # logical count, and on a laptop i5 the hyperthreads contend for the
            # same vector units this workload is bound by - more threads, less
            # throughput. num_workers stays at 1: transcription here is one
            # utterance at a time, and a second worker only duplicates the model
            # in memory.
            threads = _physical_cores()
            if self._device == "cuda":
                from voice.cuda_dlls import register_cuda_dlls
                register_cuda_dlls()
            try:
                self._model = WhisperModel(
                    str(self.model_dir),
                    device=self._device,
                    compute_type=self._compute_type,
                    local_files_only=True,
                    cpu_threads=threads if self._device == "cpu" else 0,
                    num_workers=1,
                )
            except Exception as error:
                if self._device == "cpu":
                    raise
                # CUDA libraries missing or the GPU is busy: the CPU still works,
                # and saying so beats having no speech input at all.
                logger.warning(f"STT on {self._device} failed ({error}); using the CPU")
                self._device, self._compute_type = "cpu", "int8"
                self._model = WhisperModel(str(self.model_dir), device="cpu", compute_type="int8",
                                           local_files_only=True, cpu_threads=threads, num_workers=1)
            logger.info(
                f"STT ready (faster-whisper '{self._model_size}' from {self.model_dir}, "
                f"device={self._device}, compute_type={self._compute_type}"
                + (f", {threads} thread(s)" if self._device == "cpu" else "") + ")"
            )
            return self._model

    def _load_worker(self):
        """Start the Whisper worker on the first backend in the chain that
        loads (PRIMARY -> FALLBACK -> LAST_RESORT). Raises when none does;
        the main process is unaffected either way."""
        from voice.stt_manager import STTManager, build_chain
        preference = self._device_pref
        if preference == "auto" and _verified_whisper_choice().get("device") == "cpu":
            # Setup proved the GPU path does not work here - start on the CPU.
            preference = "cpu"
        compute = config_str("stt_compute_type", "auto") or "auto"
        chain = build_chain(self.model_dir, device_pref=preference, compute_pref=compute,
                            model_label=self._model_size)
        try:
            from core.paths import log_dir
            log_path = log_dir() / "stt_worker.log"
        except Exception:
            log_path = None
        manager = STTManager(chain, cpu_threads=_physical_cores(), log_path=log_path)
        try:
            backend = manager.ensure_loaded()
        except Exception:
            manager.close()
            raise
        self._manager = manager
        self._device, self._compute_type = backend.device, backend.compute_type
        self._model = _WorkerModel(manager)
        # The backend's own label: a LAST_RESORT is models/whisper-small, not
        # the configured size, and the log has to say which one is listening.
        logger.info(f"STT ready in a worker process ({backend.role}: faster-whisper "
                    f"'{backend.label or self._model_size}' from {backend.model_dir}, device={backend.device}, "
                    f"compute_type={backend.compute_type})")
        return self._model

    def _model_or_none(self):
        """Return the loaded model, loading it on demand. Returns None (and
        records the reason) instead of raising, so a lazy-load failure
        behaves exactly like an unavailable engine."""
        if self._model is not None:
            return self._model
        if not self.is_available:
            return None
        try:
            return self._load_model()
        except Exception as e:
            self.last_error = str(e)
            if any(marker in str(e).lower() for marker in _TRANSIENT_LOAD):
                logger.warning(f"STT model could not load now (will retry on the next request): {e}")
            else:
                self.is_available = False
                logger.warning(f"STT model failed to load: {e}")
            return None

    # ------------------------------------------------------- transcription --

    def listen_once(self, duration: Optional[float] = None,
                    samplerate: int = DEFAULT_SAMPLERATE) -> str:
        """Record `duration` seconds from the default mic and transcribe it.

        Returns "" if STT is unavailable or capture/transcription fails.
        No temporary file is created: faster-whisper accepts the raw
        float32 numpy waveform directly, which also removes the
        temp-file leak that occurred when transcription raised.
        """
        if not self.is_available:
            self.last_error = self.last_error or "speech recognition is not available"
            publish_stage("STT ERROR", self.last_error)
            return ""
        self.last_error = ""
        # No explicit duration (the microphone button, the voice loop): end
        # when the speaker stops, not after a fixed time. An explicit duration
        # (diagnostics, tests) still records exactly that long.
        end_of_speech = None
        if duration is None and config_bool("listen_until_silence", True):
            wait = self.default_duration
            longest = max(wait, config_float("listen_max_seconds", 15.0) or 15.0)
            end_of_speech = (wait, longest)
            duration = longest
        duration = self.default_duration if duration is None else duration
        if duration <= 0:
            logger.warning(f"Invalid recording duration {duration}; expected > 0")
            return ""

        if self._model is None:
            publish_stage("PREPARING RECOGNISER", "loading the local Whisper model - speak when LISTENING shows")
        model = self._model_or_none()
        if model is None:
            self.last_error = self.last_error or "the speech recogniser did not load"
            publish_stage("STT ERROR", self.last_error)
            return ""

        if not self.microphone_available:
            self._probe_microphone()      # a mic may have been plugged in since
            if not self.microphone_available:
                self.last_error = "no usable microphone"
                publish_stage("MIC ERROR", self.last_error)
                return ""

        try:
            import sounddevice as sd
            import numpy as np

            # Record at a rate the DEVICE supports, then resample - asking a
            # 44.1/48 kHz-only microphone for 16 kHz is what made capture fail.
            rate = self.capture_samplerate if samplerate == DEFAULT_SAMPLERATE else samplerate
            frames = int(duration * rate)
            device = self.input_device["index"] if self.input_device else None

            # A callback stream rather than sd.rec(): the same capture, but each
            # block's loudness is published as it arrives, so the interface's
            # wave responds to the operator's real voice while LISTENING.
            import threading as _threading
            chunks = []
            finished = _threading.Event()
            captured = [0]
            blocksize = max(256, rate // 20)
            detector = (EndOfSpeech(blocksize / float(rate), end_of_speech[0],
                                    max_seconds=end_of_speech[1])
                        if end_of_speech else None)

            heard = [False]

            def _on_block(block, _frames, _time, _status):
                chunks.append(block.copy())
                captured[0] += len(block)
                rms = float(np.sqrt(np.mean(np.square(block)))) if len(block) else 0.0
                publish_level(rms)
                if not heard[0] and rms >= SILENCE_RMS * 2:
                    heard[0] = True
                    publish_stage("SPEECH DETECTED")
                if captured[0] >= frames or (detector is not None and detector.feed(rms)):
                    finished.set()

            from core.assistant_state import State, assistant_state
            with assistant_state.activity(State.LISTENING, "listening"):
                if hasattr(sd, "InputStream"):
                    with sd.InputStream(samplerate=rate, channels=1, dtype="float32",
                                        device=device, callback=_on_block,
                                        blocksize=blocksize):
                        publish_stage("LISTENING", (self.input_device or {}).get("name", "default microphone"))
                        finished.wait(duration + 2.0)
                else:                               # minimal backends: blocking capture
                    publish_stage("LISTENING", (self.input_device or {}).get("name", "default microphone"))
                    chunks.append(sd.rec(frames, samplerate=rate, channels=1,
                                         dtype="float32", device=device))
                    sd.wait()
            publish_level(0.0)

            # Blocks are (frames, channels); Whisper wants a flat mono float32
            # array at 16 kHz.
            recording = np.concatenate(chunks) if chunks else np.zeros((0, 1), dtype=np.float32)
            audio = np.asarray(recording[:frames], dtype=np.float32).reshape(-1)
            if audio.size == 0:
                logger.warning("Microphone returned no audio")
                self.last_error = "the microphone returned no audio"
                publish_stage("MIC ERROR", self.last_error)
                return ""
            audio = _resample_to_16k(audio, rate)

            level = float(np.sqrt(np.mean(np.square(audio)))) if audio.size else 0.0
            if level < SILENCE_RMS:
                logger.info(f"Captured audio is silent (RMS {level:.5f}) - not transcribing")
                self.last_error = "silence"
                publish_stage("NO SPEECH", "the microphone was silent")
                return ""
            if not heard[0]:
                publish_stage("SPEECH DETECTED")            # blocking capture: known only afterwards

            publish_stage("TRANSCRIBING", f"{audio.size / DEFAULT_SAMPLERATE:.1f} s of audio")
            with assistant_state.activity(State.UNDERSTANDING, "transcribing speech"):
                segments, _info = _transcribe(model, audio)
                text = " ".join(seg.text.strip() for seg in segments).strip()
            if text:
                publish_stage("TRANSCRIBED", text[:80])
            else:
                self.last_error = "nothing recognisable was said"
                publish_stage("NO SPEECH", self.last_error)
            return text
        except Exception as e:
            hint = ""
            text = str(e).lower()
            if "invalid sample rate" in text or "invalid number of channels" in text:
                hint = " - the microphone rejected the requested format"
            elif "unanticipated host error" in text or "-9999" in text:
                hint = (" - PortAudio could not open the device. On Windows, check "
                        "Settings > Privacy & security > Microphone.")
            elif "no default input" in text:
                hint = " - no default input device is configured"
            self.last_error = f"{e}{hint}"
            logger.warning(f"STT capture failed: {e}{hint}")
            publish_stage("STT ERROR" if ("transcri" in text or "worker" in text) else "MIC ERROR", self.last_error)
            return ""

    def transcribe_array(self, audio, samplerate: int = DEFAULT_SAMPLERATE) -> str:
        """Transcribe an already-captured mono float32 waveform.

        Lets the UI layer own microphone capture (e.g. push-to-talk in
        PySide6) while reusing the same loaded model.
        """
        if not self.is_available:
            return ""
        model = self._model_or_none()
        if model is None:
            return ""
        try:
            import numpy as np

            data = np.asarray(audio, dtype=np.float32).reshape(-1)
            if data.size == 0:
                return ""
            if samplerate != DEFAULT_SAMPLERATE:
                logger.warning(
                    f"Audio is {samplerate} Hz but Whisper expects "
                    f"{DEFAULT_SAMPLERATE} Hz; resample before calling this."
                )
            segments, _info = _transcribe(model, data)
            return " ".join(seg.text.strip() for seg in segments).strip()
        except Exception as e:
            logger.warning(f"STT transcription failed: {e}")
            return ""

    def transcribe_file(self, path: str) -> str:
        """Transcribe an audio file already on disk (wav/mp3/flac/...).

        Used by the offline test and by any workflow that records audio
        elsewhere. faster-whisper decodes the file locally - no network.
        """
        if not self.is_available:
            return ""
        model = self._model_or_none()
        if model is None:
            return ""
        target = Path(path).expanduser()
        if not target.is_file():
            logger.warning(f"Audio file not found: {target}")
            return ""
        try:
            segments, _info = _transcribe(model, str(target))
            return " ".join(seg.text.strip() for seg in segments).strip()
        except Exception as e:
            logger.warning(f"STT file transcription failed: {e}")
            return ""

    def status(self) -> dict:
        """Everything the voice doctor needs, without loading anything."""
        return {
            "available": self.is_available,
            "last_error": self.last_error,
            "model_dir": str(self.model_dir) if self.model_dir else None,
            "model_loaded": self._model is not None,
            "device": self._device,
            "compute_type": self._compute_type,
            "isolation": self.isolation,
            "worker": self._manager.status() if self._manager is not None else None,
            "microphone_available": self.microphone_available,
            "microphone": self.input_device,
            "capture_samplerate": self.capture_samplerate,
            "input_devices": list_input_devices(),
        }

    def close(self):
        """Release the model (and stop the worker). Safe to call more than once."""
        self._model = None
        manager, self._manager = self._manager, None
        if manager is not None:
            manager.close()
