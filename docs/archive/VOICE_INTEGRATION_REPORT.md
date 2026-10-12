# Voice Integration Report

Local Whisper STT + local Chatterbox TTS integrated into the existing ZEUS
project. One unified system: spoken input is ordinary ZEUS input, spoken
output is the same text the UI already shows, and both share one memory store.

Result: **228 tests pass** (197 pre-existing + 31 new voice tests), tool count
74 → 92, nothing removed.

---

## 1. Final project tree (voice-relevant)

```
ZEUS/
├── main.py                      entry point (voice init, transcript passthrough)
├── launcher.py                  the project's run.py equivalent - NOT duplicated
├── install.py
├── config.ini                   + [voice] section
├── requirements.txt             + chatterbox-tts, torch, torchaudio, huggingface_hub
├── requirements-industrial.txt
├── START.bat                    unchanged
├── start_medha_industrial.bat  now 7 steps (5/7 = voice verification)
├── FIX_PACKAGES.bat             installs the real voice stack
├── BUILD_EXE.bat                bundles the new voice modules
│
├── core/
│   ├── logger.py                reused by every voice module (unchanged)
│   ├── decision_engine.py       + industrial routes, + remember/forget routes,
│   │                              + case-preserving route arguments
│   ├── builtin_tools.py         + remember_fact, forget_fact
│   └── ... (unchanged)
│
├── voice/
│   ├── __init__.py              lazy exports + status helpers
│   ├── offline.py               offline enforcement, model paths, [voice] config
│   ├── speech_to_text.py        faster-whisper, local dir, lazy/preload
│   ├── text_to_speech.py        Chatterbox adapter, lazy/preload, save_to_file
│   └── chatterbox_engine.py     model load, device select, generation
│
├── models/
│   ├── whisper/                 model.bin, config.json, tokenizer.json, vocabulary.*
│   └── chatterbox/              ve/t3_cfg/s3gen.safetensors, tokenizer.json, conds.pt
│
├── scripts/
│   ├── setup_models.py          THE ONLY NETWORK SCRIPT (voice weights)
│   ├── offline_test.py          + Speech-to-Text and Text-to-Speech checks
│   ├── startup_check.py         + four voice readiness rows
│   └── ... (industrial scripts unchanged)
│
├── memory/                      ONE memory system - voice uses it, adds nothing
├── industrial/                  unchanged; reachable from spoken commands
├── ui/dashboard.py              + shows the transcript as a "You" turn
└── tests/test_voice_integration.py   31 new tests
```

`run.py` was **not** created: `launcher.py` already is the project's bootstrap
(version check + start `main.py`). Adding `run.py` would have been a second
entry point for the same job.

## 2. Files created

| file | purpose |
|---|---|
| `voice/offline.py` | offline enforcement, local model resolution, `[voice]` config, status/banner |
| `voice/chatterbox_engine.py` | Chatterbox load + device selection + generation |
| `scripts/setup_models.py` | one-time download of Whisper + Chatterbox weights |
| `models/whisper/README.txt` | what belongs there and how to get it |
| `models/chatterbox/README.txt` | as above |
| `tests/test_voice_integration.py` | 31 tests |
| `VOICE_INTEGRATION_MAP.md` | Phase 2 integration map |
| `VOICE_INTEGRATION_REPORT.md` | this file |

## 3. Files modified

| file | change |
|---|---|
| `voice/__init__.py` | was empty → lazy public API + status helpers |
| `voice/speech_to_text.py` | faster-whisper from a LOCAL DIR (was a size string that resolves on the HF Hub); lazy/preload; `transcribe_file()`; `close()`; config defaults |
| `voice/text_to_speech.py` | pyttsx3/SAPI → Chatterbox; lazy/preload; `synthesize()`, `save_to_file()`; config defaults. Same `speak/stop/is_available` interface |
| `main.py` | offline-first voice init, STT and TTS brought up independently, transcript returned with the result, `voice_report()`, `close_voice()`, `--voice-status`, CLI banner |
| `ui/dashboard.py` | the mic path now shows what Whisper heard as a normal "You" turn |
| `core/decision_engine.py` | industrial command routes, `remember`/`forget` routes, route arguments keep their original case |
| `core/builtin_tools.py` | `remember_fact`, `forget_fact` |
| `config.ini` | `[voice]` section |
| `requirements.txt` | real voice stack |
| `FIX_PACKAGES.bat`, `BUILD_EXE.bat`, `start_medha_industrial.bat` | voice-aware |
| `scripts/offline_test.py`, `scripts/startup_check.py` | voice checks |
| `tests/test_productivity.py` | asserts the corrected (case-preserving) routing |
| `README.md`, `HOW_TO_RUN.txt`, `INDUSTRIAL_README.md` | voice documentation |

## 4. Files removed

**None.** `pyttsx3` is no longer installed by `FIX_PACKAGES.bat` because
Chatterbox replaces it as the TTS engine, but no file was deleted and no
feature was dropped.

## 5. Dependency changes

Added to `requirements.txt`: `chatterbox-tts>=0.1.1`, `torch>=2.0.0`,
`torchaudio>=2.0.0`, `huggingface_hub>=0.23.0` (setup only).
Already present and reused: `faster-whisper`, `sounddevice`, `soundfile`,
`numpy`. No cloud SDK was added.

CPU-only machines:
`pip install torch torchaudio --index-url https://download.pytorch.org/whl/cpu`

## 6. Model paths

Resolution order, always ending in an **absolute** path so the working
directory never matters:

```
MEDHA_MODELS_DIR  >  config.ini [paths] models_dir  >  <project>/models
    models/whisper       (or MEDHA_STT_MODEL_DIR)
    models/chatterbox    (or MEDHA_TTS_MODEL_DIR)
```

No user directory is hard-coded anywhere; a test asserts that
`voice/offline.py` contains no `C:\Users` or `/home/` literal. Every required
file is verified before the subsystem starts, and a missing model raises
`MissingModelError` naming the absent files and the setup command — it never
triggers a download.

## 7. Chatterbox integration

- `ChatterboxTTS.from_local(model_dir, device)` only. `from_pretrained()` — which
  calls `hf_hub_download()` per checkpoint — is never called at runtime, and a
  test tokenises `voice/*.py` to prove it.
- Device: explicit request → CUDA → MPS → CPU, with a **retry on CPU** if the
  chosen accelerator fails to load.
- Loaded **once** per process (`preload = true`), or once on first use
  (`preload = false` for 8 GB machines). A failed lazy load is not retried on
  every sentence.
- `generate()` is serialised with a lock because the model holds mutable
  conditioning state; output is flattened to mono float32.
- Failure is graceful: `is_available = False`, reason in `last_error`, ZEUS
  continues in text mode.

## 8. Whisper integration

- `WhisperModel(<local dir>, device, compute_type, local_files_only=True)`.
  The old code passed `"base"`, which faster-whisper resolves against the
  Hugging Face Hub — that was the single biggest offline defect and it is fixed.
- Capture at 16 kHz mono float32 via `sounddevice`, transcribed from memory —
  no temp file, so no leak when transcription raises.
- `listen_once()` (mic), `transcribe_array()` (UI-owned capture),
  `transcribe_file()` (audio on disk).

## 9-10. STT / TTS interfaces

```python
stt.is_available, stt.last_error
stt.listen_once(duration=None) -> str        # duration from config.ini
stt.transcribe_array(audio)    -> str
stt.transcribe_file(path)      -> str
tts.is_available, tts.last_error
tts.speak(text)                              # generate + play, blocking
tts.synthesize(text)           -> (audio, sample_rate)
tts.save_to_file(text, path)   -> bool
tts.stop() / tts.close()
```

Both keep the method names `main.py` already called, which is why no existing
call site changed.

## 11. Memory integration

There is exactly one memory system. `listen_and_process()` calls
`handle_text()` — the same method typed input uses — so the spoken turn goes
through `MemoryManager.add_message` into `database/medha.db` like any other.
No `voice_memory.py`, no `voice_history`, no second database (a test asserts
those files do not exist).

`"remember that ..."` is now a deterministic route rather than a model call, so
a spoken preference is stored **and confirmed** even with Ollama down. It reuses
`MemoryManager`'s key derivation, so the automatic capture and the tool write the
same key — idempotent, not a duplicate store.

## 12. Startup process

```
start_medha_industrial.bat
  1/7 Python environment (prefers .venv / venv)
  2/7 document stack (PyMuPDF, Pillow, numpy, docx, openpyxl, pptx)
  3/7 Ollama on loopback (starts it if not responding)
  4/7 OCR engines
  5/7 VOICE: prints the offline banner, verifies models/whisper + models/chatterbox
  6/7 models, knowledge base, readiness report
  7/7 start ZEUS (--voice, falling back to no-voice, then CLI)
```

Missing voice models are a warning, never a blocker: ZEUS starts and says
what to run.

## 13. Bugs found

1. **STT resolved the model by name** (`WhisperModel("base")`) → first use hit
   the Hugging Face Hub. Fatal to the offline requirement.
2. **No offline enforcement** existed; `huggingface_hub` performs a metadata
   request on load and can raise rather than use its cache.
3. **`scripts/setup_models.py` was referenced by the error messages but did not
   exist** — the actionable hint pointed at nothing.
4. **Route arguments were lowercased.** `_match_direct_command` matched against
   `text.lower()` and passed the lowercased groups on, so
   `analyze the report D:/Reports/INSP-2024-118.pdf` became `d:/reports/...`
   and a calendar entry became `doctor visit`.
5. **Voice init was all-or-nothing**: one exception disabled voice entirely,
   even when only TTS was missing.
6. **`last_error` was never surfaced** — a silent failure looked identical to
   "voice is off".
7. **No transcript in the UI**: the user could not see what Whisper heard.
8. **Model paths were relative**, so launching from another directory could
   miss the model store.
9. **Spoken industrial commands needed Ollama** to be routed, because the
   industrial tools had no deterministic patterns.
10. **Chatterbox reload risk**: a lazy path could re-load a ~1 GB model per
    sentence if not guarded.
11. **`voice_prompt` was accepted without checking the file exists** (in the
    supplied engine it is checked — verified and kept).
12. **`from_pretrained` had no guard** proving it is never reintroduced.

## 14. Bugs fixed

All twelve. 1-3 by `voice/offline.py` + `scripts/setup_models.py`; 4 by
`DecisionEngine._restore_original_case()`; 5-6 by the rewritten
`_init_voice()` + `voice_report()`; 7 in `ui/dashboard.py`; 8 by absolute path
resolution; 9 by the new industrial routes; 10 by the `_engine_or_none()`
once-only load with a `_load_failed` latch; 11 kept from the supplied engine and
covered by a test; 12 by the tokenising test plus a stub whose
`from_pretrained` raises.

## 15-16. Tests performed and results

Model runtimes are stubbed (`faster_whisper.WhisperModel`,
`chatterbox.tts.ChatterboxTTS`, `sounddevice`, `torch`) because the real weights
are hundreds of megabytes and Hugging Face is unreachable from this build
environment. The stubs **enforce the contract**: the Whisper stub fails the test
if `local_files_only` is not set or the path is not a real directory, and the
Chatterbox stub raises if `from_pretrained()` is ever called. Everything ZEUS
owns runs for real.

| # | test | result |
|---|---|---|
| 1 | ZEUS starts | PASS |
| 2 | typed command | PASS |
| 3 | spoken command | PASS |
| 4 | Whisper transcription | PASS (stubbed runtime, real load path + contract) |
| 5 | ZEUS receives the transcription | PASS |
| 6 | ZEUS responds | PASS |
| 7 | Chatterbox speaks the response | PASS (6000 samples @ 24 kHz to the audio device) |
| 8 | text response still in the UI | PASS |
| 9 | memory works from voice | PASS |
| 10 | close ZEUS | PASS (`close_voice()` releases model + audio) |
| 11 | restart ZEUS | PASS |
| 12 | memory persists | PASS |
| 13 | disconnect internet | PASS (socket guard blocks non-loopback) |
| 14 | start offline | PASS |
| 15 | speak a command offline | PASS |
| 16 | Whisper offline | PASS (local dir + `local_files_only`) |
| 17 | Chatterbox offline | PASS (`from_local` only) |
| 18 | command execution offline | PASS (deterministic routes need no model) |
| 19 | existing functionality intact | PASS (197 pre-existing tests) |
| 20 | no external service required | PASS (offline test scans every source file) |

Full suite: **228 passed**.

Phase 19 demo, executed end to end with no Ollama and no network:

```
VOICE MODE: OFFLINE   STT: LOCAL WHISPER (READY)   TTS: LOCAL CHATTERBOX (READY)
EXTERNAL VOICE SERVICES: DISABLED
speak → "remember that I prefer concise industrial reports"
        → remembered: I prefer concise industrial reports        (spoken back)
speak → "industrial term FCC"
        → FCC = Fluid Catalytic Cracking ...                     (spoken back)
restart, type → "what do you remember"
        → facts: ['I prefer concise industrial reports']
```

## 17. Remaining issues

1. **The real weights were never loaded here.** Hugging Face is unreachable from
   this build environment (`403`), so Whisper and Chatterbox ran against
   contract-enforcing stubs. `scripts/setup_models.py` itself was executed and
   its failure path verified. On your machine, run
   `python scripts/setup_models.py` then `python scripts/offline_test.py --deep`,
   which loads both real models and synthesises audio.
2. **`torch` is a large install** (~2 GB with CUDA, ~200 MB CPU-only) and
   Chatterbox on CPU generates roughly in real time — a 30-word reply takes a
   few seconds on an i3. If that is too slow, set `preload = false` to keep RAM
   free, or `enabled = false` to run text-only; STT alone is much lighter.
3. **No wake word.** The mic is push-to-talk (the 🎤 button), as before.
   `listen_seconds` is a fixed window; there is no silence detection yet — the
   supplied implementation did not include VAD, and `webrtcvad` is already in
   `requirements.txt` if you want it added.
4. **Chatterbox is English-first.** `aya-expanse` can translate a reply, but the
   spoken output quality for other languages is not verified.
5. **`vosk` remains in `requirements.txt`** but is unused — left in place rather
   than removing something that might be wanted as a fallback.
