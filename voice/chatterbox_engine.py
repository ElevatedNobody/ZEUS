"""
ZEUS Chatterbox TTS engine

Wraps the Chatterbox model (https://github.com/resemble-ai/chatterbox):
device selection, one-time model loading, and text -> waveform
generation. Kept separate from `text_to_speech.py` so all
model/device/config concerns live in one place, and so `TextToSpeech`
stays a thin adapter that the rest of the app doesn't need to change.

OFFLINE: the model is loaded with `ChatterboxTTS.from_local()` from an
explicit local directory. `from_pretrained()` is never called, because
it calls `hf_hub_download()` for each checkpoint file and therefore
needs the network. See `voice/offline.py`.

Nothing here is machine-specific: device and voice/generation
parameters come from constructor args, falling back to environment
variables (see `ChatterboxConfig.from_env`).
"""

import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional, Tuple

from core.logger import get_logger

from .offline import (
    CHATTERBOX_REQUIRED_FILES,
    MissingModelError,
    enforce_offline,
    require_model_dir,
    resolve_chatterbox_dir,
)

logger = get_logger("chatterbox")


def _is_weights_only_error(error: Exception) -> bool:
    """Recognise the torch >= 2.6 `weights_only=True` unpickling refusal.

    torch words it as "Weights only load failed" (with a space) and raises
    UnpicklingError / WeightsUnpickler errors, so all three spellings are
    matched - an unpickling failure on our own local checkpoint is always
    this problem.
    """
    text = f"{type(error).__name__}: {error}".lower()
    return ("weights only" in text or "weights_only" in text
            or "weightsunpickler" in text or "unpicklingerror" in text)


def _env_str(name: str, default: str = "") -> str:
    import os
    return os.environ.get(name, default)


def _env_float(name: str, default: float) -> float:
    """Read a float env var, falling back (with a warning) if unparseable.

    A typo in an env var shouldn't crash startup, but it also shouldn't
    be silently ignored - hence the warning.
    """
    import os
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning(f"{name}={raw!r} is not a number; using default {default}")
        return default


@dataclass
class ChatterboxConfig:
    """Tunable Chatterbox generation settings.

    Environment overrides (applied by `from_env`, which
    `TextToSpeech` uses for any argument you don't pass explicitly):

        MEDHA_TTS_DEVICE        "cuda" | "cpu" | "mps" | "" (auto-detect)
        MEDHA_TTS_MODEL_DIR     local checkpoint dir (default: models/chatterbox)
        MEDHA_TTS_VOICE_PROMPT  path to a reference .wav for voice cloning
        MEDHA_TTS_EXAGGERATION  emotion intensity, default 0.5
        MEDHA_TTS_CFG_WEIGHT    pacing / prompt adherence, default 0.5
        MEDHA_TTS_TEMPERATURE   sampling temperature, default 0.8

    Chatterbox's own defaults (exaggeration=0.5, cfg_weight=0.5) are
    tuned by Resemble AI to sound natural for most prompts, so those
    are used here unless overridden.
    """
    device: str = ""
    voice_prompt: Optional[str] = None
    exaggeration: float = 0.5
    cfg_weight: float = 0.5
    temperature: float = 0.8
    model_dir: Optional[str] = None

    @classmethod
    def from_env(cls) -> "ChatterboxConfig":
        return cls(
            device=_env_str("MEDHA_TTS_DEVICE", ""),
            voice_prompt=_env_str("MEDHA_TTS_VOICE_PROMPT") or None,
            exaggeration=_env_float("MEDHA_TTS_EXAGGERATION", 0.5),
            cfg_weight=_env_float("MEDHA_TTS_CFG_WEIGHT", 0.5),
            temperature=_env_float("MEDHA_TTS_TEMPERATURE", 0.8),
            model_dir=_env_str("MEDHA_TTS_MODEL_DIR") or None,
        )


_THREADS_TUNED = False


def tune_cpu_threads() -> int:
    """Give torch the right number of threads for a laptop CPU.

    torch defaults to one thread per LOGICAL core. On a 4-core i5 that is 8
    threads for 4 real execution units, and the two hyperthreads on a core
    contend for the same vector units - which is precisely what this workload
    is bound by. The result is more scheduling overhead for less throughput.

    So: one thread per PHYSICAL core, and one for the inter-op pool, because
    speech generation here is a single sequential graph and a second op-level
    pool only adds contention.

    Returns the thread count actually set, and is safe to call repeatedly.
    """
    global _THREADS_TUNED
    if _THREADS_TUNED:
        return 0
    try:
        import torch
    except Exception:
        return 0

    physical = 0
    try:
        import psutil
        physical = psutil.cpu_count(logical=False) or 0
    except Exception:
        pass
    if not physical:
        import os
        logical = os.cpu_count() or 2
        physical = max(1, logical // 2)     # assume hyperthreading

    threads = max(1, min(physical, 8))
    try:
        torch.set_num_threads(threads)
        torch.set_num_interop_threads(1)
    except Exception as error:
        # set_num_interop_threads raises if anything has already run a
        # parallel region. Not worth failing over.
        logger.debug(f"thread tuning partially applied: {error}")
    try:
        torch.set_grad_enabled(False)       # inference only, never training
    except Exception:
        pass
    _THREADS_TUNED = True
    logger.info(f"Chatterbox CPU tuning: {threads} thread(s), gradients off")
    return threads


# Speech is generated one sentence at a time so playback can start on the
# first one. These bound how that splitting behaves.
MIN_CHUNK_CHARS = 60      # below this, merge with the next sentence: very
                          # short fragments sound clipped and cost a fixed
                          # per-call overhead that dominates the audio
MAX_CHUNK_CHARS = 280     # above this, the autoregressive pass gets long
                          # enough to hurt time-to-first-sound

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[])")


def _split_long(sentence: str) -> list:
    """Break one over-long sentence into pieces at or under the cap.

    Written as a single forward pass rather than "split the pieces that are
    still too long, repeatedly": that loop does not terminate on input with no
    break characters in it at all. `piece[:cap + 1]` is cap+1 characters long,
    which is still over the cap, so the same piece is examined forever. A 2000
    character word with no spaces - a pasted hash, a URL, a run of one
    repeated letter - hangs the speech thread, silently, because it happens
    inside a generator on a daemon thread.

    Preference order for where to cut: a clause break, then a word boundary,
    then a hard cut. Only the last of those can land mid-word, and only when
    there is no whitespace to use.
    """
    remaining = sentence.strip()
    pieces = []
    while len(remaining) > MAX_CHUNK_CHARS:
        window = remaining[:MAX_CHUNK_CHARS]
        cut = max(window.rfind(", "), window.rfind("; "), window.rfind(": "))
        if cut >= MIN_CHUNK_CHARS:
            cut += 1                       # keep the punctuation with the piece
        else:
            cut = window.rfind(" ")
        if cut < MIN_CHUNK_CHARS:
            # No usable break at all: cut hard, strictly inside the cap so the
            # remainder is always shorter than what came in.
            cut = MAX_CHUNK_CHARS
        head, remaining = remaining[:cut].strip(), remaining[cut:].strip()
        if head:
            pieces.append(head)
        if not head:
            break                          # nothing consumed - refuse to spin
    if remaining:
        pieces.append(remaining)
    return pieces


def split_for_speech(text: str) -> list:
    """Split text into chunks that are worth synthesising separately.

    Time-to-first-sound is what makes local TTS feel usable or broken. On a
    CPU-only laptop the whole reply might take a minute; the FIRST SENTENCE
    takes a few seconds, and once it is playing the rest can be generated
    behind it. So the unit of work is a sentence, not a reply.

    Sentences are merged up to MIN_CHUNK_CHARS (so "Yes." does not become its
    own model call) and split at MAX_CHUNK_CHARS on clause boundaries.
    """
    text = (text or "").strip()
    if not text:
        return []

    sentences = [s.strip() for s in _SENTENCE_END.split(text) if s.strip()]
    if not sentences:
        return []

    chunks, current = [], ""
    for sentence in sentences:
        for piece in _split_long(sentence):
            if current and len(current) + len(piece) + 1 > MAX_CHUNK_CHARS:
                chunks.append(current)
                current = piece
            elif len(current) < MIN_CHUNK_CHARS:
                current = f"{current} {piece}".strip()
            else:
                chunks.append(current)
                current = piece
    if current:
        chunks.append(current)
    return chunks


class ChatterboxLoadError(RuntimeError):
    """Raised when the Chatterbox model or its dependencies fail to load."""


class ChatterboxGenerationError(RuntimeError):
    """Raised when generating audio for a specific piece of text fails."""


class ChatterboxEngine:
    """Loads Chatterbox once, from local files, and turns text into audio.

    `generate()` is serialized with a lock since the underlying model
    holds mutable conditioning state (`self.conds` is rewritten when an
    audio prompt is supplied), so concurrent calls could interleave and
    corrupt each other. Loading happens exactly once, in `__init__`.
    """

    def __init__(self, config: Optional[ChatterboxConfig] = None):
        # Must happen before chatterbox / huggingface_hub are imported.
        enforce_offline()

        self.config = config or ChatterboxConfig.from_env()
        self._model = None
        self._lock = threading.Lock()
        # Measured speed, so the UI can report what this machine actually
        # does rather than a guess about the hardware.
        self._generations = 0
        self._compute_seconds = 0.0
        self._audio_seconds = 0.0
        self.device = self._resolve_device(self.config.device)
        if self.device == "cpu":
            tune_cpu_threads()

        if self.config.voice_prompt:
            prompt = Path(self.config.voice_prompt)
            if not prompt.is_file():
                raise ChatterboxLoadError(
                    f"Voice prompt file not found: '{prompt}'. Point "
                    "MEDHA_TTS_VOICE_PROMPT at an existing .wav, or unset it "
                    "to use Chatterbox's built-in voice."
                )

        self.model_dir = require_model_dir(
            Path(self.config.model_dir) if self.config.model_dir else resolve_chatterbox_dir(),
            CHATTERBOX_REQUIRED_FILES,
            "Chatterbox TTS",
        )
        self._load_model()

    @staticmethod
    def _resolve_device(requested: str) -> str:
        """Pick a device. Honors an explicit request but never assumes CUDA."""
        if requested:
            return requested
        try:
            import torch
            if torch.cuda.is_available():
                return "cuda"
            mps = getattr(torch.backends, "mps", None)
            if mps is not None and mps.is_available():
                return "mps"
        except Exception as e:
            logger.warning(f"Could not probe torch devices ({e}); defaulting to CPU")
        return "cpu"

    @staticmethod
    def _allow_local_checkpoint():
        """Context manager that lets torch load THIS project's own local
        Chatterbox checkpoint.

        torch 2.6 changed `torch.load` to default `weights_only=True`.
        Chatterbox's `conds.pt` is a pickled object graph, not a bare state
        dict, so on torch >= 2.6 the load raises
        "Weights only load failed / UnpicklingError" and TTS appears broken
        for no visible reason. This was the single most common Chatterbox
        failure after the torch 2.6 release.

        The relaxation is deliberately narrow: it applies only while this
        one call runs, only to the file the user themselves placed in
        models/chatterbox (verified present by require_model_dir), and it is
        logged. It is never enabled for arbitrary downloaded files, because
        an untrusted pickle is code execution.
        """
        import contextlib

        @contextlib.contextmanager
        def _patched():
            try:
                import torch
            except ImportError:
                yield
                return
            original = getattr(torch, "load", None)
            if original is None:      # partial/unusual torch build
                yield
                return

            def load(*args, **kwargs):
                kwargs.setdefault("weights_only", False)
                return original(*args, **kwargs)

            torch.load = load
            try:
                yield
            finally:
                torch.load = original

        return _patched()

    def _load_model(self):
        try:
            from chatterbox.tts import ChatterboxTTS
        except ImportError as e:
            raise ChatterboxLoadError(
                "chatterbox-tts is not installed. Install project requirements "
                "(see requirements.txt) to enable TTS."
            ) from e

        try:
            # from_local() reads only the files in self.model_dir - no network.
            self._model = ChatterboxTTS.from_local(self.model_dir, self.device)
        except Exception as e:
            if _is_weights_only_error(e):
                logger.warning(
                    "torch refused to unpickle the local Chatterbox checkpoint "
                    "(torch >= 2.6 defaults to weights_only=True). Retrying with "
                    f"full deserialisation for the local file in {self.model_dir}."
                )
                try:
                    with self._allow_local_checkpoint():
                        self._model = ChatterboxTTS.from_local(self.model_dir, self.device)
                    logger.info("Chatterbox checkpoint loaded after the torch 2.6 retry")
                    logger.info(f"Chatterbox TTS ready (device='{self.device}', "
                                f"model_dir='{self.model_dir}')")
                    return
                except Exception as retry_error:
                    e = retry_error
            # Most common failure: a GPU device was requested/auto-picked but
            # drivers/VRAM aren't usable. Retry once on CPU before giving up.
            if self.device != "cpu":
                logger.warning(
                    f"Chatterbox failed to load on device '{self.device}' ({e}); "
                    "retrying on CPU"
                )
                try:
                    self.device = "cpu"
                    self._model = ChatterboxTTS.from_local(self.model_dir, "cpu")
                except Exception as cpu_e:
                    raise ChatterboxLoadError(
                        f"Chatterbox model failed to load on CPU as well: {cpu_e}"
                    ) from cpu_e
            else:
                raise ChatterboxLoadError(f"Chatterbox model failed to load: {e}") from e

        logger.info(
            f"Chatterbox TTS ready (device='{self.device}', model_dir='{self.model_dir}')"
        )

    @property
    def sample_rate(self) -> int:
        if self._model is None:
            raise ChatterboxLoadError("Chatterbox model is not loaded")
        return self._model.sr

    def generate(self, text: str):
        """Generate a mono float32 numpy waveform for `text`.

        Returns (audio: np.ndarray, sample_rate: int). Raises
        ChatterboxGenerationError on invalid input or a model failure -
        callers should catch it rather than assume success.
        """
        if not isinstance(text, str) or not text.strip():
            raise ChatterboxGenerationError(
                f"Cannot generate speech for empty or non-string input "
                f"(got {type(text).__name__})"
            )
        if self._model is None:
            raise ChatterboxGenerationError("Chatterbox model is not loaded")

        import numpy as np

        gen_kwargs = dict(
            exaggeration=self.config.exaggeration,
            cfg_weight=self.config.cfg_weight,
            temperature=self.config.temperature,
        )
        if self.config.voice_prompt:
            gen_kwargs["audio_prompt_path"] = self.config.voice_prompt

        with self._lock:
            try:
                # Punctuation is preserved deliberately: Chatterbox normalises
                # it internally (punc_norm) and uses it for pauses, sentence
                # boundaries and emphasis. Only surrounding whitespace is cut.
                wav = self._model.generate(text.strip(), **gen_kwargs)
            except TypeError as e:
                # Defensive: some chatterbox-tts versions may not accept every
                # kwarg. Retry with the long-standing subset rather than fail.
                logger.warning(
                    f"Chatterbox rejected a generation kwarg ({e}); "
                    "retrying with reduced parameters"
                )
                fallback_kwargs = {
                    k: v for k, v in gen_kwargs.items()
                    if k in ("exaggeration", "cfg_weight", "audio_prompt_path")
                }
                try:
                    wav = self._model.generate(text.strip(), **fallback_kwargs)
                except Exception as retry_e:
                    raise ChatterboxGenerationError(
                        f"Chatterbox failed to generate audio: {retry_e}"
                    ) from retry_e
            except Exception as e:
                raise ChatterboxGenerationError(
                    f"Chatterbox failed to generate audio: {e}"
                ) from e

        # Chatterbox returns a torch tensor shaped (1, samples).
        # Flatten to mono float32 for playback.
        try:
            audio = wav.detach().cpu().numpy()
        except AttributeError:
            audio = np.asarray(wav)
        if audio.ndim > 1:
            audio = audio.mean(axis=0)
        audio = np.asarray(audio, dtype=np.float32)

        if audio.size == 0:
            raise ChatterboxGenerationError(
                "Chatterbox returned an empty waveform for the supplied text"
            )
        return audio, self.sample_rate

    def generate_stream(self, text: str) -> Iterator[Tuple[object, int, int, int]]:
        """Yield (audio, sample_rate, index, total) one chunk at a time.

        This is the difference between local TTS being usable and being
        unusable on a CPU-only laptop. Generating a three-sentence reply as
        one blob means silence for as long as the whole thing takes; yielding
        per sentence lets the caller start playing the first one while the
        second is still being generated.

        A chunk that fails is skipped with a warning rather than killing the
        rest of the reply - one awkward sentence should not swallow the
        answer.
        """
        chunks = split_for_speech(text)
        if not chunks:
            return
        total = len(chunks)
        for index, chunk in enumerate(chunks, start=1):
            started = time.time()
            try:
                audio, sample_rate = self.generate(chunk)
            except ChatterboxGenerationError as error:
                logger.warning(f"skipping chunk {index}/{total}: {error}")
                continue
            seconds = len(audio) / float(sample_rate or 1)
            elapsed = time.time() - started
            self._record_speed(elapsed, seconds)
            logger.debug(f"chunk {index}/{total}: {seconds:.1f}s of audio in "
                         f"{elapsed:.1f}s (RTF {elapsed / max(seconds, 0.01):.1f}x)")
            yield audio, sample_rate, index, total

    def _record_speed(self, elapsed: float, audio_seconds: float):
        """Remember how fast this machine actually is.

        Reported by the voice status so the speed claim comes from generations
        that happened, not from an assumption about the hardware.
        """
        if audio_seconds <= 0:
            return
        self._generations += 1
        self._compute_seconds += elapsed
        self._audio_seconds += audio_seconds

    @property
    def realtime_factor(self) -> Optional[float]:
        """Seconds of compute per second of audio, measured. None until this
        engine has actually generated something."""
        if self._audio_seconds <= 0:
            return None
        return self._compute_seconds / self._audio_seconds

    WARMUP_PHRASE = "Ready."

    def warmup(self) -> Optional[float]:
        """Generate one short phrase so the first real reply is not the slow one.

        The first call through the model pays for lazy graph construction,
        kernel selection and allocator warm-up on top of the actual work. Left
        until the user's first question, that cost lands exactly where it is
        most noticeable.

        Returns how long the warm-up took, or None if it could not run.
        """
        try:
            started = time.time()
            self.generate(self.WARMUP_PHRASE)
            elapsed = time.time() - started
            logger.info(f"Chatterbox warm-up took {elapsed:.1f}s")
            return elapsed
        except Exception as error:
            logger.warning(f"Chatterbox warm-up failed: {error}")
            return None

    def close(self):
        """Release the model and free GPU memory if it was used."""
        self._model = None
        try:
            import torch
            if self.device == "cuda" and torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass
