"""
ZEUS Logger
Structured logging with rotation, console + file output.
Falls back to stdlib logging if loguru isn't installed.
"""

import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
from core.paths import log_dir as _log_dir
LOG_DIR = _log_dir()
LOG_DIR.mkdir(parents=True, exist_ok=True)

_configured = False
_USE_LOGURU = False

try:
    from loguru import logger as _loguru_logger
    _USE_LOGURU = True
except ImportError:
    _loguru_logger = None


def setup_logger(level: str = "INFO", debug: bool = False):
    """Configure global logging once. Safe to call multiple times."""
    global _configured
    if _configured:
        return
    _configured = True

    # AGNI_CONSOLE_LEVEL (set by START.bat) keeps the console window to
    # warnings and errors; the log FILE always gets everything. With no
    # console at all (pythonw.exe) sys.stderr is None and only the file is used.
    console_level = "DEBUG" if debug else (os.environ.get("AGNI_CONSOLE_LEVEL", "").strip().upper() or level)
    if _USE_LOGURU:
        _loguru_logger.remove()
        if sys.stderr is not None:
            _loguru_logger.add(
                sys.stderr,
                level=console_level,
                format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | "
                       "<cyan>{name}</cyan> - <level>{message}</level>",
            )
        _loguru_logger.add(
            LOG_DIR / "medha.log",
            level="DEBUG",
            rotation="5 MB",
            retention=5,
            enqueue=True,
        )
    else:
        root = logging.getLogger()
        root.setLevel(logging.DEBUG if debug else getattr(logging, level, logging.INFO))
        fmt = logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)s - %(message)s", "%H:%M:%S")

        if sys.stderr is not None:
            ch = logging.StreamHandler(sys.stderr)
            ch.setFormatter(fmt)
            ch.setLevel(getattr(logging, console_level, logging.INFO))
            root.addHandler(ch)

        fh = logging.FileHandler(LOG_DIR / "medha.log", encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)


def get_logger(name: str):
    """Return a logger bound to `name`. Works whether loguru or stdlib is active."""
    if not _configured:
        setup_logger()
    if _USE_LOGURU:
        return _loguru_logger.bind(module=name)
    return logging.getLogger(name)
