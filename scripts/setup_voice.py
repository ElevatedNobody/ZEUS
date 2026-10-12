"""
ZEUS VOICE SETUP - the one command that makes ZEUS listen and speak.
Autonomous AI Command & Intelligence System.

    python scripts/setup_voice.py                 install everything
    python scripts/setup_voice.py --check         report only, change nothing
    python scripts/setup_voice.py --dry-run       print every command, run none
    python scripts/setup_voice.py --stt-only      speech IN  (Whisper) only
    python scripts/setup_voice.py --tts-only      speech OUT (Chatterbox) only
    python scripts/setup_voice.py --whisper-size small
    python scripts/setup_voice.py --cuda          GPU build of torch
    python scripts/setup_voice.py --repair        rebuild whatever is broken
    python scripts/setup_voice.py --uninstall     remove voice, keep ZEUS

Run it ONCE with the internet connected. After that, voice works with Wi-Fi
and Ethernet unplugged: voice/offline.py sets HF_HUB_OFFLINE before the model
libraries import, and both engines load from models/ by path.

The six stages
--------------
    1  environment      Python version, pip, platform, free disk
    2  listening        faster-whisper + audio I/O, in ZEUS's own environment
    3  Whisper model    models/whisper/            (~145 MB at 'base')
    4  speaking         Chatterbox, in its OWN environment  (~3.2 GB)
    5  verify           imports, model files, a real Whisper load
    6  microphone       lists input devices, so a missing mic is caught here

Why speaking and listening are installed differently
----------------------------------------------------
faster-whisper is small, pins nothing aggressively, and lives happily beside
the rest of ZEUS - so it goes in the main environment.

chatterbox-tts is the opposite. It pins torch==2.6.0, transformers==5.2.0 and
numpy<2.0.0. ZEUS's analytics and document builders need numpy 2.x, so putting
Chatterbox in the same environment means one of them loses - and the loser is
whichever pip resolved last, which is not a decision anyone made. So stage 4
hands off to scripts/setup_voice_engine.py, which builds a SECOND environment
under models/voice/chatterbox/env and talks to it over a JSON pipe. Nothing
Chatterbox pins can reach PySide6, numpy, Ollama or the RAG stack.

The weights are not duplicated. Both stages download into the directories
voice/offline.py resolves - models/whisper and models/chatterbox - so the
bridge and the in-process provider share one copy.

Nothing here reports success it has not checked. Stage 4 says READY only after
a real sentence has been synthesised to a real audio file, and stage 5 loads
the Whisper model rather than looking at the directory listing. If voice cannot
be installed, ZEUS still starts: text mode is unaffected, and the voice status
says DISABLED rather than pretending.
"""

from __future__ import annotations

import argparse
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.branding import APP_NAME, APP_TAGLINE  # noqa: E402

ENGINE_SCRIPT = ROOT / "scripts" / "setup_voice_engine.py"

BAR = "=" * 70

# Speech IN, plus audio capture and playback. Everything here is safe to put in
# ZEUS's own environment: no hard version equalities, no torch.
STT_PACKAGES = [
    {"name": "audio in and out", "packages": ["sounddevice", "soundfile"]},
    {"name": "speech to text (Whisper)",
     "packages": ["faster-whisper", "ctranslate2", "huggingface_hub"]},
]


def say(text: str = ""):
    print(text, flush=True)


def step(number: int, title: str):
    say()
    say(f"[{number}/6] {title}")


def ok(text: str):
    say(f"      [OK]   {text}")


def warn(text: str):
    say(f"      [WARN] {text}")


def bad(text: str):
    say(f"      [FAIL] {text}")


def fix(text: str):
    say(f"             -> {text}")


# ------------------------------------------------------- 1. environment --

def environment() -> dict:
    step(1, "Environment")
    info = {
        "python": sys.version.split()[0],
        "python_tuple": sys.version_info[:2],
        "executable": sys.executable,
        "platform": f"{platform.system()} {platform.release()}",
        "free_gb": None,
    }
    ok(f"Python {info['python']} ({info['executable']})")
    ok(f"Platform {info['platform']}")

    try:
        usage = shutil.disk_usage(ROOT)
        info["free_gb"] = usage.free / (1024 ** 3)
        line = f"{info['free_gb']:.1f} GB free on the ZEUS drive"
        if info["free_gb"] < 8:
            bad(line)
            fix("free up space - the voice models and their environment need "
                "about 8 GB, and about 12 GB while installing")
        elif info["free_gb"] < 12:
            warn(line + " (12 GB is comfortable; 8 GB is the floor)")
        else:
            ok(line)
    except OSError as error:
        warn(f"could not measure free disk space: {error}")

    try:
        import pip  # noqa: F401
        ok("pip is available")
    except ImportError:
        bad("pip is not available in this interpreter")
        fix(f'"{sys.executable}" -m ensurepip --upgrade')
    return info


# ---------------------------------------------- 2. listening (main env) --

def pip_install(packages, index=(), dry_run: bool = False,
                no_deps: bool = False) -> bool:
    """Install into the interpreter running THIS script.

    `sys.executable -m pip`, never bare `pip`: on a machine with two Pythons
    installed - which is the normal state of a Windows laptop that has ever
    installed Python twice - bare pip belongs to whichever one is first on
    PATH, and that is how packages end up installed for an interpreter ZEUS
    is not using.
    """
    command = [sys.executable, "-m", "pip", "install", "--upgrade"]
    if no_deps:
        command.append("--no-deps")
    command += list(packages) + list(index)
    say(f"      $ {' '.join(command[2:])}")
    if dry_run:
        return True
    try:
        return subprocess.run(command, text=True).returncode == 0
    except Exception as error:
        bad(f"pip failed to start: {error}")
        return False


def install_listening(info: dict, dry_run: bool) -> bool:
    step(2, "Installing the listening stack (ZEUS's own environment)")
    major, minor = info["python_tuple"]
    if (major, minor) < (3, 9):
        bad(f"Python {info['python']} is too old for faster-whisper (needs 3.9+)")
        fix("install Python 3.11 or 3.12 from python.org and run ZEUS on that")
        return False

    everything_ok = True
    for group in STT_PACKAGES:
        say(f"      {group['name']}")
        if not pip_install(group["packages"], dry_run=dry_run):
            bad(f"failed: {group['name']}")
            everything_ok = False
        say()
    return everything_ok


def stt_imports(dry_run: bool = False) -> dict:
    """Import what speech-in needs. This, not pip's exit code, is the proof."""
    checks = {
        "sounddevice": "microphone capture and playback",
        "soundfile": "audio file I/O",
        "numpy": "audio buffers",
        "faster_whisper": "Whisper speech to text",
        "ctranslate2": "Whisper runtime",
    }
    if dry_run:
        return {name: (None, "dry run") for name in checks}
    results = {}
    for module, purpose in checks.items():
        try:
            __import__(module)
            version = getattr(sys.modules[module], "__version__", "")
            results[module] = (True, version or purpose)
        except Exception as error:
            results[module] = (False, f"{type(error).__name__}: {error}")
    return results


# ------------------------------------------------------ 3. Whisper model --

def download_whisper(whisper_size: str, dry_run: bool) -> bool:
    step(3, "Downloading the Whisper model")
    if dry_run:
        say(f"      $ python scripts/setup_models.py --whisper-only "
            f"--whisper-size {whisper_size}")
        return True

    # setup_models.py owns every model URL in ZEUS. This calls it rather than
    # repeating the repository names, so there is exactly one place a model
    # source can be wrong.
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import setup_models
    except Exception as error:
        bad(f"could not load scripts/setup_models.py: {error}")
        return False

    argv = sys.argv
    try:
        sys.argv = ["setup_models.py", "--whisper-only",
                    "--whisper-size", whisper_size]
        return setup_models.main() == 0
    except SystemExit as error:
        return error.code in (0, None)
    except Exception as error:
        bad(f"model download failed: {type(error).__name__}: {error}")
        return False
    finally:
        sys.argv = argv


# ------------------------------------------------- 4. speaking (own env) --

def _engine(*flags: str, dry_run: bool = False) -> int:
    """Run the Chatterbox installer as a subprocess and return its exit code.

    A subprocess rather than an import, deliberately: that script creates a
    virtual environment and pip-installs a pinned torch into it, and none of
    that should share a process - or a set of loaded modules - with ZEUS.
    """
    if not ENGINE_SCRIPT.is_file():
        bad(f"missing installer: {ENGINE_SCRIPT}")
        fix("the download is incomplete - re-extract the ZEUS zip")
        return 3
    command = [sys.executable, str(ENGINE_SCRIPT), *flags]
    say(f"      $ {' '.join(command[1:])}")
    if dry_run:
        return 0
    say()
    try:
        return subprocess.run(command, text=True).returncode
    except Exception as error:
        bad(f"could not start the speech engine installer: {error}")
        return 3


def install_speaking(dry_run: bool, cuda: bool, repair: bool) -> bool:
    step(4, "Installing the speech engine (its own environment, about 3.2 GB)")
    say("      Chatterbox gets a dedicated Python environment under")
    say("      models/voice/chatterbox/env so its pinned torch, transformers")
    say("      and numpy cannot disturb ZEUS. This is the long stage.")
    say()
    flags = []
    if cuda:
        flags.append("--cuda")
    if repair:
        flags.append("--repair")
    return _engine(*flags, dry_run=dry_run) == 0


# ------------------------------------------------------------ 5. verify --

def verify(dry_run: bool) -> dict:
    step(5, "Verifying")
    outcome = {"stt_packages": {}, "whisper_model": False, "whisper_loads": False,
               "speaking": False}

    outcome["stt_packages"] = stt_imports(dry_run)
    for module, (good, detail) in outcome["stt_packages"].items():
        if good is None:
            say(f"      [ -- ] {module}: {detail}")
        elif good:
            ok(f"{module} {detail}")
        else:
            bad(f"{module}: {detail}")

    if dry_run:
        return outcome

    try:
        from voice.offline import check_voice_models
        status = check_voice_models()
        outcome["whisper_model"] = bool(status["whisper"]["present"])
        for label, key in (("Whisper", "whisper"), ("Chatterbox", "chatterbox")):
            entry = status[key]
            if entry["present"]:
                ok(f"{label} model files present in {entry['directory']}")
            else:
                bad(f"{label} model missing from {entry['directory']}: "
                    f"{', '.join(entry['missing'])}")
    except Exception as error:
        bad(f"could not check the model files: {error}")

    if outcome["whisper_model"] and outcome["stt_packages"].get(
            "faster_whisper", (False,))[0]:
        say("      loading the Whisper model (the first load takes a few "
            "seconds) ...")
        try:
            from voice.speech_to_text import SpeechToText
            stt = SpeechToText()
            if stt.load():
                ok("Whisper loaded from the local directory - speech in is ready")
                outcome["whisper_loads"] = True
                stt.close()
            else:
                bad(f"Whisper did not load: {stt.last_error}")
        except Exception as error:
            bad(f"Whisper load raised: {type(error).__name__}: {error}")

    # Speech out is verified by the engine installer, which synthesises a real
    # sentence. Asking it again is cheap and keeps this report honest rather
    # than inferring readiness from the presence of files.
    say("      checking the speech engine ...")
    outcome["speaking"] = _engine("--check") == 0
    return outcome


# -------------------------------------------------------- 6. microphone --

def microphone(dry_run: bool) -> bool:
    step(6, "Microphone and speakers")
    if dry_run:
        return True
    try:
        from voice.speech_to_text import list_input_devices
        devices = list_input_devices()
    except Exception as error:
        bad(f"could not enumerate audio devices: {error}")
        fix(f'"{sys.executable}" -m pip install sounddevice')
        return False
    if not devices:
        bad("no microphone found")
        fix("plug one in, then on Windows check Settings > Privacy & security > "
            "Microphone > 'Let desktop apps access your microphone'")
        return False
    for device in devices[:6]:
        ok(f"input {device['index']}: {device['name']} "
           f"({device['channels']} ch, {int(device['default_samplerate'])} Hz)")
    say("      Set a specific device with input_device= in config.ini [voice] "
        "if the default is wrong.")
    return True


# -------------------------------------------------------------- driver --

def report_check() -> int:
    outcome = verify(dry_run=False)
    microphone(dry_run=False)

    listening = (outcome["whisper_loads"]
                 and all(good for good, _ in outcome["stt_packages"].values()))
    speaking = outcome["speaking"]

    say()
    say(BAR)
    say(f"  LISTENING (Whisper):    {'READY' if listening else 'NOT READY'}")
    say(f"  SPEAKING  (Chatterbox): {'READY' if speaking else 'NOT READY'}")
    say()
    if listening and speaking:
        say("  Voice is ready. Start ZEUS:  start_medha_industrial.bat")
    else:
        say("  Install what is missing:      SETUP_VOICE.bat")
        say("  ZEUS still runs in text mode with no voice at all.")
    say(BAR)
    return 0 if (listening and speaking) else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Set up ZEUS's local voice: Whisper in, Chatterbox out.")
    parser.add_argument("--check", action="store_true",
                        help="report the current state and exit; installs nothing")
    parser.add_argument("--dry-run", action="store_true",
                        help="print every command that would run, run none of them")
    parser.add_argument("--stt-only", action="store_true",
                        help="speech IN only (Whisper): skip the 3.2 GB stage")
    parser.add_argument("--tts-only", action="store_true",
                        help="speech OUT only (Chatterbox)")
    parser.add_argument("--whisper-size", default="base",
                        help="tiny, base, small, medium, large-v3 (default: base)")
    parser.add_argument("--cuda", action="store_true",
                        help="install the CUDA build of torch instead of the CPU build")
    parser.add_argument("--repair", action="store_true",
                        help="reinstall whatever is broken, keep what works")
    parser.add_argument("--uninstall", action="store_true",
                        help="remove the speech engine and its environment")
    # Accepted for the older command lines that still appear in notes and docs.
    parser.add_argument("--packages-only", action="store_true",
                        help=argparse.SUPPRESS)
    parser.add_argument("--models-only", action="store_true",
                        help=argparse.SUPPRESS)
    args = parser.parse_args()

    say(BAR)
    say(f"  {APP_NAME} VOICE SETUP")
    say(f"  {APP_TAGLINE}")
    say()
    say("  Whisper listens, Chatterbox speaks. Both run on this machine.")
    say("  Needs the internet ONCE; after this, voice runs disconnected.")
    say(BAR)

    if args.uninstall:
        say()
        say("Removing the speech engine. ZEUS and its documents are untouched.")
        return _engine("--uninstall")

    info = environment()

    if args.check:
        return report_check()

    do_stt = not args.tts_only
    do_tts = not args.stt_only
    if args.models_only:                 # old flag: models, no pip
        do_stt = do_tts = True
    if args.packages_only:               # old flag: pip, no models
        do_stt, do_tts = True, False

    listening_ok = speaking_ok = True

    if do_stt:
        listening_ok = install_listening(info, args.dry_run)
        if not args.dry_run:
            results = stt_imports()
            for module, (good, detail) in results.items():
                (ok if good else bad)(f"{module}: {detail}")
            listening_ok = all(good for good, _ in results.values())
        if listening_ok:
            listening_ok = download_whisper(args.whisper_size, args.dry_run)
        else:
            step(3, "Downloading the Whisper model")
            bad("skipped - the listening packages did not install")
    else:
        step(2, "Installing the listening stack")
        say("      skipped (--tts-only)")
        step(3, "Downloading the Whisper model")
        say("      skipped (--tts-only)")

    if do_tts:
        speaking_ok = install_speaking(args.dry_run, args.cuda, args.repair)
    else:
        step(4, "Installing the speech engine")
        say("      skipped (--stt-only)")

    outcome = verify(args.dry_run)
    microphone(args.dry_run)

    say()
    say(BAR)
    if args.dry_run:
        say("  DRY RUN - nothing was installed or downloaded.")
        say(BAR)
        return 0

    listening = do_stt and outcome["whisper_loads"] and all(
        good for good, _ in outcome["stt_packages"].values())
    speaking = outcome["speaking"] if do_tts else False

    if (listening or not do_stt) and (speaking or not do_tts):
        say("  VOICE SETUP COMPLETE.")
        say()
        if do_stt:
            say("  Listening:  Whisper, from models/whisper")
        if do_tts:
            say("  Speaking:   Chatterbox, from models/chatterbox")
        say()
        say("  Try it:   python scripts\\voice_doctor.py --record --speak")
        say("  Start:    start_medha_industrial.bat   (or: python main.py --voice)")
        say("  Offline:  disconnect the network and run scripts\\offline_test.py")
        say(BAR)
        return 0

    say("  SETUP INCOMPLETE - the [FAIL] lines above say what is missing.")
    say()
    if do_stt and not listening:
        say("  Listening (Whisper) is not ready.")
    if do_tts and not speaking:
        say("  Speaking (Chatterbox) is not ready.")
        say(f'      diagnose:  "{sys.executable}" scripts\\setup_voice_engine.py --check')
        say(f'      repair:    "{sys.executable}" scripts\\setup_voice.py --repair')
    say()
    say("  Diagnose everything:  python scripts\\voice_doctor.py")
    say("  ZEUS still starts without voice. Text mode, documents and the")
    say("  industrial commands are all unaffected - voice reports DISABLED")
    say("  rather than pretending to work.")
    say(BAR)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
