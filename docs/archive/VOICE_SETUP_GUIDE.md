# Voice Setup Guide

Your readiness report says:

```
STT: LOCAL WHISPER (MODEL MISSING)
TTS: LOCAL CHATTERBOX (MODEL MISSING)
```

Nothing is broken. The code is there and tested; the **model weights have never
been downloaded**, and the Python packages that run them are probably not
installed either. Both are one command.

---

## The short version

Connected to the internet, from the ZEUS folder:

```
SETUP_VOICE.bat
```

Double-click it, or run it from a terminal. It shows what is missing, asks
before changing anything, installs the packages, downloads the models, then
offers to record from your microphone and speak a test sentence.

Roughly **2-3 GB** of downloads and 10-30 minutes on a normal connection. After
that, **disconnect the internet - voice keeps working.** That is the whole
point: `voice/offline.py` sets `HF_HUB_OFFLINE` before the model libraries load,
and both engines load from `models\` by path.

If you would rather drive it yourself:

```
python scripts\setup_voice.py --check      what is missing, changes nothing
python scripts\setup_voice.py --dry-run    prints every command, runs none
python scripts\setup_voice.py              does the work
python scripts\setup_voice.py --cuda       GPU build of torch (NVIDIA only)
python scripts\setup_voice.py --whisper-size small
```

---

## About your Python 3.14.7

Your screenshot shows **Python 3.14.7**, which is worth a note because it is
brand new and it changes which versions install:

| Python | Chatterbox pins | Works? |
|---|---|---|
| 3.10 - 3.13 | `torch==2.6.0` | yes |
| **3.14** | `torch>=2.9.0` (only in chatterbox-tts **0.1.7+**) | yes, **with 0.1.7 or newer** |

An older `chatterbox-tts` on Python 3.14 would try to install torch 2.6.0,
which has no 3.14 wheel, so pip would attempt to **build torch from source** -
that fails after a very long wait, and the error it prints does not say why.

`setup_voice.py` detects your version and pins accordingly, so this is handled.
If you hit trouble anyway, Python 3.12 is the best-tested version for this
stack:

```
py -3.12 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
SETUP_VOICE.bat
```

The launcher picks up `.venv\` automatically if it exists.

---

## What gets installed

| Group | Packages | Why |
|---|---|---|
| audio i/o | `sounddevice`, `soundfile`, `numpy` | microphone capture and playback (PortAudio ships inside the wheel on Windows) |
| speech to text | `faster-whisper`, `ctranslate2`, `huggingface_hub` | Whisper, in its fast CTranslate2 form |
| torch runtime | `torch`, `torchaudio` | what Chatterbox runs on |
| text to speech | `chatterbox-tts` | the voice itself |

**gradio is deliberately skipped.** `chatterbox-tts` depends on it for its own
web demo, and gradio drags in FastAPI, Starlette and uvicorn. ZEUS never
imports any of them, and a sovereign offline workbench should not have a web
server sitting in its environment. Chatterbox's synthesis does not need it. If
the import check fails without it, the script says so and retries with the full
dependency set rather than leaving you half-installed.

## What gets downloaded

| Into | Files | Size |
|---|---|---|
| `models\whisper\` | `model.bin`, `config.json`, `tokenizer.json`, `vocabulary.*` | ~145 MB (base) |
| `models\chatterbox\` | `ve.safetensors`, `t3_cfg.safetensors`, `s3gen.safetensors`, `tokenizer.json`, `conds.pt` | ~1 GB |

Both from Hugging Face, once. `scripts\setup_models.py` and
`scripts\setup_voice.py` are the **only** files in ZEUS allowed to touch the
network - there is a test that scans the other modules to keep it that way.

Whisper sizes, if you want a different trade-off:

| Size | Disk | Notes |
|---|---|---|
| `tiny` | ~75 MB | fastest, least accurate |
| `base` | ~145 MB | **default** - good on a laptop CPU |
| `small` | ~484 MB | noticeably better, still CPU-viable |
| `medium` | ~1.5 GB | slow on CPU |
| `large-v3` | ~3.1 GB | GPU territory |

```
python scripts\setup_voice.py --whisper-size small
```

---

## If something goes wrong

Run the diagnostic. It walks the chain in the order it actually breaks and
prints the **cause and the fix**, not a red cross:

```
python scripts\voice_doctor.py              full diagnosis
python scripts\voice_doctor.py --record     record 4 s and transcribe it
python scripts\voice_doctor.py --speak      synthesise and play a sentence
```

| Symptom | Cause | Fix |
|---|---|---|
| `no input device found` | no mic, or Windows is blocking it | Settings > Privacy & security > Microphone > "Let desktop apps access your microphone" |
| `Invalid sample rate` | mic does not do 16 kHz | already handled - ZEUS probes the device and resamples |
| `MODEL MISSING` after setup | download interrupted | `python scripts\setup_models.py --force` |
| pip tries to **build torch from source** | old chatterbox on Python 3.14 | `pip install -U "chatterbox-tts>=0.1.7"`, or use Python 3.12 |
| `WeightsUnpickler error` loading `conds.pt` | torch 2.6 changed `torch.load` | already handled - ZEUS detects that exact error and retries once |
| Voice is slow | Chatterbox on CPU is about real-time | `preload = false` in `config.ini [voice]` saves RAM; `enabled = false` runs text-only |
| Everything installed, still silent | wrong output device | check the Windows sound output; `voice_doctor.py --speak` prints which device it used |

## Configuration

`config.ini`, section `[voice]`:

```ini
[voice]
enabled = true          ; false = text only
preload = true          ; false = load models on first use (saves RAM on 8 GB machines)
device = cpu            ; cpu / cuda
compute_type = int8     ; int8 is right for CPU; float16 for GPU
input_device =          ; index or part of the device name, e.g. "USB Microphone"
listen_seconds = 6      ; push-to-talk window
```

---

## How to know it actually worked

```
python scripts\setup_voice.py --check
```

Every line should read `[OK]`, including `Whisper loaded from the local
directory`. Then the real proof, with the network **disconnected**:

```
python scripts\offline_test.py
```

`Speech-to-Text` and `Text-to-Speech` should both be `PASS`, and
`External Dependency Check` should stay `PASS` - that one scans every runtime
module and fails if any of them names a remote host.

Then start ZEUS and press the microphone button, or:

```
python main.py --voice
```

Say *"remember that I prefer concise industrial reports"* - it is a
deterministic route, so it is stored and spoken back **with no model call at
all**, which makes it a clean test of the voice path on its own.
