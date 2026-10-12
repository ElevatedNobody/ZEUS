"""
Measure how fast Whisper and Chatterbox actually are on THIS machine.

    python scripts/voice_benchmark.py

Nobody can tell you from a spec sheet whether a 0.5B neural voice is usable
on your laptop. This runs both models for real, times them, and tells you what
the numbers mean - including recommending a smaller Whisper model if the one
you have is too slow for comfortable use.

The number that matters is the REAL-TIME FACTOR: seconds of computing per
second of audio.

    RTF 0.3   three times faster than real time - excellent
    RTF 1.0   exactly real time
    RTF 5.0   a ten-second reply takes fifty seconds to produce

For speech INPUT anything under about 1.0 feels instant, because you only
transcribe a few seconds at a time.

For speech OUTPUT the RTF matters less than you would think, because ZEUS
generates one sentence at a time and starts playing the first while it makes
the second. What you actually wait for is the FIRST SENTENCE, which this
measures separately.

Nothing here downloads anything. Models that are not installed are reported
as not installed.
"""

from __future__ import annotations

import argparse
import math
import struct
import sys
import tempfile
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SHORT = "The relief valve set pressure was verified before startup."
LONG = ("The exchanger was opened for cleaning during the March outage. "
        "Before it returns to service a hydrotest shall be performed at one "
        "point three times design pressure and held for thirty minutes. "
        "A visual inspection of the shell side welds follows the hydrotest.")


def rule(title: str = ""):
    print("\n" + "=" * 72)
    if title:
        print(f"  {title}")
        print("=" * 72)


def verdict(rtf: float, for_input: bool) -> str:
    if for_input:
        if rtf <= 0.5:
            return "excellent - transcription feels instant"
        if rtf <= 1.0:
            return "good - about as fast as you speak"
        if rtf <= 2.0:
            return "usable - a short pause after each command"
        return "slow - consider a smaller model (see the advice below)"
    if rtf <= 1.0:
        return "excellent - speech is produced faster than it plays"
    if rtf <= 3.0:
        return "good - the first sentence starts quickly, the rest keeps up"
    if rtf <= 8.0:
        return "usable - noticeable pauses between sentences"
    return "slow - fine for short replies, painful for long ones"


def make_test_wav(seconds: float = 6.0) -> Path:
    """A spoken-length wav of tone-modulated noise.

    It is not speech, so the transcript will be empty or nonsense - that is
    fine and expected. What is being measured is how long the model takes to
    process N seconds of audio, and that is governed by the audio length and
    the model size, not by what the words are.
    """
    path = Path(tempfile.mkdtemp(prefix="medha_bench_")) / "sample.wav"
    rate = 16000
    frames = []
    for i in range(int(rate * seconds)):
        t = i / rate
        envelope = 0.5 * (1 + math.sin(2 * math.pi * 2.5 * t))     # syllable rate
        value = envelope * 0.3 * math.sin(2 * math.pi * 180 * t) \
            + envelope * 0.15 * math.sin(2 * math.pi * 900 * t)
        frames.append(struct.pack("<h", int(max(-1.0, min(1.0, value)) * 24000)))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"".join(frames))
    return path


# ------------------------------------------------------------- whisper --

def benchmark_whisper(runs: int) -> dict:
    rule("SPEECH INPUT — faster-whisper")
    from voice.offline import WHISPER_REQUIRED_FILES, missing_files, resolve_whisper_dir

    directory = resolve_whisper_dir()
    absent = missing_files(directory, WHISPER_REQUIRED_FILES)
    if absent:
        print(f"\n  Not installed. Missing from {directory}:")
        print(f"    {', '.join(absent)}")
        print("\n  To install:  python scripts\\setup_models.py --whisper-only")
        return {}

    from voice.speech_to_text import SpeechToText, _physical_cores

    print(f"\n  Model directory : {directory}")
    print(f"  CPU threads     : {_physical_cores()} (physical cores)")

    started = time.time()
    stt = SpeechToText(preload=True)
    if not stt.is_available:
        print(f"\n  Failed to load: {stt.last_error}")
        return {}
    load = time.time() - started
    print(f"  Load time       : {load:.1f}s")

    sample = make_test_wav(6.0)
    audio_seconds = 6.0
    times = []
    for index in range(runs):
        started = time.time()
        stt.transcribe_file(str(sample))
        elapsed = time.time() - started
        times.append(elapsed)
        label = " (first run, includes warm-up)" if index == 0 else ""
        print(f"  run {index + 1}: {elapsed:5.1f}s for {audio_seconds:.0f}s of audio"
              f"  RTF {elapsed / audio_seconds:.2f}x{label}")

    # The first run carries warm-up. Steady state is what you live with.
    steady = times[1:] or times
    rtf = (sum(steady) / len(steady)) / audio_seconds
    print(f"\n  STEADY STATE    : RTF {rtf:.2f}x — {verdict(rtf, for_input=True)}")
    stt.close()
    return {"rtf": rtf, "load": load, "directory": str(directory)}


# ---------------------------------------------------------- chatterbox --

def benchmark_chatterbox(runs: int) -> dict:
    rule("SPEECH OUTPUT — Chatterbox")
    from voice.offline import (CHATTERBOX_REQUIRED_FILES, missing_files,
                               resolve_chatterbox_dir)

    directory = resolve_chatterbox_dir()
    absent = missing_files(directory, CHATTERBOX_REQUIRED_FILES)
    if absent:
        print(f"\n  Not installed. Missing from {directory}:")
        print(f"    {', '.join(absent)}")
        print("\n  To install:  python scripts\\setup_models.py --chatterbox-only")
        return {}
    try:
        import torch  # noqa: F401
    except ImportError:
        print("\n  torch is not installed, so Chatterbox cannot run.")
        print("  Install the CPU build (much smaller than the CUDA one):")
        print("    pip install torch torchaudio --index-url "
              "https://download.pytorch.org/whl/cpu")
        return {}

    from voice.chatterbox_engine import (ChatterboxEngine, split_for_speech,
                                         tune_cpu_threads)

    print(f"\n  Model directory : {directory}")
    started = time.time()
    engine = ChatterboxEngine()
    load = time.time() - started
    print(f"  Device          : {engine.device}")
    print(f"  CPU threads     : {tune_cpu_threads() or 'already set'}")
    print(f"  Load time       : {load:.1f}s")

    print(f"\n  Warming up (the first pass is always the slow one)...")
    warm = engine.warmup()
    if warm:
        print(f"  Warm-up         : {warm:.1f}s")

    print(f"\n  A short sentence:")
    short_times = []
    for index in range(runs):
        started = time.time()
        audio, rate = engine.generate(SHORT)
        elapsed = time.time() - started
        seconds = len(audio) / float(rate)
        short_times.append((elapsed, seconds))
        print(f"    run {index + 1}: {elapsed:5.1f}s for {seconds:4.1f}s of audio"
              f"  RTF {elapsed / seconds:.1f}x")

    print(f"\n  A three-sentence reply, generated the way ZEUS does it:")
    chunks = split_for_speech(LONG)
    print(f"    split into {len(chunks)} chunk(s)")
    first_sound = None
    total_compute = 0.0
    total_audio = 0.0
    started_all = time.time()
    for audio, rate, index, total in engine.generate_stream(LONG):
        if first_sound is None:
            first_sound = time.time() - started_all
        total_audio += len(audio) / float(rate)
    total_compute = time.time() - started_all

    compute, seconds = zip(*short_times)
    rtf = sum(compute) / sum(seconds)

    print(f"\n  TIME TO FIRST SOUND : {first_sound:.1f}s   <- what you actually wait for")
    print(f"  Whole reply         : {total_compute:.1f}s of compute for "
          f"{total_audio:.1f}s of speech")
    print(f"  STEADY STATE        : RTF {rtf:.1f}x — {verdict(rtf, for_input=False)}")

    if first_sound and total_audio:
        # Once the first chunk is playing, the rest is generated behind it.
        # Whether playback ever catches up is the interesting question.
        if total_compute > total_audio:
            gap = total_compute - total_audio
            print(f"\n  Note: generating this reply took {gap:.0f}s longer than the "
                  f"speech lasts,\n        so there will be pauses between sentences. "
                  f"The text is on\n        screen immediately either way.")
        else:
            print(f"\n  Generation keeps ahead of playback - speech will be continuous.")

    engine.close()
    return {"rtf": rtf, "load": load, "first_sound": first_sound,
            "directory": str(directory)}


# ---------------------------------------------------------------- main --

def advise(whisper: dict, chatterbox: dict):
    rule("WHAT TO DO WITH THIS")
    lines = []

    if not whisper and not chatterbox:
        print("\n  Neither model is installed. To get both:\n")
        print("      pip install -r requirements-voice.txt")
        print("      pip install torch torchaudio --index-url "
              "https://download.pytorch.org/whl/cpu")
        print("      python scripts\\setup_models.py\n")
        return

    if whisper:
        rtf = whisper["rtf"]
        if rtf > 2.0:
            lines.append(
                "Speech input is slow on this machine. Use a smaller Whisper\n"
                "     model - 'base' is about 4x faster than 'small' and is\n"
                "     accurate enough for spoken commands:\n"
                "         python scripts\\setup_models.py --whisper-size base")
        elif rtf > 1.0:
            lines.append(
                "Speech input works but is not instant. 'tiny' would be faster\n"
                "     still if you only give short commands.")
        else:
            lines.append("Speech input is comfortably fast. Nothing to change.")

    if chatterbox:
        rtf = chatterbox["rtf"]
        first = chatterbox.get("first_sound") or 0
        if rtf > 8.0:
            lines.append(
                f"Chatterbox is slow here ({rtf:.0f}x real time). It is fine for\n"
                "     short confirmations but painful for long answers. Two options:\n"
                "       - keep it, and lower [voice] max_spoken_chars in config.ini\n"
                "         so only the first part of a long answer is read aloud\n"
                "       - set [voice] tts_provider = system to use the Windows\n"
                "         voice, which is instant but flatter")
        elif rtf > 3.0:
            lines.append(
                f"Chatterbox is usable ({rtf:.0f}x real time). First sound after\n"
                f"     {first:.0f}s, then pauses between sentences on long answers.")
        else:
            lines.append(f"Chatterbox runs well here ({rtf:.1f}x real time).")

    if whisper and chatterbox:
        lines.append(
            "Both are loaded at once when you run with --voice. Together they\n"
            "     hold roughly 2-3 GB. If the machine starts swapping while a 7B\n"
            "     chat model is also resident, set [voice] preload = false so the\n"
            "     voice models load only when first used.")

    for index, line in enumerate(lines, start=1):
        print(f"\n  {index}. {line}")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=3,
                        help="timed runs per model (default 3)")
    parser.add_argument("--stt-only", action="store_true")
    parser.add_argument("--tts-only", action="store_true")
    args = parser.parse_args()

    print("\n" + "=" * 72)
    print("  ZEUS — LOCAL VOICE BENCHMARK")
    print("  Autonomous AI Command & Intelligence System")
    print("=" * 72)
    print("\n  Measuring the models you actually have installed.")
    print("  Nothing is downloaded. This takes a few minutes on a laptop CPU.")

    try:
        import psutil
        print(f"\n  CPU  : {psutil.cpu_count(logical=False)} physical / "
              f"{psutil.cpu_count(logical=True)} logical cores")
        print(f"  RAM  : {psutil.virtual_memory().total / (1024 ** 3):.1f} GB total, "
              f"{psutil.virtual_memory().available / (1024 ** 3):.1f} GB free")
    except Exception:
        pass

    whisper = {} if args.tts_only else benchmark_whisper(args.runs)
    chatterbox = {} if args.stt_only else benchmark_chatterbox(args.runs)
    advise(whisper, chatterbox)
    return 0


if __name__ == "__main__":
    sys.exit(main())
