"""
Speech: Whisper (listening) tested stage by stage, the microphone, the voices
(speaking), and a round trip between them.

Whisper stages, in the order that isolates a failure (never starting with the
largest model on the GPU):
    1 runtime import      the worker imports CTranslate2/faster-whisper
    2 audio decoding      a known WAV is decoded by PyAV
    3 smallest model      the smallest Whisper model on disk, CPU
    4 CPU inference       the configured model on CPU int8
    5 real transcription  known speech -> text, compared word by word
    6 GPU inference       the same, on CUDA - only after the CPU path works
    7 crash isolation     a deliberate native crash in a worker; ZEUS survives
Every stage runs in its OWN worker process (voice/stt_worker.py), so a native
crash is recorded as that stage's result instead of ending the test.

The known speech is generated locally by the Windows speech engine (SAPI), so
the expected words are exact and nothing is downloaded.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import wave
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from agni_ops.status import (DEGRADED, FAIL, INPUT_REQUIRED, NOT_TESTED, PASS, UNAVAILABLE, WARN,
                             Result)

ROOT = Path(__file__).resolve().parent.parent
KNOWN_SENTENCE = "The pump discharge pressure is twelve bar and the compressor is running normally."


def _words(text: str) -> List[str]:
    text = (text or "").lower().replace("12", "twelve")
    return re.findall(r"[a-z]+", text)


def similarity(expected: str, heard: str) -> float:
    """Fraction of the expected words that were heard (order-insensitive)."""
    want, got = _words(expected), set(_words(heard))
    return round(sum(1 for w in want if w in got) / max(1, len(want)), 2)


def make_known_speech(folder: Path, sentence: str = KNOWN_SENTENCE) -> Optional[Path]:
    """16 kHz mono WAV of `sentence` from the Windows speech engine."""
    target = folder / "known_speech.wav"
    if sys.platform != "win32":
        return None
    script = (
        "Add-Type -AssemblyName System.Speech;"
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        "$f = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000,"
        "[System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,"
        "[System.Speech.AudioFormat.AudioChannel]::Mono);"
        f"$s.SetOutputToWaveFile('{target}', $f); $s.Speak('{sentence}'); $s.Dispose()")
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                   capture_output=True, timeout=60,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return target if target.is_file() and target.stat().st_size > 10000 else None


def wav_stats(path: Path) -> Dict:
    """Duration and loudness of a WAV. soundfile first: Chatterbox writes
    32-bit FLOAT WAV (format 3), which the stdlib wave module cannot open -
    reading it with `wave` made a working synthesis look like a failure."""
    import numpy as np
    try:
        import soundfile
        audio, rate = soundfile.read(str(path), dtype="float32", always_2d=False)
        audio = np.asarray(audio, dtype="float32").reshape(-1)
        channels = soundfile.info(str(path)).channels
        seconds = len(audio) / max(1, rate * channels)
        rms = float((audio ** 2).mean() ** 0.5) if audio.size else 0.0
        return {"seconds": round(seconds, 2), "rms": round(rms, 4), "rate": rate}
    except ImportError:
        pass
    with wave.open(str(path), "rb") as handle:
        frames = handle.readframes(handle.getnframes())
        rate, width, channels = handle.getframerate(), handle.getsampwidth(), handle.getnchannels()
    dtype = {1: np.uint8, 2: np.int16, 4: np.int32}.get(width, np.int16)
    audio = np.frombuffer(frames, dtype=dtype).astype("float32")
    if width == 2:
        audio /= 32768.0
    seconds = len(audio) / max(1, rate * channels)
    rms = float((audio ** 2).mean() ** 0.5) if audio.size else 0.0
    return {"seconds": round(seconds, 2), "rms": round(rms, 4), "rate": rate}


# ----------------------------------------------------------------- Whisper --

def _worker_stage(label: str, commands: List[Tuple[str, dict]], timeout: float = 600
                  ) -> Tuple[bool, List[dict], str, Optional[int], str]:
    """Run commands in a FRESH worker. (ok, replies, error, exit_code, classification)."""
    from voice.stt_manager import WhisperWorker, WorkerCrash, classify_failure
    try:
        from core.paths import log_dir
        log_path = log_dir() / "stt_worker.log"
    except Exception:
        log_path = None
    worker = WhisperWorker(log_path=log_path)
    replies: List[dict] = []
    try:
        worker.start()
        for command, fields in commands:
            reply = worker.request(command, timeout=timeout, **fields)
            replies.append(reply)
            if not reply.get("ok"):
                name, why = classify_failure(None, reply.get("error", ""))
                return False, replies, reply.get("error", ""), None, name
        return True, replies, "", None, ""
    except WorkerCrash as crash:
        return False, replies, str(crash), crash.exit_code, crash.classification
    finally:
        worker.stop()


def _model_dirs() -> List[Path]:
    """Complete Whisper model folders on disk, smallest first."""
    try:
        from voice.offline import resolve_whisper_dir
        primary = resolve_whisper_dir()
    except Exception:
        primary = ROOT / "models" / "whisper"
    found = [d for d in sorted(primary.parent.glob("whisper*"))
             if d.is_dir() and (d / "model.bin").is_file()]
    return sorted(found, key=lambda d: (d / "model.bin").stat().st_size)


def _room(need_gb: float) -> Tuple[bool, str]:
    from core.sysmem import has_headroom
    ok, reason = has_headroom(need_gb, margin_gb=0.5)
    if not ok:
        from core.model_lifecycle import unload_all
        unload_all()
        ok, reason = has_headroom(need_gb, margin_gb=0.5)
    return ok, reason


def whisper_suite(workdir: Path, gpu: bool = True, crash_test: bool = True) -> List[Result]:
    results: List[Result] = []
    group = "whisper"
    details: Dict = {"python": sys.version.split()[0]}

    # 1 runtime import
    ok, replies, error, code, kind = _worker_stage("probe", [("probe", {})], timeout=120)
    probe = replies[0] if replies else {}
    details.update({k: probe.get(k) for k in ("ctranslate2", "faster_whisper", "cuda_devices",
                                              "cuda_compute_types", "audio_decoder")})
    try:
        from importlib.metadata import version
        details["onnxruntime"] = version("onnxruntime")
    except Exception:
        details["onnxruntime"] = "not installed"
    results.append(Result("Whisper 1: runtime import", PASS if ok else FAIL, group=group,
                          reason=(f"CTranslate2 {probe.get('ctranslate2')}, faster-whisper "
                                  f"{probe.get('faster_whisper')}, {probe.get('cuda_devices', 0)} CUDA "
                                  f"device(s)") if ok else f"{kind}: {error}",
                          where="voice/stt_worker.py (worker process)", details=details))
    if not ok:
        return results

    # 2 audio decoding
    known = make_known_speech(workdir)
    if known is None:
        results.append(Result("Whisper 2: audio decoding", UNAVAILABLE, group=group,
                              reason="could not generate the known-speech WAV (Windows SAPI)"))
        return results
    ok, replies, error, code, kind = _worker_stage("decode", [("decode", {"path": str(known)})], 120)
    decoded = replies[0] if replies else {}
    decode_ok = ok and decoded.get("samples", 0) > 16000 and decoded.get("rms", 0) > 0.005
    results.append(Result("Whisper 2: audio decoding", PASS if decode_ok else FAIL, group=group,
                          reason=(f"{decoded.get('seconds')} s decoded by {probe.get('audio_decoder')}, "
                                  f"RMS {decoded.get('rms')}") if decode_ok else f"{kind}: {error}"))

    dirs = _model_dirs()
    if not dirs:
        results.append(Result("Whisper model", UNAVAILABLE, group=group,
                              reason="no Whisper model in models/ - run STEP 1 : INSTALL"))
        return results
    largest = dirs[-1]
    size_gb = round((largest / "model.bin").stat().st_size / 1024 ** 3, 2)
    details.update(model_path=str(largest), model_size_gb=size_gb)

    def transcribe_stage(title: str, model_dir: Path, device: str, compute: str, need_gb: float):
        room, why = _room(need_gb)
        if not room:
            return Result(title, DEGRADED, group=group, reason=f"insufficient runtime memory - {why}",
                          fallback="another backend in the chain"), None
        ok, replies, error, code, kind = _worker_stage(title, [
            ("load", {"model_dir": str(model_dir), "device": device, "compute_type": compute,
                      "cpu_threads": 8 if device == "cpu" else 0}),
            ("transcribe", {"path": str(known), "language": "en",
                            "options": {"beam_size": 1, "vad_filter": False}})])
        if not ok:
            reason = f"{kind}: {error[:240]}" + (f" (exit 0x{code & 0xFFFFFFFF:08X})" if code else "")
            return Result(title, FAIL, group=group, reason=reason,
                          where=f"{model_dir.name} {device}/{compute}",
                          evidence=error[-1200:]), None
        load, heard = replies[0], replies[1]
        score = similarity(KNOWN_SENTENCE, heard.get("text", ""))
        status = PASS if score >= 0.8 else (WARN if score >= 0.5 else FAIL)
        return Result(title, status, group=group,
                      reason=(f"heard '{heard.get('text', '')[:80]}' ({int(score * 100)}% of words; "
                              f"load {load.get('seconds')} s, transcribe {heard.get('seconds')} s, "
                              f"peak {load.get('peak_private_mb')} MB)"),
                      where=f"{model_dir.name} {device}/{compute}",
                      details={"score": score, "load": load, "heard": heard}), (device, compute, score)

    # 3 smallest model
    if len(dirs) > 1:
        result, _ = transcribe_stage("Whisper 3: smallest model (CPU)", dirs[0], "cpu", "int8", 1.5)
        results.append(result)
    else:
        results.append(Result("Whisper 3: smallest model (CPU)", NOT_TESTED, group=group,
                              reason=f"only one Whisper model is on disk ({largest.name}, {size_gb} GB); "
                                     "a smaller one was never downloaded"))
    # 4 + 5 CPU inference on real speech
    cpu_result, cpu_ok = transcribe_stage("Whisper 4-5: CPU int8, real speech", largest, "cpu", "int8", 5.0)
    results.append(cpu_result)
    # 6 GPU inference - only after the CPU path works
    gpu_ok = None
    if not gpu:
        results.append(Result("Whisper 6: GPU float16", NOT_TESTED, group=group, reason="--no-gpu"))
    elif not probe.get("cuda_devices"):
        results.append(Result("Whisper 6: GPU float16", UNAVAILABLE, group=group,
                              reason="no CUDA device visible to CTranslate2"))
    elif cpu_result.status not in (PASS, WARN):
        results.append(Result("Whisper 6: GPU float16", NOT_TESTED, group=group,
                              reason="the CPU path does not work yet - GPU is tested after it"))
    else:
        gpu_result, gpu_ok = transcribe_stage("Whisper 6: GPU float16, real speech", largest,
                                              "cuda", "float16", 4.3)
        results.append(gpu_result)

    # 7 crash isolation
    if crash_test:
        os.environ["AGNI_STT_ALLOW_CRASH_TEST"] = "1"
        started = time.time()
        ok, replies, error, code, kind = _worker_stage("crash", [("crash_test", {})], timeout=60)
        survived = True                          # we are still here
        isolated = (not ok) and kind in ("access_violation", "fail_fast", "crash")
        results.append(Result("Whisper 7: native crash isolation", PASS if isolated and survived else FAIL,
                              group=group, reason=(f"worker died with {kind} (exit 0x{(code or 0) & 0xFFFFFFFF:08X}) "
                                                   f"in {round(time.time() - started, 1)} s; ZEUS's process "
                                                   f"unaffected; no Windows error dialog")
                              if isolated else f"unexpected: ok={ok} kind={kind} {error[:200]}"))

    # the record the runtime reads (models/whisper/agni_verified.json)
    working = gpu_ok if gpu_ok else cpu_ok
    record = {"model": largest.name if largest.name != "whisper" else "large-v3-turbo",
              "verified_at": time.time(), "verified_by": "agni_ops.voice",
              "ok": bool(working), "attempts": [r.to_dict() for r in results if r.component.startswith("Whisper")]}
    if working:
        record.update(device=working[0], compute_type=working[1])
    else:
        record.update(device="cpu", compute_type="int8")     # never leave a stale 'cuda OK'
    try:
        (largest / "agni_verified.json").write_text(json.dumps(record, indent=2, default=str),
                                                    encoding="utf-8")
    except OSError:
        pass
    final = PASS if gpu_ok else (DEGRADED if cpu_ok else FAIL)
    results.append(Result("Whisper (final state)", final, group=group,
                          reason=("GPU float16 transcription verified" if gpu_ok else
                                  "CPU transcription verified; GPU backend not available" if cpu_ok else
                                  "no backend transcribed the known speech"),
                          fallback="CPU int8 (verified)" if cpu_ok else "text input",
                          details=details))
    return results


# -------------------------------------------------------------- microphone --

def microphone_test(seconds: float = 4.0, prompt: bool = False) -> Result:
    try:
        from voice.speech_to_text import SILENCE_RMS, default_input_device, list_input_devices
    except Exception as error:
        return Result("microphone", UNAVAILABLE, reason=str(error), group="voice")
    devices = list_input_devices()
    if not devices:
        return Result("microphone", UNAVAILABLE, group="voice",
                      reason="no input device - connect a microphone / allow access in "
                             "Settings > Privacy & security > Microphone")
    device = default_input_device() or devices[0]
    try:
        import numpy as np
        import sounddevice as sd
        rate = int(device.get("default_samplerate") or 16000)
        if prompt:
            print(f"    >>> Speak now for {int(seconds)} seconds: say 'ZEUS status check' <<<", flush=True)
        audio = sd.rec(int(seconds * rate), samplerate=rate, channels=1, dtype="float32",
                       device=device["index"])
        sd.wait()
        level = float(np.sqrt(np.mean(np.square(audio))))
    except Exception as error:
        return Result("microphone", FAIL, group="voice", reason=f"capture failed: {error}",
                      where=device.get("name", ""))
    if level < SILENCE_RMS:
        return Result("microphone", INPUT_REQUIRED, group="voice",
                      reason=f"captured {seconds:.0f} s from '{device['name']}' but it was silent "
                             f"(RMS {level:.4f}) - speak during TEST_AGNI.bat voice to test transcription",
                      details={"rms": level})
    return Result("microphone", PASS, group="voice",
                  reason=f"'{device['name']}' captured sound (RMS {level:.3f})",
                  details={"rms": level, "audio_rate": rate})


# -------------------------------------------------------------------- TTS --

def tts_suite(workdir: Path, chatterbox: bool = True) -> List[Result]:
    results: List[Result] = []
    # Windows voice (the always-available fallback)
    known = make_known_speech(workdir, "ZEUS voice check. Pressure is stable.")
    if known:
        stats = wav_stats(known)
        results.append(Result("TTS: Windows voice (SAPI)", PASS if stats["seconds"] > 1 and stats["rms"] > 0.01 else FAIL,
                              group="tts", reason=f"{stats['seconds']} s of audio, RMS {stats['rms']}",
                              details={"wav": str(known)}))
    else:
        results.append(Result("TTS: Windows voice (SAPI)", UNAVAILABLE, group="tts",
                              reason="SAPI did not produce audio"))
    if not chatterbox:
        results.append(Result("TTS: Chatterbox", NOT_TESTED, group="tts", reason="skipped"))
        return results
    try:
        from voice.providers import ChatterboxBridgeProvider, ProviderState
        provider = ChatterboxBridgeProvider()
        report = provider.probe()
        if report.state in (ProviderState.INSTALLABLE, ProviderState.MODEL_MISSING,
                            ProviderState.UNSUPPORTED):
            results.append(Result("TTS: Chatterbox", UNAVAILABLE, group="tts", reason=report.detail,
                                  fallback="Windows voice"))
            return results
        activated = provider.activate()
        if activated.state is ProviderState.FAILED:
            results.append(Result("TTS: Chatterbox", DEGRADED, group="tts",
                                  reason=f"environment did not load: {activated.detail}"[:240],
                                  fallback="Windows voice"))
            return results
        try:
            # Two sentences, as a reply would be spoken: the first pays any
            # load, the second shows the steady pace. Speech only flows
            # without gaps when a sentence is generated faster than it plays.
            timings = []
            for sentence in ("Pump P one oh one is running normally.",
                             "Discharge pressure is steady at ten bar."):
                started = time.time()
                wav = provider.synthesise(sentence)
                timings.append(round(time.time() - started, 1))
                if wav is None or not Path(wav).is_file():
                    failure = getattr(provider, "last_failure", None) or {}
                    results.append(Result("TTS: Chatterbox", DEGRADED, group="tts",
                                          reason=(f"synthesis failed - {failure.get('classification', '')} "
                                                  f"{failure.get('reason', '')}").strip(),
                                          evidence=str(failure)[:800],
                                          fallback="Windows voice (verified above)"))
                    return results
            stats = wav_stats(Path(wav))
            device = (getattr(provider, "_last_timing", {}) or {}).get("device") or provider._device()
            kept = provider._keep_loaded()
            reason = (f"{device.upper()}{', kept loaded' if kept else ', loaded per sentence'}: "
                      f"first sentence {timings[0]} s, next {timings[1]} s for "
                      f"{stats['seconds']} s of speech (RMS {stats['rms']})")
            if stats["seconds"] <= 0.8 or stats["rms"] <= 0.005:
                status, fallback = FAIL, "Windows voice"
            elif timings[1] > stats["seconds"] * 1.5:
                status = WARN
                fallback = ("works, but too slow for conversation - a GPU build of torch in the "
                            "Chatterbox environment fixes it (AGNI_REPAIR_CHANGELOG.md section 6)")
            else:
                status, fallback = PASS, ""
            results.append(Result("TTS: Chatterbox", status, group="tts", reason=reason,
                                  fallback=fallback, details={"wav": str(wav), "timings": timings,
                                                              "device": device, "kept_loaded": kept}))
        finally:
            provider.close()
    except Exception as error:
        results.append(Result("TTS: Chatterbox", DEGRADED, group="tts",
                              reason=f"{type(error).__name__}: {error}"[:240],
                              fallback="Windows voice"))
    return results


# ------------------------------------------------------------ WHISPER STATUS --

STATUS_CHECKS = ("Runtime available", "Model available", "Model loaded", "Microphone available", "STT pipeline ready")


def whisper_status(load: bool = True, workdir: Optional[Path] = None) -> List[Dict]:
    """The five facts about speech recognition on this machine, each MEASURED with
    the app's own code (worker probe, SpeechToText, the real microphone) - with the
    actual reason when one fails. load=False stops after the files (no model load)."""
    lines: List[Dict] = []

    def add(check: str, ok: Optional[bool], detail: str) -> None:
        lines.append({"check": check, "ok": ok, "detail": detail})

    # 1 runtime - in a worker process, the interpreter the app itself uses for Whisper
    ok, replies, error, _code, kind = _worker_stage("probe", [("probe", {})], timeout=120)
    probe = replies[0] if replies else {}
    runtime_ok = bool(ok)
    if ok:
        cuda = probe.get("cuda_devices") or 0
        add(STATUS_CHECKS[0], True, f"faster-whisper {probe.get('faster_whisper')} + CTranslate2 {probe.get('ctranslate2')}; "
            + (f"{cuda} CUDA device(s)" if cuda else "no CUDA device - CPU only"))
    else:
        missing = re.search(r"No module named '([^']+)'", error or "")
        add(STATUS_CHECKS[0], False, (f"dependency missing: {missing.group(1)} - install requirements-voice.txt "
                                      f"(SETUP_VOICE.bat)") if missing else f"{kind or 'runtime error'}: {(error or '')[:220]}")

    # 2 model files - the primary, and the low-memory fallback
    try:
        from voice.offline import WHISPER_REQUIRED_FILES, missing_files, resolve_whisper_dir
        from voice.stt_manager import _smaller_model_dirs
        primary = resolve_whisper_dir()
        absent = missing_files(primary, WHISPER_REQUIRED_FILES) if primary.is_dir() else ["the folder itself"]
        fallbacks = _smaller_model_dirs(primary.parent, primary) if primary.parent.is_dir() else []
    except Exception as exc:
        primary, absent, fallbacks = None, [f"model path could not be resolved: {exc}"], []
    files_ok = primary is not None and not absent
    if not files_ok:
        add(STATUS_CHECKS[1], False, (f"model missing in {primary}: {', '.join(absent)} - install once with "
                                      f"python scripts/setup_models.py --whisper-only --whisper-size large-v3-turbo")
            if primary is not None else absent[0])
    else:
        size = (primary / "model.bin").stat().st_size / 1024 ** 3
        fb = ", ".join(f"{d.name} ({(d / 'model.bin').stat().st_size / 1024 ** 3:.2f} GB)" for d in fallbacks)
        add(STATUS_CHECKS[1], True, f"primary {primary.name} ({size:.2f} GB) at {primary}; low-memory fallback: "
            + (fb or "none installed (python scripts/setup_models.py --whisper-fallback small)"))
    if not load:
        return lines

    # 3 model loaded - the app's own recogniser and its backend chain
    stt = None
    if runtime_ok and files_ok:
        try:
            from voice.speech_to_text import SpeechToText
            stt = SpeechToText(preload=True)
            worker = (stt.status().get("worker") or {})
            if stt.is_available and (worker.get("active") or stt.status().get("model_loaded")):
                active = worker.get("active") or f"{stt.status().get('device')}/{stt.status().get('compute_type')} in process"
                refused = [a for a in worker.get("attempts") or [] if not a.get("ok")]
                note = "; ".join(f"{a.get('backend')} refused: {str(a.get('error'))[:80]}" for a in refused[:3])
                smaller = next((d.name for d in fallbacks if f" {d.name} " in f" {active} "), "")
                add(STATUS_CHECKS[2], True, f"{active}"
                    + (f" - low-memory fallback {smaller} active" if smaller
                       else " - CPU fallback active" if "cpu/" in active else "")
                    + (f" ({note})" if note else ""))
            else:
                add(STATUS_CHECKS[2], False, f"failed to load: {stt.last_error or worker.get('last_error') or 'unknown'}")
        except Exception as exc:
            add(STATUS_CHECKS[2], False, f"failed to load: {type(exc).__name__}: {exc}")
    else:
        add(STATUS_CHECKS[2], None, "not attempted - fix the line(s) above first")

    # 4 microphone - a real stream on the configured / default device
    try:
        import numpy as np
        import sounddevice as sd
        from voice.speech_to_text import default_input_device, list_input_devices
        devices = list_input_devices()
        device = default_input_device() or (devices[0] if devices else None)
        if not device:
            add(STATUS_CHECKS[3], False, "microphone unavailable: no input device - connect one, or allow access in "
                                         "Settings > Privacy & security > Microphone")
        else:
            rate = int(device.get("default_samplerate") or 16000)
            audio = sd.rec(int(0.5 * rate), samplerate=rate, channels=1, dtype="float32", device=device["index"])
            sd.wait()
            add(STATUS_CHECKS[3], True, f"'{device['name']}' opened at {rate} Hz ({len(devices)} input device(s); "
                                        f"level {float(np.sqrt(np.mean(np.square(audio)))):.4f})")
    except Exception as exc:
        add(STATUS_CHECKS[3], False, f"microphone unavailable: {exc}")

    # 5 pipeline - known speech through the loaded recogniser
    if stt is not None and lines[2]["ok"]:
        folder = workdir or Path(__import__("tempfile").mkdtemp(prefix="agni_stt_status_"))
        wav = make_known_speech(folder)
        if wav is None:
            add(STATUS_CHECKS[4], None, "not tested - Windows voice could not make the known-speech sample")
        else:
            try:
                heard = stt.transcribe_file(str(wav))
                score = similarity(KNOWN_SENTENCE, heard)
                add(STATUS_CHECKS[4], score >= 0.7, f"heard '{heard}' ({int(score * 100)}% of the words)" if heard
                    else f"transcription returned nothing ({stt.last_error or 'no error reported'})")
            except Exception as exc:
                add(STATUS_CHECKS[4], False, f"transcription failed: {type(exc).__name__}: {exc}")
    else:
        add(STATUS_CHECKS[4], None, "not tested - the model did not load")
    if stt is not None:
        try:
            stt.close()
        except Exception:
            pass
    return lines


def render_status(lines: List[Dict]) -> str:
    out = ["WHISPER STATUS"]
    for line in lines:
        mark = "✓" if line["ok"] else ("✗" if line["ok"] is False else "–")
        out.append(f"{mark} {line['check']:<21} {line['detail']}")
    return "\n".join(out)


def stt_devices() -> Result:
    """Step 1 of the STT trace: what the operating system offers as input."""
    try:
        from voice.speech_to_text import default_input_device, list_input_devices
        devices = list_input_devices()
        default = default_input_device()
    except Exception as error:
        return Result("STT 1: input devices", FAIL, group="stt", reason=f"audio stack not usable: {error}")
    if not devices:
        return Result("STT 1: input devices", UNAVAILABLE, group="stt",
                      reason="no input device - connect a microphone / allow access in Settings > Privacy & "
                             "security > Microphone")
    return Result("STT 1: input devices", PASS, group="stt",
                  reason=f"{len(devices)} input device(s); default: {(default or devices[0])['name']}",
                  details={"devices": [d["name"] for d in devices[:12]]})


def stt_live_trace(seconds: float = 5.0, prompt: bool = False) -> Result:
    """The last step: the app's own listen_once on the real microphone, with every
    stage it publishes recorded - the same path the MIC button takes."""
    try:
        from voice.speech_to_text import SpeechToText, _stage_listeners, subscribe_stage
    except Exception as error:
        return Result("STT 5: live listen (app path)", FAIL, group="stt", reason=str(error))
    stages: List[str] = []

    def record(stage, detail=""):
        stages.append(stage if not detail else f"{stage} ({str(detail)[:60]})")
    subscribe_stage(record)
    try:
        stt = SpeechToText(preload=False)
        if prompt:
            print(f"    >>> When LISTENING appears, say: 'status of pump P-101' ({int(seconds)} s) <<<", flush=True)
        text = stt.listen_once(duration=seconds)
        error = stt.last_error
        stt.close()
    except Exception as exc:
        return Result("STT 5: live listen (app path)", FAIL, group="stt", reason=str(exc)[:240],
                      details={"stages": stages})
    finally:
        if record in _stage_listeners:
            _stage_listeners.remove(record)
    trace = " > ".join(stages) or "no stage published"
    if text:
        return Result("STT 5: live listen (app path)", PASS, group="stt", reason=f"heard '{text}' - {trace}",
                      details={"stages": stages})
    if error == "silence" or any(s.startswith("NO SPEECH") for s in stages):
        return Result("STT 5: live listen (app path)", INPUT_REQUIRED, group="stt",
                      reason=f"the microphone was silent - speak during the check to test it live ({trace})",
                      details={"stages": stages})
    return Result("STT 5: live listen (app path)", FAIL, group="stt", reason=f"{error or 'no text'} - {trace}",
                  details={"stages": stages})


def round_trip(workdir: Path) -> Result:
    """TTS -> WAV -> Whisper (worker, via SpeechToText) -> the same words back."""
    # No spelled-out numbers: "two zero one" comes back as "201", which is
    # right, but would make this a test of number formatting.
    sentence = "Open the maintenance report for the feed water compressor."
    wav = make_known_speech(workdir, sentence)
    if wav is None:
        return Result("voice round trip (TTS -> STT)", UNAVAILABLE, group="voice", reason="no TTS audio")
    try:
        from voice.speech_to_text import SpeechToText
        stt = SpeechToText(preload=True)
        if not stt.is_available:
            return Result("voice round trip (TTS -> STT)", FAIL, group="voice",
                          reason=f"STT unavailable: {stt.last_error}")
        heard = stt.transcribe_file(str(wav))
        status = stt.status()
        stt.close()
    except Exception as error:
        return Result("voice round trip (TTS -> STT)", FAIL, group="voice", reason=str(error)[:240])
    score = similarity(sentence, heard)
    return Result("voice round trip (TTS -> STT)", PASS if score >= 0.7 else FAIL, group="voice",
                  reason=f"spoke '{sentence}', heard '{heard}' ({int(score * 100)}%) via "
                         f"{(status.get('worker') or {}).get('active')} ({status.get('isolation')})")
