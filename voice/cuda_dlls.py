"""
Make the NVIDIA runtime DLLs that pip installed findable on Windows.

faster-whisper runs on CTranslate2, whose Windows wheels do not bundle cuBLAS
or cuDNN. The setup script installs them as the `nvidia-cublas-cu12` and
`nvidia-cudnn-cu12` wheels, which put the DLLs under
site-packages/nvidia/<library>/bin - a folder Windows does not search. This
adds those folders to the DLL search path before a model is loaded on CUDA.

It changes nothing if the wheels are absent; CUDA then fails to load and the
caller falls back to the CPU, as it always has.
"""

from __future__ import annotations

import os
import site
import sys
from pathlib import Path
from typing import List

_done: List[str] = []


def register_cuda_dlls() -> List[str]:
    if os.name != "nt" or _done:
        return list(_done)
    roots = []
    try:
        roots.extend(site.getsitepackages())
    except Exception:
        pass
    roots.append(str(Path(sys.prefix) / "Lib" / "site-packages"))
    for root in dict.fromkeys(roots):
        nvidia = Path(root) / "nvidia"
        if not nvidia.is_dir():
            continue
        for bin_dir in sorted(nvidia.glob("*/bin")):
            path = str(bin_dir)
            try:
                os.add_dll_directory(path)
            except (OSError, AttributeError):
                continue
            os.environ["PATH"] = path + os.pathsep + os.environ.get("PATH", "")
            _done.append(path)
    return list(_done)
