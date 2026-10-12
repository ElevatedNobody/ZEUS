"""
Voice doctor - find out why voice is not working, in order.

    python scripts/voice_doctor.py            # diagnose everything
    python scripts/voice_doctor.py --record   # also record 3s from the mic
    python scripts/voice_doctor.py --speak    # also load Chatterbox and speak
    python scripts/voice_doctor.py --json report.json

It checks the chain in the order it actually breaks, and stops guessing:
every failing step prints the CAUSE and the FIX, not just a red cross.

    1  Python version            (chatterbox-tts / torch wheel availability)
    2  Packages                  (sounddevice, faster-whisper, torch, chatterbox)
    3  torch                     (version, weights_only behaviour, devices)
    4  Audio devices             (input + output, and which sample rates open)
    5  Model files               (models/whisper, models/chatterbox)
    6  Whisper load              (from the local directory, offline)
    7  Microphone capture        (--record: real audio, level + transcript)
    8  Chatterbox load + speak   (--speak: real synthesis)
    9  Offline enforcement       (no library may reach the network)

Nothing here downloads anything.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import platform
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OK, WARN, FAIL, SKIP = "OK", "WARN", "FAIL", "SKIP"

findings = []       # (status, step, detail, fix)


def report(status, step, detail, fix=""):
    findings.append({"status": status, "step": step, "detail": detail, "fix": fix})
    marker = {OK: "[ OK ]", WARN: "[WARN]", FAIL: "[FAIL]", SKIP: "[SKIP]"}[status]
    print(f"{marker} {step}: {detail}")
    if fix and status in (WARN, FAIL):
        for line in fix.splitlines():
            print(f"       -> {line}")


def has(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        return False


def version_of(module: str) -> str:
    try:
        from importlib import metadata
        return metadata.version(module)
    except Exception:
        return "?"


# ------------------------------------------------------------------- steps

def check_python():
    major, minor = sys.version_info[:2]
    detail = f"Python {platform.python_version()} ({sys.executable})"
    if (major, minor) < (3, 10):
        report(FAIL, "1 Python", detail, "ZEUS needs Python 3.10 or newer.")
    elif (major, minor) >= (3, 13):
        report(WARN, "1 Python", detail,
               "torch and chatterbox-tts wheels lag new Python releases.\n"
               "If pip cannot find a torch wheel, install Python 3.11 or 3.12\n"
               "and create the ZEUS virtual environment with that interpreter.")
    else:
        report(OK, "1 Python", detail)


def check_packages():
    required = [
        ("sounddevice", "sounddevice", "microphone capture and playback"),
        ("numpy", "numpy", "audio buffers"),
        ("faster_whisper", "faster-whisper", "speech to text"),
        ("ctranslate2", "ctranslate2", "faster-whisper runtime"),
    ]
    optional = [
        ("soundfile", "soundfile", "writing .wav files"),
        ("torch", "torch", "Chatterbox runtime"),
        ("torchaudio", "torchaudio", "Chatterbox audio ops"),
        ("chatterbox", "chatterbox-tts", "text to speech"),
    ]
    missing_required = [(m, pip) for m, pip, _ in required if not has(m)]
    for module, pip_name, purpose in required:
        if has(module):
            report(OK, "2 Package", f"{pip_name} {version_of(pip_name)} ({purpose})")
    for module, pip_name, purpose in optional:
        if has(module):
            report(OK, "2 Package", f"{pip_name} {version_of(pip_name)} ({purpose})")
        else:
            fix = f"pip install {pip_name}"
            if pip_name == "torch":
                fix = ("pip install torch torchaudio "
                       "--index-url https://download.pytorch.org/whl/cpu   (CPU-only build)")
            report(WARN, "2 Package", f"{pip_name} is NOT installed ({purpose})", fix)
    for module, pip_name in missing_required:
        report(FAIL, "2 Package", f"{pip_name} is NOT installed",
               f"pip install {pip_name}")
    return not missing_required


def check_torch():
    if not has("torch"):
        report(SKIP, "3 torch", "not installed - Chatterbox TTS cannot run")
        return
    try:
        import torch
    except Exception as e:
        report(FAIL, "3 torch", f"installed but failed to import: {e}",
               "Reinstall torch; a partial install usually causes this.")
        return

    detail = f"torch {torch.__version__}"
    devices = ["cpu"]
    try:
        if torch.cuda.is_available():
            devices.append(f"cuda ({torch.cuda.get_device_name(0)})")
    except Exception:
        pass
    try:
        mps = getattr(torch.backends, "mps", None)
        if mps is not None and mps.is_available():
            devices.append("mps")
    except Exception:
        pass
    report(OK, "3 torch", f"{detail}, devices: {', '.join(devices)}")

    try:
        parts = torch.__version__.split(".")
        if (int(parts[0]), int(parts[1])) >= (2, 6):
            report(WARN, "3 torch",
                   "torch >= 2.6 defaults torch.load(weights_only=True)",
                   "Chatterbox's conds.pt is a pickled object, so the first load\n"
                   "raises an UnpicklingError. ZEUS detects that exact failure\n"
                   "and retries with full deserialisation for the local file in\n"
                   "models/chatterbox (see voice/chatterbox_engine.py). No action\n"
                   "needed - this note explains the warning you will see in the log.")
    except (ValueError, IndexError):
        pass


def check_audio_devices():
    if not has("sounddevice"):
        report(FAIL, "4 Audio", "sounddevice is not installed",
               "pip install sounddevice   (it ships PortAudio)")
        return None
    try:
        import sounddevice as sd
        from voice.speech_to_text import list_input_devices, default_input_device
    except Exception as e:
        report(FAIL, "4 Audio", f"sounddevice failed to load: {e}",
               "On Windows this usually means the PortAudio DLL is missing:\n"
               "pip install --force-reinstall sounddevice")
        return None

    inputs = list_input_devices()
    if not inputs:
        report(FAIL, "4 Audio", "no input device found",
               "Connect a microphone. On Windows also check\n"
               "Settings > Privacy & security > Microphone > let desktop apps access it.")
    else:
        chosen = default_input_device()
        report(OK, "4 Audio", f"{len(inputs)} input device(s); default: "
                              f"{chosen['name'] if chosen else 'unknown'}")
        for device in inputs[:6]:
            print(f"       [{device['index']}] {device['name']} "
                  f"({device['channels']} ch, {device['default_samplerate']} Hz)")

        index = chosen["index"] if chosen else None
        usable = []
        for rate in (16000, 48000, 44100, 32000, 22050, 8000):
            try:
                sd.check_input_settings(device=index, channels=1, samplerate=rate,
                                        dtype="float32")
                usable.append(rate)
            except Exception:
                continue
        if not usable:
            report(FAIL, "4 Audio", "the default microphone did not open at ANY tested rate",
                   "Another application may hold the device exclusively, or the\n"
                   "OS is blocking microphone access. Close other audio apps and retry.")
        elif 16000 in usable:
            report(OK, "4 Audio", f"microphone opens at 16 kHz (also: {usable})")
        else:
            report(OK, "4 Audio",
                   f"microphone does NOT support 16 kHz; ZEUS records at "
                   f"{usable[0]} Hz and resamples")

    try:
        outputs = [d for d in sd.query_devices() if d.get("max_output_channels", 0) > 0]
        if outputs:
            report(OK, "4 Audio", f"{len(outputs)} output device(s) for spoken replies")
        else:
            report(WARN, "4 Audio", "no audio OUTPUT device - replies cannot be played")
    except Exception as e:
        report(WARN, "4 Audio", f"could not enumerate output devices: {e}")
    return inputs


def check_models():
    from voice.offline import check_voice_models, resolve_models_dir
    models = check_voice_models()
    print(f"       model store: {resolve_models_dir()}")
    for name, info in models.items():
        if info["present"]:
            report(OK, "5 Models", f"{name}: complete at {info['directory']}")
        else:
            report(FAIL, "5 Models",
                   f"{name}: missing {', '.join(info['missing'])} in {info['directory']}",
                   "python scripts/setup_models.py"
                   + (" --whisper-only" if name == "whisper" else " --tts-only"))
    return models


def check_whisper_load(models):
    if not models["whisper"]["present"] or not has("faster_whisper"):
        report(SKIP, "6 Whisper", "cannot load - see the steps above")
        return None
    from voice.speech_to_text import SpeechToText
    started = time.perf_counter()
    stt = SpeechToText(preload=True)
    elapsed = time.perf_counter() - started
    if not stt.is_available:
        report(FAIL, "6 Whisper", f"model present but did not load: {stt.last_error}",
               "A truncated download is the usual cause:\n"
               "python scripts/setup_models.py --whisper-only --force")
        return None
    status = stt.status()
    report(OK, "6 Whisper", f"loaded in {elapsed:.1f}s from {status['model_dir']} "
                            f"(device={status['device']}, {status['compute_type']})")
    return stt


def check_capture(stt, seconds: float):
    if stt is None:
        report(SKIP, "7 Capture", "Whisper is not loaded")
        return
    if not stt.microphone_available:
        report(FAIL, "7 Capture", "no usable microphone",
               "See step 4 above.")
        return
    print(f"       Recording {seconds:.0f}s - say something now...")
    started = time.perf_counter()
    text = stt.listen_once(duration=seconds)
    elapsed = time.perf_counter() - started
    if text:
        report(OK, "7 Capture", f"transcribed in {elapsed:.1f}s: \"{text}\"")
    elif stt.last_error == "silence":
        report(WARN, "7 Capture", "captured audio was silent",
               "The microphone opened but nothing was heard. Check the input\n"
               "level in the OS mixer, or select another device with\n"
               "config.ini [voice] input_device = <index or name fragment>.")
    else:
        report(FAIL, "7 Capture", f"capture failed: {stt.last_error}",
               "See the hint in the error above.")


def check_chatterbox(models, speak: bool):
    if not has("chatterbox") or not has("torch"):
        report(SKIP, "8 Chatterbox", "chatterbox-tts and/or torch are not installed")
        return
    if not models["chatterbox"]["present"]:
        report(SKIP, "8 Chatterbox", "checkpoint missing - see step 5")
        return
    if not speak:
        report(OK, "8 Chatterbox", "checkpoint complete and libraries installed "
                                   "(run with --speak to load and synthesise)")
        return

    from voice.text_to_speech import TextToSpeech
    started = time.perf_counter()
    tts = TextToSpeech(preload=True)
    elapsed = time.perf_counter() - started
    if not tts.is_available:
        report(FAIL, "8 Chatterbox", f"did not load: {tts.last_error}",
               "If the error mentions weights_only / UnpicklingError, upgrade\n"
               "ZEUS (this build retries automatically). If it mentions a\n"
               "missing module, install torchaudio and s3tokenizer.")
        return
    report(OK, "8 Chatterbox", f"loaded in {elapsed:.1f}s "
                               f"(device={tts._engine.device if tts._engine else '?'})")
    started = time.perf_counter()
    result = tts.synthesize("Voice diagnostic complete.")
    if result is None:
        report(FAIL, "8 Chatterbox", "loaded but produced no audio",
               "Check the log for a generation error.")
        return
    audio, rate = result
    report(OK, "8 Chatterbox", f"synthesised {len(audio) / rate:.1f}s of audio in "
                               f"{time.perf_counter() - started:.1f}s")
    print("       Playing it back...")
    tts.speak("Voice diagnostic complete.")
    tts.close()


def check_offline():
    from voice.offline import is_offline_enforced, enforce_offline
    enforce_offline()
    if is_offline_enforced():
        report(OK, "9 Offline", "HF_HUB_OFFLINE / TRANSFORMERS_OFFLINE set; "
                                "no voice component may reach the network")
    else:
        report(FAIL, "9 Offline", "offline flags were not applied",
               "voice/offline.py:enforce_offline() must run before the model "
               "libraries are imported.")


# -------------------------------------------------------------------- main

def main() -> int:
    parser = argparse.ArgumentParser(description="Diagnose the ZEUS voice subsystem")
    parser.add_argument("--record", action="store_true",
                        help="record from the microphone and transcribe it")
    parser.add_argument("--seconds", type=float, default=3.0)
    parser.add_argument("--speak", action="store_true",
                        help="load Chatterbox and speak a test sentence")
    parser.add_argument("--json", default="")
    args = parser.parse_args()

    print("=" * 70)
    print("  ZEUS VOICE DOCTOR")
    print("  Autonomous AI Command & Intelligence System")
    print("  Local Whisper STT + local Chatterbox TTS. Nothing is downloaded.")
    print("=" * 70)

    check_python()
    check_packages()
    check_torch()
    check_audio_devices()
    models = check_models()
    stt = check_whisper_load(models)
    if args.record:
        check_capture(stt, args.seconds)
    else:
        report(SKIP, "7 Capture", "not requested (run with --record)")
    check_chatterbox(models, args.speak)
    check_offline()
    if stt is not None:
        stt.close()

    failures = [f for f in findings if f["status"] == FAIL]
    warnings = [f for f in findings if f["status"] == WARN]

    print()
    print("=" * 70)
    if failures:
        print("  ROOT CAUSE(S) - fix these in order:")
        for f in failures:
            print(f"    {f['step']}: {f['detail']}")
            for line in (f["fix"] or "").splitlines():
                print(f"        {line}")
    else:
        print("  No blocking problem found.")
        if warnings:
            print("  Degraded (voice still works, with less):")
            for f in warnings:
                print(f"    {f['step']}: {f['detail']}")
    print("=" * 70)

    if args.json:
        Path(args.json).write_text(json.dumps(findings, indent=2), encoding="utf-8")
        print(f"JSON written to {args.json}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
