# Voice Repair Report

Diagnosis and repair of the local voice chain: microphone → Whisper STT →
ZEUS → Chatterbox TTS → speaker. Nothing was redesigned; the failures were
found and fixed.

**41 voice tests pass**, and `scripts/voice_doctor.py` reproduces the diagnosis
on any machine.

---

## 1. Diagnostic method

`scripts/voice_doctor.py` walks the chain in the order it actually breaks and
prints the **cause and the fix**, not a red cross:

```
1 Python        version (torch / chatterbox-tts wheel availability)
2 Packages      sounddevice, faster-whisper, ctranslate2, torch, chatterbox
3 torch         version, weights_only behaviour, available devices
4 Audio         input + output devices, and which sample rates actually open
5 Models        models/whisper and models/chatterbox file-by-file
6 Whisper load  from the local directory, timed
7 Capture       --record: real audio, level, transcript
8 Chatterbox    --speak: real load and synthesis
9 Offline       the enforcement flags are in force
```

Run on this machine it correctly reported PortAudio missing, then (after
installing it) *no input device found* with the Windows privacy-settings hint,
then the two missing model directories with the exact command to fetch each.

## 2. Root causes found

| # | root cause | why voice appeared dead | fix |
|---|---|---|---|
| 1 | **Whisper was loaded by NAME** (`WhisperModel("base")`) | faster-whisper resolves a bare size string against the Hugging Face Hub, so the first spoken command needed the network and failed on a disconnected machine | load from an explicit local directory with `local_files_only=True` (`voice/offline.py` + `speech_to_text.py`) |
| 2 | **No offline enforcement** | `huggingface_hub` performs a metadata request on load and raises instead of using its cache | `enforce_offline()` sets `HF_HUB_OFFLINE` / `TRANSFORMERS_OFFLINE` **before** the model libraries are imported |
| 3 | **`scripts/setup_models.py` did not exist** | every error message pointed at a file that was not there, so the models were never downloaded | written; it is the only script allowed to use the network |
| 4 | **16 kHz was requested from the device** | most laptop/USB microphones only offer 44.1 or 48 kHz; PortAudio raises `Invalid sample rate` and the mic button silently does nothing | probe the device at startup, record at a rate it supports, resample to 16 kHz in numpy |
| 5 | **No microphone detection at startup** | a missing or blocked mic only surfaced on the first press, as a generic exception | `_probe_microphone()` enumerates devices and checks settings during init; `microphone_available` and `last_error` are reported |
| 6 | **Silence was transcribed** | Whisper hallucinates on silence (`"Thank you."`, `"you"`), and ZEUS then executed that as a command | RMS gate (`SILENCE_RMS`); a silent capture returns `""` with `last_error = "silence"` |
| 7 | **`speak()` blocked the pipeline** | `handle_text()` waited for playback, so the written answer only appeared in the UI *after* the audio finished - it looked like ZEUS had frozen | `speak_async()` with a single ordered worker queue; the text reply returns immediately |
| 8 | **torch ≥ 2.6 refuses Chatterbox's `conds.pt`** | torch 2.6 changed `torch.load` to default `weights_only=True`; Chatterbox's conditioning file is a pickled object, so loading raises `UnpicklingError` and TTS never starts | detect that exact error and retry once with full deserialisation, narrowly and loudly, for the user's own local file |
| 9 | **All-or-nothing voice init** | one exception disabled voice entirely, even when only TTS was missing | STT and TTS initialise independently |
| 10 | **`last_error` was never surfaced** | a silent failure looked the same as "voice is off" | surfaced in the log, `--voice-status`, the startup check and the offline test |
| 11 | **Relative model paths** | launching from another directory missed the model store | every path resolves absolutely (`MEDHA_MODELS_DIR` → `config.ini [paths]` → `<project>/models`) |
| 12 | **No transcript in the UI** | the user could not see what Whisper heard when it misheard | the transcript comes back with the result and is shown as a normal "You" turn |

## 3. Whisper (STT) - status

Loads from `models/whisper` by path, `local_files_only=True`, never by name.

- device and compute type from `config.ini [voice]` (`cpu` / `int8` by default)
- `preload = true` keeps the model resident; `preload = false` verifies the
  files at startup and loads on first use (8 GB machines)
- microphone: enumerated, probed, selectable by index or name fragment
  (`input_device`), recorded at a supported rate and resampled to 16 kHz
- silence gate, empty-capture guard, actionable PortAudio hints (including the
  Windows microphone-privacy case)
- `listen_once()`, `transcribe_array()`, `transcribe_file()`, `status()`,
  `close()` - the original interface is unchanged for existing callers

## 4. Chatterbox (TTS) - status

Loads from `models/chatterbox` via `ChatterboxTTS.from_local()`.
`from_pretrained()` is never called at runtime, and a test tokenises the
package to prove it.

- device: explicit → CUDA → MPS → CPU, with a **retry on CPU** if the
  accelerator fails
- loaded **once** per process; a failed lazy load is latched so it is not
  retried per sentence
- generation serialised with a lock (the model holds mutable conditioning state)
- torch 2.6 `weights_only` recovery, described above
- `speak()`, `speak_async()`, `synthesize()`, `save_to_file()`, `stop()`,
  `close()`

## 5. Memory integration

Unchanged in shape and now user-aware: `listen_and_process()` calls
`handle_text()`, the same method typed input uses, so a spoken turn goes through
the same decision engine, the same tools and the same memory - which is now
scoped to the logged-in operator. There is no voice memory, no voice history
table, no voice-only command path. Tests assert those files do not exist.

`"remember that ..."` is a deterministic route, so a spoken preference is stored
and confirmed with **no model call at all** - it works with Ollama down.

## 6. Verified end to end

With the model runtimes stubbed by contract-enforcing doubles (the real weights
are hundreds of megabytes and Hugging Face is unreachable from this build
environment), the full chain runs:

```
login (argon2id) -> voice mode on
speak "remember that I prefer concise industrial reports"
      -> heard, stored, "remembered: ..." spoken back
speak "industrial term HAZOP"
      -> HAZOP = Hazard and Operability Study ... spoken back
lock -> unlock -> second operator sees none of it
restart as the first operator -> the preference is still there
```

Specific repairs verified by test:

- a microphone that refuses 16 kHz records at 48 kHz and is resampled
- silence is not transcribed
- a missing microphone is reported, not crashed
- `speak_async` returns in under 0.2 s while a 0.4 s synthesis runs
- queued replies are spoken in order, never overlapping
- the torch `weights_only` failure is recognised and recovered on retry
- `voice_doctor.py` completes cleanly and reports findings

## 7. Remaining issues

1. **The real weights were never loaded in this environment.** Hugging Face
   returns 403 here, so Whisper and Chatterbox ran against strict stubs.
   `setup_models.py` itself was executed and its failure path verified. On your
   machine: `python scripts/setup_models.py`, then
   `python scripts/voice_doctor.py --record --speak`, then
   `python scripts/offline_test.py --deep`.
2. **Chatterbox on CPU is roughly real-time.** A 30-word reply takes a few
   seconds on an i3. `preload = false` frees RAM; `enabled = false` runs
   text-only. STT alone is much lighter.
3. **No wake word and no VAD.** The mic is push-to-talk with a fixed
   `listen_seconds` window. `webrtcvad` is already in `requirements.txt` if you
   want end-of-speech detection added next.
4. **Chatterbox is English-first**; spoken quality for other languages is not
   verified.
5. **torch is a large install** (~200 MB CPU-only, ~2 GB with CUDA).
