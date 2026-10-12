# Voice Integration Map

How the supplied voice files were folded into the existing ZEUS project.
One system, not two. Nothing was rebuilt that already worked.

## Component map

```
CURRENT ZEUS COMPONENT              VOICE COMPONENT                 FINAL INTEGRATED COMPONENT
─────────────────────────────────────────────────────────────────────────────────────────────────
voice/speech_to_text.py               speech_to_text.py               voice/speech_to_text.py
  faster-whisper by SIZE STRING         local dir + local_files_only     local dir, lazy/preload,
  ("base" -> resolves on the HF Hub)    transcribe_array()               transcribe_file(), close()
                                                                        SAME class + method names

voice/text_to_speech.py               text_to_speech.py               voice/text_to_speech.py
  pyttsx3 (Windows SAPI)                Chatterbox adapter               Chatterbox, lazy/preload,
  speak/stop/is_available               synthesize()                     save_to_file(), volume,
                                                                        SAME speak/stop/is_available

(did not exist)                       chatterbox_engine.py            voice/chatterbox_engine.py
                                        model load + device select      taken as-is (it is correct)

(did not exist)                       offline.py                      voice/offline.py
                                        HF offline flags,               + config.ini [voice],
                                        model dir resolution,           + absolute path resolution,
                                        require_model_dir()             + check_voice_models(),
                                                                        + voice_status()/voice_banner()

voice/__init__.py  (empty)            __init__.py (lazy exports)      voice/__init__.py
                                                                        + status helpers exported

(referenced, absent)                  scripts/setup_models.py         scripts/setup_models.py
                                                                        WRITTEN: the one script
                                                                        allowed to use the network

core/logger.py                        (used by all voice modules)     unchanged - reused
memory/database.py + memory_manager   (none supplied)                 unchanged - voice uses it
core/decision_engine.py               (none supplied)                 + industrial voice routes,
                                                                        + case-preserving args
main.py  MedhaAssistant              (none supplied)                 + offline-first voice init,
                                                                        + transcript passthrough,
                                                                        + voice_report(), close_voice()
ui/dashboard.py  mic button           (none supplied)                 + shows the transcript
launcher.py                           run.py (reference only)         launcher.py kept - same job,
                                                                        no duplicate entry point
models/                               models/chatterbox, models/whisper  created + documented
```

## Decisions

**A. Untouched ZEUS files.** `core/` (except `decision_engine.py`), `memory/`,
`plugins/`, `automation/`, `industrial/`, `ui/hud_widgets.py`, `START.bat`,
`install.py`, every industrial script.

**B. Integrated directly.** `chatterbox_engine.py` — correct as supplied: one-time
load, `from_local()` only, device fallback to CPU, generation lock, tensor →
mono float32.

**C. Adapted.** `offline.py` (config.ini + absolute paths + status reporting),
`speech_to_text.py` and `text_to_speech.py` (config defaults, lazy/preload
lifecycle, `close()`, file helpers), `__init__.py` (extra exports).

**D. Imports changed.** None had to change: the supplied files already use
`from core.logger import get_logger` and relative `from .offline import ...`,
which match this project exactly. That is why they dropped in cleanly.

**E. Missing dependencies added** to `requirements.txt`: `chatterbox-tts`,
`torch`, `torchaudio`, `huggingface_hub` (setup only). `faster-whisper`,
`sounddevice`, `soundfile`, `numpy` were already there.

**F. Model paths normalised.** `MEDHA_MODELS_DIR` → `config.ini [paths]
models_dir` → `<project>/models`, always resolved to an absolute path, so
ZEUS finds its models from any working directory. No user directory is
hard-coded anywhere (a test asserts this).

**G. Startup scripts updated.** `start_medha_industrial.bat` gained step 5/7
(voice verification + the offline banner). `FIX_PACKAGES.bat` now installs the
real voice stack instead of pyttsx3. `BUILD_EXE.bat` bundles the new modules.

**H. Bug fixes.** See VOICE_INTEGRATION_REPORT.md — the notable one is that
route arguments were being lowercased, which mangled file paths and calendar
titles for spoken *and* typed commands.

## Runtime flow

```
MICROPHONE ─ sounddevice ─▶ SpeechToText.listen_once()
                                  │ local faster-whisper, models/whisper
                                  ▼
                             TRANSCRIPT ────────────────────────┐
                                  │                             │
                     MedhaAssistant.handle_text()              │ shown in the
                                  │  (the SAME method typed     │ chat log as
                                  │   input uses)               │ a "You" turn
                                  ▼                             │
                     MemoryManager.add_message ─▶ SQLite ◀──────┘
                                  │
                        DecisionEngine.process
                     ┌────────────┼────────────┐
                     ▼            ▼            ▼
              direct tool    industrial    Ollama chat
              (deterministic) tools/RAG    (model router)
                     └────────────┼────────────┘
                                  ▼
                          TEXT RESPONSE ──▶ UI chat panel (always)
                                  │
                                  ▼
                  TextToSpeech.speak() ─ Chatterbox, models/chatterbox
                                  ▼
                          LOCAL AUDIO OUTPUT
```

No step in this chain touches the network. The only socket ZEUS opens is
loopback to Ollama for the reasoning step.
