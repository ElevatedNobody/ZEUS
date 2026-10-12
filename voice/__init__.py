"""
ZEUS voice package: speech-to-text and text-to-speech.

Exposes the public API at package level so callers can do:

    from voice import SpeechToText, TextToSpeech

Imports are lazy (via __getattr__) so that merely importing this
package does not pull in torch / faster-whisper - that keeps app
startup fast and lets code check `voice.offline` settings before the
heavy ML libraries are loaded.
"""

from .offline import (
    MissingModelError,
    check_voice_models,
    enforce_offline,
    is_offline_enforced,
    preload_models,
    resolve_chatterbox_dir,
    resolve_models_dir,
    resolve_whisper_dir,
    voice_config,
    voice_enabled,
    voice_status,
)
from .providers import ProviderState, VoiceState, voice_manager

__all__ = [
    "SpeechToText",
    "TextToSpeech",
    "voice_manager",
    "VoiceState",
    "ProviderState",
    "ChatterboxEngine",
    "ChatterboxConfig",
    "ChatterboxLoadError",
    "ChatterboxGenerationError",
    "MissingModelError",
    "enforce_offline",
    "is_offline_enforced",
    "resolve_models_dir",
    "resolve_whisper_dir",
    "resolve_chatterbox_dir",
    "check_voice_models",
    "voice_status",
    "voice_config",
    "voice_enabled",
    "preload_models",
]


def __getattr__(name):
    if name == "SpeechToText":
        from .speech_to_text import SpeechToText
        return SpeechToText
    if name == "TextToSpeech":
        from .text_to_speech import TextToSpeech
        return TextToSpeech
    if name in ("ChatterboxEngine", "ChatterboxConfig",
                "ChatterboxLoadError", "ChatterboxGenerationError"):
        from . import chatterbox_engine
        return getattr(chatterbox_engine, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
