"""
ZEUS offline mode + local model resolution.

This module is the single place that decides:
  1. WHERE voice models live on disk (never a Hugging Face repo id),
  2. THAT no library is allowed to reach the network at runtime, and
  3. WHAT the voice subsystem is configured to do (config.ini [voice]).

Why this exists
---------------
Both `faster-whisper` and `chatterbox-tts` will happily download model
weights from huggingface.co on first use. That is fine during setup,
but at runtime it means a machine with no network can fail to start.
Everything here removes that possibility:

  * `enforce_offline()` sets the environment flags that make
    huggingface_hub / transformers refuse to hit the network at all.
    It must run BEFORE those libraries are imported, which is why the
    engines import them lazily and call this first.
  * `resolve_*_dir()` returns an explicit local directory. The engines
    load from that directory only - `from_pretrained()` (which
    downloads) is never called at runtime.
  * `require_model_dir()` fails loudly with an actionable message
    instead of silently downloading.

Only `scripts/setup_models.py` is allowed to touch the network, and it
deliberately calls `allow_network()` instead of `enforce_offline()`.

Resolution order for every setting: explicit argument > environment
variable > config.ini > built-in default. That keeps the documented
MEDHA_* variables working while letting the project be configured in
one place.
"""

import configparser
import os
from pathlib import Path
from typing import Dict, Iterable, List, Optional

# Project root = parent of the package this file lives in.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

CONFIG_PATH = PROJECT_ROOT / "config.ini"

# Default local model store. Override with MEDHA_MODELS_DIR to keep
# weights outside the project (e.g. on a shared drive) without code changes.
DEFAULT_MODELS_DIR = PROJECT_ROOT / "models"

# Files each model directory must contain to be considered complete.
# Sourced from the libraries themselves:
#   chatterbox.tts.ChatterboxTTS.from_local() loads exactly these.
#   faster_whisper expects a CTranslate2 model dir.
CHATTERBOX_REQUIRED_FILES = (
    "ve.safetensors",
    "t3_cfg.safetensors",
    "s3gen.safetensors",
    "tokenizer.json",
    "conds.pt",
)
WHISPER_REQUIRED_FILES = (
    "model.bin",
    "config.json",
    "tokenizer.json",
)

SETUP_HINT = "Run the setup process first: python scripts/setup_models.py"


class MissingModelError(RuntimeError):
    """Raised when a required model is not present in the local model store.

    Deliberately distinct from a load/runtime failure so callers (and
    the user) can tell "you never ran setup" apart from "the model is
    there but broken".
    """


def enforce_offline() -> None:
    """Forbid all outbound model/telemetry traffic for this process.

    Must be called before huggingface_hub, transformers, faster_whisper
    or chatterbox are imported. Safe to call repeatedly.

    These flags are what actually make the "unplug the network" test
    pass: without them, huggingface_hub still performs a metadata
    request on load and can raise instead of using its cache.
    """
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    os.environ.setdefault("HF_DATASETS_OFFLINE", "1")
    # Telemetry / update pings.
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    os.environ.setdefault("HF_HUB_DISABLE_IMPLICIT_TOKEN", "1")
    os.environ.setdefault("DISABLE_TELEMETRY", "1")
    # Gradio ships as a chatterbox dependency; stop it phoning home if
    # anything ever imports it.
    os.environ.setdefault("GRADIO_ANALYTICS_ENABLED", "False")


def is_offline_enforced() -> bool:
    """True if enforce_offline() has taken effect in this process."""
    return os.environ.get("HF_HUB_OFFLINE") == "1"


# Flags that block downloads. Cleared by allow_network() during setup.
_OFFLINE_FLAGS = (
    "HF_HUB_OFFLINE",
    "TRANSFORMERS_OFFLINE",
    "HF_DATASETS_OFFLINE",
)


def allow_network() -> None:
    """Clear offline flags so downloads can happen. SETUP ONLY.

    Without this, a machine that exports HF_HUB_OFFLINE=1 globally (or a
    process that already called enforce_offline()) would make the setup
    script fail with a misleading "check your internet connection" error
    even on a perfectly good connection.

    Never call this from runtime code.
    """
    for flag in _OFFLINE_FLAGS:
        os.environ.pop(flag, None)


# --------------------------------------------------------------------------
# config.ini [voice]
# --------------------------------------------------------------------------

_CONFIG_DEFAULTS: Dict[str, str] = {
    "enabled": "true",
    "stt_engine": "whisper",
    "tts_engine": "chatterbox",
    "preload": "true",          # load models at startup instead of first use
    "stt_device": "cpu",
    "stt_compute_type": "int8",
    "stt_model_size": "base",   # label only - weights come from the local dir
    "listen_seconds": "2.8",
    "tts_device": "",           # "" = auto-detect (cuda > mps > cpu)
    "tts_volume": "1.0",
    "tts_voice_prompt": "",
    "tts_exaggeration": "",
    "tts_cfg_weight": "",
    "tts_temperature": "",
    # -------------------------------------------------------- Chatterbox --
    "voice_profile": "medha",     # models/voice/profiles/<name>/
    "voice_device": "auto",        # auto | cpu | cuda | mps
    "voice_streaming": "true",     # speak sentence 1 while making sentence 2
    "voice_interruptible": "true", # a new request stops the current one
    "voice_fallback": "true",      # next provider if Chatterbox fails
    "voice_style": "true",         # ask the model for speakable phrasing
    "voice_language": "en",        # the profile's language
    "voice_speed": "0.95",         # 0.9 measured, 1.1 brisk
    "voice_auto_play": "true",     # speak replies without being asked to
    "voice_polish": "true",        # de-click and level chunks (light touch)
}

_config_cache: Optional[Dict[str, str]] = None


def voice_config(refresh: bool = False) -> Dict[str, str]:
    """Read the [voice] section of config.ini, with defaults filled in.

    Missing file or missing section is not an error - the defaults are
    the documented behaviour, and ZEUS must start on a fresh checkout.
    """
    global _config_cache
    if _config_cache is not None and not refresh:
        return _config_cache

    values = dict(_CONFIG_DEFAULTS)
    if CONFIG_PATH.exists():
        parser = configparser.ConfigParser()
        try:
            parser.read(CONFIG_PATH, encoding="utf-8")
            if parser.has_section("voice"):
                for key, value in parser.items("voice"):
                    values[key.strip().lower()] = value.strip()
        except (configparser.Error, OSError):
            pass  # a broken config must not stop ZEUS from starting
    _config_cache = values
    return values


def config_bool(key: str, default: bool = False) -> bool:
    raw = str(voice_config().get(key, "")).strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return default


def config_float(key: str, default: Optional[float] = None) -> Optional[float]:
    raw = str(voice_config().get(key, "")).strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def config_str(key: str, default: str = "") -> str:
    return str(voice_config().get(key, "") or default).strip()


def voice_enabled() -> bool:
    """Master switch. `--voice` still has to be passed; this can turn the
    subsystem off entirely on machines with no audio hardware."""
    return config_bool("enabled", True)


def preload_models() -> bool:
    """True = load Whisper/Chatterbox during startup (Phase 16 default).
    False = lazy-load on first use, for memory-constrained machines."""
    return config_bool("preload", True)


# --------------------------------------------------------------------------
# model locations
# --------------------------------------------------------------------------

def _configured_models_dir() -> Optional[Path]:
    """models_dir from config.ini [paths], resolved against the project root
    so ZEUS finds its models no matter which directory it was launched from."""
    if not CONFIG_PATH.exists():
        return None
    parser = configparser.ConfigParser()
    try:
        parser.read(CONFIG_PATH, encoding="utf-8")
        raw = parser.get("paths", "models_dir", fallback="").strip()
    except (configparser.Error, OSError):
        return None
    if not raw:
        return None
    candidate = Path(raw)
    return candidate if candidate.is_absolute() else (PROJECT_ROOT / candidate)


def resolve_models_dir() -> Path:
    """Root of the local model store.

    MEDHA_MODELS_DIR > config.ini [paths] models_dir > <project>/models.
    Always absolute, so the working directory never matters.
    """
    override = os.environ.get("MEDHA_MODELS_DIR")
    if override:
        return Path(override).expanduser().resolve()
    configured = _configured_models_dir()
    if configured:
        return configured.expanduser().resolve()
    return DEFAULT_MODELS_DIR.resolve()


def resolve_whisper_dir() -> Path:
    """Local directory holding the CTranslate2 Whisper model."""
    override = os.environ.get("MEDHA_STT_MODEL_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return resolve_models_dir() / "whisper"


def resolve_chatterbox_dir() -> Path:
    """Local directory holding the Chatterbox checkpoint files."""
    override = os.environ.get("MEDHA_TTS_MODEL_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return resolve_models_dir() / "chatterbox"


def missing_files(directory: Path, required: Iterable[str]) -> List[str]:
    """Return the subset of `required` that is absent from `directory`."""
    if not directory.is_dir():
        return list(required)
    return [name for name in required if not (directory / name).is_file()]


def require_model_dir(directory: Path, required: Iterable[str], label: str) -> Path:
    """Return `directory`, or raise MissingModelError explaining what's absent.

    Never downloads. This is the behaviour the offline requirement
    depends on: a missing model is a loud, actionable error, not a
    silent 2 GB download.
    """
    directory = Path(directory).expanduser().resolve()
    if not directory.is_dir():
        raise MissingModelError(
            f"Required {label} model not found locally at '{directory}'. {SETUP_HINT}"
        )
    absent = missing_files(directory, required)
    if absent:
        raise MissingModelError(
            f"{label} model at '{directory}' is incomplete - missing: "
            f"{', '.join(absent)}. {SETUP_HINT}"
        )
    return directory


# --------------------------------------------------------------------------
# status reporting (used by startup, the offline test and the BAT file)
# --------------------------------------------------------------------------

def check_voice_models() -> Dict[str, dict]:
    """Report which local voice models are present and complete.

    Pure filesystem inspection: imports nothing heavy, downloads nothing,
    and is safe to call at startup on a machine with no torch installed.
    """
    whisper_dir = resolve_whisper_dir()
    chatterbox_dir = resolve_chatterbox_dir()
    whisper_missing = missing_files(whisper_dir, WHISPER_REQUIRED_FILES)
    chatterbox_missing = missing_files(chatterbox_dir, CHATTERBOX_REQUIRED_FILES)
    return {
        "whisper": {
            "directory": str(whisper_dir),
            "present": not whisper_missing,
            "missing": whisper_missing,
            "required": list(WHISPER_REQUIRED_FILES),
        },
        "chatterbox": {
            "directory": str(chatterbox_dir),
            "present": not chatterbox_missing,
            "missing": chatterbox_missing,
            "required": list(CHATTERBOX_REQUIRED_FILES),
        },
    }


def _has(module: str) -> bool:
    import importlib.util
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        return False


def voice_status() -> dict:
    """Everything the rest of ZEUS needs to know about the voice subsystem
    without constructing (and therefore loading) it."""
    models = check_voice_models()
    return {
        "enabled": voice_enabled(),
        "preload": preload_models(),
        "offline_enforced": is_offline_enforced(),
        "models_dir": str(resolve_models_dir()),
        "stt": {
            "engine": "faster-whisper (local)",
            "library_installed": _has("faster_whisper"),
            "model": models["whisper"],
        },
        "tts": {
            "engine": "Chatterbox (local)",
            "library_installed": _has("chatterbox"),
            "torch_installed": _has("torch"),
            "model": models["chatterbox"],
        },
        "audio_io": {"sounddevice": _has("sounddevice"), "soundfile": _has("soundfile")},
        "external_voice_services": "DISABLED",
    }


# NOTE: the startup banner used to live here, and it printed
#
#     STT: LOCAL WHISPER (READY)
#     TTS: LOCAL CHATTERBOX (READY)
#
# from nothing but a directory listing. Files being present is not the same as
# a model that loads, and it named two engines whether or not they were the
# ones in use. The banner is now `voice.providers.voice_manager.banner()`,
# which prints the provider that actually loaded and nothing else.
#
# What remains in this module is filesystem truth - which local model
# directories are complete - used by the setup and doctor scripts.
