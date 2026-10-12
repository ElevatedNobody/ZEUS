"""
What this machine actually has - measured, not assumed.

Used by the setup script (to choose which models to download), by the model
router (to know how much memory a model can have, including GPU memory), and
by the Models panel.

GPU detection uses `nvidia-smi`, which ships with the NVIDIA driver. No
Python GPU library is needed and nothing is loaded onto the GPU to find out.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


@dataclass
class GPU:
    name: str
    vram_total_gb: float
    vram_free_gb: float
    driver: str = ""
    compute_capability: str = ""
    vendor: str = "nvidia"


@dataclass
class HardwareProfile:
    cpu: str
    cores: int
    threads: int
    ram_total_gb: float
    ram_available_gb: float
    gpus: List[GPU] = field(default_factory=list)
    other_display_adapters: List[str] = field(default_factory=list)
    disk_free_gb: float = 0.0
    os: str = ""
    python: str = ""
    measured_at: float = field(default_factory=time.time)

    @property
    def nvidia(self) -> Optional[GPU]:
        return self.gpus[0] if self.gpus else None

    @property
    def vram_gb(self) -> float:
        return self.nvidia.vram_total_gb if self.nvidia else 0.0

    @property
    def tier(self) -> str:
        """A coarse class used to choose models."""
        if self.vram_gb >= 15:
            return "gpu_16gb"
        if self.vram_gb >= 11:
            return "gpu_12gb"
        if self.vram_gb >= 7.5:
            return "gpu_8gb"
        if self.vram_gb >= 5.5:
            return "gpu_6gb"
        return "cpu_16gb" if self.ram_total_gb >= 14 else "cpu_8gb"

    def as_dict(self) -> Dict:
        data = asdict(self)
        data["tier"] = self.tier
        data["vram_gb"] = self.vram_gb
        return data


def _os_name() -> str:
    release = platform.release()
    version = platform.version()
    try:
        if platform.system() == "Windows" and int(version.split(".")[2]) >= 22000:
            release = "11"          # platform.release() still says 10 on Windows 11
    except (IndexError, ValueError):
        pass
    return f"{platform.system()} {release} ({version})"


def _run(command: List[str], timeout: float = 8.0) -> str:
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout,
                              creationflags=_CREATE_NO_WINDOW).stdout
    except Exception:
        return ""


def _cpu_name() -> str:
    if sys.platform == "win32":
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                 r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
            return winreg.QueryValueEx(key, "ProcessorNameString")[0].strip()
        except Exception:
            pass
    return platform.processor() or platform.machine()


def nvidia_gpus() -> List[GPU]:
    exe = shutil.which("nvidia-smi")
    if not exe and sys.platform == "win32":
        candidate = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "nvidia-smi.exe"
        exe = str(candidate) if candidate.exists() else None
    if not exe:
        return []
    output = _run([exe, "--query-gpu=name,memory.total,memory.free,driver_version,compute_cap",
                   "--format=csv,noheader,nounits"])
    if not output.strip():
        output = _run([exe, "--query-gpu=name,memory.total,memory.free,driver_version",
                       "--format=csv,noheader,nounits"])
    gpus = []
    for line in output.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        try:
            gpus.append(GPU(name=parts[0], vram_total_gb=round(float(parts[1]) / 1024, 2),
                            vram_free_gb=round(float(parts[2]) / 1024, 2),
                            driver=parts[3] if len(parts) > 3 else "",
                            compute_capability=parts[4] if len(parts) > 4 else ""))
        except ValueError:
            continue
    return gpus


def _display_adapters() -> List[str]:
    if sys.platform != "win32":
        return []
    output = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                   "(Get-CimInstance Win32_VideoController).Name"], timeout=10)
    return [line.strip() for line in output.splitlines() if line.strip()]


_cache: Optional[HardwareProfile] = None
_lock = threading.Lock()


def profile(force: bool = False, include_adapters: bool = False) -> HardwareProfile:
    global _cache
    with _lock:
        if _cache is not None and not force and time.time() - _cache.measured_at < 60:
            return _cache
    try:
        import psutil
        memory = psutil.virtual_memory()
        ram_total, ram_available = memory.total / 1024 ** 3, memory.available / 1024 ** 3
        cores = psutil.cpu_count(logical=False) or os.cpu_count() or 1
        threads = psutil.cpu_count(logical=True) or cores
    except Exception:
        ram_total = ram_available = 0.0
        cores = threads = os.cpu_count() or 1
    try:
        disk = shutil.disk_usage(Path(__file__).resolve().anchor).free / 1024 ** 3
    except Exception:
        disk = 0.0
    gpus = nvidia_gpus()
    adapters = _display_adapters() if include_adapters else []
    result = HardwareProfile(cpu=_cpu_name(), cores=int(cores), threads=int(threads),
                             ram_total_gb=round(ram_total, 1),
                             ram_available_gb=round(ram_available, 1), gpus=gpus,
                             other_display_adapters=[a for a in adapters
                                                     if not any(g.name in a for g in gpus)],
                             disk_free_gb=round(disk, 1),
                             os=_os_name(),
                             python=sys.version.split()[0])
    with _lock:
        _cache = result
    return result


def available_model_memory_gb() -> float:
    """How much memory a new model could occupy right now: free VRAM on the
    first NVIDIA GPU plus free system RAM (Ollama splits layers across both)."""
    hw = profile()
    gpu = hw.nvidia
    try:
        import psutil
        ram_free = psutil.virtual_memory().available / 1024 ** 3
    except Exception:
        ram_free = hw.ram_available_gb
    return round(ram_free + (gpu.vram_free_gb if gpu else 0.0), 2)


# ---------------------------------------------------------- model plans --

# Approximate download sizes (GB) of the Ollama library tags, for the setup
# report and the disk check. Sizes are the library's published q4 sizes.
MODEL_SIZES_GB = {
    "qwen3:1.7b": 1.4, "qwen3:4b": 2.5, "qwen3:8b": 5.2, "qwen3:14b": 9.3,
    "deepseek-r1:8b": 5.2, "deepseek-r1:14b": 9.0,
    "qwen2.5-coder:7b": 4.7, "qwen2.5-coder:14b": 9.0, "qwen2.5-coder:3b": 1.9,
    "qwen3-vl:4b": 3.3, "qwen3-vl:8b": 6.1,
    "bge-m3": 1.2, "qwen3-embedding:0.6b": 0.64,
}


def model_plan(hw: Optional[HardwareProfile] = None) -> Dict:
    """Which model serves each slot on this hardware, plus what is optional.

    The rule: every model in the REQUIRED set must fit entirely in VRAM (fast,
    and leaves system RAM for the application), and only one large model is
    resident at a time (OLLAMA_MAX_LOADED_MODELS=1 is set by START.bat).
    """
    hw = hw or profile()
    tier = hw.tier
    if tier in ("gpu_16gb", "gpu_12gb"):
        slots = {"fast": "qwen3:4b", "classify": "qwen3:1.7b", "default": "qwen3:8b",
                 "reasoning": "deepseek-r1:14b" if tier == "gpu_16gb" else "deepseek-r1:8b",
                 "json_structured": "qwen3:8b", "code": "qwen2.5-coder:14b" if tier == "gpu_16gb"
                 else "qwen2.5-coder:7b", "vision": "qwen3-vl:8b", "embedding": "bge-m3"}
        optional = ["qwen3:14b", "qwen3-embedding:0.6b"]
    elif tier == "gpu_8gb":
        # RTX 5060 / 4060 / 3070 laptop class. 8B at q4 (~5.2 GB) fits in 8 GB
        # with an 8k context; the 8B vision model (~6.1 GB) fits when nothing
        # else is loaded, which the one-model-at-a-time policy guarantees.
        slots = {"fast": "qwen3:4b", "classify": "qwen3:1.7b", "default": "qwen3:8b",
                 "reasoning": "deepseek-r1:8b", "json_structured": "qwen3:8b",
                 "code": "qwen2.5-coder:7b", "vision": "qwen3-vl:8b", "embedding": "bge-m3"}
        optional = ["qwen3-vl:4b", "qwen3-embedding:0.6b"]
    elif tier == "gpu_6gb":
        slots = {"fast": "qwen3:4b", "classify": "qwen3:1.7b", "default": "qwen3:4b",
                 "reasoning": "deepseek-r1:8b", "json_structured": "qwen3:4b",
                 "code": "qwen2.5-coder:7b", "vision": "qwen3-vl:4b", "embedding": "bge-m3"}
        optional = ["qwen3:8b", "qwen3-embedding:0.6b"]
    elif tier == "cpu_16gb":
        slots = {"fast": "qwen3:4b", "classify": "qwen3:1.7b", "default": "qwen3:4b",
                 "reasoning": "qwen3:8b", "json_structured": "qwen3:4b",
                 "code": "qwen2.5-coder:7b", "vision": "qwen3-vl:4b", "embedding": "bge-m3"}
        optional = ["deepseek-r1:8b", "qwen3-embedding:0.6b"]
    else:
        slots = {"fast": "qwen3:1.7b", "classify": "qwen3:1.7b", "default": "qwen3:4b",
                 "reasoning": "qwen3:4b", "json_structured": "qwen3:4b",
                 "code": "qwen2.5-coder:3b", "vision": "qwen3-vl:4b", "embedding": "bge-m3"}
        optional = ["qwen3-embedding:0.6b"]
    required = sorted(set(slots.values()))
    return {"tier": tier, "slots": slots, "required": required, "optional": optional,
            "download_gb": round(sum(MODEL_SIZES_GB.get(m, 0) for m in required), 1),
            "whisper": whisper_choice(hw)}


def whisper_choice(hw: Optional[HardwareProfile] = None) -> Dict:
    """Speech recognition model and device for this machine. The GPU choice is
    confirmed by the setup script with a real transcription; if CUDA does not
    load it falls back to the CPU choice automatically."""
    hw = hw or profile()
    if hw.vram_gb >= 6:
        return {"model": "large-v3-turbo", "device": "cuda", "compute_type": "float16",
                "cpu_fallback": {"model": "small", "compute_type": "int8"}}
    if hw.threads >= 12:
        return {"model": "small", "device": "cpu", "compute_type": "int8"}
    return {"model": "base", "device": "cpu", "compute_type": "int8"}


if __name__ == "__main__":
    import json
    hw = profile(include_adapters=True)
    print(json.dumps({"hardware": hw.as_dict(), "plan": model_plan(hw)}, indent=2))
