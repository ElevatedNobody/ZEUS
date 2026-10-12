"""
Prove which device the local Whisper model actually runs on, and record it.

    python scripts/verify_whisper.py [--model large-v3-turbo] [--cpu-only]

Runs the staged test in agni_ops/voice.py: runtime import, audio decoding,
the CPU path on KNOWN speech (generated locally by the Windows speech engine
and compared word by word), then the GPU path - each stage in its own worker
process, so a native crash is recorded as that stage's result instead of
ending the test. The result is written to models/whisper/agni_verified.json,
which voice/speech_to_text.py reads when config.ini says `stt_device = auto`.

What changed (2026-09-25): this used to try CUDA and the CPU in-process on one
second of random noise. On 2026-09-24 every attempt failed with
"mkl_malloc: failed to allocate memory" - the machine was out of commit
memory, not broken - and the failed run left the previous "cuda/float16 OK"
record in place. Now the record is rewritten on every run, failures included.

Nothing is downloaded; the model must already be in models/whisper.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="", help="model name to record (e.g. large-v3-turbo)")
    parser.add_argument("--cpu-only", action="store_true")
    args = parser.parse_args()

    from voice.offline import WHISPER_REQUIRED_FILES, missing_files, resolve_whisper_dir
    model_dir = resolve_whisper_dir()
    absent = missing_files(model_dir, WHISPER_REQUIRED_FILES)
    if absent:
        print(f"FAIL  no Whisper model in {model_dir} (missing {', '.join(absent)})")
        return 1

    from agni_ops.voice import whisper_suite
    results = whisper_suite(Path(tempfile.mkdtemp(prefix="agni_verify_whisper_")),
                            gpu=not args.cpu_only, crash_test=False)
    for result in results:
        print(f"{result.status:<14} {result.component}: {result.reason}")

    record_path = model_dir / "agni_verified.json"
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
        if args.model:
            record["model"] = args.model
            record_path.write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
    except Exception:
        record = {}
    if not record.get("ok"):
        print("FAIL  Whisper did not transcribe the test speech on any device")
        return 1
    print(f"OK    recorded {record.get('device')}/{record.get('compute_type')} in {record_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
