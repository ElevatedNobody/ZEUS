"""
ZEUS Chatterbox speech engine installer.
Autonomous AI Command & Intelligence System.

Normally you do not run this directly - SETUP_VOICE.bat and
scripts/setup_voice.py call it as their text-to-speech stage. It is a
standalone script so that stage can be re-run, checked or undone on its own:

    python scripts/setup_voice_engine.py             install
    python scripts/setup_voice_engine.py --check     report, change nothing
    python scripts/setup_voice_engine.py --repair    fix what is broken
    python scripts/setup_voice_engine.py --uninstall remove the speech engine

What it does, in order:

    1. detect the hardware and the Python versions available
    2. create a DEDICATED virtual environment for Chatterbox
    3. install the right torch build for this machine (CUDA or CPU)
    4. install chatterbox-tts and its dependencies
    5. download the model checkpoint
    6. write the ZEUS voice profile
    7. verify by SYNTHESISING A REAL SENTENCE and checking the audio

Why a dedicated environment
---------------------------
chatterbox-tts pins its whole world: torch==2.6.0, transformers==5.2.0,
numpy<2.0.0. ZEUS itself needs numpy 2.x for the analytics and document
builders, so installing Chatterbox alongside it would force one of the two to
lose. It does not have to: this installer never touches the ZEUS environment.
It builds a second one under models/voice/chatterbox/env and speaks to it over
a JSON pipe (voice/chatterbox_bridge.py). Nothing Chatterbox pins can reach
Ollama, PySide6, numpy or the RAG stack.

The model weights are NOT duplicated. They go to the same models/chatterbox
directory that voice/offline.py resolves, so the in-process provider and the
bridge share one 3.2 GB download.

Resumable
---------
Every step checks whether it is already done. An interrupted download resumes;
a completed environment is reused; a verified checkpoint is not re-fetched.
Running this twice is safe and fast.

Nothing here is claimed without being checked. The final report says READY
only after a real sentence has been synthesised into a real audio file.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

HOME = ROOT / "models" / "voice" / "chatterbox"
ENV_DIR = HOME / "env"

# The checkpoint lives where voice/offline.py looks for it - models/chatterbox
# by default, or wherever MEDHA_TTS_MODEL_DIR / config.ini point. Using the
# same directory as setup_models.py is deliberate: the two installers must
# share one 3.2 GB download, not race to fill two directories with the same
# weights. resolve_chatterbox_dir() is the single definition of that path.
try:
    from voice.offline import resolve_chatterbox_dir
    MODEL_DIR = resolve_chatterbox_dir()
except Exception:                                    # pragma: no cover
    # A broken checkout must still be able to run the installer.
    MODEL_DIR = ROOT / "models" / "chatterbox"

try:
    from core.branding import APP_TAGLINE as TAGLINE
except Exception:                                    # pragma: no cover
    TAGLINE = "Autonomous AI Command & Intelligence System"

PROFILE_DIR = ROOT / "models" / "voice" / "profiles" / "medha"
LOG_PATH = ROOT / "logs" / "voice_setup.log"

# The published Chatterbox v1 base model. Only these files are fetched - the
# repository also carries older revisions and other languages, and pulling all
# of it would be several extra gigabytes nobody asked for.
# Exactly the five files ChatterboxTTS.from_local() loads. The repository
# carries considerably more; fetching all of it would add gigabytes nobody
# asked for.
MODEL_REPO = "ResembleAI/chatterbox"
MODEL_FILES = [
    ("ve.safetensors", "the voice encoder", 6_000_000),
    ("t3_cfg.safetensors", "the text-to-token model", 2_100_000_000),
    ("s3gen.safetensors", "the speech generator", 1_060_000_000),
    ("tokenizer.json", "the tokenizer", 1_500_000),
    ("conds.pt", "the default speaker conditioning", 110_000),
]
VOCODER_REPO = ""
VOCODER_FILES = []

MIN_PYTHON = (3, 10)
MAX_PYTHON = (3, 12)          # Chatterbox wheels do not reliably build above this
MIN_FREE_GB = 12.0

# PINNED to what chatterbox-tts itself requires.
#
# chatterbox-tts declares `torch==2.6.0` as a hard equality, so this is not a
# preference - installing anything else and then installing chatterbox would
# simply move it back.
#
# It is also the reason Chatterbox replaced F5-TTS here. F5-TTS declares
# `torchcodec`, which loads FFmpeg shared libraries when imported; Windows
# ships no FFmpeg, so that install completed, downloaded 1.4 GB of model, and
# then died on a missing DLL three frames from its cause. torch 2.6 sits below
# the release where torchaudio began delegating decoding to torchcodec, so the
# whole failure mode is absent.
#
# It is pinned explicitly rather than left to chatterbox's own requirement so
# the CPU build can be chosen deliberately: pip resolving `torch==2.6.0` from
# the default index fetches the CUDA build, which is ~2.5 GB of GPU libraries
# an Iris Xe cannot use.
TORCH_VERSION = "2.6.0"

# RTX 50-series (Blackwell, sm_120) GPUs. torch 2.6 has no kernels for them,
# so the voice ran on the CPU: 43 s per sentence on the RTX 5060 laptop. Here
# chatterbox-tts is installed against the 2.6 pin as usual, and then torch
# alone is swapped (--no-deps) for this CUDA 12.8 build. MEASURED 2026-09-25
# on an RTX 5060 Laptop GPU with chatterbox-tts 0.1.7: loads, and speaks
# faster than real time (2.7-3.0 s per ~4 s sentence). chatterbox-tts 0.1.7
# itself accepts torch>=2.9 on Python 3.14, so its code is not tied to 2.6.
# 2.8 rather than 2.9+: from torchaudio 2.9, load/save go through
# torchcodec/FFmpeg - the failure described above. If the swap does not work
# on a machine, the CPU build is put back and the voice still speaks.
BLACKWELL_TORCH_VERSION = "2.8.0"
BLACKWELL_TORCH_INDEX = "https://download.pytorch.org/whl/cu128"

# The Chatterbox release this was verified against. Pinned so a future version
# with different requirements cannot silently change the torch underneath a
# working install - which is exactly how the previous engine broke.
CHATTERBOX_VERSION = "0.1.7"

_log_lines: List[str] = []


# ------------------------------------------------------------- reporting --

class Console:
    GREEN, RED, AMBER, DIM, BOLD, OFF = (
        "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m")

    @staticmethod
    def rule(title: str = "", subtitle: str = ""):
        print("\n" + "=" * 68)
        if title:
            print(f"  {title}")
            if subtitle:
                print(f"  {subtitle}")
            print("=" * 68)

    @classmethod
    def step(cls, number: str, message: str):
        print(f"\n{cls.BOLD}[{number}]{cls.OFF} {message}")

    @classmethod
    def ok(cls, message: str):
        print(f"  {cls.GREEN}OK{cls.OFF}    {message}")
        log(f"OK    {message}")

    @classmethod
    def warn(cls, message: str):
        print(f"  {cls.AMBER}WARN{cls.OFF}  {message}")
        log(f"WARN  {message}")

    @classmethod
    def fail(cls, message: str, why: str = "", fix: str = ""):
        print(f"  {cls.RED}FAIL{cls.OFF}  {message}")
        if why:
            print(f"        why: {why}")
        if fix:
            print(f"        fix: {fix}")
        log(f"FAIL  {message} | why={why} | fix={fix}")

    @classmethod
    def info(cls, message: str):
        print(f"        {cls.DIM}{message}{cls.OFF}")
        log(f"      {message}")


def log(line: str):
    _log_lines.append(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {line}")


def write_log():
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        LOG_PATH.write_text("\n".join(_log_lines) + "\n", encoding="utf-8")
        print(f"\n  Full log: {LOG_PATH}")
    except Exception as error:
        print(f"\n  (could not write the log: {error})")


# ------------------------------------------------------------- hardware --

def detect_hardware() -> dict:
    """What this machine is. Every later decision follows from this."""
    info: Dict[str, object] = {
        "os": f"{platform.system()} {platform.release()}",
        "machine": platform.machine(),
        "cpu": platform.processor() or "unknown",
        "cores": os.cpu_count(),
        "medha_python": platform.python_version(),
        "ram_gb": None, "free_disk_gb": None,
        "gpu": None, "vram_gb": None, "cuda": None, "driver": None,
    }
    try:
        import psutil
        info["ram_gb"] = round(psutil.virtual_memory().total / (1024 ** 3), 1)
    except Exception:
        pass
    try:
        usage = shutil.disk_usage(str(ROOT))
        info["free_disk_gb"] = round(usage.free / (1024 ** 3), 1)
    except Exception:
        pass

    # nvidia-smi is the honest source: it reports what the DRIVER sees, which
    # is what torch will also see. Anything else would be a guess.
    try:
        result = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=name,memory.total,driver_version",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=20)
        if result.returncode == 0 and result.stdout.strip():
            name, memory, driver = [
                part.strip() for part in result.stdout.strip().splitlines()[0].split(",")]
            info["gpu"] = name
            info["vram_gb"] = round(float(memory) / 1024, 1)
            info["driver"] = driver
    except Exception:
        pass

    if info["gpu"]:
        try:
            result = subprocess.run(["nvcc", "--version"], capture_output=True,
                                    text=True, timeout=20)
            for line in result.stdout.splitlines():
                if "release" in line:
                    info["cuda"] = line.split("release")[1].split(",")[0].strip()
        except Exception:
            info["cuda"] = "driver only (no CUDA toolkit)"
    return info


def is_blackwell(hardware: dict) -> bool:
    return bool(re.search(r"RTX\s*50\d0|Blackwell", str(hardware.get("gpu") or ""),
                          re.IGNORECASE))


def final_torch_version(hardware: dict, force: str = "") -> str:
    """The torch the voice environment should END with."""
    if force == "cpu" or not is_blackwell(hardware):
        return TORCH_VERSION
    return BLACKWELL_TORCH_VERSION


def torch_index(hardware: dict, force: str = "") -> Tuple[str, str]:
    """Which torch build to install, and why.

    Returns (index url, human explanation). The CPU build is roughly 200 MB;
    the CUDA build is about 2.5 GB. Installing the CUDA one on a machine with
    no NVIDIA GPU wastes both.

    `force` is "cuda" or "cpu" from the command line. An operator who passes
    --cuda on a machine where detection found no GPU is told what the detection
    saw and then given what they asked for: a driver this script cannot read is
    not proof there is no card.
    """
    if force == "cpu":
        return ("https://download.pytorch.org/whl/cpu",
                "--cpu was given - installing the CPU build")
    if force == "cuda":
        if not hardware.get("gpu"):
            Console.warn("--cuda was given but no NVIDIA GPU was detected. "
                         "Installing the CUDA build anyway; if torch then "
                         "fails to start, re-run with --cpu.")
        return ("https://download.pytorch.org/whl/cu121",
                "--cuda was given - installing the CUDA 12.1 build")
    if not hardware.get("gpu"):
        return ("https://download.pytorch.org/whl/cpu",
                "no NVIDIA GPU detected - installing the CPU build "
                "(~200 MB instead of ~2.5 GB)")
    # RTX 50-series (Blackwell, compute capability 12.x) needs a CUDA 12.8
    # build, and the first torch with one is 2.7. chatterbox-tts pins
    # torch==2.6.0, whose CUDA builds carry no Blackwell kernels: it would
    # install, import, and then fail at the first kernel launch. So the CPU
    # 2.6 build goes in first (chatterbox-tts installs against it), and
    # install_gpu_torch() then swaps in BLACKWELL_TORCH_VERSION for CUDA 12.8.
    if is_blackwell(hardware):
        return ("https://download.pytorch.org/whl/cpu",
                f"{hardware['gpu']} is a Blackwell (RTX 50-series) GPU and torch "
                f"{TORCH_VERSION}, which chatterbox-tts pins, has no CUDA kernels for "
                f"it - installing the CPU build first; torch {BLACKWELL_TORCH_VERSION} "
                f"for CUDA 12.8 replaces it after Chatterbox is installed")
    driver = str(hardware.get("driver") or "")
    try:
        major = int(driver.split(".")[0])
    except (ValueError, IndexError):
        major = 0
    # cu121 needs driver >= 530, cu118 needs >= 450. Choosing by driver rather
    # than by the CUDA toolkit, because the toolkit is often absent while the
    # driver is what actually has to support the runtime.
    if major >= 530:
        return ("https://download.pytorch.org/whl/cu121",
                f"{hardware['gpu']} with driver {driver} - installing CUDA 12.1")
    if major >= 450:
        return ("https://download.pytorch.org/whl/cu118",
                f"{hardware['gpu']} with driver {driver} - installing CUDA 11.8")
    if major == 0:
        # A GPU is present but the driver version could not be read. Guessing a
        # CUDA build here risks a torch that imports and then fails at the first
        # kernel launch. The CPU build always works, so it is the safe answer -
        # and the message says exactly that rather than blaming the driver.
        return ("https://download.pytorch.org/whl/cpu",
                f"{hardware['gpu']} was detected but its driver version could "
                f"not be read - installing the CPU build, which always works. "
                f"Run `nvidia-smi` and reinstall if you want GPU speed")
    return ("https://download.pytorch.org/whl/cpu",
            f"the NVIDIA driver ({driver}) is too old for a supported CUDA "
            f"build - installing the CPU build")


def find_python() -> Optional[Tuple[Path, str]]:
    """An interpreter Chatterbox can actually use.

    Checked by RUNNING each candidate, not by trusting its name: `python3.11`
    on PATH is sometimes a shim for something else entirely.
    """
    candidates: List[str] = []
    if platform.system() == "Windows":
        for minor in (11, 10, 12):
            candidates.append(f"py -3.{minor}")
        candidates += ["python3.11", "python3.10", "python"]
    else:
        candidates += [f"python3.{minor}" for minor in (11, 10, 12)]
        candidates += ["python3", "python"]

    seen = set()
    for candidate in candidates:
        parts = candidate.split()
        program = shutil.which(parts[0])
        if not program:
            continue
        command = [program] + parts[1:]
        try:
            result = subprocess.run(
                command + ["-c",
                           "import sys,json;"
                           "print(json.dumps([sys.version_info[:3], sys.executable]))"],
                capture_output=True, text=True, timeout=30)
            if result.returncode != 0:
                continue
            version, executable = json.loads(result.stdout.strip())
        except Exception:
            continue
        if executable in seen:
            continue
        seen.add(executable)
        if MIN_PYTHON <= tuple(version[:2]) <= MAX_PYTHON:
            return Path(executable), ".".join(str(part) for part in version)
    return None


# ---------------------------------------------------------- environment --

def env_python() -> Optional[Path]:
    for candidate in (ENV_DIR / "Scripts" / "python.exe", ENV_DIR / "bin" / "python"):
        if candidate.is_file():
            return candidate
    return None


def run(command: List[str], description: str, timeout: int = 3600) -> bool:
    """Run a step, streaming nothing but reporting honestly."""
    log(f"RUN   {' '.join(str(part) for part in command)}")
    try:
        result = subprocess.run(command, capture_output=True, text=True,
                                timeout=timeout)
    except subprocess.TimeoutExpired:
        Console.fail(description, f"it did not finish within {timeout // 60} minutes",
                     "check your connection and run the installer again - "
                     "completed work is kept")
        return False
    except Exception as error:
        Console.fail(description, str(error))
        return False
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip().splitlines()[-6:]
        Console.fail(description, f"exit code {result.returncode}")
        for line in tail:
            Console.info(line[:150])
        log("STDERR " + (result.stderr or "")[-3000:])
        return False
    return True


def create_environment(python: Path, version: str) -> bool:
    existing = env_python()
    if existing:
        Console.ok(f"the Chatterbox environment already exists ({existing})")
        return True
    Console.info(f"creating a dedicated environment with Python {version}")
    ENV_DIR.parent.mkdir(parents=True, exist_ok=True)
    if not run([str(python), "-m", "venv", str(ENV_DIR)],
               "create the Chatterbox environment", timeout=600):
        return False
    if env_python() is None:
        Console.fail("create the Chatterbox environment",
                     "the environment was created but has no interpreter")
        return False
    Console.ok(f"environment created at {ENV_DIR}")
    return True


def installed_in_env(module: str) -> bool:
    python = env_python()
    if python is None:
        return False
    try:
        result = subprocess.run([str(python), "-c", f"import {module}"],
                                capture_output=True, timeout=180)
        return result.returncode == 0
    except Exception:
        return False


def installed_version(module: str) -> str:
    """Which version is actually in the voice environment, or ''.

    `installed_in_env` only answers whether something IMPORTS. That is not the
    same question, and confusing the two is what let a torch 2.14 install sail
    past a step whose whole purpose was to pin 2.8: torch imported, so the
    step reported "already installed" and skipped the pin. The version is the
    thing being asserted, so the version is what has to be read.
    """
    python = env_python()
    if python is None:
        return ""
    try:
        result = subprocess.run(
            [str(python), "-c",
             f"import {module}; print({module}.__version__)"],
            capture_output=True, text=True, timeout=300)
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


def install_torch(hardware: dict, force: str = "") -> bool:
    present = installed_version("torch")
    final = final_torch_version(hardware, force)
    if final != TORCH_VERSION and present.startswith(final) and "+cu" in present \
            and installed_in_env("chatterbox"):
        # A Blackwell machine that already finished: re-installing the CPU
        # 2.6 here would undo the GPU voice (that is what happened on every
        # repair run before 2026-09-25).
        Console.ok(f"torch {present} (the GPU build for this card) is already installed")
        return True
    correct = present.startswith(TORCH_VERSION)
    if correct and not installed_in_env("torchcodec"):
        Console.ok(f"torch {present} is already installed and pinned correctly")
        return True
    if present and not correct:
        Console.warn(f"torch {present} is installed, but {TORCH_VERSION} is "
                     f"required - replacing it")
    if installed_in_env("torchcodec"):
        # Left behind by an earlier unpinned install. See TORCH_VERSION below
        # for why it has to go: while it is importable, torchaudio routes
        # decoding through it and fails on a machine with no FFmpeg DLLs.
        Console.info("removing torchcodec left by an earlier install")
        run([str(env_python()), "-m", "pip", "uninstall", "-y", "torchcodec"],
            "remove torchcodec", timeout=600, )

    index, explanation = torch_index(hardware, force)
    Console.info(explanation)
    Console.info(f"pinning torch {TORCH_VERSION} - the version Chatterbox is built against")
    python = env_python()
    run([str(python), "-m", "pip", "install", "--upgrade", "pip", "-q"],
        "upgrade pip", timeout=600)
    # The CPU wheel is about 620 MB, and pip's default 5 retries with a 15
    # second timeout is not enough for a connection that drops. Downloading
    # FIRST and installing from disk separates the flaky part from the part
    # that touches the environment: a failed download can simply be re-run,
    # and the install itself never touches the network.
    wheels = HOME / "wheels"
    wheels.mkdir(parents=True, exist_ok=True)
    packages = [f"torch=={TORCH_VERSION}", f"torchaudio=={TORCH_VERSION}"]
    patience = ["--retries", "20", "--timeout", "300"]

    Console.info("downloading torch (about 620 MB) - this is the long step")
    if not run([str(python), "-m", "pip", "download", *packages,
                "--index-url", index, "-d", str(wheels), *patience],
               "download torch and torchaudio", timeout=7200):
        Console.info("the download did not finish - run the installer again "
                     "and it resumes from what is already in "
                     f"{wheels}")
        return False

    Console.info("installing from the downloaded files (no network needed)")
    # --force-reinstall because this is often a DOWNGRADE from a newer torch,
    # and without it pip can decide the requirement is already satisfied and
    # leave the wrong version in place.
    if not run([str(python), "-m", "pip", "install", "--no-index",
                "--find-links", str(wheels), "--force-reinstall", *packages],
               "install torch and torchaudio", timeout=3600):
        return False
    return _confirm_torch_did_not_move(python)


def _gpu_works(python: Path) -> Tuple[bool, str]:
    """Does torch in the voice environment actually run a kernel on the GPU?
    Importing is not enough: a build without this card's kernels imports
    fine and fails at the first launch."""
    probe = ("import torch; assert torch.cuda.is_available(), 'CUDA not available'; "
             "x = torch.randn(256, 256, device='cuda'); float((x @ x).sum()); "
             "print(torch.__version__, torch.cuda.get_device_name(0))")
    try:
        result = subprocess.run([str(python), "-c", probe], capture_output=True, text=True,
                                timeout=600)
    except Exception as error:
        return False, str(error)
    if result.returncode != 0:
        return False, ((result.stderr or "").strip().splitlines() or ["no detail"])[-1][:200]
    return True, result.stdout.strip()


def install_gpu_torch(hardware: dict, force: str = "") -> bool:
    """On an RTX 50-series card, replace the CPU torch with the CUDA 12.8 build
    AFTER chatterbox-tts is installed. Never leaves the voice broken: if the
    GPU build does not run a kernel here, the CPU build is put back."""
    final = final_torch_version(hardware, force)
    if final == TORCH_VERSION:
        return True
    python = env_python()
    present = installed_version("torch")
    if present.startswith(final) and "+cu" in present:
        works, detail = _gpu_works(python)
        if works:
            Console.ok(f"torch {present} runs on the GPU ({detail})")
            return True
    Console.info(f"installing torch {final} for CUDA 12.8 so the voice runs on "
                 f"{hardware.get('gpu')} (about 3.5 GB - the long step)")
    wheels = HOME / "wheels"
    wheels.mkdir(parents=True, exist_ok=True)
    packages = [f"torch=={final}", f"torchaudio=={final}"]
    if not run([str(python), "-m", "pip", "download", *packages, "--no-deps",
                "--index-url", BLACKWELL_TORCH_INDEX, "-d", str(wheels),
                "--retries", "20", "--timeout", "300"],
               "download the GPU build of torch", timeout=7200):
        Console.warn("the GPU build did not download - the voice keeps the CPU build "
                     "(it works, but slowly). Run the installer again to retry.")
        return True
    if not run([str(python), "-m", "pip", "install", "--no-index", "--find-links",
                str(wheels), "--no-deps", "--force-reinstall", *packages],
               "install the GPU build of torch", timeout=3600):
        return _back_to_cpu_torch(python, wheels, "the GPU build did not install")
    works, detail = _gpu_works(python)
    if not works:
        return _back_to_cpu_torch(python, wheels, f"the GPU build did not run here ({detail})")
    Console.ok(f"torch {installed_version('torch')} runs on the GPU ({detail})")
    return True


def _back_to_cpu_torch(python: Path, wheels: Path, why: str) -> bool:
    Console.warn(f"{why} - putting the CPU build back so the voice still speaks")
    run([str(python), "-m", "pip", "install", "--no-index", "--find-links", str(wheels),
         "--no-deps", "--force-reinstall", f"torch=={TORCH_VERSION}",
         f"torchaudio=={TORCH_VERSION}"], "restore the CPU build of torch", timeout=3600)
    return _confirm_torch_did_not_move(python)


def install_chatterbox(python: Path) -> bool:
    if installed_in_env("chatterbox") and not installed_in_env("torchcodec"):
        Console.ok("chatterbox-tts is already installed in the voice environment")
        return True

    # A constraints file, so nothing chatterbox-tts depends on can move torch.
    # Without it pip is free to upgrade torch to satisfy some transitive
    # requirement - which is precisely how the previous engine's pin got
    # silently undone by the very next install step.
    constraints = HOME / "constraints.txt"
    constraints.write_text(
        f"torch=={TORCH_VERSION}\ntorchaudio=={TORCH_VERSION}\n",
        encoding="utf-8")

    Console.info(f"installing chatterbox-tts {CHATTERBOX_VERSION} "
                 f"(about 2 GB of dependencies - the long step)")
    if not run([str(python), "-m", "pip", "install",
                f"chatterbox-tts=={CHATTERBOX_VERSION}", "soundfile",
                "--constraint", str(constraints), "--retries", "20",
                "--timeout", "300"],
               "install chatterbox-tts", timeout=7200):
        return False

    # Belt and braces. Chatterbox does not require torchcodec, but if some
    # future dependency drags it in, remove it here rather than discovering it
    # during synthesis three steps later.
    if installed_in_env("torchcodec"):
        Console.info("a dependency pulled torchcodec in - removing it")
        run([str(python), "-m", "pip", "uninstall", "-y", "torchcodec"],
            "remove torchcodec", timeout=600)

    return (_confirm_torch_did_not_move(python)
            and _confirm_chatterbox_imports(python))


def _confirm_chatterbox_imports(python: Path) -> bool:
    """Ask the environment whether Chatterbox actually loads.

    pip prints dependency conflicts prefixed with "ERROR:" in red even when
    the install succeeded. A real one seen here was:

        hf-gradio 0.4.1 requires gradio-client<3.0,>=2.0, but you have
        gradio-client 1.10.4 which is incompatible.

    gradio is only used by chatterbox's optional web demo; the API this bridge
    calls never imports it, so that conflict changes nothing. But nobody can
    be expected to know which of pip's red messages matter.

    So the question is not "did pip complain" - it is "does the import work".
    That is checkable, and it makes every dependency warning irrelevant.
    """
    try:
        result = subprocess.run(
            [str(python), "-c", "from chatterbox.tts import ChatterboxTTS; print('ok')"],
            capture_output=True, text=True, timeout=600)
    except Exception as error:
        Console.warn(f"could not confirm the Chatterbox import: {error}")
        return True

    if "ok" in (result.stdout or ""):
        Console.ok("Chatterbox imports - any dependency warnings above are noise")
        return True

    Console.fail("Chatterbox will not import",
                 (result.stderr or "").strip().splitlines()[-1][:160]
                 if result.stderr else "no detail",
                 "run SETUP_VOICE.bat")
    return False


def _confirm_torch_did_not_move(python: Path) -> bool:
    """Check the pin actually held.

    Installing a package can silently change torch underneath you. Asking the
    environment what it now HAS is the only way to know - trusting that a
    constraint was honoured is how this broke the first time.
    """
    try:
        result = subprocess.run(
            [str(python), "-c", "import torch, torchaudio; "
                                "print(torch.__version__, torchaudio.__version__)"],
            capture_output=True, text=True, timeout=300)
        versions = result.stdout.strip()
    except Exception as error:
        Console.warn(f"could not confirm the torch version: {error}")
        return True

    if not versions.startswith(TORCH_VERSION):
        Console.fail(f"torch moved to {versions.split()[0] if versions else '?'}",
                     f"something upgraded it past the pinned {TORCH_VERSION}",
                     "run SETUP_VOICE.bat, which reinstalls the pin")
        return False
    Console.ok(f"torch {versions} - the pin held")
    return True


# ------------------------------------------------------------ downloads --

def download(repo: str, filename: str, target: Path, description: str,
             approximate: int) -> bool:
    """Fetch one file, resumably, into the local model directory.

    huggingface_hub is used INSIDE the voice environment so this installer
    does not add a dependency to ZEUS. It caches and resumes by itself, so
    an interrupted download continues rather than restarting.
    """
    if target.is_file() and target.stat().st_size > approximate * 0.9:
        size = target.stat().st_size / (1024 ** 2)
        Console.ok(f"{description} is already downloaded ({size:.0f} MB)")
        return True

    python = env_python()
    if not installed_in_env("huggingface_hub"):
        if not run([str(python), "-m", "pip", "install", "huggingface_hub", "-q"],
                   "install the downloader", timeout=900):
            return False

    Console.info(f"downloading {description} (~{approximate / (1024**2):.0f} MB)")
    script = (
        "import sys, shutil\n"
        "from pathlib import Path\n"
        "from huggingface_hub import hf_hub_download\n"
        "path = hf_hub_download(repo_id=sys.argv[1], filename=sys.argv[2],\n"
        "                       resume_download=True)\n"
        "target = Path(sys.argv[3]); target.parent.mkdir(parents=True, exist_ok=True)\n"
        "shutil.copyfile(path, target)\n"
        "print(target)\n")
    if not run([str(python), "-c", script, repo, filename, str(target)],
               f"download {description}", timeout=7200):
        return False
    if not target.is_file() or target.stat().st_size == 0:
        Console.fail(f"download {description}", "the file did not arrive")
        return False
    Console.ok(f"{description} downloaded "
               f"({target.stat().st_size / (1024 ** 2):.0f} MB)")
    return True


def download_models() -> bool:
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    for filename, description, size in MODEL_FILES:
        if not download(MODEL_REPO, filename, MODEL_DIR / Path(filename).name,
                        description, size):
            return False
    # Chatterbox ships its vocoder inside s3gen, so there is no separate
    # download. The loop is kept, guarded, rather than deleted: a future model
    # that does need one only has to fill in VOCODER_FILES.
    for filename, description, size in VOCODER_FILES:
        if not download(VOCODER_REPO, filename,
                        MODEL_DIR / "vocoder" / Path(filename).name, description, size):
            Console.warn(f"{description} could not be downloaded")
    return True


# ------------------------------------------------------------- profile --

def write_profile(hardware: dict) -> bool:
    """The ZEUS voice profile.

    The reference clip is what Chatterbox clones. This installer does NOT ship a
    recording of a real person: cloning a voice you have no permission to use
    is not something this project does. It generates a neutral reference with
    whatever local synthesiser is available, and tells the operator how to
    replace it with their own recording.
    """
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    reference = PROFILE_DIR / "reference.wav"
    reference_text = ("Good morning. All plant systems are nominal and the "
                      "overnight readings are within limits.")

    if not reference.is_file():
        Console.info("no reference clip yet - generating one to clone from")
        Console.info("(optional: Chatterbox has a built-in speaker if this fails)")
        if not _make_reference(reference, reference_text):
            # Every automatic route failed. Give the exact command rather than
            # an instruction to "record something" - the operator is four
            # steps into an install and wants one line to paste, not a task.
            # A warning, not a failure. Chatterbox ships a default speaker in
            # conds.pt, so it speaks perfectly well without one. Treating a
            # missing clip as fatal is what stranded the previous engine after
            # a completed multi-gigabyte download.
            Console.warn("could not generate a reference clip - Chatterbox will "
                         "use its built-in speaker")
            Console.info("Run this one line, then `SETUP_VOICE.bat`:")
            print()
            if platform.system() == "Windows":
                print(f'    powershell -Command "Add-Type -AssemblyName '
                      f'System.Speech; $s = New-Object '
                      f'System.Speech.Synthesis.SpeechSynthesizer; '
                      f"$s.SetOutputToWaveFile('{reference}'); "
                      f"$s.Speak('{reference_text}'); $s.Dispose()\"")
            else:
                print(f"    espeak -w '{reference}' '{reference_text}'")
            print()
            Console.info("Or record about 10 seconds of your own clear speech,")
            Console.info(f"save it as {reference}, and set reference_text in")
            Console.info("profile.json to exactly what you said.")
    else:
        Console.ok(f"reference clip present ({reference.name})")

    profile = {
        "name": "medha",
        "description": "Calm, warm, precise industrial assistant.",
        "language": "en",
        "reference": "reference.wav",
        "reference_text": reference_text,
        "engine": "chatterbox",
        "device": "auto",
        "exaggeration": 0.5,
        "cfg_weight": 0.5,
        "temperature": 0.8,
        "volume": 1.0,
        "trim_silence": True,
        "max_spoken_chars": 700,
        "timeout": 300,
        "style": {
            "pace": "measured",
            "tone": "warm, controlled, never dramatic",
            "pauses": "between thoughts, not after every comma",
        },
        "created_at": time.time(),
        "created_on": hardware.get("os"),
        "note": ("Replace reference.wav with your own 10-second recording to "
                 "change the voice, and update reference_text to match exactly "
                 "what is said in it. Use only a voice you are authorised to "
                 "use."),
    }
    (PROFILE_DIR / "profile.json").write_text(
        json.dumps(profile, indent=2), encoding="utf-8")
    Console.ok(f"voice profile written to {PROFILE_DIR / 'profile.json'}")
    return True


def _make_reference(target: Path, text: str) -> bool:
    """Produce a reference clip using the speech engine already in the OS.

    Every method here clones nobody and downloads nothing: the operating
    system can already speak, and Chatterbox only needs the clip for timbre and
    pacing. The operator replaces it with their own recording when they want a
    better voice.

    They are tried in order of how likely they are to be present. The first
    version of this used ONLY pyttsx3 - a third-party package the installer
    never installs - so on a machine without it the whole install finished
    with "Voice profile: MISSING" after successfully downloading 1.4 GB of
    model. Depending on an optional package for a mandatory step was the bug.
    """
    target.parent.mkdir(parents=True, exist_ok=True)

    for name, attempt in (("Windows Speech", _reference_via_powershell),
                          ("pyttsx3", _reference_via_pyttsx3),
                          ("espeak", _reference_via_espeak),
                          ("macOS say", _reference_via_say)):
        try:
            if attempt(target, text) and _usable_wav(target):
                Console.ok(f"generated a neutral reference clip using {name} "
                           f"({target.stat().st_size // 1024} KB)")
                return True
        except Exception as error:
            log(f"reference via {name} failed: {error}")
        target.unlink(missing_ok=True)     # never leave a truncated clip behind
    return False


def _usable_wav(path: Path) -> bool:
    """A file is not a recording. This checks it is real, readable audio of a
    sensible length - a zero-byte or half-second file would produce garbage
    speech that is harder to diagnose than no speech."""
    if not path.is_file() or path.stat().st_size < 8000:
        return False
    try:
        import wave
        with wave.open(str(path), "rb") as handle:
            seconds = handle.getnframes() / float(handle.getframerate() or 1)
        return seconds >= 1.5
    except Exception:
        return False


def _reference_via_powershell(target: Path, text: str) -> bool:
    """System.Speech, built into every Windows install since Vista.

    This is first because it needs no installation at all, which is exactly
    what the previous implementation got wrong.
    """
    if platform.system() != "Windows":
        return False
    script = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        "$s.Rate = -1; "
        f"$s.SetOutputToWaveFile('{target}'); "
        f"$s.Speak('{text.replace(chr(39), chr(39) * 2)}'); "
        "$s.Dispose()")
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, timeout=180)
    if result.returncode != 0:
        log(f"powershell speech: {(result.stderr or '')[-400:]}")
    return result.returncode == 0


def _reference_via_pyttsx3(target: Path, text: str) -> bool:
    """The cross-platform wrapper, if it happens to be installed.

    Not installed on demand: it pulls a driver stack per platform, and this
    step has three other ways to succeed.
    """
    import pyttsx3
    engine = pyttsx3.init()
    engine.setProperty("rate", 165)
    engine.save_to_file(text, str(target))
    engine.runAndWait()
    return True


def _reference_via_espeak(target: Path, text: str) -> bool:
    if not shutil.which("espeak-ng") and not shutil.which("espeak"):
        return False
    program = shutil.which("espeak-ng") or shutil.which("espeak")
    return subprocess.run([program, "-s", "150", "-w", str(target), text],
                          capture_output=True, timeout=120).returncode == 0


def _reference_via_say(target: Path, text: str) -> bool:
    if platform.system() != "Darwin" or not shutil.which("say"):
        return False
    # `say` writes AIFF; afconvert turns it into the WAV Chatterbox expects.
    aiff = target.with_suffix(".aiff")
    if subprocess.run(["say", "-o", str(aiff), text],
                      capture_output=True, timeout=120).returncode != 0:
        return False
    try:
        return subprocess.run(
            ["afconvert", "-f", "WAVE", "-d", "LEI16", str(aiff), str(target)],
            capture_output=True, timeout=120).returncode == 0
    finally:
        aiff.unlink(missing_ok=True)


# -------------------------------------------------------- verification --

def verify(quiet: bool = False) -> dict:
    """Prove it works by SYNTHESISING A REAL SENTENCE.

    Checking that files exist proves nothing about whether the model loads or
    the audio is produced. This runs the whole path the application will run.
    """
    result = {"environment": False, "torch": None, "cuda": False, "device": "cpu",
              "gpu": None, "vram_gb": None, "chatterbox": False, "model": False,
              "profile": False, "synthesis": False, "audio_seconds": None,
              "compute_seconds": None, "detail": ""}

    python = env_python()
    if python is None:
        result["detail"] = "the voice environment does not exist"
        return result
    result["environment"] = True

    bridge = ROOT / "voice" / "chatterbox_bridge.py"
    try:
        probe = subprocess.run([str(python), str(bridge), "--probe"],
                               capture_output=True, text=True, timeout=300,
                               cwd=str(ROOT))
        answer = json.loads(probe.stdout.strip() or "{}")
        environment = answer.get("environment", {})
    except Exception as error:
        result["detail"] = f"the voice environment did not answer: {error}"
        return result

    result.update({"torch": environment.get("torch"),
                   "cuda": bool(environment.get("cuda_available")),
                   "device": environment.get("device", "cpu"),
                   "gpu": environment.get("gpu_name"),
                   "vram_gb": environment.get("vram_gb"),
                   "chatterbox": bool(environment.get("chatterbox"))})
    if not result["chatterbox"]:
        result["detail"] = (f"chatterbox will not import: "
                            f"{environment.get('chatterbox_error', 'unknown')}")
        return result

    checkpoints = list(MODEL_DIR.glob("*.safetensors")) + list(MODEL_DIR.glob("*.pt"))
    result["model"] = bool(checkpoints)
    if not checkpoints:
        result["detail"] = f"no model checkpoint in {MODEL_DIR}"
        return result

    # Chatterbox ships a default speaker in conds.pt, so a reference clip is
    # OPTIONAL here - unlike a pure voice-cloning engine, which cannot speak
    # without one at all. That
    # difference removes the step that stranded the previous install after a
    # successful 1.4 GB download.
    profile_file = PROFILE_DIR / "profile.json"
    reference = PROFILE_DIR / "reference.wav"
    result["profile"] = profile_file.is_file()
    result["cloned_voice"] = reference.is_file()
    if not result["profile"]:
        result["detail"] = "the voice profile is missing"
        return result

    if not quiet:
        Console.info("synthesising a test sentence (this loads the model, so the "
                     "first run is the slow one)…")
    output = HOME / "verify.wav"
    output.unlink(missing_ok=True)
    profile = json.loads(profile_file.read_text(encoding="utf-8"))
    request = {
        "text": "Hello. I am ZEUS. Your local Autonomous AI Command & Intelligence System is ready.",
        "reference": str(reference) if reference.is_file() else "",
        "output": str(output),
        "model_dir": str(MODEL_DIR),
        "device": "auto",
        "exaggeration": profile.get("exaggeration", 0.5),
        "cfg_weight": profile.get("cfg_weight", 0.5),
        "temperature": profile.get("temperature", 0.8),
    }
    started = time.time()
    try:
        spoken = subprocess.run([str(python), str(bridge)],
                               input=json.dumps(request), capture_output=True,
                               text=True, timeout=1800, cwd=str(ROOT))
        answer = json.loads(spoken.stdout.strip() or "{}")
    except subprocess.TimeoutExpired:
        result["detail"] = "synthesis did not finish within 30 minutes"
        return result
    except Exception as error:
        result["detail"] = f"synthesis could not be started: {error}"
        return result

    if not answer.get("ok"):
        result["detail"] = _diagnose(
            f"{answer.get('error', 'synthesis failed')}: "
            f"{answer.get('detail', '')} {answer.get('traceback', '')}")
        return result

    result["synthesis"] = output.is_file() and output.stat().st_size > 1000
    result["audio_seconds"] = answer.get("seconds")
    result["compute_seconds"] = round(time.time() - started, 1)
    result["sample"] = str(output)
    if not result["synthesis"]:
        result["detail"] = "the bridge reported success but produced no audio"
    return result



def _diagnose(raw: str) -> str:
    """Turn a known library failure into something a person can act on.

    A traceback that ends in `libtorchcodec_core5.dll` names the symptom three
    frames from the cause and says nothing about what to do. These are the
    failures that have actually happened, each with the reason and the fix.
    Anything unrecognised is passed through unchanged - inventing a diagnosis
    would be worse than showing the real error.
    """
    raw = raw or ""
    text = raw.lower()

    if "torchcodec" in text or "libtorchcodec" in text:
        return ("torchaudio is trying to decode audio through torchcodec, which "
                "needs FFmpeg libraries this machine does not have. The cause is "
                "a torch version above 2.8: from torchaudio 2.9 onwards decoding "
                "is delegated to torchcodec. Fix: SETUP_VOICE.bat, "
                f"which pins torch {TORCH_VERSION} and removes torchcodec")

    if "gradio" in text and ("incompatible" in text or "conflict" in text):
        return ("this is a dependency warning about gradio, which chatterbox-tts uses "
                "only for its optional web demo. ZEUS never loads it, so the "
                "warning does not affect speech. pip prints these in red with "
                "an ERROR prefix even when the install succeeded - check the "
                "last line, which should say 'Successfully installed'")

    if "winerror 32" in text or "being used by another process" in text:
        return ("a file is locked by another program, so pip could not replace "
                "it. Close ZEUS and any Python windows, then run "
                "`taskkill /f /im python.exe`. If it persists, add the ZEUS "
                "folder to your antivirus exclusions - real-time scanning holds "
                "newly downloaded files open. pip's own advice to 'check the "
                "permissions' is misleading here; nothing is wrong with them")

    if "connection interrupted" in text or "incomplete download" in text:
        return ("the download kept being cut off. Run the installer again - it "
                "resumes from what already arrived rather than starting over")

    if "out of memory" in text or "cannot allocate" in text:
        return ("the machine ran out of memory loading the model. Close other "
                "applications - especially anything holding a chat model - and "
                "try again. Chatterbox needs about 2.5 GB free")

    if ".safetensors" in text:
        return ("the model checkpoint is missing or was not fully downloaded. "
                "Fix: SETUP_VOICE.bat")

    if "ffmpeg" in text:
        return ("an audio library wants FFmpeg, which is not installed. Fix: "
                "SETUP_VOICE.bat, which installs a torch build that "
                "does not need it")

    return raw.strip()[:400]


def report(hardware: dict, checks: dict) -> bool:
    Console.rule("ZEUS — CHATTERBOX VOICE", TAGLINE)
    ready = all((checks["environment"], checks["chatterbox"], checks["model"],
                 checks["profile"], checks["synthesis"]))

    def line(label: str, value: str):
        print(f"  {label:<22} {value}")

    line("Hardware:", f"{hardware.get('cpu', 'unknown')[:44]}")
    line("Cores / RAM:", f"{hardware.get('cores')} cores, "
                         f"{hardware.get('ram_gb') or '?'} GB")
    line("GPU:", hardware.get("gpu") or "none detected")
    line("VRAM:", f"{checks.get('vram_gb') or hardware.get('vram_gb') or '—'} GB"
         if (checks.get("vram_gb") or hardware.get("vram_gb")) else "—")
    line("ZEUS Python:", hardware.get("medha_python", "?"))
    line("Voice Python:", hardware.get("voice_python", "—"))
    line("PyTorch:", checks.get("torch") or "not installed")
    line("CUDA:", ("available" if checks.get("cuda") else "not available"))
    line("Device:", checks.get("device", "cpu").upper())
    line("Chatterbox:", "INSTALLED" if checks.get("chatterbox") else "NOT INSTALLED")
    line("Voice model:", "DOWNLOADED" if checks.get("model") else "MISSING")
    line("Voice profile:", "READY" if checks.get("profile") else "MISSING")
    line("Test synthesis:", "PASSED" if checks.get("synthesis") else "FAILED")
    if checks.get("audio_seconds"):
        factor = (checks["compute_seconds"] / checks["audio_seconds"]
                  if checks["audio_seconds"] else 0)
        line("Speed:", f"{checks['audio_seconds']}s of speech in "
                       f"{checks['compute_seconds']}s  (RTF {factor:.1f}x)")
    line("Offline runtime:", "READY" if ready else "not verified")

    print()
    if ready:
        print(f"  {Console.GREEN}{Console.BOLD}ZEUS VOICE: READY{Console.OFF}")
        if checks.get("sample"):
            print(f"\n  Listen to the test: {checks['sample']}")
        print("\n  Start ZEUS with:  python main.py --voice")
        if checks.get("device") == "cpu":
            print(f"\n  {Console.AMBER}Note{Console.OFF}: running on CPU. Speech is "
                  f"generated a sentence at a time so the\n        first one starts "
                  f"quickly, but long answers will pause between\n        sentences. "
                  f"That is expected without an NVIDIA GPU.")
    else:
        print(f"  {Console.RED}{Console.BOLD}ZEUS VOICE: NOT READY{Console.OFF}")
        print(f"\n  What is wrong: {checks.get('detail') or 'see the failures above'}")
        print(f"\n  ZEUS still runs normally without voice - this affects "
              f"spoken\n  replies only. Try:  SETUP_VOICE.bat")
    return ready


# ------------------------------------------------------------ commands --

def do_check() -> int:
    Console.rule("ZEUS — CHATTERBOX VOICE: STATUS CHECK", TAGLINE)
    print("  Reading the installation. Nothing is changed.\n")
    hardware = detect_hardware()
    python = env_python()
    if python:
        try:
            result = subprocess.run(
                [str(python), "-c", "import platform;print(platform.python_version())"],
                capture_output=True, text=True, timeout=60)
            hardware["voice_python"] = result.stdout.strip()
        except Exception:
            pass
    checks = verify()
    return 0 if report(hardware, checks) else 1


def do_uninstall() -> int:
    Console.rule("ZEUS — REMOVE THE CHATTERBOX VOICE", TAGLINE)
    print(f"\n  This removes ONLY the voice environment and its models:")
    print(f"    {ENV_DIR}")
    print(f"    {MODEL_DIR}")
    print(f"\n  It does NOT touch ZEUS, your account, your documents, the "
          f"vault,\n  or anything else.\n")
    answer = input("  Type REMOVE to confirm: ").strip()
    if answer != "REMOVE":
        print("\n  Nothing was removed.")
        return 0
    for target in (ENV_DIR, MODEL_DIR):
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
            Console.ok(f"removed {target}")
    print("\n  The voice profile was kept, so a reinstall reuses your reference "
          "clip.")
    print("  ZEUS will now run in text mode, or fall back to the system voice.")
    return 0


def do_install(repair: bool = False, force: str = "") -> int:
    Console.rule("ZEUS — CHATTERBOX VOICE INSTALLER", TAGLINE)
    print("\n  This installs a COMPLETE local voice system:")
    print("    - a dedicated Python environment, separate from ZEUS")
    print("    - the right PyTorch build for this machine")
    print("    - Chatterbox and its dependencies")
    print("    - the Chatterbox model (~3.2 GB)")
    print("    - the ZEUS voice profile")
    print("\n  Everything is resumable. If it stops, run it again.")

    Console.step("1/7", "Detecting hardware")
    hardware = detect_hardware()
    Console.ok(f"{hardware['os']} on {hardware['machine']}, "
               f"{hardware['cores']} cores, {hardware.get('ram_gb') or '?'} GB RAM")
    if hardware.get("gpu"):
        Console.ok(f"NVIDIA {hardware['gpu']}, {hardware['vram_gb']} GB VRAM, "
                   f"driver {hardware['driver']}")
    else:
        Console.warn("no NVIDIA GPU detected - Chatterbox will run on the CPU")
        Console.info("that works, but expect several seconds of compute per "
                     "second of speech")
    free = hardware.get("free_disk_gb")
    if free is not None and free < MIN_FREE_GB:
        Console.fail("check free disk space",
                     f"only {free} GB free, and this needs about {MIN_FREE_GB} GB",
                     "free some space and run the installer again")
        write_log()
        return 1
    Console.ok(f"{free} GB free on disk")

    Console.step("2/7", "Finding a Python for the voice environment")
    found = find_python()
    if found is None:
        Console.fail("find a suitable Python",
                     f"Chatterbox needs Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} to "
                     f"{MAX_PYTHON[0]}.{MAX_PYTHON[1]}, and none was found on "
                     f"this machine (ZEUS itself runs on "
                     f"{hardware['medha_python']})",
                     "install Python 3.11 from python.org, tick 'Add to PATH', "
                     "then run this installer again. ZEUS's own Python is "
                     "left alone either way.")
        write_log()
        return 1
    python, version = found
    hardware["voice_python"] = version
    Console.ok(f"using Python {version} at {python}")
    if version.split(".")[:2] != hardware["medha_python"].split(".")[:2]:
        Console.info(f"ZEUS runs on {hardware['medha_python']}; the voice "
                     f"environment is separate and neither affects the other")

    Console.step("3/7", "Creating the dedicated voice environment")
    if not create_environment(python, version):
        write_log()
        return 1

    Console.step("4/7", "Installing PyTorch")
    if not install_torch(hardware, force):
        Console.info("this is the largest install step and the most likely to be "
                     "interrupted - running the installer again resumes it")
        write_log()
        return 1
    Console.ok("torch installed")

    Console.step("5/7", "Installing Chatterbox")
    if not install_chatterbox(env_python()):
        write_log()
        return 1
    Console.ok("chatterbox-tts installed")
    if not install_gpu_torch(hardware, force):
        write_log()
        return 1

    Console.step("6/7", "Downloading the voice model")
    if not download_models():
        write_log()
        return 1

    Console.step("7/7", "Writing the voice profile and verifying")
    write_profile(hardware)
    checks = verify()
    ready = report(hardware, checks)
    write_log()
    return 0 if ready else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Install the ZEUS Chatterbox voice.")
    parser.add_argument("--check", action="store_true",
                        help="report the status and change nothing")
    parser.add_argument("--repair", action="store_true",
                        help="reinstall whatever is missing or broken")
    parser.add_argument("--uninstall", action="store_true",
                        help="remove the voice environment and models only")
    build = parser.add_mutually_exclusive_group()
    build.add_argument("--cuda", action="store_true",
                       help="force the CUDA build of torch (needs an NVIDIA GPU)")
    build.add_argument("--cpu", action="store_true",
                       help="force the CPU build of torch")
    arguments = parser.parse_args()

    force = "cuda" if arguments.cuda else ("cpu" if arguments.cpu else "")

    log(f"setup_voice {' '.join(sys.argv[1:]) or '(install)'}")
    try:
        if arguments.check:
            return do_check()
        if arguments.uninstall:
            return do_uninstall()
        return do_install(repair=arguments.repair, force=force)
    except KeyboardInterrupt:
        print("\n\n  Stopped. Nothing was corrupted - run the installer again "
              "to continue where it left off.")
        write_log()
        return 130


if __name__ == "__main__":
    sys.exit(main())
