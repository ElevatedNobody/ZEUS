"""
ZEUS Text-to-Speech
Uses Chatterbox (offline, GPU-accelerated when available) for natural,
expressive speech instead of the previous SAPI-based engine. If the
dependencies aren't installed, the model can't load, or no audio
device exists, `is_available` is False and ZEUS silently stays in
text-only mode - nothing crashes. The reason is kept in `last_error`.

Model loading, device selection, and generation parameters live in
`chatterbox_engine.py`. This module only adapts that engine to the
project's existing TextToSpeech interface (speak/stop/is_available)
so callers elsewhere in the app don't need to change.

Loading strategy (config.ini [voice] preload):
  preload = true   load Chatterbox at startup, keep it resident, so the
                   first spoken reply is not delayed by a model load.
  preload = false  verify the model files at startup but load the model
                   on the first speak() - for memory-constrained machines.
Either way the model is loaded AT MOST ONCE per process.
"""

import queue
import threading
from typing import Optional

from core.logger import get_logger

from .chatterbox_engine import (
    ChatterboxConfig,
    ChatterboxEngine,
    ChatterboxGenerationError,
    ChatterboxLoadError,
)
from .offline import (
    CHATTERBOX_REQUIRED_FILES,
    MissingModelError,
    config_float,
    config_str,
    preload_models,
    require_model_dir,
    resolve_chatterbox_dir,
)

logger = get_logger("tts")


class TextToSpeech:
    def __init__(
        self,
        rate: int = 175,
        volume: Optional[float] = None,
        voice_prompt: Optional[str] = None,
        exaggeration: Optional[float] = None,
        cfg_weight: Optional[float] = None,
        temperature: Optional[float] = None,
        device: Optional[str] = None,
        model_dir: Optional[str] = None,
        preload: Optional[bool] = None,
        warmup: bool = True,
    ):
        """
        `rate` and `volume` are kept so existing call sites (e.g.
        `TextToSpeech()`) don't need to change:
          - `rate` has NO effect: Chatterbox has no speaking-rate knob
            (pacing is influenced by `cfg_weight` instead). Accepted
            only so existing calls don't break.
          - `volume` works; it scales the generated waveform before
            playback.

        The Chatterbox tunables default to None, meaning "not
        specified" - those fall back to config.ini [voice], then to the
        MEDHA_TTS_* environment variables, then to Chatterbox's own
        defaults. Passing a value explicitly always wins.
        """
        self.is_available = False
        self.last_error: Optional[str] = None
        self._rate = rate  # accepted for compatibility; not used by Chatterbox
        self._engine: Optional[ChatterboxEngine] = None
        self._load_failed = False
        # Speech runs on its own thread so the TEXT reply reaches the UI
        # immediately instead of after playback finishes. One worker, one
        # queue: replies are spoken in order and never overlap.
        self._speech_queue: "queue.Queue[Optional[str]]" = queue.Queue()
        self._speech_thread: Optional[threading.Thread] = None
        self._speech_lock = threading.Lock()
        # Interrupts the per-sentence generation loop, not just playback.
        self._stop = threading.Event()
        # How much of a long answer is worth saying out loud. The whole thing
        # is always on screen; reading two pages aloud on a CPU takes minutes.
        self._max_spoken_chars = int(config_float("max_spoken_chars", 600) or 600)

        if volume is None:
            volume = config_float("tts_volume", 1.0)
        self.volume = max(0.0, min(float(volume if volume is not None else 1.0), 2.0))

        if rate != 175:
            logger.info(
                f"TTS 'rate' ({rate}) is ignored: Chatterbox has no rate control. "
                "Use cfg_weight to influence pacing."
            )

        # Start from the environment (ChatterboxConfig.from_env), then let
        # config.ini fill any gap, then let explicit args override. This is
        # what makes both the documented MEDHA_TTS_* variables and the
        # config file work without one silently defeating the other.
        config = ChatterboxConfig.from_env()
        if not config.device:
            config.device = config_str("tts_device", "")
        if config.voice_prompt is None:
            config.voice_prompt = config_str("tts_voice_prompt", "") or None
        for name, key in (("exaggeration", "tts_exaggeration"),
                          ("cfg_weight", "tts_cfg_weight"),
                          ("temperature", "tts_temperature")):
            configured = config_float(key, None)
            if configured is not None:
                setattr(config, name, configured)

        if device is not None:
            config.device = device
        if voice_prompt is not None:
            config.voice_prompt = voice_prompt
        if exaggeration is not None:
            config.exaggeration = exaggeration
        if cfg_weight is not None:
            config.cfg_weight = cfg_weight
        if temperature is not None:
            config.temperature = temperature
        if model_dir is not None:
            config.model_dir = model_dir
        self.config = config

        try:
            import sounddevice  # noqa: F401 - playback dependency, checked eagerly

            # Verify the local checkpoint before declaring availability, even
            # in lazy mode: "you never ran setup" must surface at startup.
            from pathlib import Path
            self.model_dir = require_model_dir(
                Path(config.model_dir) if config.model_dir else resolve_chatterbox_dir(),
                CHATTERBOX_REQUIRED_FILES,
                "Chatterbox TTS",
            )
            should_preload = preload_models() if preload is None else preload
            if should_preload:
                self._engine = ChatterboxEngine(config)
                logger.info("TTS engine ready (Chatterbox, preloaded)")
                # Warm it on a background thread. The first pass through the
                # model pays for lazy graph construction and kernel selection
                # on top of the real work; left until the user's first
                # question, that cost lands where it is most noticeable. Doing
                # it here does not delay startup, because nothing waits on it.
                #
                # `warmup=False` skips it - for tests that count generations,
                # and for a machine tight enough on memory that one extra
                # forward pass at startup is not worth it.
                if warmup:
                    threading.Thread(target=self._engine.warmup,
                                     name="medha-tts-warmup", daemon=True).start()
            else:
                logger.info(
                    f"TTS ready (lazy: Chatterbox will load from {self.model_dir} "
                    "on first speak)"
                )
            self.is_available = True
        except MissingModelError as e:
            # Setup hasn't been run - surface loudly, it's actionable.
            self.last_error = str(e)
            logger.error(f"TTS unavailable: {e}")
        except Exception as e:
            # Covers ChatterboxLoadError (bad install / broken weights) and
            # sounddevice/PortAudio issues - either way we degrade quietly
            # so the app stays usable in text-only mode.
            self.last_error = str(e)
            logger.warning(f"TTS unavailable: {e}")

    # ------------------------------------------------------------ engine --

    def _engine_or_none(self) -> Optional[ChatterboxEngine]:
        """Return the engine, constructing it on first use in lazy mode.

        A failed lazy load disables TTS exactly like a failed preload -
        and is attempted only once, so a broken install does not retry a
        multi-second model load on every single reply.
        """
        if self._engine is not None:
            return self._engine
        if not self.is_available or self._load_failed:
            return None
        try:
            self._engine = ChatterboxEngine(self.config)
            logger.info("TTS engine ready (Chatterbox, loaded on demand)")
            return self._engine
        except (ChatterboxLoadError, MissingModelError, Exception) as e:
            self._load_failed = True
            self.is_available = False
            self.last_error = str(e)
            logger.warning(f"TTS unavailable: {e}")
            return None

    # ------------------------------------------------------------ speaking --

    def speak(self, text: str):
        """Synthesize and play `text`, a sentence at a time.

        Each chunk starts playing as soon as it exists, while the next one is
        generated behind it. On a CPU-only laptop generating a whole reply
        before making any sound means many seconds of silence; this way the
        wait is only for the first sentence, and the rest is hidden behind
        playback.

        Blocks until playback finishes, so `speak_async` still serialises
        replies and they never talk over each other.
        """
        # Guard non-string input before touching it: previously
        # `text.strip()` here raised AttributeError on None.
        if not self.is_available or not isinstance(text, str) or not text.strip():
            return
        engine = self._engine_or_none()
        if engine is None:
            return

        spoken = self._speakable(text)
        if not spoken:
            return

        try:
            import sounddevice as sd
            import numpy as np

            played = 0
            for audio, sample_rate, index, total in engine.generate_stream(spoken):
                if self._stop.is_set():
                    break
                if self.volume != 1.0:
                    audio = np.clip(audio * self.volume, -1.0, 1.0)
                # Wait for the PREVIOUS chunk to finish before queueing this
                # one, so the sentences play back to back instead of cutting
                # each other off - but the generation above already happened
                # while that chunk was playing.
                sd.wait()
                if self._stop.is_set():
                    break
                sd.play(audio, samplerate=sample_rate)
                played += 1
            sd.wait()
            if played:
                factor = engine.realtime_factor
                if factor is not None:
                    logger.debug(f"spoke {played} chunk(s), {factor:.1f}x real time")
        except ChatterboxGenerationError as e:
            # A bad sentence shouldn't disable the whole engine.
            logger.warning(f"TTS speak failed: {e}")
        except Exception as e:
            logger.warning(f"TTS playback failed: {e}")
            self.is_available = False
            self.last_error = str(e)

    @property
    def max_spoken_chars(self) -> int:
        return self._max_spoken_chars

    def _speakable(self, text: str) -> str:
        """What is actually worth saying out loud.

        A grounded answer over a plant document can be two pages long, and on
        a CPU-only laptop reading all of it aloud takes minutes that nobody
        wants to sit through - the full text is on screen the whole time.
        So speech is capped at a sensible length, cut at a sentence boundary,
        and the fact that it was shortened is said rather than hidden.
        """
        text = text.strip()
        limit = self.max_spoken_chars
        if limit <= 0 or len(text) <= limit:
            return text

        cut = text.rfind(". ", 0, limit)
        if cut < limit // 2:                 # no sentence break in range
            cut = text.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        return text[:cut + 1].strip() + " The rest of the answer is on screen."

    # ------------------------------------------------------ async speaking --

    def speak_async(self, text: str):
        """Queue `text` for playback and return immediately.

        Used by the main pipeline so a spoken answer never delays the written
        one. Playback still happens strictly in order.
        """
        if not self.is_available or not isinstance(text, str) or not text.strip():
            return
        with self._speech_lock:
            if self._speech_thread is None or not self._speech_thread.is_alive():
                self._speech_thread = threading.Thread(
                    target=self._speech_worker, name="medha-tts", daemon=True)
                self._speech_thread.start()
        self._speech_queue.put(text)

    def _speech_worker(self):
        while True:
            try:
                text = self._speech_queue.get(timeout=300)
            except queue.Empty:
                return          # idle - the thread restarts on the next request
            if text is None:
                return
            try:
                self.speak(text)
            except Exception as e:      # never let the worker die silently
                logger.warning(f"TTS worker error: {e}")
            finally:
                self._speech_queue.task_done()

    def wait_until_spoken(self, timeout: Optional[float] = None):
        """Block until the speech queue drains - used by tests and shutdown."""
        if self._speech_thread is None:
            return
        self._speech_queue.join()

    def synthesize(self, text: str):
        """Return (audio, sample_rate) without playing it.

        Useful for the UI layer (saving to file, streaming, or driving a
        waveform widget). Returns None if TTS is unavailable or generation
        fails.
        """
        if not self.is_available or not isinstance(text, str) or not text.strip():
            return None
        engine = self._engine_or_none()
        if engine is None:
            return None
        try:
            return engine.generate(text)
        except ChatterboxGenerationError as e:
            logger.warning(f"TTS synthesis failed: {e}")
            return None

    def save_to_file(self, text: str, path: str) -> bool:
        """Synthesize `text` and write it to a .wav file. Returns success.

        Local only - uses soundfile if present, otherwise the stdlib wave
        module, so no extra dependency is required.
        """
        result = self.synthesize(text)
        if result is None:
            return False
        audio, sample_rate = result
        try:
            import numpy as np
            try:
                import soundfile as sf
                sf.write(str(path), audio, sample_rate)
            except ImportError:
                import wave
                pcm = np.clip(audio, -1.0, 1.0)
                pcm = (pcm * 32767.0).astype("<i2")
                with wave.open(str(path), "wb") as fh:
                    fh.setnchannels(1)
                    fh.setsampwidth(2)
                    fh.setframerate(int(sample_rate))
                    fh.writeframes(pcm.tobytes())
            return True
        except Exception as e:
            logger.warning(f"TTS save failed: {e}")
            return False

    def stop(self):
        """Stop playback and drop anything still queued.

        Sets the stop flag first: a reply is now generated chunk by chunk, so
        stopping has to interrupt the loop that is still producing sentences,
        not just silence the one currently playing.
        """
        self._stop.set()
        while True:
            try:
                self._speech_queue.get_nowait()
                self._speech_queue.task_done()
            except queue.Empty:
                break
        if self.is_available:
            try:
                import sounddevice as sd
                sd.stop()
            except Exception as e:
                logger.warning(f"TTS stop failed: {e}")
        # Cleared immediately: stop() ends the CURRENT utterance, it does not
        # mute the engine for everything that follows.
        self._stop.clear()

    def close(self):
        """Release the model (frees GPU memory)."""
        self.stop()
        if self._engine is not None:
            self._engine.close()
            self._engine = None
        self.is_available = False
